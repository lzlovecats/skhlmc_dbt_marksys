"""Dedicated kiosk authentication and privacy-bounded full-match AI review.

The browser records one low-bitrate microphone track and uploads it directly
to private R2.  Render verifies and downloads the bounded object only when the
operator asks for a review, deletes the temporary object before calling the AI
provider, and persists only normal AI-fund usage metadata.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import re
import uuid

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
)
from schema import TABLE_R2_UPLOAD_INTENTS
from system_limits import (
    KIOSK_MATCH_REVIEW_CONCURRENCY,
    KIOSK_MATCH_REVIEW_DAILY_LIMIT,
    KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
    KIOSK_MATCH_REVIEW_MAX_SECONDS,
    KIOSK_MATCH_REVIEW_MONTHLY_LIMIT,
    R2_OBJECT_CACHE_MAX_AGE_SECONDS,
    R2_UPLOAD_CLAIM_TTL_SECONDS,
)


router = APIRouter(prefix="/api/kiosk", tags=["kiosk"])
KIOSK_MATCH_REVIEW_SEMAPHORE = asyncio.Semaphore(
    KIOSK_MATCH_REVIEW_CONCURRENCY
)
KIOSK_MATCH_REVIEW_MIN_SECONDS = 10
AI_PROVIDER_PUBLIC_ERROR = "AI 評判暫時無法完成分析，請稍後重新錄製再試。"


class KioskLoginBody(BaseModel):
    password: str = Field(max_length=512)


class MatchReviewUploadIntentBody(BaseModel):
    mime_type: str = Field(default="audio/webm", max_length=80)
    byte_size: int = Field(gt=0, le=KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES)
    sha256: str = Field(min_length=64, max_length=64)
    duration_seconds: float = Field(
        ge=1, le=KIOSK_MATCH_REVIEW_MAX_SECONDS + 1
    )


class MatchReviewBody(BaseModel):
    upload_token: str = Field(min_length=20, max_length=10_000)
    topic: str = Field(min_length=1, max_length=500)
    debate_format: str = Field(default="校園隨想", max_length=80)
    pro_team: str = Field(default="正方", max_length=200)
    con_team: str = Field(default="反方", max_length=200)
    recording_notice_confirmed: bool = False


class MatchReviewDiscardBody(BaseModel):
    upload_token: str = Field(min_length=20, max_length=10_000)


def require_kiosk_user(request: Request) -> str:
    """Require the dedicated account even if another committee cookie is valid."""
    return require_page_user(request, "kiosk")


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
    if scope == "user_daily":
        return f"kiosk 今日最多可分析 {KIOSK_MATCH_REVIEW_DAILY_LIMIT} 場。"
    return "本月全系統全場錄音分析次數已達上限。"


@router.post("/match-review/upload-intent")
def match_review_upload_intent(
    body: MatchReviewUploadIntentBody, request: Request
):
    """Issue one short-lived direct-to-R2 PUT for an ephemeral recording."""
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret, get_vote_db

    user_id = require_kiosk_user(request)
    db = get_vote_db()
    if not r2_storage.configured():
        raise HTTPException(503, "Cloudflare R2 尚未完成設定，暫停全場錄音分析。")
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
    key = (
        "pending/audio/kiosk-match-review/"
        f"{datetime.datetime.now(datetime.timezone.utc):%Y/%m}/{intent_id}."
        f"{audio_extension(mime)}"
    )
    claim = {
        "kind": "kiosk_match_review",
        "intent_id": intent_id,
        "user": user_id,
        "mime_type": mime,
        "byte_size": body.byte_size,
        "sha256": digest,
        "duration_seconds": round(float(body.duration_seconds), 3),
        "pending_r2_key": key,
    }
    upload_token = r2_storage.sign_upload_claim(
        claim, secret, expires=R2_UPLOAD_CLAIM_TTL_SECONDS
    )
    reserved, scope = r2_storage.reserve_upload_intent(
        db,
        intent_id=intent_id,
        user_id=user_id,
        media_kind="kiosk_match_review",
        object_keys=[key],
        declared_bytes=body.byte_size,
        user_daily_limit=KIOSK_MATCH_REVIEW_DAILY_LIMIT,
        global_monthly_limit=KIOSK_MATCH_REVIEW_MONTHLY_LIMIT,
    )
    if not reserved:
        raise HTTPException(429, _storage_error(scope, storage_budget))
    return JSONResponse(
        {
            "upload_token": upload_token,
            "url": r2_storage.presign_put(key, mime, digest, body.byte_size),
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


def _match_review_prompts(body: MatchReviewBody, measured_duration: float) -> tuple[str, str]:
    system = """你是香港中學中文辯論比賽的資深評判。你會聽一段由場內單一收音咪錄下的完整比賽錄音，提供具體評語及建議勝方。

