"""Competition-day coordination for AI評判易.

The projector kiosk owns the microphone and speaker.  A separately logged-in
competition-staff device only issues monotonic commands and previews the
result.  Recordings still follow :mod:`api.kiosk_api`'s direct-to-R2,
delete-before-provider path; this module stores only authenticated-encrypted,
two-hour result state and segment marker metadata.
"""

from __future__ import annotations

import base64
import asyncio
import datetime as dt
import hashlib
import json
import re
import uuid
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from account_access import KIOSK_ACCOUNT_ID
from api.access import require_competition_staff, require_page_user
from system_limits import (
    KIOSK_MATCH_REVIEW_MARKER_LIMIT,
    KIOSK_MATCH_REVIEW_DAILY_LIMIT,
    KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
    KIOSK_MATCH_REVIEW_MAX_SECONDS,
    KIOSK_MATCH_REVIEW_MONTHLY_LIMIT,
    KIOSK_MATCH_REVIEW_TRANSCRIPT_MAX_CHARS,
    TTS_MAX_RESPONSE_BYTES,
    TTS_TEXT_MAX_CHARS,
)


router = APIRouter(tags=["projector-ai"])

TABLE_SESSIONS = "projector_ai_sessions"
TABLE_CONTROLS = "projector_ai_controls"
TABLE_MARKERS = "projector_ai_markers"
RESULT_TTL = dt.timedelta(hours=2)
HARDWARE_TTL = dt.timedelta(minutes=30)
KIOSK_ONLINE_TTL = dt.timedelta(seconds=12)
TTS_CLAIM_TTL = dt.timedelta(minutes=10)
DISPLAY_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")
ACTIVE_RECORDING_STATES = {"start_requested", "recording", "stop_requested", "processing"}
RESULT_STATES = {"ready", "published"}
ADVISORY = "AI輔助第二意見，正式賽果以評判團為準。"
ACK_STATES_BY_COMMAND = {
    "hardware_test": {"hardware_ready", "error"},
    # A 90-minute/size auto-stop still belongs to the original start command.
    "start": {"recording", "processing", "error"},
    "stop": {"processing", "error"},
    "publish": {"published", "error"},
    "play": {"speaking", "played", "error"},
    "stop_speech": {"stopped", "error"},
    "clear": {"cleared", "error"},
}
ACK_PROGRESS = {
    "start": {"recording": 1, "processing": 2, "error": 3},
    "stop": {"processing": 1, "error": 2},
    "play": {"speaking": 1, "played": 2, "error": 2},
}


class DisplayBody(BaseModel):
    display: str = Field(default="main", min_length=1, max_length=80)


class HardwareConfirmBody(DisplayBody):
    screen_confirmed: bool = False
    audio_confirmed: bool = False


class StartBody(DisplayBody):
    match_id: str = Field(min_length=1, max_length=200)
    recording_notice_confirmed: bool = False


class SessionBody(DisplayBody):
    session_id: str = Field(min_length=20, max_length=80)


class PublishBody(SessionBody):
    speak: bool = True


class SpeechBody(SessionBody):
    action: Literal["play", "stop"]


class HeartbeatBody(DisplayBody):
    capabilities: dict = Field(default_factory=dict)


class KioskAckBody(DisplayBody):
    revision: int = Field(ge=0)
    session_id: str = Field(default="", max_length=80)
    state: Literal[
        "hardware_ready",
        "recording",
        "processing",
        "published",
        "speaking",
        "played",
        "stopped",
        "cleared",
        "error",
    ]
    detail: str = Field(default="", max_length=1000)
    payload: dict = Field(default_factory=dict)


class KioskResultBody(DisplayBody):
    session_id: str = Field(min_length=20, max_length=80)
    revision: int = Field(ge=0)
    markdown: str = Field(min_length=1, max_length=60_000)
    transcript: str = Field(min_length=1, max_length=KIOSK_MATCH_REVIEW_TRANSCRIPT_MAX_CHARS)
    projector_summary: str = Field(min_length=1, max_length=TTS_TEXT_MAX_CHARS)
    model_label: str = Field(default="", max_length=200)
    audio: dict = Field(default_factory=dict)
    recording_deleted: bool = False


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _display(value: str) -> str:
    cleaned = str(value or "main").strip()
    if not DISPLAY_RE.fullmatch(cleaned):
        raise HTTPException(400, "投影畫面代號格式不正確。")
    return cleaned


def _db():
    from deploy.proxy import get_vote_db

    return get_vote_db()


def _require_kiosk(request: Request) -> str:
    return require_page_user(request, "kiosk")


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}
    return {}


