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
import binascii
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from account_access import KIOSK_ACCOUNT_ID
from api.access import require_competition_staff, require_page_user
from schema import TABLE_MATCHES
from system_limits import (
    KIOSK_MATCH_REVIEW_MARKER_LIMIT,
    KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
    KIOSK_MATCH_REVIEW_MAX_SECONDS,
    COMMITTEE_SESSION_MAX_AGE_SECONDS,
    PROJECTOR_KIOSK_LEASE_TTL_SECONDS,
    PROJECTOR_START_COMMAND_TTL_SECONDS,
    TTS_MAX_RESPONSE_BYTES,
    TTS_TEXT_MAX_CHARS,
)


router = APIRouter(tags=["projector-ai"])

TABLE_SESSIONS = "projector_ai_sessions"
TABLE_CONTROLS = "projector_ai_controls"
TABLE_MARKERS = "projector_ai_markers"
TABLE_KIOSK_DEVICES = "projector_kiosk_devices"
RESULT_TTL = dt.timedelta(hours=2)
HARDWARE_TTL = dt.timedelta(minutes=30)
KIOSK_ONLINE_TTL = dt.timedelta(seconds=12)
TTS_CLAIM_TTL = dt.timedelta(minutes=10)
DISPLAY_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")
CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{20,100}")
DEVICE_COOKIE_NAME = "projector_kiosk_device"
LEASE_TOKEN_HEADER = "X-Kiosk-Lease-Token"
LEASE_CLIENT_HEADER = "X-Kiosk-Client-Id"
LEASE_GENERATION_HEADER = "X-Kiosk-Lease-Generation"
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
    "cancel_start": {"cancelled", "error"},
}
ACK_PROGRESS = {
    "start": {"recording": 1, "processing": 2, "error": 3},
    "stop": {"processing": 1, "error": 2},
    "play": {"speaking": 1, "played": 2, "error": 2},
}


class DisplayBody(BaseModel):
    display: str = Field(default="main", min_length=1, max_length=80)


class HardwareConfirmBody(DisplayBody):
    revision: int = Field(ge=0)
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
    client_id: str = Field(default="", max_length=100)
    lease_generation: int = Field(default=0, ge=0)
    capabilities: dict = Field(default_factory=dict)


class KioskAckBody(DisplayBody):
    client_id: str = Field(default="", max_length=100)
    lease_generation: int = Field(default=0, ge=0)
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
        "cancelled",
        "error",
    ]
    detail: str = Field(default="", max_length=1000)
    payload: dict = Field(default_factory=dict)


class LeaseClaimBody(DisplayBody):
    client_id: str = Field(min_length=20, max_length=100)
    lease_generation: int = Field(default=0, ge=0)
    capabilities: dict = Field(default_factory=dict)


class LeaseTakeoverBody(DisplayBody):
    expected_generation: int = Field(ge=0)
    confirm_interrupt_active_session: bool = False


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _display(value: str) -> str:
    cleaned = str(value or "main").strip()
    if not DISPLAY_RE.fullmatch(cleaned):
        raise HTTPException(400, "投影畫面代號格式不正確。")
    return cleaned


def _client_id(value: str) -> str:
    cleaned = str(value or "").strip()
    if not CLIENT_ID_RE.fullmatch(cleaned):
        raise HTTPException(400, "Kiosk 分頁識別碼格式不正確。")
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


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    raw = value.encode("ascii")
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


def _device_cookie_secret() -> str:
    from deploy.proxy import _get_relay_cookie_secret

    secret = str(_get_relay_cookie_secret() or "")
    if not secret:
        raise HTTPException(503, "Kiosk 裝置識別服務未就緒。")
    return secret


def _sign_device_cookie(device_id: str, generation: int) -> str:
    payload = {
        "v": 1,
        "device_id": str(device_id),
        "generation": int(generation),
        "exp": int(time.time()) + COMMITTEE_SESSION_MAX_AGE_SECONDS,
    }
    encoded = _b64url(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signature = hmac.new(
        _device_cookie_secret().encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded}.{_b64url(signature)}"


def _verify_device_cookie(token: str) -> dict | None:
    if not token or len(str(token)) > 2048:
        return None
    try:
        encoded, supplied = str(token).split(".", 1)
        expected = hmac.new(
            _device_cookie_secret().encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64url(expected), supplied):
            return None
        payload = json.loads(_b64url_decode(encoded))
        if not isinstance(payload, dict) or int(payload.get("v") or 0) != 1:
            return None
        if int(payload.get("exp") or 0) < int(time.time()):
            return None
        if not re.fullmatch(r"[a-f0-9]{32}", str(payload.get("device_id") or "")):
            return None
        if int(payload.get("generation") or 0) < 1:
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeError, binascii.Error):
        return None


def _lease_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _as_naive_timestamp(value) -> dt.datetime | None:
    if value is None:
        return None
    try:
        result = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
        if result.tzinfo is not None:
            result = result.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return result
    except (TypeError, ValueError):
        return None