這只是 AI 輔助第二意見，不是正式賽果。不可因聲線、性別、口音或身份作判斷，只可按可聽到的辯論內容、攻防、回應、論證、組織和表達評估。若錄音太嘈、殘缺、未能可靠區分正反方或內容不足，必須判為「未能判定」，不可猜測。不要虛構逐字引述、分數、發言者或環節。用香港繁體中文書面粵語作答。"""
    user = f"""請分析隨附的全場錄音。

辯題：{body.topic.strip()}
賽制：{body.debate_format.strip() or '未提供'}
正方隊伍：{body.pro_team.strip() or '正方'}
反方隊伍：{body.con_team.strip() or '反方'}
實際錄音長度：約 {measured_duration / 60:.1f} 分鐘

請嚴格按以下格式輸出：
1. 聲明：先寫「以下只屬 AI 輔助評語，正式賽果以評判團為準。」
2. 建議勝方：只可寫「正方」、「反方」或「未能判定」；另列信心「高／中／低」。
3. 判定理由：3 至 6 點，引用可可靠辨認的實際論點或攻防（只能意譯；聽不清就明說）。
4. 正方評語：主要優點、最大漏洞、錯失的反駁或回應機會。
5. 反方評語：主要優點、最大漏洞、錯失的反駁或回應機會。
6. 全場及各環節評語：只評論你能可靠辨認的環節，涵蓋主線一致性、證據、回應、組織、表達及時間運用；不能辨認就略過並說明。
7. 改善建議：兩方各提供 2 至 3 項可立即實行的練習。
8. 錄音限制：交代收音清晰度、未能辨認的內容，以及限制如何影響判定。

不要提供看似官方的分數，不要將 AI 建議描述成正式裁決。"""
    return system, user


def _log_review_usage(db, label: str, config: dict, success: bool, *, usage=None, error=""):
    """Best-effort AI-fund accounting using provider token metadata."""
    try:
        from core.funds_logic import log_ai_usage

        actual = usage or {}
        input_tokens = int(actual.get("input_tokens") or 0) if success else 0
        output_tokens = int(actual.get("output_tokens") or 0) if success else 0
        audio_tokens = int(actual.get("audio_tokens") or 0) if success else 0
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
            },
            error_message=str(error or "")[:300],
            db=db,
        )
    except Exception:
        # An optional accounting failure must not discard a completed review.
        pass


@router.post("/match-review/analyze")
async def analyze_match_review(body: MatchReviewBody, request: Request):
    """Verify one recording, delete it, then obtain an advisory AI decision."""
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
        raise HTTPException(503, "全場評判模型尚未完成設定。") from exc
    if config.get("provider") != "gemini" or not config.get("supports_audio"):
        await asyncio.to_thread(cleanup)
        raise HTTPException(503, "全場評判模型必須支援錄音分析。")
    key_name = str(config.get("api_key") or "GEMINI_API_KEY")
    api_key = _get_proxy_secret(key_name).strip()
    if not api_key:
        await asyncio.to_thread(cleanup)
        _log_review_usage(db, label, config, False, error=f"{key_name} missing")
        raise HTTPException(503, f"未設定 {key_name}，暫時無法使用 AI 評判。")

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
            encoded_audio = base64.b64encode(audio).decode("ascii")
            del audio
            # Delete the raw recording before it leaves this bounded request.
            if not await asyncio.to_thread(cleanup):
                raise HTTPException(
                    502, "未能刪除暫存錄音，為保障私隱已取消 AI 分析。"
                )
            system_prompt, user_prompt = _match_review_prompts(
                body, float(probe["duration"])
            )
            await asyncio.to_thread(
                record_bandwidth_usage,
                "kiosk_match_review_provider",
                len(encoded_audio) + len(system_prompt.encode("utf-8"))
                + len(user_prompt.encode("utf-8")),
                user_id,
                aggregate_key=f"user={user_id[:120]}",
            )
            result, usage = await generate_text(
                config,
                system_prompt,
                user_prompt,
                api_key=api_key,
                audio_base64=encoded_audio,
                audio_mime=expected_mime,
                web_search=False,
            )
        except MediaProbeError as exc:
            await asyncio.to_thread(cleanup)
            raise HTTPException(
                503 if exc.service_unavailable else 400, str(exc)
            ) from exc
        except HTTPException:
            await asyncio.to_thread(cleanup)
            raise
        except Exception as exc:
            await asyncio.to_thread(cleanup)
            _log_review_usage(
                db, label, config, False, error=AI_PROVIDER_PUBLIC_ERROR
            )
            raise HTTPException(502, AI_PROVIDER_PUBLIC_ERROR) from exc

    _log_review_usage(db, label, config, True, usage=usage)
    return JSONResponse(
        {
            "ok": True,
            "advisory_only": True,
            "markdown": result,
            "model_label": label,
            "recording_deleted": cleaned,
            "audio": {
                "duration_seconds": probe["duration"],
                "sample_rate": probe["sample_rate"],
                "channels": probe["channels"],
            },
        },
        headers={"Cache-Control": "no-store"},
    )
