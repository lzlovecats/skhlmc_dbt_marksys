"""Dedicated kiosk authentication and the privacy-bounded AI評判易 service.

The browser records one low-bitrate microphone track and uploads it directly
to private R2.  Render verifies and downloads the bounded object only when the
operator asks for a review, deletes the temporary object before calling the AI
provider, and persists only normal AI-fund usage metadata.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import re
import shutil
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from account_access import KIOSK_ACCOUNT_ID
from api.access import require_page_user
from core.media_probe import (
    MediaProbeError,
    audio_extension,
    canonical_audio_mime,
    probe_audio,
    transcode_audio_for_provider,
)
from prompts import (
    build_kiosk_match_review_prompts,
    build_kiosk_transcript_prompts,
)
from schema import TABLE_DEBATERS, TABLE_MATCHES, TABLE_R2_UPLOAD_INTENTS
from system_limits import (
    KIOSK_MATCH_REVIEW_CONCURRENCY,
    KIOSK_MATCH_REVIEW_MARKER_LIMIT,
    KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
    KIOSK_MATCH_REVIEW_MAX_SECONDS,
    KIOSK_MATCH_REVIEW_PROVIDER_TIMEOUT_SECONDS,
    KIOSK_MATCH_REVIEW_TRANSCRIPT_MAX_CHARS,
    KIOSK_MATCH_REVIEW_TRANSCRIPT_MAX_OUTPUT_TOKENS,
    MATCH_INVENTORY_LIMIT,
    R2_OBJECT_CACHE_MAX_AGE_SECONDS,
    R2_UPLOAD_CLAIM_TTL_SECONDS,
    TTS_TEXT_MAX_CHARS,
)


router = APIRouter(prefix="/api/kiosk", tags=["kiosk"])
KIOSK_MATCH_REVIEW_SEMAPHORE = asyncio.Semaphore(
    KIOSK_MATCH_REVIEW_CONCURRENCY
)
KIOSK_MATCH_REVIEW_MIN_SECONDS = 10
AI_PROVIDER_PUBLIC_ERROR = (
    "AI評判易暫時無法完成分析；正式比賽毋須亦不可重錄，"
    "請保留 Kiosk 本機錄音備份交由工作人員跟進。"
)
GEMINI_INLINE_REQUEST_SAFE_BYTES = 19_000_000


class KioskLoginBody(BaseModel):
    password: str = Field(max_length=512)


class MatchReviewUploadIntentBody(BaseModel):
    match_id: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(default="", max_length=80)
    mime_type: str = Field(default="audio/webm", max_length=80)
    byte_size: int = Field(gt=0, le=KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES)
    sha256: str = Field(min_length=64, max_length=64)
    duration_seconds: float = Field(
        ge=1, le=KIOSK_MATCH_REVIEW_MAX_SECONDS + 1
    )


class MatchReviewSpeakerMarker(BaseModel):
    offset_seconds: float = Field(ge=0, le=KIOSK_MATCH_REVIEW_MAX_SECONDS)
    side: Literal["pro", "con", "both", "unknown"] = "unknown"
    segment: str = Field(default="", max_length=80)


class MatchReviewBody(BaseModel):
    upload_token: str = Field(min_length=20, max_length=10_000)
    match_id: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(default="", max_length=80)
    speaker_markers: list[MatchReviewSpeakerMarker] = Field(
        default_factory=list, max_length=KIOSK_MATCH_REVIEW_MARKER_LIMIT
    )
    recording_notice_confirmed: bool = False


class MatchReviewDiscardBody(BaseModel):
    upload_token: str = Field(min_length=20, max_length=10_000)


class MatchReviewUploadProbeIntentBody(BaseModel):
    byte_size: int = Field(ge=256, le=65_536)
    sha256: str = Field(min_length=64, max_length=64)


class MatchReviewUploadProbeCompleteBody(BaseModel):
    upload_token: str = Field(min_length=20, max_length=10_000)


def require_kiosk_user(request: Request) -> str:
    """Require the dedicated account even if another committee cookie is valid."""
    return require_page_user(request, "kiosk")


def _clean_db_text(value) -> str:
    text_value = str(value or "").strip()
    return "" if text_value.lower() in {"nan", "nat", "none", "<na>"} else text_value


_ROSTER_ROLE_LABELS = {1: "主辯", 2: "一副", 3: "二副", 4: "結辯"}


def _attach_official_rosters(db, records: list[dict]) -> None:
    """Attach every roster slot and a conservative name-deduplicated headcount."""
    match_ids = [str(item.get("match_id") or "").strip() for item in records]
    match_ids = [value for value in match_ids if value]
    for record in records:
        record.update(
            {
                "roster_slots": [],
                "listed_participants": [],
                "listed_roster_slot_count": 0,
                "listed_unique_participant_count": 0,
                "duplicate_role_assignments": [],
                "participant_count_method": (
                    "按正式名單的非空姓名去除空白及大小寫後計算；同名多個辯位視為同一位，"
                    "但同名同姓的不同學生仍可能被低估"
                ),
            }
        )
    if not match_ids:
        return
    params = {f"roster_match_{index}": value for index, value in enumerate(match_ids)}
    placeholders = ",".join(f":{name}" for name in params)
    roster_rows = db.query(
        f"""SELECT match_id,side,position,debater_name
            FROM {TABLE_DEBATERS}
            WHERE match_id IN ({placeholders})
            ORDER BY match_id,side,position""",
        params,
    )
    by_match: dict[str, list[dict]] = {value: [] for value in match_ids}
    for _, row in roster_rows.iterrows():
        match_key = _clean_db_text(row.get("match_id"))
        side = _clean_db_text(row.get("side")).lower()
        name = _clean_db_text(row.get("debater_name"))
        try:
            position = int(row.get("position"))
        except (TypeError, ValueError, OverflowError):
            continue
        if match_key not in by_match or side not in {"pro", "con"} or not name:
            continue
        side_label = "正方" if side == "pro" else "反方"
        role = _ROSTER_ROLE_LABELS.get(position, f"第{position}位")
        by_match[match_key].append(
            {
                "side": side,
                "side_label": side_label,
                "position": position,
                "role": role,
                "role_label": f"{side_label}{role}",
                "debater_name": name,
            }
        )

    for record in records:
        slots = sorted(
            by_match.get(str(record.get("match_id") or ""), []),
            key=lambda item: (0 if item["side"] == "pro" else 1, item["position"]),
        )
        people: dict[str, dict] = {}
        for slot in slots:
            identity_key = re.sub(r"\s+", "", slot["debater_name"]).casefold()
            participant = people.setdefault(
                identity_key,
                {"debater_name": slot["debater_name"], "assignments": []},
            )
            participant["assignments"].append(
                {
                    "side": slot["side"],
                    "side_label": slot["side_label"],
                    "position": slot["position"],
                    "role": slot["role"],
                    "role_label": slot["role_label"],
                }
            )
        participants = list(people.values())
        record["roster_slots"] = slots
        record["listed_participants"] = participants
        record["listed_roster_slot_count"] = len(slots)
        record["listed_unique_participant_count"] = len(participants)
        record["duplicate_role_assignments"] = [
            participant
            for participant in participants
            if len(participant["assignments"]) > 1
        ]


def _official_match_records(db, match_id: str = "") -> list[dict]:
    """Load only the safe official metadata AI評判易 needs."""
    from debate_timing import DEBATE_FORMATS

    requested = _clean_db_text(match_id)
    where = "WHERE match_id=:match_id" if requested else ""
    params = {"match_id": requested, "limit": MATCH_INVENTORY_LIMIT}
    rows = db.query(
        f"""SELECT match_id,match_date,match_time,topic_text,pro_team,con_team,
                   debate_format,free_debate_minutes
            FROM {TABLE_MATCHES} {where}
            ORDER BY match_date DESC NULLS LAST,match_time DESC NULLS LAST,match_id DESC
            LIMIT :limit""",
        params,
    )
    records = []
    for _, row in rows.iterrows():
        debate_format = _clean_db_text(row.get("debate_format"))
        if debate_format not in DEBATE_FORMATS:
            debate_format = DEBATE_FORMATS[0]
        free_minutes = None
        try:
            raw_free = row.get("free_debate_minutes")
            if raw_free is not None and _clean_db_text(raw_free):
                free_minutes = max(2.0, min(10.0, float(raw_free)))
        except (TypeError, ValueError, OverflowError):
            free_minutes = None
        match_date = row.get("match_date")
        match_time = row.get("match_time")
        records.append(
            {
                "match_id": _clean_db_text(row.get("match_id")),
                "match_date": (
                    match_date.strftime("%Y-%m-%d")
                    if hasattr(match_date, "strftime")
                    else _clean_db_text(match_date)[:10]
                ),
                "match_time": (
                    match_time.strftime("%H:%M")
                    if hasattr(match_time, "strftime")
                    else _clean_db_text(match_time)[:5]
                ),
                "topic": _clean_db_text(row.get("topic_text")),
                "pro_team": _clean_db_text(row.get("pro_team")) or "正方",
                "con_team": _clean_db_text(row.get("con_team")) or "反方",
                "debate_format": debate_format,
                "free_debate_minutes": free_minutes,
            }
        )
    _attach_official_rosters(db, records)
    return records


def _official_match(db, match_id: str) -> dict | None:
    records = _official_match_records(db, match_id)
    return records[0] if records else None


def _paid_gemini_project_confirmed() -> bool:
    """Report whether deployment has confirmed paid Gemini data terms."""
    from core.runtime_secrets import get_secret

    return str(get_secret("GEMINI_PAID_TIER_CONFIRMED", "") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@router.get("/session")
def session(request: Request):
    return JSONResponse(
        {
            "authenticated": True,
            "user_id": require_kiosk_user(request),
            "match_review_limits": {
                "max_bytes": KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
                "max_seconds": KIOSK_MATCH_REVIEW_MAX_SECONDS,
                "min_seconds": KIOSK_MATCH_REVIEW_MIN_SECONDS,
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/match-review/matches")
def match_review_matches(request: Request):
    """Return bounded official match metadata without passwords or roster links."""
    from deploy.proxy import get_vote_db

    require_kiosk_user(request)
    matches = _official_match_records(get_vote_db())
    return JSONResponse(
        {"matches": matches}, headers={"Cache-Control": "no-store"}
    )


@router.get("/match-review/preflight")
async def match_review_preflight(request: Request):
    """Check readiness without uploading audio or calling a paid AI provider."""
    from ai_model_config import get_feature_model
    from core import r2_storage
    from deploy.proxy import (
        _bandwidth_essential_gate_error,
        _get_proxy_secret,
        get_vote_db,
    )

    user_id = require_kiosk_user(request)
    db = get_vote_db()
    checks = {}

    r2_configured = r2_storage.configured()
    r2_reachable = False
    storage_blocked = True
    if r2_configured:
        r2_reachable = await asyncio.to_thread(r2_storage.connection_ready)
        try:
            storage_blocked = bool(
                r2_storage.storage_budget_status(db, refresh=False)["blocked"]
            )
        except Exception:
            storage_blocked = True
    checks["r2"] = {
        "ok": r2_configured and r2_reachable and not storage_blocked,
        "detail": (
            "私人 R2 暫存區可用"
            if r2_configured and r2_reachable and not storage_blocked
            else "私人 R2 暫存區未就緒或已達保護上限"
        ),
    }

    label = ""
    model_ready = False
    try:
        label, config = get_feature_model("kiosk_match_review")
        key_name = str(config.get("api_key") or "GEMINI_API_KEY")
        model_ready = bool(
            config.get("provider") == "gemini"
            and config.get("supports_audio")
            and _get_proxy_secret(key_name).strip()
        )
    except Exception:
        model_ready = False
    checks["ai"] = {
        "ok": model_ready,
        "detail": f"{label} 設定就緒" if model_ready else "AI 模型或 API 設定未就緒",
    }

    paid_confirmed = _paid_gemini_project_confirmed()
    checks["privacy"] = {
        "ok": True,
        "detail": (
            "已確認使用付費 Gemini project（內容不作產品訓練）"
            if paid_confirmed
            else (
                "測試模式：允許 Free Tier；提交內容可能用於改善 Google 產品及由人手審閱，"
                "正式學生比賽應改用已確認的 Paid Tier project"
            )
        ),
        "paid_tier_confirmed": paid_confirmed,
        "free_tier_test_allowed": not paid_confirmed,
    }
    media_ready = bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))
    checks["media"] = {
        "ok": media_ready,
        "detail": "錄音驗證及格式轉換工具可用" if media_ready else "伺服器音訊工具未就緒",
    }
    bandwidth_ready = not bool(_bandwidth_essential_gate_error())
    checks["bandwidth"] = {
        "ok": bandwidth_ready,
        "detail": "網絡用量保護正常" if bandwidth_ready else "網絡用量已達保護上限",
    }
    official_matches = _official_match_records(db)
    checks["matches"] = {
        "ok": bool(official_matches),
        "detail": (
            f"已載入 {len(official_matches)} 個正式場次"
            if official_matches
            else "未有可用的正式場次"
        ),
    }

    return JSONResponse(
        {
            "ok": all(bool(item.get("ok")) for item in checks.values()),
            "checks": checks,
            "model_label": label,
            "does_not_call_ai": True,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/login")
def login(body: KioskLoginBody, request: Request, response: Response):
    """Log in the fixed kiosk identity; no caller-supplied account is accepted."""
    from api.auth_api import COOKIE_MAX_AGE, COOKIE_NAME
    from core.auth_logic import (
        authenticate_login,
        login_rate_limit_retry_after,
        record_login,
    )
    from deploy.proxy import _sign_committee_token, get_vote_db

    password = str(body.password or "").strip()
    if not password:
        raise HTTPException(400, "請輸入 kiosk 密碼")
    retry_after = login_rate_limit_retry_after(request, KIOSK_ACCOUNT_ID)
    if retry_after is not None:
        raise HTTPException(
            429,
            "登入嘗試次數過多，請稍後再試。",
            headers={"Retry-After": str(retry_after)},
        )
    db = get_vote_db()
    credential_hash = authenticate_login(KIOSK_ACCOUNT_ID, password, db=db)
    if credential_hash is None:
        raise HTTPException(401, "kiosk 密碼錯誤")
    token = _sign_committee_token(
        KIOSK_ACCOUNT_ID, credential_hash=credential_hash,
    )
    if not token:
        raise HTTPException(503, "登入服務暫時未能使用")
    record_login(KIOSK_ACCOUNT_ID, db=db)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        path="/",
        samesite="lax",
        httponly=True,
        secure=True,
    )
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok", "user_id": KIOSK_ACCOUNT_ID}


@router.post("/logout")
def logout(response: Response):
    from api.auth_api import COOKIE_NAME

    response.delete_cookie(
        COOKIE_NAME, path="/", samesite="lax", httponly=True, secure=True,
    )
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok"}


def _storage_error(scope: str, storage_budget: dict) -> str:
    if scope == "storage_global":
        stop_gb = float(storage_budget.get("stop_bytes") or 0) / 1_000_000_000
        return f"R2儲存量已達{stop_gb:g}GB保護上限，暫停新錄音。"
    return "R2全系統儲存保護上限已啟用，暫停新錄音。"


@router.post("/match-review/upload-probe-intent")
def match_review_upload_probe_intent(
    body: MatchReviewUploadProbeIntentBody, request: Request
):
    """Issue a tiny browser-to-R2 PUT used by the formal hardware test."""
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret, get_vote_db

    user_id = require_kiosk_user(request)
    if not r2_storage.configured():
        raise HTTPException(503, "Cloudflare R2 尚未完成設定。")
    digest = str(body.sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HTTPException(400, "測試檔案雜湊格式不正確。")
    secret = _get_relay_cookie_secret()
    if not secret:
        raise HTTPException(503, "系統簽署設定不可用。")
    db = get_vote_db()
    storage_budget = r2_storage.storage_budget_status(db, refresh=True)
    if storage_budget["blocked"]:
        raise HTTPException(429, _storage_error("storage_global", storage_budget))

    intent_id = uuid.uuid4().hex
    key = f"pending/probe/kiosk/{intent_id}.bin"
    claim = {
        "kind": "kiosk_upload_probe",
        "intent_id": intent_id,
        "user": user_id,
        "byte_size": body.byte_size,
        "sha256": digest,
        "pending_r2_key": key,
    }
    upload_token = r2_storage.sign_upload_claim(
        claim, secret, expires=R2_UPLOAD_CLAIM_TTL_SECONDS
    )
    try:
        upload_url = r2_storage.presign_put(
            key, "application/octet-stream", digest, body.byte_size
        )
    except Exception as exc:
        raise HTTPException(503, "暫時未能建立瀏覽器上載測試連結。") from exc
    reserved, scope = r2_storage.reserve_upload_intent(
        db,
        intent_id=intent_id,
        user_id=user_id,
        media_kind="kiosk_upload_probe",
        object_keys=[key],
        declared_bytes=body.byte_size,
    )
    if not reserved:
        raise HTTPException(429, _storage_error(scope, storage_budget))
    return JSONResponse(
        {
            "upload_token": upload_token,
            "url": upload_url,
            "headers": {
                "Content-Type": "application/octet-stream",
                "Cache-Control": (
                    f"private, max-age={R2_OBJECT_CACHE_MAX_AGE_SECONDS}"
                ),
                "x-amz-meta-sha256": digest,
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/match-review/upload-probe-complete")
async def match_review_upload_probe_complete(
    body: MatchReviewUploadProbeCompleteBody, request: Request
):
    """Verify and immediately delete the hardware-test object."""
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret, get_vote_db

    user_id = require_kiosk_user(request)
    secret = _get_relay_cookie_secret()
    claim = r2_storage.verify_upload_claim(body.upload_token, secret or "")
    if (
        not claim
        or claim.get("kind") != "kiosk_upload_probe"
        or claim.get("user") != user_id
    ):
        raise HTTPException(400, "瀏覽器上載測試憑證無效或已過期。")
    key = str(claim.get("pending_r2_key") or "")
    intent_id = str(claim.get("intent_id") or "")
    if not key or not intent_id:
        raise HTTPException(400, "瀏覽器上載測試憑證內容無效。")
    try:
        remote = await asyncio.to_thread(r2_storage.head, key)
    except Exception as exc:
        raise HTTPException(400, "R2 未能確認瀏覽器測試上載。") from exc
    valid = (
        int(remote.get("ContentLength") or 0) == int(claim.get("byte_size") or 0)
        and str((remote.get("Metadata") or {}).get("sha256") or "").lower()
        == str(claim.get("sha256") or "").lower()
        and str(remote.get("ContentType") or "").split(";", 1)[0].lower()
        == "application/octet-stream"
    )
    deleted = await asyncio.to_thread(
        r2_storage.delete_intent_objects, get_vote_db(), intent_id, (key,)
    )
    if not deleted:
        raise HTTPException(502, "瀏覽器測試檔未能即時刪除；清理程序會重試。")
    if not valid:
        raise HTTPException(400, "瀏覽器至 R2 的測試上載驗證失敗。")
    return JSONResponse(
        {"ok": True, "probe_deleted": True},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/match-review/upload-intent")
def match_review_upload_intent(
    body: MatchReviewUploadIntentBody, request: Request
):
    """Issue one short-lived direct-to-R2 PUT for an ephemeral recording."""
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret, get_vote_db

    user_id = require_kiosk_user(request)
    db = get_vote_db()
    official_match = _official_match(db, body.match_id)
    if not official_match:
        raise HTTPException(404, "找不到所選正式場次，請重新載入場次資料。")
    if not official_match.get("topic"):
        raise HTTPException(400, "所選正式場次尚未設定辯題。")
    if not r2_storage.configured():
        raise HTTPException(503, "Cloudflare R2 尚未完成設定，AI評判易暫停服務。")
    storage_budget = r2_storage.storage_budget_status(db, refresh=True)
    if storage_budget["blocked"]:
        raise HTTPException(429, _storage_error("storage_global", storage_budget))
    try:
        mime = canonical_audio_mime(body.mime_type)
    except MediaProbeError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not 1_000 <= body.byte_size <= KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES:
        raise HTTPException(
            400,
            "錄音大小必須介乎 1KB 至 "
            f"{KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES // (1024 * 1024)}MB。",
        )
    if not KIOSK_MATCH_REVIEW_MIN_SECONDS <= body.duration_seconds <= KIOSK_MATCH_REVIEW_MAX_SECONDS:
        raise HTTPException(
            400,
            f"錄音長度必須為 {KIOSK_MATCH_REVIEW_MIN_SECONDS} 至 "
            f"{KIOSK_MATCH_REVIEW_MAX_SECONDS // 60} 分鐘。",
        )
    digest = body.sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HTTPException(400, "錄音雜湊格式不正確。")
    secret = _get_relay_cookie_secret()
    if not secret:
        raise HTTPException(503, "系統簽署設定不可用。")

    intent_id = uuid.uuid4().hex
    operation_id = str(body.operation_id or "").strip() or intent_id
    key = (
        "pending/audio/kiosk-match-review/"
        f"{datetime.datetime.now(datetime.timezone.utc):%Y/%m}/{intent_id}."
        f"{audio_extension(mime)}"
    )
    claim = {
        "kind": "kiosk_match_review",
        "intent_id": intent_id,
        "operation_id": operation_id,
        "user": user_id,
        "match_id": official_match["match_id"],
        "mime_type": mime,
        "byte_size": body.byte_size,
        "sha256": digest,
        "duration_seconds": round(float(body.duration_seconds), 3),
        "pending_r2_key": key,
    }
    upload_token = r2_storage.sign_upload_claim(
        claim, secret, expires=R2_UPLOAD_CLAIM_TTL_SECONDS
    )
    try:
        upload_url = r2_storage.presign_put(key, mime, digest, body.byte_size)
    except Exception as exc:
        # Do not reserve system-wide R2 capacity when no URL can be issued.
        raise HTTPException(503, "暫時未能建立錄音上載連結，請稍後再試。") from exc
    reserved, scope = r2_storage.reserve_upload_intent(
        db,
        intent_id=intent_id,
        user_id=user_id,
        media_kind="kiosk_match_review",
        object_keys=[key],
        declared_bytes=body.byte_size,
    )
    if not reserved:
        raise HTTPException(429, _storage_error(scope, storage_budget))
    return JSONResponse(
        {
            "upload_token": upload_token,
            "operation_id": operation_id,
            "url": upload_url,
            "headers": {
                "Content-Type": mime,
                "Cache-Control": (
                    f"private, max-age={R2_OBJECT_CACHE_MAX_AGE_SECONDS}"
                ),
                "x-amz-meta-sha256": digest,
            },
            "limits": {
                "max_bytes": KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
                "max_seconds": KIOSK_MATCH_REVIEW_MAX_SECONDS,
            },
        },
        headers={"Cache-Control": "no-store"},
    )


def _claim_review_intent(db, intent_id: str) -> bool:
    """Atomically make a valid upload token single-use before provider spend.

    ``processing`` deliberately remains an open/orphan-cleanable state if this
    worker crashes or R2 deletion fails. It must never become ``completed``
    until an object has durable application metadata (these temporary files
    never do).
    """
    with db.transaction() as conn:
        row = conn.execute(
            text(
                f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
                    SET status='processing',completed_at=NULL
                    WHERE intent_id=:intent_id AND media_kind='kiosk_match_review'
                      AND status='issued'
                    RETURNING intent_id"""
            ),
            {"intent_id": str(intent_id)},
        ).fetchone()
    return row is not None