def _lease_can_claim(
    *,
    owner_device_id: str,
    owner_client_id: str,
    requesting_device_id: str,
    requesting_client_id: str,
    lease_expires_at,
    session_status: str,
    now: dt.datetime,
) -> bool:
    """Return whether a claimant may become owner before credential checks."""
    if not owner_device_id:
        return True
    if (
        owner_device_id == requesting_device_id
        and owner_client_id == requesting_client_id
    ):
        return True
    expires = _as_naive_timestamp(lease_expires_at)
    if expires is not None and expires > now:
        return False
    return str(session_status or "") not in ACTIVE_RECORDING_STATES


def _safe_capabilities(value: dict) -> dict:
    return {
        str(key)[:80]: item
        for key, item in list((value or {}).items())[:30]
        if isinstance(item, (str, int, float, bool, type(None)))
    }


def _lease_credentials(
    request: Request,
    *,
    client_id: str = "",
    lease_generation: int = 0,
) -> tuple[str, str, int]:
    token = str(request.headers.get(LEASE_TOKEN_HEADER) or "").strip()
    client = _client_id(
        request.headers.get(LEASE_CLIENT_HEADER) or client_id
    )
    raw_generation = request.headers.get(LEASE_GENERATION_HEADER)
    try:
        generation = int(raw_generation if raw_generation is not None else lease_generation)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Kiosk lease generation 格式不正確。") from exc
    if generation < 1 or not token:
        raise HTTPException(423, "此 Kiosk 並非目前 Active 裝置。")
    return token, client, generation


def _locked_lease_control(conn, display: str):
    return conn.execute(
        text(
            f"""SELECT c.*,s.status AS current_session_status,
                       d.label AS lease_device_label,
                       d.enabled AS lease_device_enabled,
                       d.revoked_at AS lease_device_revoked_at,
                       d.credential_generation AS lease_device_credential_generation
                FROM {TABLE_CONTROLS} AS c
                LEFT JOIN {TABLE_SESSIONS} AS s
                  ON s.session_id=c.current_session_id
                LEFT JOIN {TABLE_KIOSK_DEVICES} AS d
                  ON d.device_id=c.lease_device_id
                WHERE c.display_key=:display
                FOR UPDATE OF c"""
        ),
        {"display": display},
    ).fetchone()


def _assert_lease_mapping(
    mapping,
    request: Request,
    *,
    client_id: str = "",
    lease_generation: int = 0,
    now: dt.datetime | None = None,
    require_command_generation: bool = False,
    allow_expired: bool = False,
) -> dict:
    current = now or _now()
    token, client, generation = _lease_credentials(
        request,
        client_id=client_id,
        lease_generation=lease_generation,
    )
    device_claim = _verify_device_cookie(
        str(request.cookies.get(DEVICE_COOKIE_NAME) or "")
    )
    owner_device = str(mapping.get("lease_device_id") or "")
    owner_generation = int(mapping.get("lease_generation") or 0)
    owner_client = str(mapping.get("lease_client_id") or "")
    credential_generation = int(
        mapping.get("lease_device_credential_generation") or 0
    )
    expires = _as_naive_timestamp(mapping.get("lease_expires_at"))
    if (
        not owner_device
        or owner_client != client
        or owner_generation != generation
        or not hmac.compare_digest(
            str(mapping.get("lease_token_hash") or ""), _lease_token_hash(token)
        )
        or expires is None
        or (expires <= current and not allow_expired)
        or not bool(mapping.get("lease_device_enabled"))
        or mapping.get("lease_device_revoked_at") is not None
        or device_claim is None
        or str(device_claim.get("device_id") or "") != owner_device
        or int(device_claim.get("generation") or 0) != credential_generation
    ):
        raise HTTPException(423, "此 Kiosk lease 已失效；裝置已轉為 Standby。")
    if require_command_generation and int(
        mapping.get("command_lease_generation") or 0
    ) != generation:
        raise HTTPException(409, "控制指令屬於另一個 Kiosk lease generation。")
    return {
        "device_id": owner_device,
        "client_id": client,
        "lease_generation": generation,
        "lease_expires_at": expires,
    }


def _device_for_claim(conn, request: Request, response: Response, now: dt.datetime) -> dict:
    claim = _verify_device_cookie(
        str(request.cookies.get(DEVICE_COOKIE_NAME) or "")
    )
    row = None
    if claim is not None:
        row = conn.execute(
            text(
                f"""SELECT device_id,label,enabled,credential_generation,revoked_at
                    FROM {TABLE_KIOSK_DEVICES}
                    WHERE device_id=:device FOR UPDATE"""
            ),
            {"device": str(claim.get("device_id") or "")},
        ).fetchone()
        if (
            row is None
            or not bool(row._mapping.get("enabled"))
            or row._mapping.get("revoked_at") is not None
            or int(row._mapping.get("credential_generation") or 0)
            != int(claim.get("generation") or 0)
        ):
            raise HTTPException(403, "此 Kiosk 裝置識別已被停用。")
    if row is None:
        device_id = uuid.uuid4().hex
        label = f"Kiosk {device_id[-6:].upper()}"
        row = conn.execute(
            text(
                f"""INSERT INTO {TABLE_KIOSK_DEVICES}
                    (device_id,label,enabled,credential_generation,created_at,last_seen_at)
                    VALUES(:device,:label,TRUE,1,:now,:now)
                    RETURNING device_id,label,enabled,credential_generation,revoked_at"""
            ),
            {"device": device_id, "label": label, "now": now},
        ).fetchone()
    values = dict(row._mapping)
    conn.execute(
        text(
            f"""UPDATE {TABLE_KIOSK_DEVICES} SET last_seen_at=:now
                WHERE device_id=:device"""
        ),
        {"device": values["device_id"], "now": now},
    )
    response.set_cookie(
        DEVICE_COOKIE_NAME,
        _sign_device_cookie(
            str(values["device_id"]), int(values["credential_generation"])
        ),
        max_age=COMMITTEE_SESSION_MAX_AGE_SECONDS,
        path="/",
        samesite="lax",
        httponly=True,
        secure=True,
    )
    return values