def _json_param(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _fernet() -> Fernet:
    from deploy.proxy import _get_relay_cookie_secret

    secret = str(_get_relay_cookie_secret() or "")
    if not secret:
        raise HTTPException(503, "AI評判易加密設定未就緒。")
    material = hashlib.sha256(
        ("projector-ai-result:v1:" + secret).encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def _seal_json(payload: dict) -> bytes:
    raw = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return _fernet().encrypt(raw)


def _open_json(ciphertext) -> dict:
    if not ciphertext:
        return {}
    try:
        raw = bytes(ciphertext)
        loaded = json.loads(_fernet().decrypt(raw).decode("utf-8"))
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(503, "AI評判易暫存結果無法解密。") from exc
    return loaded if isinstance(loaded, dict) else {}


def _seal_bytes(payload: bytes) -> bytes:
    return _fernet().encrypt(bytes(payload))


def _open_bytes(ciphertext) -> bytes:
    try:
        return _fernet().decrypt(bytes(ciphertext or b""))
    except (InvalidToken, TypeError, ValueError) as exc:
        raise HTTPException(503, "AI評判易暫存語音無法解密。") from exc


def _prune_expired(db, now: dt.datetime | None = None) -> None:
    current = now or _now()
    db.execute(
        f"""UPDATE {TABLE_SESSIONS}
            SET status='expired',status_detail='兩小時私人預覽期限已屆滿',
                result_ciphertext=NULL,tts_audio_ciphertext=NULL,tts_mime=NULL,
                tts_claim_token=NULL,tts_status='stopped',published=FALSE,updated_at=:now
            WHERE result_expires_at IS NOT NULL AND result_expires_at<=:now
              AND (result_ciphertext IS NOT NULL OR tts_audio_ciphertext IS NOT NULL
                   OR published=TRUE)""",
        {"now": current},
    )


def _ensure_control(conn, display: str, now: dt.datetime) -> None:
    conn.execute(
        text(
            f"""INSERT INTO {TABLE_CONTROLS}
                (display_key,created_at,updated_at)
                VALUES(:display,:now,:now)
                ON CONFLICT(display_key) DO NOTHING"""
        ),
        {"display": display, "now": now},
    )


def _lock_current_session(conn, display: str, session_id: str) -> None:
    """Reject stale operator actions while holding the display control lock."""
    control = conn.execute(
        text(
            f"""SELECT current_session_id FROM {TABLE_CONTROLS}
                WHERE display_key=:display FOR UPDATE"""
        ),
        {"display": display},
    ).fetchone()
    if (
        control is None
        or str(control._mapping.get("current_session_id") or "") != session_id
    ):
        raise HTTPException(409, "操作所屬場次已不是目前投影場次，請重新載入控制頁。")


def _issue_command(
    conn,
    display: str,
    command: str,
    *,
    session_id: str | None = None,
    detail: str = "",
    payload: dict | None = None,
    now: dt.datetime | None = None,
):
    current = now or _now()
    _ensure_control(conn, display, current)
    row = conn.execute(
        text(
            f"""UPDATE {TABLE_CONTROLS}
                SET current_session_id=COALESCE(:session_id,current_session_id),
                    command=:command,command_revision=command_revision+1,
                    status_detail=:detail,command_payload=CAST(:payload AS JSONB),
                    updated_at=:now
                WHERE display_key=:display
                RETURNING command_revision"""
        ),
        {
            "display": display,
            "session_id": session_id,
            "command": command,
            "detail": str(detail or "")[:1000],
            "payload": _json_param(payload or {}),
            "now": current,
        },
    ).fetchone()
    return int(row[0])


def _official_match(db, match_id: str) -> dict | None:
    from api.kiosk_api import _official_match as load_match

    return load_match(db, str(match_id or ""))


def _match_sequence(match: dict) -> list[dict]:
    from debate_timing import get_full_mock_sequence

    return get_full_mock_sequence(
        match["debate_format"], match.get("free_debate_minutes")
    )


def _marker_for_segment(match: dict, seg_index: int) -> tuple[str, str, int]:
    sequence = _match_sequence(match)
    if not sequence:
        return "unknown", "未標示環節", 0
    index = max(0, min(int(seg_index), len(sequence) - 1))
    segment = sequence[index]
    label = str(segment.get("label") or "未標示環節")[:80]
    side_label = str(segment.get("side") or "")
    segment_id = str(segment.get("id") or "").lower()
    if side_label == "正方":
        side = "pro"
    elif side_label == "反方":
        side = "con"
    elif side_label == "雙方" or "free" in segment_id:
        # The existing projector intentionally treats free debate as one
        # combined segment.  The transcript model assigns individual turns.
        side = "both"
    else:
        side = "unknown"
    return side, label, index


def record_projector_segment_change(
    conn,
    *,
    display: str,
    match: dict,
    seg_index: int,
    now: dt.datetime | None = None,
    force: bool = False,
) -> bool:
    """Append one recording marker inside the projector-state transaction."""
    current = now or _now()
    session = conn.execute(
        text(
            f"""SELECT session_id,recording_started_at
                FROM {TABLE_SESSIONS}
                WHERE display_key=:display AND match_id=:match_id
                  AND status='recording' AND recording_started_at IS NOT NULL
                ORDER BY created_at DESC LIMIT 1"""
        ),
        {"display": display, "match_id": match["match_id"]},
    ).fetchone()
    if session is None:
        return False
    side, label, index = _marker_for_segment(match, seg_index)
    started = session._mapping["recording_started_at"]
    offset = max(0.0, min(float(KIOSK_MATCH_REVIEW_MAX_SECONDS), (current - started).total_seconds()))
    if not force:
        previous = conn.execute(
            text(
                f"""SELECT seg_index FROM {TABLE_MARKERS}
                    WHERE session_id=:session ORDER BY id DESC LIMIT 1"""
            ),
            {"session": session._mapping["session_id"]},
        ).fetchone()
        if previous is not None and int(previous[0]) == index:
            return False
    conn.execute(
        text(
            f"""INSERT INTO {TABLE_MARKERS}
                (session_id,offset_seconds,side,segment,seg_index,created_at)
                VALUES(:session,:offset,:side,:segment,:index,:now)"""
        ),
        {
            "session": session._mapping["session_id"],
            "offset": round(offset, 3),
            "side": side,
            "segment": label,
            "index": index,
            "now": current,
        },
    )
    return True


def _hardware_is_fresh(hardware: dict, now: dt.datetime) -> bool:
    if not hardware.get("passed") or not hardware.get("operator_confirmed"):
        return False
    try:
        tested = dt.datetime.fromisoformat(str(hardware.get("tested_at") or ""))
        if tested.tzinfo is not None:
            tested = tested.astimezone(dt.timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return False
    return dt.timedelta(0) <= now - tested <= HARDWARE_TTL


def _kiosk_seen_is_fresh(last_seen, now: dt.datetime) -> bool:
    if last_seen is None:
        return False
    try:
        seen = last_seen
        if not isinstance(seen, dt.datetime):
            seen = dt.datetime.fromisoformat(str(seen))
        if seen.tzinfo is not None:
            seen = seen.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return dt.timedelta(0) <= now - seen <= KIOSK_ONLINE_TTL
    except (TypeError, ValueError):
        return False


def _timestamp_is_fresh(value, now: dt.datetime, ttl: dt.timedelta) -> bool:
    if value is None:
        return False
    try:
        timestamp = value
        if not isinstance(timestamp, dt.datetime):
            timestamp = dt.datetime.fromisoformat(str(timestamp))
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return dt.timedelta(0) <= now - timestamp <= ttl
    except (TypeError, ValueError):
        return False


def _control_status(db, display: str, *, include_private: bool) -> dict:
    _prune_expired(db)
    rows = db.query(
        f"""SELECT display_key,current_session_id,command,command_revision,
                   ack_revision,kiosk_status,status_detail,command_payload,
                   hardware_status,capabilities,kiosk_last_seen_at,updated_at
            FROM {TABLE_CONTROLS} WHERE display_key=:display""",
        {"display": display},
    )
    if rows.empty:
        control = {
            "display_key": display,
            "command": "",
            "command_revision": 0,
            "ack_revision": 0,
            "kiosk_status": "offline",
            "status_detail": "尚未收到比賽日 Kiosk 連線",
            "hardware": {},
            "capabilities": {},
            "kiosk_online": False,
        }
        return {"control": control, "session": None}
    row = rows.iloc[0]
    last_seen = row.get("kiosk_last_seen_at")
    online = _kiosk_seen_is_fresh(last_seen, _now())
    control = {
        "display_key": display,
        "command": str(row.get("command") or ""),
        "command_revision": int(row.get("command_revision") or 0),
        "ack_revision": int(row.get("ack_revision") or 0),
        "kiosk_status": str(row.get("kiosk_status") or "offline"),
        "status_detail": str(row.get("status_detail") or ""),
        "hardware": _json_object(row.get("hardware_status")),
        "capabilities": _json_object(row.get("capabilities")),
        "kiosk_last_seen_at": str(last_seen or ""),
        "kiosk_online": online,
    }
    session_id = str(row.get("current_session_id") or "")
    if not session_id:
        return {"control": control, "session": None}
    sessions = db.query(
        f"""SELECT session_id,display_key,match_id,status,status_detail,
                   recording_started_at,recording_duration_seconds,recording_bytes,
                   result_ciphertext,tts_audio_ciphertext,tts_mime,tts_status,
                   published,publish_revision,result_expires_at,created_at,updated_at
            FROM {TABLE_SESSIONS} WHERE session_id=:session""",
        {"session": session_id},
    )
    if sessions.empty:
        return {"control": control, "session": None}
    s = sessions.iloc[0]
    match = _official_match(db, str(s.get("match_id") or "")) or {
        "match_id": str(s.get("match_id") or "")
    }
    session = {
        "session_id": session_id,
        "display_key": display,
        "status": str(s.get("status") or ""),
        "status_detail": str(s.get("status_detail") or ""),
        "match": match,
        "recording_started_at": str(s.get("recording_started_at") or ""),
        "duration_seconds": float(s.get("recording_duration_seconds") or 0),
        "recording_bytes": int(s.get("recording_bytes") or 0),
        "result_expires_at": str(s.get("result_expires_at") or ""),
        "published": bool(s.get("published")),
        "publish_revision": int(s.get("publish_revision") or 0),
        "tts_status": str(s.get("tts_status") or "not_requested"),
        "tts_audio_ready": bool(s.get("tts_audio_ciphertext")),
    }
    if include_private and s.get("result_ciphertext"):
        private = _open_json(s.get("result_ciphertext"))
        session.update(
            result_markdown=str(private.get("markdown") or ""),
            transcript=str(private.get("transcript") or ""),
            projector_summary=str(private.get("projector_summary") or ""),
            model_label=str(private.get("model_label") or ""),
            audio=private.get("audio") if isinstance(private.get("audio"), dict) else {},
            recording_deleted=bool(private.get("recording_deleted")),
        )
    return {"control": control, "session": session}


def persist_completed_review_for_projector(
    *,
    session_id: str,
    match_id: str,
    markdown: str,
    transcript: str,
    projector_summary: str,
    model_label: str,
    audio: dict,
    recording_deleted: bool,
) -> dict | None:
    """Persist a completed kiosk review before its HTTP response is returned.

    ``operation_id`` is the projector session id. Standalone kiosk reviews have
    no matching row and simply return ``None``. This closes the otherwise
    unrecoverable gap where the provider succeeds but the browser loses the
    analyze response before it can POST the result back.
    """
    sid = str(session_id or "").strip()
    if not sid or not recording_deleted:
        return None
    summary = str(projector_summary or "").strip()
    if not summary or len(summary) > TTS_TEXT_MAX_CHARS:
        raise ValueError("invalid projector summary")
    payload = {
        "markdown": str(markdown or ""),
        "transcript": str(transcript or ""),
        "projector_summary": summary,
        "model_label": str(model_label or "")[:200],
        "audio": audio if isinstance(audio, dict) else {},
        "recording_deleted": True,
        "advisory": ADVISORY,
    }
    if not payload["markdown"] or not payload["transcript"]:
        raise ValueError("incomplete projector review")
    sealed = _seal_json(payload)
    db = _db()
    now = _now()
    expires = now + RESULT_TTL
    with db.transaction() as conn:
        row = conn.execute(
            text(
                f"""SELECT s.display_key,s.match_id,s.status,s.result_ciphertext,
                           s.result_expires_at,c.command_revision,c.current_session_id
                    FROM {TABLE_SESSIONS} s
                    JOIN {TABLE_CONTROLS} c ON c.display_key=s.display_key
                    WHERE s.session_id=:session
                    FOR UPDATE OF s,c"""
            ),
            {"session": sid},
        ).fetchone()
        if row is None:
            return None
        values = row._mapping
        if str(values.get("match_id") or "") != str(match_id or ""):
            raise ValueError("projector match mismatch")
        if str(values.get("current_session_id") or "") != sid:
            raise ValueError("projector session is no longer current")
        status = str(values.get("status") or "")
        if status in RESULT_STATES and values.get("result_ciphertext"):
            return {
                "display": str(values.get("display_key") or "main"),
                "revision": int(values.get("command_revision") or 0),
                "expires_at": str(values.get("result_expires_at") or ""),
                "idempotent": True,
            }
        if status not in {"start_requested", "recording", "stop_requested", "processing"}:
            raise ValueError("projector session cannot receive a completed review")
        conn.execute(
            text(
                f"""UPDATE {TABLE_SESSIONS}
                    SET status='ready',status_detail='AI評判易完成；等待賽會人員私人預覽',
                        result_ciphertext=:result,result_expires_at=:expires,
                        published=FALSE,tts_audio_ciphertext=NULL,tts_mime=NULL,
                        tts_status='not_requested',tts_claim_token=NULL,updated_at=:now
                    WHERE session_id=:session"""
            ),
            {"session": sid, "result": sealed, "expires": expires, "now": now},
        )
        revision = int(values.get("command_revision") or 0)
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET ack_revision=GREATEST(ack_revision,:revision),
                        kiosk_status='ready',status_detail='AI評判易完成；等待私人預覽',
                        kiosk_last_seen_at=:now,updated_at=:now
                    WHERE display_key=:display"""
            ),
            {
                "display": str(values.get("display_key") or "main"),
                "revision": revision,
                "now": now,
            },
        )
    return {
        "display": str(values.get("display_key") or "main"),
        "revision": revision,
        "expires_at": expires.isoformat(),
        "idempotent": False,
    }


@router.get("/api/projector/ai/status")
def operator_status(request: Request, display: str = "main"):
    require_competition_staff(request)
    key = _display(display)
    payload = _control_status(_db(), key, include_private=True)
    from deploy.proxy import tts_provider_configured

    payload["control"]["tts_available"] = bool(tts_provider_configured())
    try:
        from core.r2_storage import upload_intent_quota_status

        quota = upload_intent_quota_status(
            _db(),
            user_id=KIOSK_ACCOUNT_ID,
            media_kind="kiosk_match_review",
            user_daily_limit=KIOSK_MATCH_REVIEW_DAILY_LIMIT,
            global_monthly_limit=KIOSK_MATCH_REVIEW_MONTHLY_LIMIT,
        )
    except Exception:
        quota = {
            "allowed": False,
            "user_daily_remaining": 0,
            "global_monthly_remaining": 0,
            "blocked_scope": "status_unavailable",
        }
    payload["limits"] = {
        "max_seconds": KIOSK_MATCH_REVIEW_MAX_SECONDS,
        "max_bytes": KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
        "tts_max_chars": TTS_TEXT_MAX_CHARS,
        "result_ttl_seconds": int(RESULT_TTL.total_seconds()),
        "quota": quota,
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.post("/api/projector/ai/hardware-test")
def request_hardware_test(body: DisplayBody, request: Request):
    require_competition_staff(request)
    display = _display(body.display)
    now = _now()
    with _db().transaction() as conn:
        revision = _issue_command(
            conn,
            display,
            "hardware_test",
            detail="等待 Kiosk 測試收音咪、喇叭、投影及服務設定",
            payload={"requested_at": now.isoformat()},
            now=now,
        )
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET hardware_status='{{}}'::jsonb,kiosk_status='testing',updated_at=:now
                    WHERE display_key=:display"""
            ),
            {"display": display, "now": now},
        )
    return {"ok": True, "revision": revision}


@router.post("/api/projector/ai/hardware-confirm")
def confirm_hardware(body: HardwareConfirmBody, request: Request):
    require_competition_staff(request)
    display = _display(body.display)
    db = _db()
    status = _control_status(db, display, include_private=False)
    hardware = status["control"].get("hardware") or {}
    if not hardware.get("passed"):
        raise HTTPException(409, "Kiosk 尚未通過必要硬件測試。")
    if not body.screen_confirmed or not body.audio_confirmed:
        raise HTTPException(400, "請確認現場看見測試畫面並聽到測試聲。")
    hardware.update(operator_confirmed=True, confirmed_at=_now().isoformat())
    db.execute(
        f"""UPDATE {TABLE_CONTROLS}
            SET hardware_status=CAST(:hardware AS JSONB),status_detail=:detail,updated_at=:now
            WHERE display_key=:display""",
        {
            "display": display,
            "hardware": _json_param(hardware),
            "detail": "賽會人員已確認投影、喇叭及 Kiosk 測試結果",
            "now": _now(),
        },
    )
    return {"ok": True, "hardware": hardware}


@router.post("/api/projector/ai/start")
def start_session(body: StartBody, request: Request):
    require_competition_staff(request)
    if not body.recording_notice_confirmed:
        raise HTTPException(400, "請先確認已通知在場人士錄音及雲端處理安排。")
    display = _display(body.display)
    db = _db()
    now = _now()
    match = _official_match(db, body.match_id)
    if not match:
        raise HTTPException(404, "找不到所選正式場次。")
    if not match.get("topic") or not match.get("pro_team") or not match.get("con_team"):
        raise HTTPException(400, "正式場次的辯題及正反方資料未完整。")
    current = db.query(
        "SELECT match_id FROM projector_state WHERE display_key=:display",
        {"display": display},
    )
    if current.empty or str(current.iloc[0].get("match_id") or "") != match["match_id"]:
        raise HTTPException(409, "投影控制所選場次與 AI評判易場次不一致。")
    try:
        from core.r2_storage import upload_intent_quota_status

        quota = upload_intent_quota_status(
            db,
            user_id=KIOSK_ACCOUNT_ID,
            media_kind="kiosk_match_review",
            user_daily_limit=KIOSK_MATCH_REVIEW_DAILY_LIMIT,
            global_monthly_limit=KIOSK_MATCH_REVIEW_MONTHLY_LIMIT,
        )
    except Exception as exc:
        raise HTTPException(503, "未能確認 AI評判易目前配額，為免白錄已暫停開始。") from exc
    if not quota.get("allowed"):
        scope = str(quota.get("blocked_scope") or "")
        detail = (
            "AI評判易今日 kiosk 共用場次已用完，不能開始錄音。"
            if scope == "user_daily"
            else "AI評判易本月全系統場次已用完，不能開始錄音。"
        )
        raise HTTPException(429, detail)
    session_id = uuid.uuid4().hex
    with db.transaction() as conn:
        _ensure_control(conn, display, now)
        control = conn.execute(
            text(
                f"""SELECT kiosk_last_seen_at,hardware_status,capabilities
                    FROM {TABLE_CONTROLS}
                    WHERE display_key=:display FOR UPDATE"""
            ),
            {"display": display},
        ).fetchone()
        if control is None or not _kiosk_seen_is_fresh(
            control._mapping.get("kiosk_last_seen_at"), now
        ):
            raise HTTPException(409, "比賽日 Kiosk 未連線，不能開始錄音。")
        if not _hardware_is_fresh(
            _json_object(control._mapping.get("hardware_status")), now
        ):
            raise HTTPException(409, "請先完成硬件測試並由賽會人員確認。")
        capabilities = _json_object(control._mapping.get("capabilities"))
        if not bool(capabilities.get("media_primed")):
            raise HTTPException(409, "請先在 Kiosk 本機啟用收音咪及聲音。")
        active = conn.execute(
            text(
                f"""SELECT session_id FROM {TABLE_SESSIONS}
                    WHERE display_key=:display
                      AND (
                          status IN ('start_requested','recording','stop_requested','processing')
                          OR (
                              tts_status='generating'
                              AND updated_at>:tts_claim_cutoff
                          )
                      )
                    LIMIT 1"""
            ),
            {
                "display": display,
                "tts_claim_cutoff": now - TTS_CLAIM_TTL,
            },
        ).fetchone()
        if active is not None:
            raise HTTPException(409, "已有一場 AI評判易錄音或分析正在進行。")
        conn.execute(
            text(
                f"""INSERT INTO {TABLE_SESSIONS}
                    (session_id,display_key,match_id,status,status_detail,created_at,updated_at)
                    VALUES(:session,:display,:match,'start_requested',:detail,:now,:now)"""
            ),
            {
                "session": session_id,
                "display": display,
                "match": match["match_id"],
                "detail": "等待 Kiosk 開始全場錄音",
                "now": now,
            },
        )
        revision = _issue_command(
            conn,
            display,
            "start",
            session_id=session_id,
            detail="等待 Kiosk 開始全場錄音",
            payload={"session_id": session_id, "match_id": match["match_id"]},
            now=now,
        )
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET kiosk_status='start_requested',updated_at=:now
                    WHERE display_key=:display"""
            ),
            {"display": display, "now": now},
        )
    return {"ok": True, "session_id": session_id, "revision": revision, "match": match}


@router.post("/api/projector/ai/stop")
def stop_session(body: SessionBody, request: Request):
    require_competition_staff(request)
    display = _display(body.display)
    db = _db()
    now = _now()
    with db.transaction() as conn:
        _ensure_control(conn, display, now)
        control = conn.execute(
            text(
                f"""SELECT current_session_id FROM {TABLE_CONTROLS}
                    WHERE display_key=:display FOR UPDATE"""
            ),
            {"display": display},
        ).fetchone()
        if (
            control is None
            or str(control._mapping.get("current_session_id") or "")
            != body.session_id
        ):
            raise HTTPException(409, "停止指令與目前投影場次不一致。")
        stopped = conn.execute(
            text(
                f"""UPDATE {TABLE_SESSIONS}
                    SET status='stop_requested',status_detail='等待 Kiosk 封裝錄音及開始分析',updated_at=:now
                    WHERE session_id=:session AND display_key=:display
                      AND status='recording'
                    RETURNING session_id"""
            ),
            {"session": body.session_id, "display": display, "now": now},
        ).fetchone()
        if stopped is None:
            raise HTTPException(409, "AI評判易目前並非錄音中，停止指令沒有重複發出。")
        revision = _issue_command(
            conn,
            display,
            "stop",
            session_id=body.session_id,
            detail="等待 Kiosk 封裝錄音、上載及分析",
            payload={"session_id": body.session_id},
            now=now,
        )
    return {"ok": True, "revision": revision}


async def _synthesize_projector_summary(summary: str, session_id: str):
    from deploy import proxy

    if not proxy.tts_provider_configured():
        return None, "", "unavailable", "未設定可用 TTS provider；只投影文字。"
    if proxy._bandwidth_live_gate_error():
        return None, "", "unavailable", "網絡用量已達保護上限；只投影文字。"
    try:
        audio, mime, _meta = await proxy.synthesize_tts_accounted(
            summary,
            user_id=KIOSK_ACCOUNT_ID,
            feature="kiosk_match_review_tts",
            operation_id=session_id,
            operation_stage="result_tts",
        )
        if len(audio) > TTS_MAX_RESPONSE_BYTES:
            raise ValueError("TTS response exceeds bound")
        return bytes(audio), str(mime or "audio/mpeg"), "ready", "粵語評語語音已準備"
    except Exception:
        return None, "", "failed", "TTS 暫時未能合成；評語仍會以文字投影。"


@router.post("/api/projector/ai/publish")
async def publish_session(body: PublishBody, request: Request):
    require_competition_staff(request)
    display = _display(body.display)
    db = _db()
    _prune_expired(db)
    now = _now()
    claim_token = ""
    summary = ""
    sealed_audio = None
    mime = ""
    tts_status = "not_requested"
    tts_detail = "只投影文字。"
    revision = 0

    # Claim synthesis under a row lock before any provider call. This prevents
    # a double-click or two staff devices from paying for the same narration.
    with db.transaction() as conn:
        _lock_current_session(conn, display, body.session_id)
        row = conn.execute(
            text(
                f"""SELECT status,result_ciphertext,result_expires_at,published,
                           tts_audio_ciphertext,tts_mime,tts_status,
                           tts_claim_token,updated_at
                    FROM {TABLE_SESSIONS}
                    WHERE session_id=:session AND display_key=:display
                    FOR UPDATE"""
            ),
            {"session": body.session_id, "display": display},
        ).fetchone()
        if row is None:
            raise HTTPException(404, "找不到 AI評判易結果。")
        values = row._mapping
        if (
            str(values.get("status") or "") not in RESULT_STATES
            or not values.get("result_ciphertext")
        ):
            raise HTTPException(409, "AI評判易結果尚未完成，不能投影。")
        private = _open_json(values.get("result_ciphertext"))
        summary = str(private.get("projector_summary") or "").strip()
        if not summary or len(summary) > TTS_TEXT_MAX_CHARS:
            raise HTTPException(409, "AI評判易投影摘要格式不完整。")

        current_tts = str(values.get("tts_status") or "not_requested")
        if current_tts == "generating" and _timestamp_is_fresh(
            values.get("updated_at"), now, TTS_CLAIM_TTL
        ):
            raise HTTPException(409, "粵語評語正在準備，請勿重複提交。")

        sealed_audio = values.get("tts_audio_ciphertext")
        mime = str(values.get("tts_mime") or "") if sealed_audio else ""
        if body.speak and not sealed_audio:
            claim_token = uuid.uuid4().hex
            conn.execute(
                text(
                    f"""UPDATE {TABLE_SESSIONS}
                        SET status='published',published=TRUE,
                            publish_revision=publish_revision+1,
                            tts_status='generating',tts_claim_token=:claim,
                            status_detail='評語已投影，正在準備粵語語音',updated_at=:now
                        WHERE session_id=:session"""
                ),
                {"session": body.session_id, "claim": claim_token, "now": now},
            )
        else:
            if sealed_audio:
                tts_status = "ready"
                tts_detail = "已重用兩小時私人暫存的粵語語音。"
            elif current_tts in {"unavailable", "failed"}:
                tts_status = current_tts
            conn.execute(
                text(
                    f"""UPDATE {TABLE_SESSIONS}
                        SET status='published',status_detail=:detail,published=TRUE,
                            publish_revision=publish_revision+1,tts_status=:tts_status,
                            tts_claim_token=NULL,updated_at=:now
                        WHERE session_id=:session"""
                ),
                {
                    "session": body.session_id,
                    "detail": "評語已投影。" + tts_detail,
                    "tts_status": tts_status,
                    "now": now,
                },
            )
            revision = _issue_command(
                conn,
                display,
                "publish",
                session_id=body.session_id,
                detail=(
                    "評語已發布到大屏並準備好粵語語音"
                    if sealed_audio
                    else "評語已發布到大屏（文字模式）"
                ),
                payload={
                    "session_id": body.session_id,
                    "has_audio": bool(sealed_audio),
                },
                now=now,
            )

    if not claim_token:
        return {
            "ok": True,
            "revision": revision,
            "published": True,
            "tts_available": bool(sealed_audio),
            "tts_status": tts_status,
            "tts_detail": tts_detail,
        }

    audio, mime, tts_status, tts_detail = await _synthesize_projector_summary(
        summary, body.session_id
    )
    try:
        sealed_audio = _seal_bytes(audio) if audio else None
    except Exception:
        sealed_audio = None
        mime = ""
        tts_status = "failed"
        tts_detail = "粵語語音未能加密暫存；評語仍會以文字投影。"

    finished = _now()
    with db.transaction() as conn:
        _lock_current_session(conn, display, body.session_id)
        owner = conn.execute(
            text(
                f"""SELECT status,result_expires_at,tts_claim_token
                    FROM {TABLE_SESSIONS}
                    WHERE session_id=:session AND display_key=:display
                    FOR UPDATE"""
            ),
            {"session": body.session_id, "display": display},
        ).fetchone()
        if owner is None:
            raise HTTPException(409, "AI評判易場次已不存在。")
        owner_values = owner._mapping
        if str(owner_values.get("tts_claim_token") or "") != claim_token:
            raise HTTPException(409, "粵語評語已由另一個發布要求處理。")
        expiry = owner_values.get("result_expires_at")
        try:
            expiry_time = expiry
            if not isinstance(expiry_time, dt.datetime):
                expiry_time = dt.datetime.fromisoformat(str(expiry_time))
            if expiry_time.tzinfo is not None:
                expiry_time = expiry_time.astimezone(dt.timezone.utc).replace(tzinfo=None)
            result_expired = expiry_time <= finished
        except (TypeError, ValueError):
            result_expired = True
        if (
            str(owner_values.get("status") or "") in {"cleared", "expired"}
            or result_expired
        ):
            conn.execute(
                text(
                    f"""UPDATE {TABLE_SESSIONS}
                        SET tts_claim_token=NULL,tts_status='stopped',updated_at=:now
                        WHERE session_id=:session"""
                ),
                {"session": body.session_id, "now": finished},
            )
            raise HTTPException(409, "AI評判易結果已清除或超過兩小時期限。")
        conn.execute(
            text(
                f"""UPDATE {TABLE_SESSIONS}
                    SET status='published',status_detail=:detail,published=TRUE,
                        tts_audio_ciphertext=:audio,tts_mime=:mime,
                        tts_status=:tts_status,tts_claim_token=NULL,updated_at=:now
                    WHERE session_id=:session AND tts_claim_token=:claim"""
            ),
            {
                "session": body.session_id,
                "claim": claim_token,
                "detail": "評語已投影。" + tts_detail,
                "audio": sealed_audio,
                "mime": mime or None,
                "tts_status": tts_status,
                "now": finished,
            },
        )
        revision = _issue_command(
            conn,
            display,
            "publish",
            session_id=body.session_id,
            detail=(
                "評語已發布到大屏並準備好粵語語音"
                if sealed_audio
                else "評語已發布到大屏（文字模式）"
            ),
            payload={"session_id": body.session_id, "has_audio": bool(sealed_audio)},
            now=finished,
        )
    return {
        "ok": True,
        "revision": revision,
        "published": True,
        "tts_available": bool(sealed_audio),
        "tts_status": tts_status,
        "tts_detail": tts_detail,
    }


@router.post("/api/projector/ai/speech")
def speech_command(body: SpeechBody, request: Request):
    require_competition_staff(request)
    display = _display(body.display)
    db = _db()
    _prune_expired(db)
    command = "play" if body.action == "play" else "stop_speech"
    with db.transaction() as conn:
        _lock_current_session(conn, display, body.session_id)
        row = conn.execute(
            text(
                f"""SELECT published,tts_audio_ciphertext
                    FROM {TABLE_SESSIONS}
                    WHERE session_id=:session AND display_key=:display
                      AND result_expires_at>:now
                    FOR UPDATE"""
            ),
            {"session": body.session_id, "display": display, "now": _now()},
        ).fetchone()
        if row is None or not bool(row._mapping.get("published")):
            raise HTTPException(409, "評語尚未投影。")
        if body.action == "play" and not row._mapping.get("tts_audio_ciphertext"):
            raise HTTPException(409, "沒有可播放的 TTS 語音；目前只可投影文字。")
        revision = _issue_command(
            conn,
            display,
            command,
            session_id=body.session_id,
            detail="等待 Kiosk " + ("播放粵語評語" if command == "play" else "停止朗讀"),
            payload={"session_id": body.session_id},
        )
    return {"ok": True, "revision": revision}


@router.post("/api/projector/ai/clear")
def clear_session(body: SessionBody, request: Request):
    require_competition_staff(request)
    display = _display(body.display)
    now = _now()
    db = _db()
    with db.transaction() as conn:
        _lock_current_session(conn, display, body.session_id)
        updated = conn.execute(
            text(
                f"""UPDATE {TABLE_SESSIONS}
                    SET status='cleared',status_detail='賽會人員已清除私人結果',
                        result_ciphertext=NULL,tts_audio_ciphertext=NULL,tts_mime=NULL,
                        tts_claim_token=NULL,published=FALSE,tts_status='stopped',updated_at=:now
                    WHERE session_id=:session AND display_key=:display
                      AND status NOT IN ('start_requested','recording','stop_requested','processing')
                    RETURNING session_id"""
            ),
            {"session": body.session_id, "display": display, "now": now},
        ).fetchone()
        if updated is None:
            raise HTTPException(409, "錄音或分析進行期間不能清除 AI評判易場次。")
        revision = _issue_command(
            conn,
            display,
            "clear",
            session_id=body.session_id,
            detail="已清除 AI評判易投影及私人暫存結果",
            payload={"session_id": body.session_id},
            now=now,
        )
    return {"ok": True, "revision": revision}


@router.get("/api/projector/ai/public")
def public_result(display: str = "main"):
    key = _display(display)
    db = _db()
    _prune_expired(db)
    rows = db.query(
        f"""SELECT s.match_id,s.result_ciphertext,s.result_expires_at,s.publish_revision,
                   s.tts_status,s.updated_at
            FROM {TABLE_CONTROLS} c
            JOIN {TABLE_SESSIONS} s ON s.session_id=c.current_session_id
            WHERE c.display_key=:display AND s.published=TRUE
              AND s.result_expires_at>:now""",
        {"display": key, "now": _now()},
    )
    if rows.empty:
        return JSONResponse(
            {"published": False, "display_key": key},
            headers={"Cache-Control": "no-store"},
        )
    row = rows.iloc[0]
    private = _open_json(row.get("result_ciphertext"))
    official = _official_match(db, str(row.get("match_id") or "")) or {}
    safe_match = {
        "match_id": str(official.get("match_id") or row.get("match_id") or "")[:200],
        "motion": str(official.get("topic") or "")[:500],
        "pro_team": str(official.get("pro_team") or "")[:100],
        "con_team": str(official.get("con_team") or "")[:100],
        "debate_format": str(official.get("debate_format") or "")[:80],
    }
    return JSONResponse(
        {
            "published": True,
            "display_key": key,
            "match_id": safe_match["match_id"],
            "match": safe_match,
            "projector_summary": str(private.get("projector_summary") or ""),
            "advisory": ADVISORY,
            "revision": int(row.get("publish_revision") or 0),
            "expires_at": str(row.get("result_expires_at") or ""),
            "tts_status": str(row.get("tts_status") or "not_requested"),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/kiosk/projector-ai/command")
def kiosk_command(request: Request, display: str = "main"):
    _require_kiosk(request)
    key = _display(display)
    db = _db()
    now = _now()
    with db.transaction() as conn:
        _ensure_control(conn, key, now)
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET kiosk_last_seen_at=:now,
                        kiosk_status=CASE WHEN kiosk_status='offline' THEN 'online' ELSE kiosk_status END,
                        updated_at=:now WHERE display_key=:display"""
            ),
            {"display": key, "now": now},
        )
    status = _control_status(db, key, include_private=False)
    session = status.get("session")
    if session:
        markers = db.query(
            f"""SELECT offset_seconds,side,segment,seg_index
                FROM {TABLE_MARKERS} WHERE session_id=:session
                ORDER BY offset_seconds,id LIMIT :limit""",
            {"session": session["session_id"], "limit": KIOSK_MATCH_REVIEW_MARKER_LIMIT},
        )
        session["markers"] = [
            {
                "offset_seconds": float(item.get("offset_seconds") or 0),
                "side": str(item.get("side") or "unknown"),
                "segment": str(item.get("segment") or ""),
                "seg_index": int(item.get("seg_index") or 0),
            }
            for item in markers.to_dict("records")
        ]
    from deploy.proxy import tts_provider_configured

    return JSONResponse(
        {
            **status,
            "tts_available": bool(tts_provider_configured()),
            "limits": {
                "max_seconds": KIOSK_MATCH_REVIEW_MAX_SECONDS,
                "max_bytes": KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
                "tts_max_chars": TTS_TEXT_MAX_CHARS,
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/kiosk/projector-ai/heartbeat")
def kiosk_heartbeat(body: HeartbeatBody, request: Request):
    _require_kiosk(request)
    display = _display(body.display)
    capabilities = {
        str(key)[:80]: value
        for key, value in list(body.capabilities.items())[:30]
        if isinstance(value, (str, int, float, bool, type(None)))
    }
    now = _now()
    with _db().transaction() as conn:
        _ensure_control(conn, display, now)
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET kiosk_last_seen_at=:now,
                        kiosk_status=CASE
                            WHEN COALESCE(kiosk_status,'') IN ('','offline') THEN 'online'
                            ELSE kiosk_status
                        END,
                        capabilities=CAST(:capabilities AS JSONB),updated_at=:now
                    WHERE display_key=:display"""
            ),
            {
                "display": display,
                "capabilities": _json_param(capabilities),
                "now": now,
            },
        )
    return {"ok": True, "server_time": now.isoformat()}


@router.post("/api/kiosk/projector-ai/ack")
def kiosk_ack(body: KioskAckBody, request: Request):
    _require_kiosk(request)
    display = _display(body.display)
    db = _db()
    now = _now()
    with db.transaction() as conn:
        _ensure_control(conn, display, now)
        control = conn.execute(
            text(
                f"""SELECT command,command_revision,ack_revision,current_session_id,
                           kiosk_status
                    FROM {TABLE_CONTROLS} WHERE display_key=:display FOR UPDATE"""
            ),
            {"display": display},
        ).fetchone()
        command_revision = int(control._mapping["command_revision"] or 0)
        ack_revision = int(control._mapping["ack_revision"] or 0)
        if body.revision > command_revision:
            raise HTTPException(409, "Kiosk ACK revision 超出目前指令。")
        if body.revision < command_revision:
            return {
                "ok": True,
                "stale": True,
                "ack_revision": ack_revision,
                "command_revision": command_revision,
            }
        command = str(control._mapping.get("command") or "")
        if body.state not in ACK_STATES_BY_COMMAND.get(command, set()):
            raise HTTPException(409, "Kiosk ACK 狀態與目前指令不相容。")
        current_session = str(control._mapping.get("current_session_id") or "")
        if body.session_id and current_session and body.session_id != current_session:
            raise HTTPException(409, "Kiosk ACK 與目前場次不一致。")

        kiosk_status = body.state
        detail = str(body.detail or "")[:1000]
        current_kiosk_status = str(control._mapping.get("kiosk_status") or "")
        if body.revision == ack_revision:
            if current_kiosk_status == body.state:
                conn.execute(
                    text(
                        f"""UPDATE {TABLE_CONTROLS}
                            SET kiosk_last_seen_at=:now,updated_at=:now
                            WHERE display_key=:display"""
                    ),
                    {"display": display, "now": now},
                )
                return {"ok": True, "idempotent": True, "ack_revision": ack_revision}
            progress = ACK_PROGRESS.get(command, {})
            current_rank = progress.get(current_kiosk_status)
            incoming_rank = progress.get(body.state)
            if (
                current_rank is not None
                and incoming_rank is not None
                and incoming_rank <= current_rank
            ):
                return {"ok": True, "stale": True, "ack_revision": ack_revision}

        hardware_json = None
        if body.state == "hardware_ready":
            safe_payload = {
                str(key)[:80]: value
                for key, value in list(body.payload.items())[:40]
                if isinstance(value, (str, int, float, bool, list, type(None)))
            }
            safe_payload.update(
                passed=bool(body.payload.get("passed")),
                tested_at=now.isoformat(),
                operator_confirmed=False,
            )
            hardware_json = _json_param(safe_payload)
        session_id = body.session_id or current_session
        session_status = ""
        if session_id:
            session_row = conn.execute(
                text(
                    f"""SELECT status FROM {TABLE_SESSIONS}
                        WHERE session_id=:session AND display_key=:display"""
                ),
                {"session": session_id, "display": display},
            ).fetchone()
            session_status = (
                str(session_row._mapping.get("status") or "")
                if session_row is not None
                else ""
            )
        if (
            command in {"start", "stop"}
            and session_status in {"ready", "published", "cleared", "expired", "error"}
            and body.state in {"recording", "processing", "error"}
        ):
            return {"ok": True, "stale": True, "ack_revision": ack_revision}

        if session_id:
            if body.state == "recording":
                transitioned = conn.execute(
                    text(
                        f"""UPDATE {TABLE_SESSIONS}
                            SET status='recording',status_detail=:detail,
                                recording_started_at=:now,updated_at=:now
                            WHERE session_id=:session AND status='start_requested'
                            RETURNING session_id"""
                    ),
                    {"session": session_id, "detail": detail or "全場錄音中", "now": now},
                ).fetchone()
                if transitioned is not None:
                    projector = conn.execute(
                        text("SELECT match_id,seg_index FROM projector_state WHERE display_key=:display"),
                        {"display": display},
                    ).fetchone()
                    if projector is not None:
                        match = _official_match(db, str(projector._mapping.get("match_id") or ""))
                        if match:
                            record_projector_segment_change(
                                conn,
                                display=display,
                                match=match,
                                seg_index=int(projector._mapping.get("seg_index") or 0),
                                now=now,
                                force=True,
                            )
            elif body.state == "processing":
                duration = max(0.0, min(float(body.payload.get("duration_seconds") or 0), KIOSK_MATCH_REVIEW_MAX_SECONDS))
                size = max(0, min(int(body.payload.get("recording_bytes") or 0), KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES))
                conn.execute(
                    text(
                        f"""UPDATE {TABLE_SESSIONS}
                            SET status='processing',status_detail=:detail,
                                recording_duration_seconds=:duration,recording_bytes=:bytes,
                                updated_at=:now WHERE session_id=:session
                                  AND status IN ('recording','stop_requested')"""
                    ),
                    {
                        "session": session_id,
                        "detail": detail or "錄音已停止，正上載及分析",
                        "duration": duration,
                        "bytes": size,
                        "now": now,
                    },
                )
            elif body.state == "error":
                if command in {"start", "stop"}:
                    conn.execute(
                        text(
                            f"""UPDATE {TABLE_SESSIONS}
                                SET status='error',status_detail=:detail,updated_at=:now
                                WHERE session_id=:session
                                  AND status IN ('start_requested','recording','stop_requested','processing')"""
                        ),
                        {"session": session_id, "detail": detail or "Kiosk 發生錯誤", "now": now},
                    )
                elif command == "play":
                    conn.execute(
                        text(
                            f"""UPDATE {TABLE_SESSIONS}
                                SET tts_status='failed',status_detail=:detail,updated_at=:now
                                WHERE session_id=:session AND published=TRUE"""
                        ),
                        {"session": session_id, "detail": detail or "粵語語音播放失敗", "now": now},
                    )
            elif body.state in {"speaking", "played", "stopped"}:
                tts_status = {"speaking": "playing", "played": "played", "stopped": "stopped"}[body.state]
                conn.execute(
                    text(
                        f"""UPDATE {TABLE_SESSIONS}
                            SET tts_status=:tts,status_detail=:detail,updated_at=:now
                            WHERE session_id=:session AND published=TRUE"""
                    ),
                    {"session": session_id, "tts": tts_status, "detail": detail, "now": now},
                )
            elif body.state == "published":
                # Publishing in text-only mode must preserve unavailable/failed;
                # only the synthesis path may mark narration ready.
                conn.execute(
                    text(
                        f"""UPDATE {TABLE_SESSIONS}
                            SET status_detail=:detail,updated_at=:now
                            WHERE session_id=:session AND published=TRUE"""
                    ),
                    {"session": session_id, "detail": detail, "now": now},
                )

        query = f"""UPDATE {TABLE_CONTROLS}
            SET ack_revision=GREATEST(ack_revision,:revision),
                kiosk_status=:status,status_detail=:detail,kiosk_last_seen_at=:now,
                updated_at=:now"""
        params = {
            "display": display,
            "revision": body.revision,
            "status": kiosk_status,
            "detail": detail,
            "now": now,
        }
        if hardware_json is not None:
            query += ",hardware_status=CAST(:hardware AS JSONB)"
            params["hardware"] = hardware_json
        query += " WHERE display_key=:display"
        conn.execute(text(query), params)
    return {"ok": True, "ack_revision": body.revision}


@router.post("/api/kiosk/projector-ai/result")
def kiosk_result(body: KioskResultBody, request: Request):
    _require_kiosk(request)
    display = _display(body.display)
    if not body.recording_deleted:
        raise HTTPException(409, "未確認私人 R2 錄音已刪除，拒絕保存 AI 結果。")
    summary = str(body.projector_summary or "").strip()
    if len(summary) > TTS_TEXT_MAX_CHARS:
        raise HTTPException(400, "投影摘要超出 1,200 字。")
    payload = {
        "markdown": body.markdown,
        "transcript": body.transcript,
        "projector_summary": summary,
        "model_label": body.model_label,
        "audio": body.audio,
        "recording_deleted": bool(body.recording_deleted),
        "advisory": ADVISORY,
    }
    sealed = _seal_json(payload)
    now = _now()
    expires = now + RESULT_TTL
    db = _db()
    with db.transaction() as conn:
        control = conn.execute(
            text(
                f"""SELECT current_session_id,command_revision
                    FROM {TABLE_CONTROLS} WHERE display_key=:display FOR UPDATE"""
            ),
            {"display": display},
        ).fetchone()
        if control is None or str(control._mapping.get("current_session_id") or "") != body.session_id:
            raise HTTPException(409, "分析結果與目前投影場次不一致。")
        if int(control._mapping.get("command_revision") or 0) != body.revision:
            raise HTTPException(409, "分析結果使用了過期的 Kiosk 指令。")
        existing = conn.execute(
            text(
                f"""SELECT status,result_ciphertext,result_expires_at
                    FROM {TABLE_SESSIONS}
                    WHERE session_id=:session AND display_key=:display
                    FOR UPDATE"""
            ),
            {"session": body.session_id, "display": display},
        ).fetchone()
        if existing is None:
            raise HTTPException(409, "找不到可接收結果的 AI評判易場次。")
        existing_values = existing._mapping
        if (
            str(existing_values.get("status") or "") in RESULT_STATES
            and existing_values.get("result_ciphertext")
        ):
            saved = _open_json(existing_values.get("result_ciphertext"))
            same_result = (
                str(saved.get("markdown") or "") == body.markdown
                and str(saved.get("transcript") or "") == body.transcript
                and str(saved.get("projector_summary") or "") == summary
                and str(saved.get("model_label") or "") == body.model_label
                and (saved.get("audio") or {}) == body.audio
                and bool(saved.get("recording_deleted"))
            )
            if same_result:
                return {
                    "ok": True,
                    "idempotent": True,
                    "expires_at": str(existing_values.get("result_expires_at") or ""),
                }
            raise HTTPException(409, "此場次已保存另一份 AI評判易結果。")
        updated = conn.execute(
            text(
                f"""UPDATE {TABLE_SESSIONS}
                    SET status='ready',status_detail='AI評判易完成；等待賽會人員私人預覽',
                        result_ciphertext=:result,result_expires_at=:expires,
                        published=FALSE,tts_audio_ciphertext=NULL,tts_mime=NULL,
                        tts_status='not_requested',tts_claim_token=NULL,updated_at=:now
                    WHERE session_id=:session AND display_key=:display
                      AND status='processing'
                    RETURNING session_id"""
            ),
            {
                "session": body.session_id,
                "display": display,
                "result": sealed,
                "expires": expires,
                "now": now,
            },
        ).fetchone()
        if updated is None:
            raise HTTPException(409, "AI評判易場次不在可接收結果的狀態。")
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET ack_revision=GREATEST(ack_revision,:revision),
                        kiosk_status='ready',status_detail='AI評判易完成；等待私人預覽',
                        kiosk_last_seen_at=:now,updated_at=:now
                    WHERE display_key=:display"""
            ),
            {"display": display, "revision": body.revision, "now": now},
        )
    return {"ok": True, "expires_at": expires.isoformat()}


@router.get("/api/kiosk/projector-ai/audio/{session_id}")
async def kiosk_audio(session_id: str, request: Request, revision: int = 0):
    _require_kiosk(request)
    from deploy import proxy

    if proxy._bandwidth_live_gate_error():
        raise HTTPException(429, "網絡用量已達保護上限；評語只會投影文字。")
    db = _db()
    _prune_expired(db)
    rows = db.query(
        f"""SELECT tts_audio_ciphertext,tts_mime,publish_revision
            FROM {TABLE_SESSIONS}
            WHERE session_id=:session AND published=TRUE AND result_expires_at>:now""",
        {"session": str(session_id)[:80], "now": _now()},
    )
    if rows.empty or not rows.iloc[0].get("tts_audio_ciphertext"):
        raise HTTPException(404, "此場次沒有可播放的 TTS 語音。")
    publish_revision = int(rows.iloc[0].get("publish_revision") or 0)
    if revision and int(revision) != publish_revision:
        raise HTTPException(409, "TTS 語音版本已更新。")
    audio = _open_bytes(rows.iloc[0].get("tts_audio_ciphertext"))
    await asyncio.to_thread(
        proxy.record_bandwidth_usage,
        "tts_audio_response",
        len(audio),
        KIOSK_ACCOUNT_ID,
        aggregate_key=f"user={KIOSK_ACCOUNT_ID[:120]}",
    )
    return Response(
        content=audio,
        media_type=str(rows.iloc[0].get("tts_mime") or "audio/mpeg"),
        headers={"Cache-Control": "no-store", "Content-Encoding": "identity"},
    )