def _set_review_intent_provider_status(db, intent_id: str, status: str) -> None:
    """Separate real provider spend from abandoned/failed pre-provider uploads."""
    if status not in {"provider_processing", "consumed"}:
        raise ValueError("invalid kiosk review provider status")
    completed_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    updated = db.execute_count(
        f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
            SET status=:status,completed_at=:completed_at
            WHERE intent_id=:intent_id AND media_kind='kiosk_match_review'
              AND status IN ('processing','orphan_deleted','provider_processing')""",
        {
            "intent_id": str(intent_id),
            "status": status,
            "completed_at": completed_at if status == "consumed" else None,
        },
    )
    if status == "provider_processing" and int(updated or 0) != 1:
        # Never call the paid provider unless the usage reservation has been
        # durably converted from a released upload into a provider-spend slot.
        raise RuntimeError("kiosk review provider usage transition failed")


@router.post("/match-review/discard")
async def discard_match_review(body: MatchReviewDiscardBody, request: Request):
    """Best-effort explicit deletion for an upload the operator abandons."""
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret, get_vote_db

    user_id = require_kiosk_user(request)
    secret = _get_relay_cookie_secret()
    claim = r2_storage.verify_upload_claim(body.upload_token, secret or "")
    if (
        not claim
        or claim.get("kind") != "kiosk_match_review"
        or claim.get("user") != user_id
    ):
        raise HTTPException(400, "錄音上載憑證無效或已過期。")
    key = str(claim.get("pending_r2_key") or "")
    intent_id = str(claim.get("intent_id") or "")
    if not key or not intent_id:
        raise HTTPException(400, "錄音上載憑證內容無效。")
    deleted = await asyncio.to_thread(
        r2_storage.delete_intent_objects, get_vote_db(), intent_id, (key,)
    )
    if not deleted:
        raise HTTPException(502, "暫時未能刪除錄音；系統會由孤兒檔清理程序重試。")
    return JSONResponse(
        {"ok": True, "recording_deleted": True},
        headers={"Cache-Control": "no-store"},
    )


_MARKER_SIDE_LABELS = {
    "pro": "正方",
    "con": "反方",
    "both": "雙方／自由辯論",
    "unknown": "未能確定／休息",
}


def _validated_markers(
    markers: list[MatchReviewSpeakerMarker], measured_duration: float
) -> list[dict]:
    records = []
    previous = -1.0
    for marker in markers:
        offset = round(float(marker.offset_seconds), 3)
        if offset < previous:
            raise HTTPException(400, "發言方時間標記必須按時間先後排列。")
        if offset > float(measured_duration) + 1:
            raise HTTPException(400, "發言方時間標記超出實際錄音長度。")
        previous = offset
        records.append(
            {
                "offset_seconds": offset,
                "side": marker.side,
                "side_label": _MARKER_SIDE_LABELS[marker.side],
                "segment": marker.segment.strip() or "未標示環節",
            }
        )
    return records


def _expected_sequence(match: dict) -> list[dict]:
    from debate_timing import get_full_mock_sequence

    return [
        {
            "id": item["id"],
            "label": item["label"],
            "side": item["side"],
            "planned_seconds": item["seconds"],
        }
        for item in get_full_mock_sequence(
            match["debate_format"], match.get("free_debate_minutes")
        )
    ]


def _evidence_context(
    match: dict, measured_duration: float, markers: list[dict]
) -> str:
    payload = {
        "official_match": match,
        "measured_duration_seconds": round(float(measured_duration), 3),
        "expected_format_sequence": _expected_sequence(match),
        "operator_side_markers": markers,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _transcript_prompts(
    match: dict, measured_duration: float, markers: list[dict]
) -> tuple[str, str]:
    return build_kiosk_transcript_prompts(
        _evidence_context(match, measured_duration, markers),
        KIOSK_MATCH_REVIEW_TRANSCRIPT_MAX_CHARS,
    )


def _match_review_prompts(
    match: dict,
    measured_duration: float,
    markers: list[dict],
    transcript: str,
) -> tuple[str, str]:
    return build_kiosk_match_review_prompts(
        _evidence_context(match, measured_duration, markers),
        transcript,
        TTS_TEXT_MAX_CHARS,
    )


_PROJECTOR_SUMMARY_START = "PROJECTOR_SUMMARY_START"
_PROJECTOR_SUMMARY_END = "PROJECTOR_SUMMARY_END"
_FULL_REVIEW_START = "FULL_REVIEW_START"
_FULL_REVIEW_END = "FULL_REVIEW_END"
_PROJECTOR_ADVISORY = "AI輔助第二意見，正式賽果以評判團為準。"


def _bounded_review_output(raw_result: str) -> tuple[str, str]:
    """Return private full review plus a public/TTS-safe bounded summary."""
    raw = str(raw_result or "").strip()
    summary = ""
    full = ""
    if _PROJECTOR_SUMMARY_START in raw and _PROJECTOR_SUMMARY_END in raw:
        summary = raw.split(_PROJECTOR_SUMMARY_START, 1)[1].split(
            _PROJECTOR_SUMMARY_END, 1
        )[0].strip()
    if _FULL_REVIEW_START in raw and _FULL_REVIEW_END in raw:
        full = raw.split(_FULL_REVIEW_START, 1)[1].split(
            _FULL_REVIEW_END, 1
        )[0].strip()
    if not full:
        full = raw
        for marker in (
            _PROJECTOR_SUMMARY_START,
            _PROJECTOR_SUMMARY_END,
            _FULL_REVIEW_START,
            _FULL_REVIEW_END,
        ):
            full = full.replace(marker, "")
        full = full.strip()
    if not summary:
        prefix = _PROJECTOR_ADVISORY + "\n\n"
        summary = prefix + full[: max(0, TTS_TEXT_MAX_CHARS - len(prefix))]
    if _PROJECTOR_ADVISORY not in summary:
        summary = _PROJECTOR_ADVISORY + "\n\n" + summary
    return full, summary[:TTS_TEXT_MAX_CHARS].strip()


def _log_review_usage(
    db,
    label: str,
    config: dict,
    success: bool,
    *,
    usage=None,
    error="",
    operation_id="",
    operation_stage="",
):
    """Best-effort AI-fund accounting using provider token metadata."""
    try:
        from core.funds_logic import log_ai_usage

        actual = usage or {}
        input_tokens = int(actual.get("input_tokens") or 0)
        output_tokens = int(actual.get("output_tokens") or 0)
        audio_tokens = int(actual.get("audio_tokens") or 0)
        usd = (
            input_tokens * float(config.get("input_price_per_million") or 0)
            + audio_tokens
            * float(
                config.get("audio_input_price_per_million")
                or config.get("input_price_per_million")
                or 0
            )
            + output_tokens * float(config.get("output_price_per_million") or 0)
        ) / 1_000_000
        log_ai_usage(
            KIOSK_ACCOUNT_ID,
            "kiosk_match_review",
            success,
            usage={
                "model_label": label,
                "provider": config.get("provider") or "gemini",
                "estimated_cost_usd": usd,
                "estimated_cost_hkd": usd * 7.8,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "audio_tokens": audio_tokens,
                "search_calls": 0,
                "cost_source": actual.get("cost_source") or "estimate",
                "operation_id": str(operation_id or "")[:200],
                "operation_stage": str(operation_stage or "")[:80],
            },
            error_message=str(error or "")[:300],
            db=db,
        )
    except Exception:
        # An optional accounting failure must not discard a completed review.
        pass


@router.post("/match-review/analyze")
async def analyze_match_review(body: MatchReviewBody, request: Request):
    """Delete one verified recording, then transcribe and judge it in memory."""
    from ai_model_config import get_feature_model
    from core import r2_storage
    from core.ai_provider import generate_text
    from deploy.proxy import (
        _bandwidth_essential_gate_error,
        _get_proxy_secret,
        _get_relay_cookie_secret,
        get_vote_db,
        record_bandwidth_usage,
    )

    user_id = require_kiosk_user(request)
    if not body.recording_notice_confirmed:
        raise HTTPException(400, "請先確認已通知在場人士錄音及雲端 AI 處理安排。")
    budget_error = _bandwidth_essential_gate_error()
    if budget_error:
        raise HTTPException(429, budget_error)
    secret = _get_relay_cookie_secret()
    if not secret:
        raise HTTPException(503, "錄音驗證服務暫時不可用。")
    claim = r2_storage.verify_upload_claim(body.upload_token, secret)
    if (
        not claim
        or claim.get("kind") != "kiosk_match_review"
        or claim.get("user") != user_id
    ):
        raise HTTPException(400, "錄音上載憑證無效或已過期。")

    key = str(claim.get("pending_r2_key") or "")
    intent_id = str(claim.get("intent_id") or "")
    accounting_operation_id = str(claim.get("operation_id") or intent_id).strip()
    db = get_vote_db()
    cleaned = False

    def cleanup() -> bool:
        nonlocal cleaned
        if cleaned:
            return True
        cleaned = r2_storage.delete_intent_objects(db, intent_id, (key,))
        return cleaned

    if not r2_storage.configured() or not key or not intent_id:
        raise HTTPException(503, "錄音暫存服務不可用。")
    if str(claim.get("match_id") or "") != body.match_id:
        await asyncio.to_thread(cleanup)
        raise HTTPException(400, "錄音憑證與所選正式場次不相符。")
    if body.operation_id and str(body.operation_id).strip() != accounting_operation_id:
        await asyncio.to_thread(cleanup)
        raise HTTPException(400, "錄音憑證與 AI評判易任務不相符。")
    official_match = _official_match(db, body.match_id)
    if not official_match:
        await asyncio.to_thread(cleanup)
        raise HTTPException(404, "正式場次已不存在，已刪除暫存錄音。")
    if not official_match.get("topic"):
        await asyncio.to_thread(cleanup)
        raise HTTPException(400, "正式場次尚未設定辯題，已刪除暫存錄音。")
    try:
        remote = await asyncio.to_thread(r2_storage.head, key)
    except Exception as exc:
        raise HTTPException(400, "R2 未能確認錄音已完成上載。") from exc
    remote_sha = str((remote.get("Metadata") or {}).get("sha256") or "").lower()
    remote_mime = str(remote.get("ContentType") or "").split(";", 1)[0].lower()
    try:
        expected_mime = canonical_audio_mime(str(claim.get("mime_type") or ""))
        expected_size = int(claim.get("byte_size") or 0)
        expected_duration = float(claim.get("duration_seconds") or 0)
    except (MediaProbeError, TypeError, ValueError, OverflowError) as exc:
        await asyncio.to_thread(cleanup)
        raise HTTPException(400, "錄音上載憑證內容無效。") from exc
    if (
        not 1_000 <= expected_size <= KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES
        or int(remote.get("ContentLength") or 0) != expected_size
        or remote_sha != str(claim.get("sha256") or "").lower()
        or remote_mime != expected_mime
        or not KIOSK_MATCH_REVIEW_MIN_SECONDS <= expected_duration <= KIOSK_MATCH_REVIEW_MAX_SECONDS
    ):
        await asyncio.to_thread(cleanup)
        raise HTTPException(400, "R2 錄音大小、格式或雜湊驗證失敗。")
    if not await asyncio.to_thread(_claim_review_intent, db, intent_id):
        raise HTTPException(409, "此錄音已經分析或正在分析，請勿重複提交。")

    try:
        label, config = get_feature_model("kiosk_match_review")
    except Exception as exc:
        await asyncio.to_thread(cleanup)
        raise HTTPException(503, "AI評判易模型尚未完成設定。") from exc
    if config.get("provider") != "gemini" or not config.get("supports_audio"):
        await asyncio.to_thread(cleanup)
        raise HTTPException(503, "AI評判易模型必須支援錄音分析。")
    key_name = str(config.get("api_key") or "GEMINI_API_KEY")
    api_key = _get_proxy_secret(key_name).strip()
    if not api_key:
        await asyncio.to_thread(cleanup)
        raise HTTPException(503, f"未設定 {key_name}，暫時無法使用 AI評判易。")

    provider_stage = "transcription"
    any_provider_attempted = False
    current_provider_attempted = False
    transcript = ""
    async with KIOSK_MATCH_REVIEW_SEMAPHORE:
        try:
            audio = await asyncio.to_thread(
                r2_storage.download_bytes,
                key,
                KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
            )
            probe = await asyncio.to_thread(
                probe_audio,
                audio,
                expected_mime,
                expected_duration,
                max_seconds=KIOSK_MATCH_REVIEW_MAX_SECONDS,
            )
            if probe["sha256"] != str(claim.get("sha256") or "").lower():
                raise MediaProbeError("錄音內容雜湊與上載憑證不符")
            markers = _validated_markers(
                body.speaker_markers, float(probe["duration"])
            )
            provider_audio, provider_mime = await asyncio.to_thread(
                transcode_audio_for_provider,
                audio,
                expected_mime,
                max_output_bytes=KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
            )
            del audio
            encoded_audio = base64.b64encode(provider_audio).decode("ascii")
            del provider_audio
            # Delete the raw recording before it leaves this bounded request.
            if not await asyncio.to_thread(cleanup):
                raise HTTPException(
                    502, "未能刪除暫存錄音，為保障私隱已取消 AI 分析。"
                )
            transcript_system, transcript_user = _transcript_prompts(
                official_match, float(probe["duration"]), markers
            )
            transcript_request_bytes = (
                len(encoded_audio)
                + len(transcript_system.encode("utf-8"))
                + len(transcript_user.encode("utf-8"))
            )
            if transcript_request_bytes > GEMINI_INLINE_REQUEST_SAFE_BYTES:
                raise MediaProbeError("錄音轉換後超出 AI 安全傳送上限")
            await asyncio.to_thread(
                record_bandwidth_usage,
                "kiosk_match_transcription_provider",
                transcript_request_bytes,
                user_id,
                aggregate_key=f"user={user_id[:120]}",
            )
            # The object is already deleted and the local request gates have
            # passed. Only now does this reserved match become a provider-spend
            # slot; pre-provider failures remain orphan_deleted and are released.
            await asyncio.to_thread(
                _set_review_intent_provider_status,
                db,
                intent_id,
                "provider_processing",
            )
            current_provider_attempted = True
            any_provider_attempted = True
            transcript, transcript_usage = await generate_text(
                config,
                transcript_system,
                transcript_user,
                api_key=api_key,
                audio_base64=encoded_audio,
                audio_mime=provider_mime,
                web_search=False,
                max_output_tokens=KIOSK_MATCH_REVIEW_TRANSCRIPT_MAX_OUTPUT_TOKENS,
                max_prompt_chars=100_000,
                timeout_seconds=KIOSK_MATCH_REVIEW_PROVIDER_TIMEOUT_SECONDS,
                temperature=None,
                require_complete=True,
            )
            transcript = str(transcript or "").strip()
            if not transcript:
                raise ValueError("AI transcript is empty")
            if len(transcript) > KIOSK_MATCH_REVIEW_TRANSCRIPT_MAX_CHARS:
                raise ValueError("AI transcript exceeds kiosk evidence limit")
            _log_review_usage(
                db,
                label,
                config,
                True,
                usage=transcript_usage,
                operation_id=accounting_operation_id,
                operation_stage="transcription",
            )
            current_provider_attempted = False

            provider_stage = "judgement"
            system_prompt, user_prompt = _match_review_prompts(
                official_match,
                float(probe["duration"]),
                markers,
                transcript,
            )
            review_request_bytes = (
                len(encoded_audio)
                + len(system_prompt.encode("utf-8"))
                + len(user_prompt.encode("utf-8"))
            )
            if review_request_bytes > GEMINI_INLINE_REQUEST_SAFE_BYTES:
                raise MediaProbeError("錄音連逐字稿評審要求超出 AI 安全傳送上限")
            await asyncio.to_thread(
                record_bandwidth_usage,
                "kiosk_match_review_provider",
                review_request_bytes,
                user_id,
                aggregate_key=f"user={user_id[:120]}",
            )
            current_provider_attempted = True
            any_provider_attempted = True
            result, usage = await generate_text(
                config,
                system_prompt,
                user_prompt,
                api_key=api_key,
                audio_base64=encoded_audio,
                audio_mime=provider_mime,
                web_search=False,
                max_prompt_chars=KIOSK_MATCH_REVIEW_TRANSCRIPT_MAX_CHARS + 100_000,
                timeout_seconds=KIOSK_MATCH_REVIEW_PROVIDER_TIMEOUT_SECONDS,
                temperature=None,
                require_complete=True,
            )
            del encoded_audio
        except MediaProbeError as exc:
            await asyncio.to_thread(cleanup)
            if any_provider_attempted:
                await asyncio.to_thread(
                    _set_review_intent_provider_status,
                    db,
                    intent_id,
                    "consumed",
                )
            raise HTTPException(
                503 if exc.service_unavailable else 400, str(exc)
            ) from exc
        except HTTPException:
            await asyncio.to_thread(cleanup)
            if any_provider_attempted:
                await asyncio.to_thread(
                    _set_review_intent_provider_status,
                    db,
                    intent_id,
                    "consumed",
                )
            raise
        except Exception as exc:
            await asyncio.to_thread(cleanup)
            if current_provider_attempted:
                _log_review_usage(
                    db,
                    label,
                    config,
                    False,
                    error=f"{provider_stage}：{AI_PROVIDER_PUBLIC_ERROR}",
                    operation_id=accounting_operation_id,
                    operation_stage=provider_stage,
                )
            if any_provider_attempted:
                await asyncio.to_thread(
                    _set_review_intent_provider_status,
                    db,
                    intent_id,
                    "consumed",
                )
            raise HTTPException(502, AI_PROVIDER_PUBLIC_ERROR) from exc

    full_result, projector_summary = _bounded_review_output(result)
    _log_review_usage(
        db,
        label,
        config,
        True,
        usage=usage,
        operation_id=accounting_operation_id,
        operation_stage="judgement",
    )
    await asyncio.to_thread(
        _set_review_intent_provider_status,
        db,
        intent_id,
        "consumed",
    )
    audio_metadata = {
        "duration_seconds": probe["duration"],
        "sample_rate": probe["sample_rate"],
        "channels": probe["channels"],
    }
    projector_saved = None
    try:
        from api.projector_ai_api import persist_completed_review_for_projector

        projector_saved = await asyncio.to_thread(
            persist_completed_review_for_projector,
            session_id=accounting_operation_id,
            match_id=official_match["match_id"],
            markdown=full_result,
            transcript=transcript,
            projector_summary=projector_summary,
            model_label=label,
            audio=audio_metadata,
            recording_deleted=cleaned,
        )
    except Exception:
        # The authenticated browser still receives the complete response and
        # retries the idempotent projector result endpoint. Standalone reviews
        # intentionally have no projector session to persist into.
        projector_saved = None
    return JSONResponse(
        {
            "ok": True,
            "advisory_only": True,
            "judgement_evidence_mode": "audio_and_transcript",
            "markdown": full_result,
            "transcript": transcript,
            "projector_summary": projector_summary,
            "operation_id": accounting_operation_id,
            "model_label": label,
            "recording_deleted": cleaned,
            "projector_persisted": bool(projector_saved),
            "projector_revision": int((projector_saved or {}).get("revision") or 0),
            "match": official_match,
            "speaker_marker_count": len(markers),
            "audio": audio_metadata,
        },
        headers={"Cache-Control": "no-store"},
    )