def validate_projector_lease(
    request: Request,
    *,
    operation_id: str,
    match_id: str = "",
    required_statuses: set[str] | None = None,
) -> dict:
    """Validate an active projector owner before R2 or provider side effects."""
    session_id = str(operation_id or "").strip()
    if not session_id:
        raise HTTPException(400, "缺少 Projector AI 任務識別碼。")
    now = _now()
    with _db().transaction() as conn:
        row = conn.execute(
            text(
                f"""SELECT c.*,s.session_id,s.match_id,s.status AS current_session_status,
                           s.kiosk_device_id,s.kiosk_lease_generation,
                           d.enabled AS lease_device_enabled,
                           d.revoked_at AS lease_device_revoked_at,
                           d.credential_generation AS lease_device_credential_generation
                    FROM {TABLE_CONTROLS} AS c
                    JOIN {TABLE_SESSIONS} AS s
                      ON s.session_id=c.current_session_id
                    LEFT JOIN {TABLE_KIOSK_DEVICES} AS d
                      ON d.device_id=c.lease_device_id
                    WHERE s.session_id=:session
                    FOR UPDATE OF c"""
            ),
            {"session": session_id},
        ).fetchone()
        if row is None:
            raise HTTPException(409, "Projector AI 場次已不是目前控制場次。")
        values = row._mapping
        lease = _assert_lease_mapping(values, request, now=now)
        if (
            str(values.get("kiosk_device_id") or "") != lease["device_id"]
            or int(values.get("kiosk_lease_generation") or 0)
            != lease["lease_generation"]
        ):
            raise HTTPException(409, "錄音場次屬於另一個 Kiosk lease。")
        if match_id and str(values.get("match_id") or "") != str(match_id):
            raise HTTPException(409, "錄音場次與正式場次不一致。")
        accepted_statuses = required_statuses or {"processing"}
        if str(values.get("current_session_status") or "") not in accepted_statuses:
            raise HTTPException(409, "Projector AI 場次並非等待上載及分析。")
        return lease


def validate_kiosk_display_lease(request: Request, *, display: str) -> dict:
    """Validate the current owner for a display-level hardware side effect."""
    key = _display(display)
    now = _now()
    with _db().transaction() as conn:
        _ensure_control(conn, key, now)
        row = _locked_lease_control(conn, key)
        if row is None:
            raise HTTPException(423, "此 Kiosk 並非目前 Active 裝置。")
        return _assert_lease_mapping(row._mapping, request, now=now)


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


def load_official_ai_judge_evidence(
    match_id: str,
    *,
    session_id: str = "",
    db=None,
) -> dict:
    """Load one unexpired encrypted full-match transcript for staff scoring."""
    executor = db or _db()
    now = _now()
    _prune_expired(executor, now)
    requested_session = str(session_id or "").strip()
    session_filter = "AND session_id=:session" if requested_session else ""
    rows = executor.query(
        f"""SELECT session_id,match_id,status,result_ciphertext,result_expires_at
            FROM {TABLE_SESSIONS}
            WHERE match_id=:match_id {session_filter}
              AND status IN ('ready','published')
              AND result_ciphertext IS NOT NULL
              AND result_expires_at>:now
            ORDER BY created_at DESC LIMIT 1""",
        {
            "match_id": str(match_id or ""),
            "session": requested_session,
            "now": now,
        },
    )
    if rows.empty:
        raise ValueError("未有可用的完整比賽逐字稿，或兩小時私人保存期限已過。")
    row = rows.iloc[0]
    private = _open_json(row.get("result_ciphertext"))
    transcript = str(private.get("transcript") or "").strip()
    if not transcript or not bool(private.get("recording_deleted")):
        raise ValueError("完整比賽逐字稿未完成私隱處理，暫不可用作正式 AI 分紙。")
    return {
        "session_id": str(row.get("session_id") or ""),
        "match_id": str(row.get("match_id") or ""),
        "transcript": transcript,
        "result_expires_at": str(row.get("result_expires_at") or ""),
        "source_model_label": str(private.get("model_label") or ""),
        "recording_deleted": True,
    }


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
    db.execute(
        f"""WITH expired_controls AS (
            UPDATE {TABLE_CONTROLS} AS control
            SET command='cancel_start',
                command_revision=control.command_revision+1,
                command_lease_generation=control.lease_generation,
                kiosk_status='cancelled',
                status_detail='開始錄音指令已逾時取消',
                command_payload=jsonb_build_object(
                    'reason','start_command_expired','expired_at',:now
                ),
                updated_at=:now
            FROM {TABLE_SESSIONS} AS session
            WHERE control.current_session_id=session.session_id
              AND session.status='start_requested'
              AND session.updated_at<=:cutoff
              AND control.command='start'
              AND control.ack_revision<control.command_revision
            RETURNING control.current_session_id AS session_id
        )
        UPDATE {TABLE_SESSIONS} AS session
        SET status='cancelled',
            status_detail='Kiosk 未在限時內確認開始錄音；指令已自動取消',
            updated_at=:now
        FROM expired_controls
        WHERE session.session_id=expired_controls.session_id
          AND session.status='start_requested'""",
        {
            "now": current,
            "cutoff": current - dt.timedelta(
                seconds=PROJECTOR_START_COMMAND_TTL_SECONDS,
            ),
        },
    )
    db.execute(
        f"""UPDATE {TABLE_SESSIONS} AS session
            SET status='cancelled',
                status_detail='已清理不再屬於目前控制指令的逾時開始要求',
                updated_at=:now
            WHERE session.status='start_requested'
              AND session.updated_at<=:cutoff
              AND NOT EXISTS (
                  SELECT 1 FROM {TABLE_CONTROLS} AS control
                  WHERE control.current_session_id=session.session_id
                    AND control.command='start'
                    AND control.ack_revision<control.command_revision
              )""",
        {
            "now": current,
            "cutoff": current - dt.timedelta(
                seconds=PROJECTOR_START_COMMAND_TTL_SECONDS,
            ),
        },
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
    allow_expired_lease: bool = False,
):
    current = now or _now()
    _ensure_control(conn, display, current)
    row = conn.execute(
        text(
            f"""UPDATE {TABLE_CONTROLS}
                SET current_session_id=COALESCE(:session_id,current_session_id),
                    command=:command,command_revision=command_revision+1,
                    command_lease_generation=lease_generation,
                    status_detail=:detail,command_payload=CAST(:payload AS JSONB),
                    updated_at=:now
                WHERE display_key=:display
                  AND lease_device_id IS NOT NULL
                  AND lease_token_hash IS NOT NULL
                  AND (lease_expires_at>:now OR :allow_expired_lease)
                RETURNING command_revision"""
        ),
        {
            "display": display,
            "session_id": session_id,
            "command": command,
            "detail": str(detail or "")[:1000],
            "payload": _json_param(payload or {}),
            "now": current,
            "allow_expired_lease": bool(allow_expired_lease),
        },
    ).fetchone()
    if row is None:
        raise HTTPException(409, "此投影畫面未有可用的 Active Kiosk。")
    return int(row[0])


def _official_match(db, match_id: str) -> dict | None:
    from api.kiosk_api import _official_match as load_match

    return load_match(db, str(match_id or ""))


def _match_timing_on_connection(conn, match_id: str) -> dict | None:
    """Load only marker timing fields without leaving the caller transaction."""
    from debate_timing import DEBATE_FORMATS

    row = conn.execute(
        text(
            f"""SELECT match_id,debate_format,free_debate_minutes
                FROM {TABLE_MATCHES} WHERE match_id=:match_id"""
        ),
        {"match_id": str(match_id or "")},
    ).fetchone()
    if row is None:
        return None
    values = row._mapping
    debate_format = str(values.get("debate_format") or "").strip()
    if debate_format not in DEBATE_FORMATS:
        debate_format = DEBATE_FORMATS[0]
    free_minutes = None
    try:
        raw_free = values.get("free_debate_minutes")
        if raw_free is not None and str(raw_free).strip():
            free_minutes = max(2.0, min(10.0, float(raw_free)))
    except (TypeError, ValueError, OverflowError):
        free_minutes = None
    return {
        "match_id": str(values.get("match_id") or "").strip(),
        "debate_format": debate_format,
        "free_debate_minutes": free_minutes,
    }


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
        f"""SELECT c.display_key,c.current_session_id,c.command,c.command_revision,
                   ack_revision,kiosk_status,status_detail,command_payload,
                   hardware_status,capabilities,kiosk_last_seen_at,c.updated_at,
                   lease_device_id,lease_client_id,lease_generation,
                   lease_expires_at,lease_last_seen_at,command_lease_generation,
                   d.label AS lease_device_label
            FROM {TABLE_CONTROLS} AS c
            LEFT JOIN {TABLE_KIOSK_DEVICES} AS d ON d.device_id=c.lease_device_id
            WHERE c.display_key=:display""",
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
            "lease": {
                "claimed": False,
                "active": False,
                "generation": 0,
            },
        }
        return {"control": control, "session": None}
    row = rows.iloc[0]
    last_seen = row.get("kiosk_last_seen_at")
    current = _now()
    lease_expires = _as_naive_timestamp(row.get("lease_expires_at"))
    lease_active = bool(row.get("lease_device_id")) and bool(
        lease_expires and lease_expires > current
    )
    online = lease_active and _kiosk_seen_is_fresh(last_seen, current)
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
        "lease": {
            "claimed": bool(row.get("lease_device_id")),
            "active": lease_active,
            "device_label": str(row.get("lease_device_label") or ""),
            "generation": int(row.get("lease_generation") or 0),
            "expires_at": str(row.get("lease_expires_at") or ""),
            "last_seen_at": str(row.get("lease_last_seen_at") or ""),
            "command_generation": int(row.get("command_lease_generation") or 0),
        },
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
    db = _db()
    now = _now()
    expires = now + RESULT_TTL
    with db.transaction() as conn:
        control_row = conn.execute(
            text(
                f"""SELECT display_key,command_revision,current_session_id
                    FROM {TABLE_CONTROLS}
                    WHERE current_session_id=:session
                    FOR UPDATE"""
            ),
            {"session": sid},
        ).fetchone()
        if control_row is None:
            return None
        control_values = control_row._mapping
        display = str(control_values.get("display_key") or "main")
        session_row = conn.execute(
            text(
                f"""SELECT match_id,status,result_ciphertext,result_expires_at
                    FROM {TABLE_SESSIONS}
                    WHERE session_id=:session AND display_key=:display
                    FOR UPDATE"""
            ),
            {"session": sid, "display": display},
        ).fetchone()
        if session_row is None:
            return None
        session_values = session_row._mapping
        if str(session_values.get("match_id") or "") != str(match_id or ""):
            raise ValueError("projector match mismatch")
        status = str(session_values.get("status") or "")
        if status in RESULT_STATES and session_values.get("result_ciphertext"):
            return {
                "display": display,
                "revision": int(control_values.get("command_revision") or 0),
                "expires_at": str(session_values.get("result_expires_at") or ""),
                "idempotent": True,
            }
        if status not in {"start_requested", "recording", "stop_requested", "processing"}:
            raise ValueError("projector session cannot receive a completed review")
        sealed = _seal_json(payload)
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
        revision = int(control_values.get("command_revision") or 0)
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET ack_revision=GREATEST(ack_revision,:revision),
                        kiosk_status='ready',status_detail='AI評判易完成；等待私人預覽',
                        kiosk_last_seen_at=:now,updated_at=:now
                    WHERE display_key=:display"""
            ),
            {
                "display": display,
                "revision": revision,
                "now": now,
            },
        )
    return {
        "display": display,
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
    payload["limits"] = {
        "max_seconds": KIOSK_MATCH_REVIEW_MAX_SECONDS,
        "max_bytes": KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
        "tts_max_chars": TTS_TEXT_MAX_CHARS,
        "result_ttl_seconds": int(RESULT_TTL.total_seconds()),
        "start_command_ttl_seconds": PROJECTOR_START_COMMAND_TTL_SECONDS,
        "kiosk_lease_ttl_seconds": PROJECTOR_KIOSK_LEASE_TTL_SECONDS,
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
    if not body.screen_confirmed or not body.audio_confirmed:
        raise HTTPException(400, "請確認現場看見測試畫面並聽到測試聲。")
    now = _now()
    with db.transaction() as conn:
        _ensure_control(conn, display, now)
        control = conn.execute(
            text(
                f"""SELECT command,command_revision,hardware_status
                    FROM {TABLE_CONTROLS}
                    WHERE display_key=:display FOR UPDATE"""
            ),
            {"display": display},
        ).fetchone()
        if (
            control is None
            or str(control._mapping.get("command") or "") != "hardware_test"
            or int(control._mapping.get("command_revision") or 0) != body.revision
        ):
            raise HTTPException(409, "硬件測試結果已更新，請重新載入後確認最新測試。")
        hardware = _json_object(control._mapping.get("hardware_status"))
        if not hardware.get("passed"):
            raise HTTPException(409, "Kiosk 尚未通過必要硬件測試。")
        hardware.update(operator_confirmed=True, confirmed_at=now.isoformat())
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET hardware_status=CAST(:hardware AS JSONB),
                        status_detail=:detail,updated_at=:now
                    WHERE display_key=:display AND command_revision=:revision"""
            ),
            {
                "display": display,
                "revision": body.revision,
                "hardware": _json_param(hardware),
                "detail": "賽會人員已確認投影、喇叭及 Kiosk 測試結果",
                "now": now,
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
    _prune_expired(db, now)
    match = _official_match(db, body.match_id)
    if not match:
        raise HTTPException(404, "找不到所選正式場次。")
    if not match.get("topic") or not match.get("pro_team") or not match.get("con_team"):
        raise HTTPException(400, "正式場次的辯題及正反方資料未完整。")
    session_id = uuid.uuid4().hex
    with db.transaction() as conn:
        current = conn.execute(
            text(
                """SELECT match_id FROM projector_state
                    WHERE display_key=:display FOR UPDATE"""
            ),
            {"display": display},
        ).fetchone()
        if (
            current is None
            or str(current._mapping.get("match_id") or "") != match["match_id"]
        ):
            raise HTTPException(409, "投影控制所選場次與 AI評判易場次不一致。")
        _ensure_control(conn, display, now)
        control = conn.execute(
            text(
                f"""SELECT kiosk_last_seen_at,hardware_status,capabilities,
                           command_revision,ack_revision,lease_device_id,
                           lease_generation,lease_expires_at
                    FROM {TABLE_CONTROLS}
                    WHERE display_key=:display FOR UPDATE"""
            ),
            {"display": display},
        ).fetchone()
        if control is None or not _kiosk_seen_is_fresh(
            control._mapping.get("kiosk_last_seen_at"), now
        ) or not (
            _as_naive_timestamp(control._mapping.get("lease_expires_at"))
            and _as_naive_timestamp(control._mapping.get("lease_expires_at")) > now
        ):
            raise HTTPException(409, "比賽日 Kiosk 未連線，不能開始錄音。")
        if int(control._mapping.get("ack_revision") or 0) < int(
            control._mapping.get("command_revision") or 0
        ):
            raise HTTPException(409, "Kiosk 尚未確認上一個控制指令，請稍候再開始。")
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
                    (session_id,display_key,match_id,status,status_detail,
                     kiosk_device_id,kiosk_lease_generation,created_at,updated_at)
                    VALUES(:session,:display,:match,'start_requested',:detail,
                           :device,:lease_generation,:now,:now)"""
            ),
            {
                "session": session_id,
                "display": display,
                "match": match["match_id"],
                "detail": "等待 Kiosk 開始全場錄音",
                "device": str(control._mapping.get("lease_device_id") or ""),
                "lease_generation": int(
                    control._mapping.get("lease_generation") or 0
                ),
                "now": now,
            },
        )
        revision = _issue_command(
            conn,
            display,
            "start",
            session_id=session_id,
            detail="等待 Kiosk 開始全場錄音",
            payload={
                "session_id": session_id,
                "match_id": match["match_id"],
                "expires_at": (
                    now + dt.timedelta(seconds=PROJECTOR_START_COMMAND_TTL_SECONDS)
                ).isoformat(),
            },
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


@router.post("/api/projector/ai/cancel-start")
def cancel_start_session(body: SessionBody, request: Request):
    require_competition_staff(request)
    display = _display(body.display)
    db = _db()
    now = _now()
    with db.transaction() as conn:
        _lock_current_session(conn, display, body.session_id)
        cancelled = conn.execute(
            text(
                f"""UPDATE {TABLE_SESSIONS}
                    SET status='cancelled',status_detail='賽會人員已取消等待開始錄音',
                        updated_at=:now
                    WHERE session_id=:session AND display_key=:display
                      AND status='start_requested'
                    RETURNING session_id"""
            ),
            {"session": body.session_id, "display": display, "now": now},
        ).fetchone()
        if cancelled is None:
            raise HTTPException(409, "只有等待 Kiosk 開始的場次可以取消。")
        revision = _issue_command(
            conn,
            display,
            "cancel_start",
            session_id=body.session_id,
            detail="已取消等待開始錄音",
            payload={"session_id": body.session_id, "reason": "operator_cancelled"},
            now=now,
            allow_expired_lease=True,
        )
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET kiosk_status='cancelled',updated_at=:now
                    WHERE display_key=:display"""
            ),
            {"display": display, "now": now},
        )
    return {"ok": True, "revision": revision, "status": "cancelled"}


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
            allow_expired_lease=True,
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


@router.post("/api/kiosk/projector-ai/lease/claim")
def claim_kiosk_lease(
    body: LeaseClaimBody,
    request: Request,
    response: Response,
):
    _require_kiosk(request)
    display = _display(body.display)
    client = _client_id(body.client_id)
    capabilities = _safe_capabilities(body.capabilities)
    now = _now()
    expires = now + dt.timedelta(seconds=PROJECTOR_KIOSK_LEASE_TTL_SECONDS)
    db = _db()
    with db.transaction() as conn:
        _ensure_control(conn, display, now)
        control = _locked_lease_control(conn, display)
        if control is None:  # pragma: no cover - protected by _ensure_control
            raise HTTPException(503, "未能建立 Projector Kiosk 控制狀態。")
        device = _device_for_claim(conn, request, response, now)
        values = control._mapping
        owner_device = str(values.get("lease_device_id") or "")
        owner_client = str(values.get("lease_client_id") or "")
        same_owner = (
            owner_device == str(device["device_id"])
            and owner_client == client
        )
        session_pinned = (
            str(values.get("current_session_status") or "")
            in ACTIVE_RECORDING_STATES
        )
        credentialless_reload = (
            same_owner
            and not str(request.headers.get(LEASE_TOKEN_HEADER) or "").strip()
            and request.headers.get(LEASE_GENERATION_HEADER) is None
            and body.lease_generation == 0
        )
        can_claim = _lease_can_claim(
            owner_device_id=owner_device,
            owner_client_id=owner_client,
            requesting_device_id=str(device["device_id"]),
            requesting_client_id=client,
            lease_expires_at=values.get("lease_expires_at"),
            session_status=str(values.get("current_session_status") or ""),
            now=now,
        )
        if same_owner and not credentialless_reload:
            _assert_lease_mapping(
                values,
                request,
                client_id=client,
                lease_generation=body.lease_generation,
                now=now,
                allow_expired=True,
            )
            generation = int(values.get("lease_generation") or 0)
            token = str(request.headers.get(LEASE_TOKEN_HEADER) or "").strip()
            conn.execute(
                text(
                    f"""UPDATE {TABLE_CONTROLS}
                        SET lease_expires_at=:expires,lease_last_seen_at=:now,
                            kiosk_last_seen_at=:now,capabilities=CAST(:capabilities AS JSONB),
                            kiosk_status=CASE
                                WHEN COALESCE(kiosk_status,'') IN ('','offline') THEN 'online'
                                ELSE kiosk_status
                            END,updated_at=:now
                        WHERE display_key=:display AND lease_generation=:generation"""
                ),
                {
                    "display": display,
                    "generation": generation,
                    "expires": expires,
                    "capabilities": _json_param(capabilities),
                    "now": now,
                },
            )
        elif can_claim and not (same_owner and session_pinned):
            token = secrets.token_urlsafe(32)
            generation = int(values.get("lease_generation") or 0) + 1
            conn.execute(
                text(
                    f"""UPDATE {TABLE_CONTROLS}
                        SET lease_device_id=:device,lease_client_id=:client,
                            lease_token_hash=:token_hash,lease_generation=:generation,
                            lease_expires_at=:expires,lease_last_seen_at=:now,
                            command='',command_revision=command_revision+1,
                            ack_revision=command_revision+1,
                            command_lease_generation=:generation,
                            command_payload='{{}}'::jsonb,hardware_status='{{}}'::jsonb,
                            capabilities=CAST(:capabilities AS JSONB),
                            kiosk_last_seen_at=:now,kiosk_status='online',
                            status_detail='Active Kiosk 已連線；請重新完成硬件測試',
                            updated_at=:now
                        WHERE display_key=:display"""
                ),
                {
                    "display": display,
                    "device": str(device["device_id"]),
                    "client": client,
                    "token_hash": _lease_token_hash(token),
                    "generation": generation,
                    "expires": expires,
                    "capabilities": _json_param(capabilities),
                    "now": now,
                },
            )
        else:
            response.headers["Cache-Control"] = "no-store"
            return {
                "ok": True,
                "role": "standby",
                "display": display,
                "device": {
                    "id": str(device["device_id"]),
                    "label": str(device["label"]),
                },
                "owner": {
                    "label": str(values.get("lease_device_label") or "Active Kiosk"),
                    "generation": int(values.get("lease_generation") or 0),
                    "expires_at": str(values.get("lease_expires_at") or ""),
                },
                "reason": "active_session_pinned" if session_pinned else "lease_held",
                "lease_ttl_seconds": PROJECTOR_KIOSK_LEASE_TTL_SECONDS,
            }
    response.headers["Cache-Control"] = "no-store"
    return {
        "ok": True,
        "role": "active",
        "display": display,
        "lease_token": token,
        "lease_generation": generation,
        "expires_at": expires.isoformat(),
        "lease_ttl_seconds": PROJECTOR_KIOSK_LEASE_TTL_SECONDS,
        "device": {
            "id": str(device["device_id"]),
            "label": str(device["label"]),
        },
    }


@router.post("/api/projector/ai/lease/takeover")
def takeover_kiosk_lease(body: LeaseTakeoverBody, request: Request):
    require_competition_staff(request)
    display = _display(body.display)
    now = _now()
    with _db().transaction() as conn:
        _ensure_control(conn, display, now)
        control = _locked_lease_control(conn, display)
        if control is None:
            raise HTTPException(409, "此投影畫面未有 Kiosk lease。")
        values = control._mapping
        generation = int(values.get("lease_generation") or 0)
        if generation != body.expected_generation:
            raise HTTPException(409, "Kiosk lease 已更新，請重新載入控制頁。")
        if not values.get("lease_device_id"):
            return {"ok": True, "released": False, "generation": generation}
        session_id = str(values.get("current_session_id") or "")
        session_status = str(values.get("current_session_status") or "")
        if (
            session_status in ACTIVE_RECORDING_STATES
            and not body.confirm_interrupt_active_session
        ):
            raise HTTPException(409, "接管會中斷或取消目前場次；必須明確確認後再執行。")
        if session_id and session_status == "start_requested":
            conn.execute(
                text(
                    f"""UPDATE {TABLE_SESSIONS}
                        SET status='cancelled',status_detail='賽會人員接管 Kiosk；開始要求已取消',
                            updated_at=:now WHERE session_id=:session"""
                ),
                {"session": session_id, "now": now},
            )
        elif session_id and session_status in {"recording", "stop_requested"}:
            conn.execute(
                text(
                    f"""UPDATE {TABLE_SESSIONS}
                        SET status='interrupted',status_detail='賽會人員接管 Kiosk；本場錄音已中斷',
                            updated_at=:now WHERE session_id=:session"""
                ),
                {"session": session_id, "now": now},
            )
        elif session_id and session_status == "processing":
            conn.execute(
                text(
                    f"""UPDATE {TABLE_SESSIONS}
                        SET status='cancelled',
                            status_detail='賽會人員強制接管 Kiosk；AI 分析已取消，其後完成的 AI 結果不會寫入',
                            updated_at=:now WHERE session_id=:session
                              AND status='processing'"""
                ),
                {"session": session_id, "now": now},
            )
        new_generation = generation + 1
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET lease_device_id=NULL,lease_client_id=NULL,lease_token_hash=NULL,
                        lease_generation=:generation,lease_expires_at=NULL,
                        lease_last_seen_at=NULL,command='',
                        command_revision=command_revision+1,
                        ack_revision=command_revision+1,
                        command_lease_generation=:generation,
                        command_payload='{{}}'::jsonb,hardware_status='{{}}'::jsonb,
                        capabilities='{{}}'::jsonb,kiosk_last_seen_at=NULL,
                        kiosk_status='awaiting_kiosk',
                        status_detail='舊 Kiosk lease 已撤銷；等待一部 Standby Kiosk 成為 Active',
                        updated_at=:now WHERE display_key=:display"""
            ),
            {"display": display, "generation": new_generation, "now": now},
        )
    return {
        "ok": True,
        "released": True,
        "generation": new_generation,
        "interrupted_session": session_id if session_status in ACTIVE_RECORDING_STATES else "",
    }


@router.get("/api/kiosk/projector-ai/command")
def kiosk_command(request: Request, display: str = "main"):
    _require_kiosk(request)
    key = _display(display)
    db = _db()
    now = _now()
    expires = now + dt.timedelta(seconds=PROJECTOR_KIOSK_LEASE_TTL_SECONDS)
    with db.transaction() as conn:
        _ensure_control(conn, key, now)
        control = _locked_lease_control(conn, key)
        if control is None:
            raise HTTPException(423, "此 Kiosk 並非目前 Active 裝置。")
        lease = _assert_lease_mapping(
            control._mapping,
            request,
            now=now,
            require_command_generation=True,
            allow_expired=True,
        )
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET kiosk_last_seen_at=:now,
                        lease_last_seen_at=:now,lease_expires_at=:expires,updated_at=:now
                    WHERE display_key=:display AND lease_generation=:generation"""
            ),
            {
                "display": key,
                "generation": lease["lease_generation"],
                "expires": expires,
                "now": now,
            },
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
            "lease_generation": lease["lease_generation"],
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
    capabilities = _safe_capabilities(body.capabilities)
    now = _now()
    expires = now + dt.timedelta(seconds=PROJECTOR_KIOSK_LEASE_TTL_SECONDS)
    with _db().transaction() as conn:
        _ensure_control(conn, display, now)
        control = _locked_lease_control(conn, display)
        if control is None:
            raise HTTPException(423, "此 Kiosk 並非目前 Active 裝置。")
        lease = _assert_lease_mapping(
            control._mapping,
            request,
            client_id=body.client_id,
            lease_generation=body.lease_generation,
            now=now,
            allow_expired=True,
        )
        conn.execute(
            text(
                f"""UPDATE {TABLE_CONTROLS}
                    SET kiosk_last_seen_at=:now,
                        kiosk_status=CASE
                            WHEN COALESCE(kiosk_status,'') IN ('','offline') THEN 'online'
                            ELSE kiosk_status
                        END,
                        capabilities=CAST(:capabilities AS JSONB),
                        lease_last_seen_at=:now,lease_expires_at=:expires,
                        updated_at=:now
                    WHERE display_key=:display AND lease_generation=:generation"""
            ),
            {
                "display": display,
                "capabilities": _json_param(capabilities),
                "generation": lease["lease_generation"],
                "expires": expires,
                "now": now,
            },
        )
    return {
        "ok": True,
        "server_time": now.isoformat(),
        "expires_at": expires.isoformat(),
        "lease_generation": lease["lease_generation"],
    }


@router.post("/api/kiosk/projector-ai/ack")
def kiosk_ack(body: KioskAckBody, request: Request):
    _require_kiosk(request)
    display = _display(body.display)
    db = _db()
    now = _now()
    with db.transaction() as conn:
        _ensure_control(conn, display, now)
        control = _locked_lease_control(conn, display)
        if control is None:
            raise HTTPException(423, "此 Kiosk 並非目前 Active 裝置。")
        lease = _assert_lease_mapping(
            control._mapping,
            request,
            client_id=body.client_id,
            lease_generation=body.lease_generation,
            now=now,
            require_command_generation=True,
        )
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
                kiosk_device_id=lease["device_id"],
                kiosk_lease_generation=lease["lease_generation"],
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
            and session_status in {"ready", "published", "cleared", "expired", "error", "cancelled"}
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
                        match = _match_timing_on_connection(
                            conn, str(projector._mapping.get("match_id") or "")
                        )
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
        query += " WHERE display_key=:display AND lease_generation=:lease_generation"
        params["lease_generation"] = lease["lease_generation"]
        conn.execute(text(query), params)
    return {"ok": True, "ack_revision": body.revision}


@router.get("/api/kiosk/projector-ai/audio/{session_id}")
async def kiosk_audio(session_id: str, request: Request, revision: int = 0):
    _require_kiosk(request)
    validate_projector_lease(
        request,
        operation_id=str(session_id)[:80],
        required_statuses={"published"},
    )
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
