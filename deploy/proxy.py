import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.gzip import GZipMiddleware
from starlette.websockets import WebSocketDisconnect

from account_access import (
    account_can_access,
    access_denial_message,
    normalize_account_id,
)
from schema import (
    TABLE_ACCOUNTS,
    TABLE_AI_DATASET_SNAPSHOTS,
    TABLE_AI_DATASET_SNAPSHOT_ITEMS,
    TABLE_AI_FUND_USAGE_LOGS,
    TABLE_AI_MODEL_VERSIONS,
    TABLE_BANDWIDTH_USAGE_LOGS,
    TABLE_PRACTICE_DAILY_USAGE,
    TABLE_PUSH_SUBSCRIPTIONS,
    TABLE_MATCHES,
    TABLE_VIDEO_PROGRESS,
    TABLE_VIDEO_VIEWS,
)
from core.config_store import (
    get_configs_from_connection,
    set_configs_on_connection,
)
from core.ai_provider import post_json_bounded, _usage as _provider_usage
from core.db_runtime import RuntimeDb, dispose_db_engine, get_db_engine
from core.runtime_secrets import get_secret
from debate_timing import (  # pure helpers, no side effects
    get_full_mock_sequence,
    split_mock_into_sessions,
    full_mock_total_seconds,
    get_debate_timer_config,
    FREE_DEBATE_FORMATS,
    DEBATE_FORMATS,
)
from ai_model_config import (
    AZURE_TTS_PROVIDER,
    CUSTOM_TTS_PROVIDER,
    DEFAULT_TTS_PROVIDER,
    GEMINI_LIVE_MODEL,
    GEMINI_LIVE_MODEL_LABEL,
    GEMINI_LIVE_PROVIDER,
    TTS_PROVIDER_OPTIONS,
    TTS_PROVIDER_SECRET,
    get_model_by_slug,
    get_tts_provider_config,
    model_slugs_for_feature,
)
from prompts import build_free_debate_live_prompt, build_full_mock_live_prompt, LIVE_RUNTIME_PROMPTS
from prompts import build_room_judgement_prompt
from api.vote_api import router as vote_router
from api.auth_api import router as committee_router
from api.open_db_api import router as open_db_router
from api.home_api import router as home_router
from api.bug_report_api import router as bug_report_router
from api.registration_api import router as registration_router
from api.registration_admin_api import router as registration_admin_router
from api.video_replay_api import router as video_replay_router
from api.video_admin_api import router as video_admin_router
from api.match_photos_api import router as match_photos_router
from api.team_roster_api import router as team_roster_router
from api.match_info_api import router as match_info_router
from api.schedule_api import router as schedule_router
from api.management_api import router as management_router
from api.judging_api import router as judging_router
from api.review_api import router as review_router
from api.funds_api import router as funds_router
from api.chairperson_api import router as chairperson_router
from api.ai_coach_api import router as ai_coach_router
from api.ai_training_api import router as ai_training_router
from api.admin_console_api import router as admin_console_router
from api.kiosk_api import router as kiosk_router, require_kiosk_user
from api.projector_ai_api import router as projector_ai_router
from api.access import require_competition_staff, require_page_user
from version import APP_VERSION
from system_limits import (
    BANDWIDTH_CHECKPOINT_SECONDS, BANDWIDTH_ESSENTIAL_ONLY_BYTES,
    BANDWIDTH_LOG_RETENTION_DAYS, BANDWIDTH_STOP_LIVE_BYTES,
    BANDWIDTH_WARN_BYTES, CACHE_HTML_MAX_AGE_SECONDS, CACHE_HTML_STALE_SECONDS,
    CACHE_MANIFEST_MAX_AGE_SECONDS, CACHE_SHARED_MAX_AGE_SECONDS,
    CACHE_SHARED_STALE_SECONDS, CACHE_STATIC_MAX_AGE_SECONDS,
    COMMITTEE_SESSION_CLOCK_SKEW_SECONDS,
    COMMITTEE_SESSION_MAX_AGE_SECONDS,
    COMMITTEE_SESSION_TOKEN_MAX_CHARS,
    JUDGING_SESSION_CLOCK_SKEW_SECONDS,
    JUDGING_SESSION_TOKEN_MAX_CHARS,
    JUDGING_SESSION_TTL_SECONDS,
    GEMINI_WS_MAX_QUEUE, GEMINI_WS_MAX_SIZE,
    GZIP_COMPRESS_LEVEL, GZIP_MINIMUM_SIZE,
    AI_PROVIDER_PROMPT_MAX_CHARS, AI_PROVIDER_RESPONSE_MAX_BYTES,
    LIVE_CONTEXT_COMPRESSION_TARGET_TOKENS,
    LIVE_CONTEXT_COMPRESSION_TRIGGER_TOKENS, LIVE_FREE_MAX_MINUTES,
    LIVE_FREE_SESSION_MAX_SECONDS, LIVE_PRACTICE_CLAIM_MAX_CHARS,
    LIVE_PRACTICE_CLAIM_TTL_SECONDS, LIVE_SYSTEM_PROMPT_MAX_CHARS,
    LIVE_MOCK_OVERALL_GRACE_SECONDS,
    LIVE_TOKEN_EXPIRY_GRACE_SECONDS, LIVE_TOKEN_LEDGER_DB_TIMEOUT_SECONDS,
    LIVE_TOKEN_MINT_TIMEOUT_SECONDS,
    LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS,
    LIVE_TOKEN_RESPONSE_CACHE_MAX_ENTRIES,
    LIVE_TOKEN_RESPONSE_CACHE_SAFETY_SECONDS,
    LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS,
    MAINTENANCE_PRUNE_INTERVAL_SECONDS, MAX_HTTP_BODY_BYTES, MAX_ROOMS,
    MULTIPLAYER_FREE_MONTHLY_ROOMS,
    MULTIPLAYER_MOCK_MONTHLY_ROOMS, PRACTICE_LIVE_MAX_PER_HOUR,
    PRACTICE_LIVE_MIN_GAP_SECONDS, PRACTICE_LIVE_RATE_WINDOW_SECONDS,
    PROJECTOR_MATCH_LIMIT,
    PUSH_ACTIVE_DEVICES_PER_USER, PUSH_ENDPOINT_MAX_CHARS,
    PUSH_INACTIVE_RETENTION_DAYS, PUSH_KEY_MAX_CHARS,
    PUSH_SUBSCRIPTION_MAX_BYTES,
    REQUEST_BODY_BUFFER_CONCURRENCY, ROOM_AUDIO_FRAME_MAX_BYTES,
    ROOM_AUDIO_RATE_BURST_MESSAGES, ROOM_AUDIO_RATE_MESSAGES_PER_SECOND,
    ROOM_AUDIO_RATE_BURST_BYTES, ROOM_AUDIO_RATE_BYTES_PER_SECOND,
    ROOM_CONTROL_RATE_BURST_MESSAGES, ROOM_CONTROL_RATE_MESSAGES_PER_SECOND,
    ROOM_EMPTY_GRACE_SECONDS,
    ROOM_FINAL_JUDGEMENT_TIMEOUT_SECONDS,
    ROOM_GEMINI_RESUME_DELAY_SECONDS, ROOM_GEMINI_RESUME_MAX_ATTEMPTS,
    ROOM_GEMINI_SETUP_TIMEOUT_SECONDS,
    ROOM_JUDGEMENT_TIMEOUT_SECONDS,
    ROOM_MAX_AGE_SECONDS, ROOM_MAX_CAPACITY, ROOM_NATIVE_AUDIO_BUFFER_MAX_BYTES,
    ROOM_PENDING_TRANSCRIPT_MAX_CHARS, ROOM_TRANSCRIPT_ITEM_MAX_CHARS,
    ROOM_TEST_AUDIO_ACK_TTL_MS, ROOM_TEST_AUDIO_COOLDOWN_MS,
    ROOM_TEST_AUDIO_MAX_BYTES, ROOM_TEST_AUDIO_PENDING_MAX,
    ROOM_TEST_RECEIVED_COOLDOWN_MS,
    ROOM_TRANSCRIPT_MAX_ITEMS, ROOM_WS_SEND_TIMEOUT_SECONDS,
    ROOM_WS_TEXT_MAX_BYTES,
    SOLO_FREE_DAILY_LIMIT, SOLO_FREE_MONTHLY_LIMIT,
    SOLO_MOCK_MONTHLY_LIMIT, SOLO_MOCK_WEEKLY_LIMIT,
    TTS_CONCURRENCY, TTS_LEXICON_CACHE_TTL_SECONDS, TTS_LEXICON_LIMIT,
    TTS_MAX_RESPONSE_BYTES, TTS_PROVIDER_CONNECT_TIMEOUT_SECONDS,
    TTS_PROVIDER_TIMEOUT_SECONDS, TTS_TEXT_MAX_CHARS, VIDEO_PROGRESS_MAX_SECONDS,
    VIDEO_VIEW_DEDUPE_HOURS,
)


BASE_DIR = Path(__file__).resolve().parents[1]

CACHE_NO_CACHE = "no-cache"
CACHE_NO_STORE = "no-store"
CACHE_HTML = f"private, max-age={CACHE_HTML_MAX_AGE_SECONDS}, stale-while-revalidate={CACHE_HTML_STALE_SECONDS}"
CACHE_MANIFEST = f"public, max-age={CACHE_MANIFEST_MAX_AGE_SECONDS}"
CACHE_STATIC = f"public, max-age={CACHE_STATIC_MAX_AGE_SECONDS}, immutable"
CACHE_SHARED = f"public, max-age={CACHE_SHARED_MAX_AGE_SECONDS}, stale-while-revalidate={CACHE_SHARED_STALE_SECONDS}"
CACHE_BELL = "public, max-age=86400, must-revalidate"

@asynccontextmanager
async def _lifespan(_app):
    try:
        yield
    finally:
        dispose_db_engine()


app = FastAPI(lifespan=_lifespan)
ESSENTIAL_ONLY_BLOCKED_PATHS = {
    "/api/ai-coach/run",
    "/api/ai-training/llm",
    "/api/ai-training/recordings/quality-check",
    "/api/ai-training/coverage/ai",
    "/api/ai-training/regenerate-suggestions",
    "/api/ai-training/rag/reindex",
    "/api/kiosk/match-review/analyze",
    "/api/tts/azure",
    "/api/tts/synthesize",
    "/api/vote/ai-review",
    "/api/vote/analysis/ai",
}


class RequestBodyLimitMiddleware:
    """Enforce the body cap from actual ASGI chunks, including chunked uploads.

    ``Content-Length`` remains a cheap early rejection, but is never trusted as
    the sole RAM guard.  The request is replayed only after the complete body
    has stayed within the cap, so endpoint-level ``except Exception`` blocks
    cannot accidentally turn an oversized chunked request into a normal 400.
    """

    def __init__(self, inner, max_bytes: int):
        self.inner = inner
        self.max_bytes = max(1, int(max_bytes))
        self.buffer_slots = asyncio.Semaphore(REQUEST_BODY_BUFFER_CONCURRENCY)

    async def _reject(self, send):
        body = json.dumps({
            "detail": f"Request body exceeds the {self.max_bytes // (1024 * 1024)}MB server limit"
        }).encode("utf-8")
        await send({
            "type": "http.response.start", "status": 413,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii"))],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.inner(scope, receive, send)
            return
        method = str(scope.get("method") or "").upper()
        if method in {"GET", "HEAD"}:
            await self.inner(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        content_length = headers.get(b"content-length")
        try:
            declared = int(content_length or b"0")
        except ValueError:
            declared = self.max_bytes + 1
        if declared < 0 or declared > self.max_bytes:
            await self._reject(send)
            return
        # At most a few 5MB buffers may be assembled simultaneously.  This is
        # separate from Uvicorn's endpoint concurrency because the complete
        # request has to be verified before Pydantic/endpoint code can run.
        # Keep the slot until the downstream handler and response have fully
        # completed: replaying the body does not release the retained buffer.
        await self.buffer_slots.acquire()
        slot_held = True
        try:
            buffered = []
            received = 0
            complete = False
            if content_length is None or declared == 0:
                # ASGI servers may dechunk a request and omit transfer-encoding,
                # and a malicious client may declare Content-Length: 0 while
                # still sending bytes.  Both cases must acquire a slot before
                # the first chunk. Release it only after proving that the first
                # message is an empty terminal request.
                first = await receive()
                if (
                    first.get("type") == "http.request"
                    and not first.get("body")
                    and not first.get("more_body", False)
                ):
                    self.buffer_slots.release()
                    slot_held = False
                    replayed = False

                    async def replay_empty():
                        nonlocal replayed
                        if not replayed:
                            replayed = True
                            return first
                        return {
                            "type": "http.request", "body": b"", "more_body": False,
                        }

                    await self.inner(scope, replay_empty, send)
                    return
                buffered.append(first)
                if first.get("type") == "http.request":
                    received = len(first.get("body") or b"")
                    complete = not first.get("more_body", False)
                elif first.get("type") == "http.disconnect":
                    complete = True
            if received > self.max_bytes:
                await self._reject(send)
                return
            while not complete:
                message = await receive()
                buffered.append(message)
                if message.get("type") == "http.request":
                    received += len(message.get("body") or b"")
                    if received > self.max_bytes:
                        await self._reject(send)
                        return
                    if not message.get("more_body", False):
                        complete = True
                elif message.get("type") == "http.disconnect":
                    complete = True

            index = 0

            async def replay_receive():
                nonlocal index
                if index < len(buffered):
                    message = buffered[index]
                    index += 1
                    return message
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.inner(scope, replay_receive, send)
        finally:
            if slot_held:
                self.buffer_slots.release()


app.add_middleware(
    GZipMiddleware, minimum_size=GZIP_MINIMUM_SIZE,
    compresslevel=GZIP_COMPRESS_LEVEL,
)
app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_HTTP_BODY_BYTES)


@app.middleware("http")
async def enforce_essential_only_budget(request: Request, call_next):
    """At 4GB, block provider calls while retaining pages, JSON, R2 and admin."""
    if request.url.path in ESSENTIAL_ONLY_BLOCKED_PATHS:
        budget_error = _bandwidth_essential_gate_error()
        if budget_error:
            return JSONResponse({"detail": budget_error}, status_code=429)
    return await call_next(request)


@app.middleware("http")
async def add_shared_static_cache_headers(request: Request, call_next):
    """Make shared assets edge-cacheable without making unversioned files immutable."""
    response = await call_next(request)
    if request.url.path.startswith("/shared/") and response.status_code == 200:
        response.headers["Cache-Control"] = CACHE_SHARED
    return response


app.mount("/shared", StaticFiles(directory=BASE_DIR / "frontend" / "shared"), name="shared")
# Register all explicit HTML/API routes before the final 404 handlers.
app.include_router(vote_router)
app.include_router(committee_router)
app.include_router(open_db_router)
app.include_router(home_router)
app.include_router(bug_report_router)
app.include_router(registration_router)
app.include_router(registration_admin_router)
app.include_router(video_replay_router)
app.include_router(video_admin_router)
app.include_router(match_photos_router)
app.include_router(team_roster_router)
app.include_router(match_info_router)
app.include_router(schedule_router)
app.include_router(management_router)
app.include_router(judging_router)
app.include_router(review_router)
app.include_router(funds_router)
app.include_router(chairperson_router)
app.include_router(ai_coach_router)
app.include_router(ai_training_router)
app.include_router(admin_console_router)
app.include_router(kiosk_router)
app.include_router(projector_ai_router)
logger = logging.getLogger("skh_proxy")


def _cache_headers(cache_control):
    return {"Cache-Control": cache_control}


def _binary_cache_headers(cache_control):
    # Starlette's generic gzip middleware only excludes event streams.  Mark
    # already-compressed PNG/MP3 payloads as identity to avoid wasting CPU or
    # making those files larger while text/JSON still benefits from gzip.
    return {"Cache-Control": cache_control, "Content-Encoding": "identity"}


def _get_proxy_secret(key: str, default: str = "") -> str:
    return get_secret(key, default)


def _get_vapid():
    """Return server-side VAPID configuration, or ``None`` when incomplete."""
    public_key = _get_proxy_secret("VAPID_PUBLIC_KEY")
    private_key = _get_proxy_secret("VAPID_PRIVATE_KEY")
    subject = _get_proxy_secret("VAPID_SUBJECT", "https://skhlmc-dbt-marksys.onrender.com")
    if not public_key or not private_key:
        return None
    return {"public_key": public_key, "private_key": private_key, "subject": subject}


def _get_db_engine():
    """Compatibility wrapper for API code and existing test patch points."""
    return get_db_engine()


def get_vote_db():
    """The DB executor passed to ``core.vote_logic`` from the API handlers."""
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return RuntimeDb(engine)


def _committee_auth_material(user_id: str):
    """Load the current secret, access state and credential hash atomically."""
    engine = _get_db_engine()
    if engine is None:
        return None
    try:
        with engine.begin() as conn:
            configs = get_configs_from_connection(
                conn, ("cookie_secret", "login_disabled_accounts")
            )
            row = conn.execute(
                text(
                    f"SELECT password_hash FROM {TABLE_ACCOUNTS} "
                    "WHERE user_id=:user_id"
                ),
                {"user_id": user_id},
            ).fetchone()
    except Exception:
        return None
    secret = configs.get("cookie_secret")
    if not secret or row is None:
        return None
    try:
        password_hash = str(row._mapping["password_hash"])
    except (AttributeError, KeyError, TypeError):
        try:
            password_hash = str(row[0])
        except (IndexError, TypeError):
            return None
    disabled = configs.get("login_disabled_accounts") or []
    disabled_keys = (
        {normalize_account_id(value) for value in disabled}
        if isinstance(disabled, list)
        else set()
    )
    return str(secret), password_hash, disabled_keys


def _committee_credential_fingerprint(
    secret: str, user_id: str, password_hash: str,
) -> str:
    material = f"committee-credential-v1\0{user_id}\0{password_hash}".encode()
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()


def _verify_committee_token(token: str):
    """Verify a bounded, expiring session against the current credential hash."""
    value = str(token or "")
    if not value or len(value) > COMMITTEE_SESSION_TOKEN_MAX_CHARS:
        return None
    try:
        prefix, encoded, signature = value.split(".", 2)
        if prefix != "ct1":
            return None
        payload = json.loads(_claim_b64decode(encoded))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            return None
        user_id = str(payload.get("sub") or "")
        issued_at = int(payload.get("iat"))
        expires_at = int(payload.get("exp"))
        credential = str(payload.get("cred") or "")
    except (
        OverflowError, TypeError, ValueError, UnicodeError, json.JSONDecodeError,
    ):
        return None
    now = int(time.time())
    if (
        not user_id
        or len(user_id) > 200
        or issued_at > now + COMMITTEE_SESSION_CLOCK_SKEW_SECONDS
        or expires_at <= now
        or expires_at <= issued_at
        or expires_at - issued_at > COMMITTEE_SESSION_MAX_AGE_SECONDS
    ):
        return None
    material = _committee_auth_material(user_id)
    if material is None:
        return None
    secret, password_hash, disabled_keys = material
    expected_signature = _claim_b64(
        hmac.new(
            secret.encode(), f"ct1.{encoded}".encode("ascii"), hashlib.sha256,
        ).digest()
    )
    expected_credential = _committee_credential_fingerprint(
        secret, user_id, password_hash,
    )
    if (
        normalize_account_id(user_id) in disabled_keys
        or not hmac.compare_digest(signature, expected_signature)
        or not hmac.compare_digest(credential, expected_credential)
    ):
        return None
    return user_id


def _verify_committee_cookie(request: Request):
    return _verify_committee_token(request.cookies.get("committee_user") or "")


_relay_cookie_secret = None


def _get_relay_cookie_secret():
    """Read and cache the shared signing secret used by authenticated claims.

    The historic name is retained because R2 upload and review claims import it.
    """
    global _relay_cookie_secret
    if _relay_cookie_secret is not None:
        return _relay_cookie_secret

    engine = _get_db_engine()
    if engine is None:
        return None
    with engine.begin() as conn:
        configs = get_configs_from_connection(conn, ("cookie_secret",))
    secret = configs.get("cookie_secret")
    if not secret:
        return None
    _relay_cookie_secret = str(secret)
    return _relay_cookie_secret


def _claim_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _claim_b64decode(value: str) -> bytes:
    raw = value.encode("ascii")
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


def _bounded_live_system_prompt(value: str) -> str:
    return str(value or "")[:LIVE_SYSTEM_PROMPT_MAX_CHARS]


def _sign_live_practice_claim(
    user_id: str,
    mode: str,
    *,
    practice_id: str = "",
    session_seconds: list[int] | None = None,
    system_prompt: str = "",
    expires_at: int | None = None,
) -> str:
    """Sign a Solo launch/JIT-mint claim without exposing any server secret."""
    secret = _get_relay_cookie_secret()
    if not secret or mode not in ("free", "mock") or not str(user_id or ""):
        return ""
    identifier = str(practice_id or secrets.token_urlsafe(12))
    expiry = int(expires_at or (time.time() + LIVE_PRACTICE_CLAIM_TTL_SECONDS))
    payload = {
        "v": 1,
        "user_id": str(user_id),
        "mode": mode,
        "practice_id": identifier,
        "session_seconds": [int(value) for value in (session_seconds or [])],
        "system_prompt": _bounded_live_system_prompt(system_prompt),
        "exp": expiry,
    }
    encoded = _claim_b64(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    signature = hmac.new(secret.encode(), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_claim_b64(signature)}"


def _verify_live_practice_claim(
    signed_claim: str,
    *,
    expected_user_id: str = "",
    expected_mode: str = "",
) -> dict | None:
    """Verify a short-lived Solo claim and all server-authoritative fields."""
    if not signed_claim or len(str(signed_claim)) > LIVE_PRACTICE_CLAIM_MAX_CHARS:
        return None
    secret = _get_relay_cookie_secret()
    if not secret:
        return None
    try:
        encoded, supplied = signed_claim.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_claim_b64(expected), supplied):
            return None
        payload = json.loads(_claim_b64decode(encoded))
        if not isinstance(payload, dict) or int(payload.get("v") or 0) != 1:
            return None
        user_id = str(payload.get("user_id") or "")
        mode = str(payload.get("mode") or "")
        practice_id = str(payload.get("practice_id") or "")
        if mode not in ("free", "mock") or not user_id:
            return None
        if expected_user_id and user_id != str(expected_user_id):
            return None
        if expected_mode and mode != expected_mode:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", practice_id):
            return None
        expiry = int(payload.get("exp") or 0)
        if expiry < int(time.time()) or expiry > int(time.time()) + LIVE_PRACTICE_CLAIM_TTL_SECONDS + 60:
            return None
        sessions = payload.get("session_seconds") or []
        if not isinstance(sessions, list) or len(sessions) > 32:
            return None
        normalized_sessions = [int(value) for value in sessions]
        if any(value < 1 or value > LIVE_FREE_SESSION_MAX_SECONDS for value in normalized_sessions):
            return None
        payload["session_seconds"] = normalized_sessions
        system_prompt = payload.get("system_prompt") or ""
        if not isinstance(system_prompt, str) or len(system_prompt) > LIVE_SYSTEM_PROMPT_MAX_CHARS:
            return None
        payload["system_prompt"] = system_prompt
        return payload
    except Exception:
        return None


def _new_live_practice_claim(user_id: str, mode: str) -> str:
    return _sign_live_practice_claim(user_id, mode)


def _planned_live_practice_claim(
    claim: dict, session_seconds: list[int], system_prompt: str,
) -> str:
    return _sign_live_practice_claim(
        str(claim.get("user_id") or ""),
        str(claim.get("mode") or ""),
        practice_id=str(claim.get("practice_id") or ""),
        session_seconds=session_seconds,
        system_prompt=system_prompt,
        expires_at=int(claim.get("exp") or 0),
    )


def _sign_committee_token(user_id: str, *, credential_hash: str | None = None):
    """Mint one versioned session bound to the account's current password hash."""
    normalized_user = str(user_id or "").strip()
    if not normalized_user or len(normalized_user) > 200:
        return None
    material = _committee_auth_material(normalized_user)
    if material is None:
        return None
    secret, password_hash, disabled_keys = material
    if (
        normalize_account_id(normalized_user) in disabled_keys
        or credential_hash is not None
        and not hmac.compare_digest(str(credential_hash), password_hash)
    ):
        return None
    issued_at = int(time.time())
    payload = {
        "v": 1,
        "sub": normalized_user,
        "iat": issued_at,
        "exp": issued_at + COMMITTEE_SESSION_MAX_AGE_SECONDS,
        "cred": _committee_credential_fingerprint(
            secret, normalized_user, password_hash,
        ),
    }
    encoded = _claim_b64(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signature = _claim_b64(
        hmac.new(
            secret.encode(), f"ct1.{encoded}".encode("ascii"), hashlib.sha256,
        ).digest()
    )
    token = f"ct1.{encoded}.{signature}"
    return token if len(token) <= COMMITTEE_SESSION_TOKEN_MAX_CHARS else None


def _sign_registration_admin_token():
    """Mint a dedicated session token for the organiser registration console."""
    secret = _get_relay_cookie_secret()
    if not secret:
        return None
    subject = "registration_admin"
    sig = hmac.new(str(secret).encode(), subject.encode(), hashlib.sha256).hexdigest()
    return f"{subject}:{sig}"


def _verify_registration_admin_token(token: str) -> bool:
    if not token or ":" not in token:
        return False
    subject, sig = token.rsplit(":", 1)
    if subject != "registration_admin":
        return False
    secret = _get_relay_cookie_secret()
    if not secret:
        return False
    expected = hmac.new(str(secret).encode(), subject.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _judging_auth_material(match_id: str):
    """Load the current signing secret and still-open match credential."""
    engine = _get_db_engine()
    if engine is None:
        return None
    try:
        with engine.begin() as conn:
            configs = get_configs_from_connection(conn, ("cookie_secret",))
            row = conn.execute(
                text(
                    f"SELECT access_code_hash FROM {TABLE_MATCHES} "
                    "WHERE match_id=:match_id"
                ),
                {"match_id": match_id},
            ).fetchone()
    except Exception:
        return None
    secret = str(configs.get("cookie_secret") or "")
    if not secret or row is None:
        return None
    try:
        raw_hash = row._mapping["access_code_hash"]
    except (AttributeError, KeyError, TypeError):
        try:
            raw_hash = row[0]
        except (IndexError, TypeError):
            return None
    access_code_hash = "" if raw_hash is None else str(raw_hash).strip()
    if not access_code_hash or access_code_hash.lower() in {"nan", "none", "<na>"}:
        return None
    return secret, access_code_hash


def _judging_credential_fingerprint(
    secret: str, match_id: str, access_code_hash: str,
) -> str:
    material = (
        f"judging-credential-v1\0{match_id}\0{access_code_hash}".encode()
    )
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()


def _sign_judging_token(match_id: str, *, credential_hash: str):
    """Mint one expiring match session bound to the current access-code hash."""
    normalized_match = str(match_id or "").strip()
    if not normalized_match or len(normalized_match) > 200:
        return None
    material = _judging_auth_material(normalized_match)
    if material is None:
        return None
    secret, current_hash = material
    if not hmac.compare_digest(str(credential_hash or ""), current_hash):
        return None
    issued_at = int(time.time())
    payload = {
        "v": 1,
        "sub": normalized_match,
        "iat": issued_at,
        "exp": issued_at + JUDGING_SESSION_TTL_SECONDS,
        "cred": _judging_credential_fingerprint(
            secret, normalized_match, current_hash,
        ),
    }
    encoded = _claim_b64(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signature = _claim_b64(
        hmac.new(
            secret.encode(), f"jt1.{encoded}".encode("ascii"), hashlib.sha256,
        ).digest()
    )
    token = f"jt1.{encoded}.{signature}"
    return token if len(token) <= JUDGING_SESSION_TOKEN_MAX_CHARS else None


def _verify_judging_token(token: str):
    """Verify time, signature, match existence and the live access credential."""
    value = str(token or "")
    if not value or len(value) > JUDGING_SESSION_TOKEN_MAX_CHARS:
        return None
    try:
        prefix, encoded, signature = value.split(".", 2)
        if prefix != "jt1":
            return None
        payload = json.loads(_claim_b64decode(encoded))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            return None
        match_id = str(payload.get("sub") or "")
        issued_at = int(payload.get("iat"))
        expires_at = int(payload.get("exp"))
        credential = str(payload.get("cred") or "")
    except (
        OverflowError, TypeError, ValueError, UnicodeError, json.JSONDecodeError,
    ):
        return None
    now = int(time.time())
    if (
        not match_id
        or len(match_id) > 200
        or issued_at > now + JUDGING_SESSION_CLOCK_SKEW_SECONDS
        or expires_at <= now
        or expires_at <= issued_at
        or expires_at - issued_at > JUDGING_SESSION_TTL_SECONDS
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", signature)
        or not re.fullmatch(r"[0-9a-f]{64}", credential)
    ):
        return None
    material = _judging_auth_material(match_id)
    if material is None:
        return None
    secret, access_code_hash = material
    expected_signature = _claim_b64(
        hmac.new(
            secret.encode(), f"jt1.{encoded}".encode("ascii"), hashlib.sha256,
        ).digest()
    )
    expected_credential = _judging_credential_fingerprint(
        secret, match_id, access_code_hash,
    )
    if (
        not hmac.compare_digest(signature, expected_signature)
        or not hmac.compare_digest(credential, expected_credential)
    ):
        return None
    return match_id


def _sign_review_token(match_id: str):
    secret = _get_relay_cookie_secret(); subject = f"review:{match_id}"
    if not secret or not match_id: return None
    return f"{subject}:{hmac.new(str(secret).encode(), subject.encode(), hashlib.sha256).hexdigest()}"


def _verify_review_token(token: str):
    if not token or ":" not in token: return None
    subject, sig = token.rsplit(":", 1)
    if not subject.startswith("review:"): return None
    secret = _get_relay_cookie_secret(); match_id = subject[7:]
    if not secret or not match_id: return None
    expected = hmac.new(str(secret).encode(), subject.encode(), hashlib.sha256).hexdigest()
    return match_id if hmac.compare_digest(sig, expected) else None


def _require_committee_user(request: Request):
    user_id = _verify_committee_cookie(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user_id


def _validated_push_subscription(payload):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid push subscription")
    endpoint = str(payload.get("endpoint") or "").strip()
    keys = payload.get("keys")
    if (
        not endpoint.startswith("https://")
        or len(endpoint) > PUSH_ENDPOINT_MAX_CHARS
        or not isinstance(keys, dict)
        or not str(keys.get("p256dh") or "").strip()
        or not str(keys.get("auth") or "").strip()
        or len(str(keys.get("p256dh") or "")) > PUSH_KEY_MAX_CHARS
        or len(str(keys.get("auth") or "")) > PUSH_KEY_MAX_CHARS
        or len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > PUSH_SUBSCRIPTION_MAX_BYTES
    ):
        raise HTTPException(status_code=400, detail="Invalid push subscription")
    return endpoint


def _bound_push_subscriptions(conn, user_id: str, now) -> None:
    """Keep only five active devices per member and discard stale inactive rows."""
    conn.execute(text(f"""UPDATE {TABLE_PUSH_SUBSCRIPTIONS} SET is_active=FALSE,updated_at=:now
        WHERE user_id=:user_id AND is_active=TRUE AND endpoint NOT IN
          (SELECT endpoint FROM {TABLE_PUSH_SUBSCRIPTIONS} WHERE user_id=:user_id AND is_active=TRUE
           ORDER BY updated_at DESC NULLS LAST LIMIT :device_limit)"""),
        {"user_id": user_id, "now": now, "device_limit": PUSH_ACTIVE_DEVICES_PER_USER})
    conn.execute(text(f"""DELETE FROM {TABLE_PUSH_SUBSCRIPTIONS}
        WHERE is_active=FALSE AND updated_at<:cutoff"""),
        {"cutoff": now - datetime.timedelta(days=PUSH_INACTIVE_RETENTION_DAYS)})


@app.get("/manifest.json")
async def manifest():
    return FileResponse(
        BASE_DIR / "static" / "manifest.json",
        media_type="application/manifest+json",
        headers=_cache_headers(CACHE_MANIFEST),
    )


@app.get("/sw.js")
async def service_worker():
    # Inject the VAPID public key so the service worker can re-subscribe on its
    # own when the browser rotates the push endpoint (pushsubscriptionchange).
    source = (BASE_DIR / "deploy" / "sw.js").read_text(encoding="utf-8")
    vapid = _get_vapid()
    public_key = vapid["public_key"] if vapid else ""
    source = source.replace("__VAPID_PUBLIC_KEY__", public_key)
    return Response(
        content=source,
        media_type="application/javascript",
        headers=_cache_headers(CACHE_NO_CACHE),
    )


@app.get("/app-icon-{size}.png")
async def app_icon(size: str):
    icon_path = BASE_DIR / "static" / f"app-icon-{size}.png"
    if size not in {"180", "192", "512"} or not icon_path.exists():
        return Response(status_code=404)
    return FileResponse(icon_path, media_type="image/png",
                        headers=_binary_cache_headers(CACHE_STATIC))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(
        BASE_DIR / "static" / "app-icon-192.png",
        media_type="image/png",
        headers=_binary_cache_headers(CACHE_STATIC),
    )


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    user_id = require_page_user(request, "member_profile")
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")

    try:
        subscription = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    endpoint = _validated_push_subscription(subscription)

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {TABLE_PUSH_SUBSCRIPTIONS} "
                "(endpoint, user_id, subscription_json, is_active, created_at, updated_at, last_error) "
                "VALUES (:endpoint, :user_id, :subscription_json, TRUE, :now, :now, NULL) "
                "ON CONFLICT (endpoint) DO UPDATE SET "
                "user_id = EXCLUDED.user_id, "
                "subscription_json = EXCLUDED.subscription_json, "
                "is_active = TRUE, "
                "updated_at = EXCLUDED.updated_at, "
                "last_error = NULL"
            ),
            {
                "endpoint": endpoint,
                "user_id": user_id,
                "subscription_json": json.dumps(subscription, ensure_ascii=False),
                "now": now,
            },
        )
        _bound_push_subscriptions(conn, user_id, now)

    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    user_id = require_page_user(request, "member_profile")
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")

    payload = {}
    try:
        payload = await request.json()
    except Exception:
        pass
    endpoint = str(payload.get("endpoint", "")).strip() if isinstance(payload, dict) else ""

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        if endpoint:
            conn.execute(
                text(
                    f"UPDATE {TABLE_PUSH_SUBSCRIPTIONS} "
                    "SET is_active = FALSE, updated_at = :now "
                    "WHERE endpoint = :endpoint AND user_id = :user_id"
                ),
                {"endpoint": endpoint, "user_id": user_id, "now": now},
            )
        else:
            conn.execute(
                text(
                    f"UPDATE {TABLE_PUSH_SUBSCRIPTIONS} "
                    "SET is_active = FALSE, updated_at = :now "
                    "WHERE user_id = :user_id"
                ),
                {"user_id": user_id, "now": now},
            )

    return {"ok": True}


@app.post("/api/push/resubscribe")
async def push_resubscribe(request: Request):
    """Migrate a push subscription to a new endpoint after the browser rotated it.

    Called from the service worker's ``pushsubscriptionchange`` handler, which has
    no auth token — we authenticate implicitly on the ``old_endpoint`` (a secret,
    unguessable capability URL already stored against a user). We carry over that
    row's ``user_id`` so the member keeps receiving notifications without having to
    manually re-enable them.
    """
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    old_endpoint = str(payload.get("old_endpoint", "")).strip() if isinstance(payload, dict) else ""
    subscription = payload.get("subscription") if isinstance(payload, dict) else None
    new_endpoint = _validated_push_subscription(subscription)
    if not old_endpoint:
        raise HTTPException(status_code=400, detail="Missing old endpoint")

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        # The old endpoint is the capability that identifies the owning member.
        # Never create an active orphan subscription when it is missing/unknown;
        # the authenticated page reconciliation will safely recover it later.
        row = conn.execute(
            text(
                f"SELECT user_id FROM {TABLE_PUSH_SUBSCRIPTIONS} "
                "WHERE endpoint = :old_endpoint"
            ),
            {"old_endpoint": old_endpoint},
        ).fetchone()
        user_id = row[0] if row is not None else None
        if not user_id:
            raise HTTPException(status_code=404, detail="Old subscription not found")
        if not account_can_access(user_id, "member_profile"):
            raise HTTPException(
                status_code=403,
                detail=access_denial_message("member_profile"),
            )

        conn.execute(
            text(
                f"INSERT INTO {TABLE_PUSH_SUBSCRIPTIONS} "
                "(endpoint, user_id, subscription_json, is_active, created_at, updated_at, last_error) "
                "VALUES (:endpoint, :user_id, :subscription_json, TRUE, :now, :now, NULL) "
                "ON CONFLICT (endpoint) DO UPDATE SET "
                "user_id = COALESCE(EXCLUDED.user_id, {table}.user_id), "
                "subscription_json = EXCLUDED.subscription_json, "
                "is_active = TRUE, "
                "updated_at = EXCLUDED.updated_at, "
                "last_error = NULL".format(table=TABLE_PUSH_SUBSCRIPTIONS)
            ),
            {
                "endpoint": new_endpoint,
                "user_id": user_id,
                "subscription_json": json.dumps(subscription, ensure_ascii=False),
                "now": now,
            },
        )

        # Drop the stale row so we don't keep pushing to a dead endpoint.
        if old_endpoint and old_endpoint != new_endpoint:
            conn.execute(
                text(
                    f"DELETE FROM {TABLE_PUSH_SUBSCRIPTIONS} WHERE endpoint = :old_endpoint"
                ),
                {"old_endpoint": old_endpoint},
            )
        _bound_push_subscriptions(conn, str(user_id), now)

    return {"ok": True}


def _build_azure_tts_ssml(text_value: str, voice: str, rate: str) -> str:
    azure_config = TTS_PROVIDER_OPTIONS[AZURE_TTS_PROVIDER]
    voice = xml_escape(
        voice or azure_config["default_voice"], {'"': "&quot;"}
    )
    rate = xml_escape(rate or azure_config["default_rate"], {'"': "&quot;"})
    text_value = xml_escape(text_value)
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xml:lang="zh-HK">'
        f'<voice name="{voice}"><prosody rate="{rate}">{text_value}</prosody></voice>'
        "</speak>"
    )


class TtsUnavailable(Exception):
    """Raised when TTS cannot synthesize (unconfigured, upstream error, etc.).

    ``status`` mirrors the HTTP code the /api/tts/azure endpoint should surface:
    503 = provider not configured, 502 = upstream synth failed.
    """

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _selected_tts_provider():
    raw = _get_proxy_secret(TTS_PROVIDER_SECRET, DEFAULT_TTS_PROVIDER)
    return get_tts_provider_config(raw)


def tts_provider_configured() -> bool:
    """Whether the selected provider or its supported fallback can synthesize.

    Custom TTS deliberately falls back to Azure in :func:`_synthesize_tts`.
    Readiness must use the same rule or competition-day callers can incorrectly
    suppress speech even though the Azure fallback is fully configured.
    """
    provider, config = _selected_tts_provider()
    if provider == CUSTOM_TTS_PROVIDER:
        model_id = _get_proxy_secret(config["model_secret"]).strip()
        custom_ready = bool(
            _get_proxy_secret(config["url_secret"]).strip()
            and _get_proxy_secret(config["api_key_secret"]).strip()
            and model_id
            and _model_is_deployable(model_id, config["registry_model_type"])
        )
        if custom_ready:
            return True
        config = TTS_PROVIDER_OPTIONS[AZURE_TTS_PROVIDER]
    return bool(
        _get_proxy_secret(config["speech_key_secret"]).strip()
        and _get_proxy_secret(config["region_secret"]).strip()
    )


_lexicon_cache = {"rows": None, "at": 0.0}
TTS_SEMAPHORE = asyncio.Semaphore(TTS_CONCURRENCY)


async def _read_bounded_audio(response) -> bytes:
    try:
        declared = int(response.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > TTS_MAX_RESPONSE_BYTES:
        raise TtsUnavailable("TTS response exceeds server limit", status=502)
    data = bytearray()
    async for chunk in response.aiter_bytes():
        if len(data) + len(chunk) > TTS_MAX_RESPONSE_BYTES:
            raise TtsUnavailable("TTS response exceeds server limit", status=502)
        data.extend(chunk)
    if not data:
        raise TtsUnavailable("TTS returned empty audio", status=502)
    return bytes(data)


def _model_is_deployable(model_id: str, model_type: str) -> bool:
    """Check the formal registry migration and live status before every call."""
    try:
        from core.schema_features import READY, feature_bundle_state

        db = get_vote_db()
        if feature_bundle_state(db, "dataset_model", (
            TABLE_AI_DATASET_SNAPSHOTS,
            TABLE_AI_DATASET_SNAPSHOT_ITEMS,
            TABLE_AI_MODEL_VERSIONS,
        )) != READY:
            return False
        engine = _get_db_engine()
        with engine.connect() as conn:
            return bool(conn.execute(text(f"""SELECT EXISTS(SELECT 1 FROM {TABLE_AI_MODEL_VERSIONS}
                WHERE model_id=:model AND model_type=:type AND status='deployable')"""),
                {"model": model_id, "type": model_type}).scalar())
    except Exception as exc:
        logger.info("model deployable gate unavailable: %s", exc)
        return False


def _load_lexicon_overrides():
    """Active (term, reading) pairs from tts_lexicon, longest term first so
    overlapping terms don't partially clobber. Cached with a short TTL; on DB
    error, keep the last good snapshot rather than dropping overrides."""
    now = time.monotonic()
    cached = _lexicon_cache["rows"]
    if cached is not None and (now - _lexicon_cache["at"]) < TTS_LEXICON_CACHE_TTL_SECONDS:
        return cached
    rows = []
    try:
        engine = _get_db_engine()
        with engine.begin() as conn:
            result = conn.execute(
                text("SELECT term, reading FROM tts_lexicon WHERE is_active = TRUE LIMIT :limit"),
                {"limit": TTS_LEXICON_LIMIT},
            )
            for term_value, reading_value in result:
                term_value = (term_value or "").strip()
                reading_value = (reading_value or "").strip()
                if term_value and reading_value:
                    rows.append((term_value, reading_value))
        rows.sort(key=lambda pair: len(pair[0]), reverse=True)
    except Exception as e:
        logger.info("lexicon load failed, keeping previous overrides: %s", e)
        if cached is not None:
            return cached
        rows = []
    _lexicon_cache["rows"] = rows
    _lexicon_cache["at"] = now
    return rows


@lru_cache(maxsize=1)
def _compiled_lexicon(rows):
    """Compile the potentially large alternation once per cached lexicon."""
    replacements = {}
    for term_value, reading_value in rows:
        replacements.setdefault(term_value, reading_value)
    if not replacements:
        return None, replacements
    pattern = re.compile("|".join(re.escape(term) for term in replacements))
    return pattern, replacements


def _preprocess_tts_text(text_value: str) -> str:
    """讀音字典前處理 (docs/ROADMAP.md P3「讀音層」). 合成前把 tts_lexicon 嘅
    term → reading 覆寫。單人 (/api/tts/azure) 同聯機 (_room_gemini_pump) 都經呢度,
    改字典一次兩邊生效。將來可喺呢度加 G2P (ToJyutping/PyCantonese)。"""
    processed = (text_value or "").strip()
    if not processed:
        return processed
    rows = tuple(_load_lexicon_overrides())
    pattern, replacements = _compiled_lexicon(rows)
    if pattern is None:
        return processed
    return pattern.sub(lambda match: replacements[match.group(0)], processed)


async def _record_tts_attempt(
    accounting: dict | None,
    *,
    provider: str,
    text_value: str,
    model_label: str,
    success: bool,
    error_message: str = "",
) -> None:
    """Best-effort ledger write for one real provider HTTP attempt."""
    if not accounting:
        return
    try:
        from core.funds_logic import log_tts_usage

        await asyncio.to_thread(
            log_tts_usage,
            accounting.get("user_id"),
            accounting.get("feature") or "tts",
            success,
            provider=provider,
            text=text_value,
            operation_id=accounting.get("operation_id"),
            operation_stage=accounting.get("operation_stage") or "synthesis",
            model_label=model_label,
            error_message=str(error_message or "")[:300],
            db=get_vote_db(),
        )
    except Exception as exc:
        # Voice playback remains available during a temporary ledger outage;
        # the failed write is visible in server logs for reconciliation.
        logger.warning("TTS usage ledger write failed: %s", type(exc).__name__)


async def _synthesize_azure(
    text_value: str, *, accounting: dict | None = None
) -> tuple[bytes, str]:
    config = TTS_PROVIDER_OPTIONS[AZURE_TTS_PROVIDER]
    speech_key = _get_proxy_secret(config["speech_key_secret"]).strip()
    speech_region = _get_proxy_secret(config["region_secret"]).strip()
    if not speech_key or not speech_region:
        raise TtsUnavailable("Azure TTS is not configured", status=503)

    voice = (
        _get_proxy_secret(config["voice_secret"], config["default_voice"]).strip()
        or config["default_voice"]
    )
    rate = (
        _get_proxy_secret(config["rate_secret"], config["default_rate"]).strip()
        or config["default_rate"]
    )
    output_format = (
        _get_proxy_secret(
            config["output_format_secret"], config["default_output_format"]
        ).strip()
        or config["default_output_format"]
    )
    ssml = _build_azure_tts_ssml(text_value, voice, rate)
    endpoint = f"https://{speech_region}.tts.speech.microsoft.com/cognitiveservices/v1"

    attempted = False
    succeeded = False
    failure = ""
    try:
        async with httpx.AsyncClient(timeout=TTS_PROVIDER_TIMEOUT_SECONDS) as client:
            request = client.build_request(
                "POST",
                endpoint,
                content=ssml.encode("utf-8"),
                headers={
                    "Ocp-Apim-Subscription-Key": speech_key,
                    "Content-Type": "application/ssml+xml; charset=utf-8",
                    "X-Microsoft-OutputFormat": output_format,
                    "User-Agent": "skhlmc-dbt-marksys",
                },
            )
            # Client/request construction is local setup, not a provider call.
            # Mark exactly when send() crosses the HTTP boundary so transport
            # failures count but malformed local requests do not.
            attempted = True
            azure_response = await client.send(request, stream=True)
            try:
                if azure_response.status_code != 200:
                    logger.warning("Azure TTS returned %s", azure_response.status_code)
                    raise TtsUnavailable("Azure TTS request failed", status=502)
                audio = await _read_bounded_audio(azure_response)
                mime = azure_response.headers.get("content-type") or "audio/mpeg"
            finally:
                await azure_response.aclose()
        succeeded = True
    except httpx.HTTPError as e:
        failure = type(e).__name__
        logger.warning("Azure TTS request failed: %s", e)
        raise TtsUnavailable("Azure TTS request failed", status=502)
    except TtsUnavailable as exc:
        failure = str(exc)
        raise
    finally:
        if attempted:
            await _record_tts_attempt(
                accounting,
                provider=AZURE_TTS_PROVIDER,
                text_value=text_value,
                model_label=voice,
                success=succeeded,
                error_message=failure,
            )

    return audio, mime


async def _synthesize_custom(
    text_value: str, *, accounting: dict | None = None
) -> tuple[bytes, str]:
    """Call the authenticated custom TTS service using the stable wire contract."""
    config = TTS_PROVIDER_OPTIONS[CUSTOM_TTS_PROVIDER]
    custom_url = _get_proxy_secret(config["url_secret"]).strip()
    api_key = _get_proxy_secret(config["api_key_secret"]).strip()
    model_version = _get_proxy_secret(config["model_secret"]).strip()
    if not custom_url or not api_key or not model_version:
        raise TtsUnavailable("Custom TTS is not configured", status=503)
    if not _model_is_deployable(model_version, config["registry_model_type"]):
        raise TtsUnavailable("Custom TTS model has not passed the deployable gate", status=503)
    request_id = secrets.token_urlsafe(12)
    started = time.monotonic()
    attempted = False
    succeeded = False
    failure = ""
    response_model = model_version
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(
            TTS_PROVIDER_TIMEOUT_SECONDS, connect=TTS_PROVIDER_CONNECT_TIMEOUT_SECONDS,
        )) as client:
            request = client.build_request(
                "POST",
                custom_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "audio/*",
                    "X-Request-ID": request_id,
                },
                json={
                    "text": text_value,
                    "model_version": model_version,
                    "request_id": request_id,
                },
            )
            attempted = True
            response = await client.send(request, stream=True)
            try:
                response.raise_for_status()
                mime = (response.headers.get("content-type") or "audio/wav").split(";", 1)[0]
                if not mime.startswith("audio/"):
                    raise TtsUnavailable("Custom TTS returned invalid audio", status=502)
                audio = await _read_bounded_audio(response)
                response_model = response.headers.get("x-model-version") or model_version
            finally:
                await response.aclose()
        succeeded = True
    except httpx.HTTPError as exc:
        failure = type(exc).__name__
        logger.warning("custom TTS failed request_id=%s: %s", request_id, exc)
        raise TtsUnavailable("Custom TTS request failed", status=502) from exc
    except TtsUnavailable as exc:
        failure = str(exc)
        raise
    finally:
        if attempted:
            await _record_tts_attempt(
                accounting,
                provider=CUSTOM_TTS_PROVIDER,
                text_value=text_value,
                model_label=response_model,
                success=succeeded,
                error_message=failure,
            )
    logger.info("custom TTS success request_id=%s model=%s elapsed_ms=%d bytes=%d",
                request_id, response_model,
                int((time.monotonic() - started) * 1000), len(audio))
    return audio, mime


async def _synthesize_tts(
    text_value: str, *, accounting: dict | None = None
) -> tuple[bytes, str]:
    """統一 TTS 入口:單人 (/api/tts/azure route)、聯機 (_room_gemini_pump)、
    將來 custom model 全部行呢度。換 provider = 改 TTS_PROVIDER secret。"""
    raw = str(text_value or "").strip()
    if len(raw) > TTS_TEXT_MAX_CHARS:
        raise TtsUnavailable("TTS text exceeds server limit", status=400)
    processed = _preprocess_tts_text(raw)
    if not processed:
        raise TtsUnavailable("Missing text", status=400)
    if len(processed) > TTS_TEXT_MAX_CHARS:
        raise TtsUnavailable("TTS lexicon expansion exceeds server limit", status=400)
    provider, _config = _selected_tts_provider()
    async with TTS_SEMAPHORE:
        if provider == CUSTOM_TTS_PROVIDER:
            try:
                return await _synthesize_custom(processed, accounting=accounting)
            except TtsUnavailable as exc:
                logger.warning("custom TTS unavailable; falling back to Azure: %s", exc)
                return await _synthesize_azure(processed, accounting=accounting)
        return await _synthesize_azure(processed, accounting=accounting)


async def synthesize_tts_accounted(
    text_value: str,
    *,
    user_id: str | None,
    feature: str = "tts",
    operation_id: str,
    operation_stage: str = "synthesis",
):
    """Synthesize and account every actual provider/fallback attempt."""
    operation = str(operation_id or "").strip()[:200]
    if not operation:
        raise TtsUnavailable("Missing TTS accounting operation id", status=400)
    audio, mime = await _synthesize_tts(
        text_value,
        accounting={
            "user_id": user_id,
            "feature": feature,
            "operation_id": operation,
            "operation_stage": str(operation_stage or "synthesis")[:80],
        },
    )
    return audio, mime, {"operation_id": operation, "feature": feature}


@app.post("/api/tts/synthesize")
@app.post("/api/tts/azure", include_in_schema=False)
async def azure_tts(request: Request):
    user_id = require_page_user(request, "tts")
    budget_error = _bandwidth_live_gate_error()
    if budget_error:
        raise HTTPException(
            status_code=429,
            detail="本月 Render 傳輸量已達3.5GB，Solo server TTS已停用；請使用Gemini原生聲音。",
        )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    tts_text = str(payload.get("text") or "").strip()
    if not tts_text:
        raise HTTPException(status_code=400, detail="Missing text")
    if len(tts_text) > TTS_TEXT_MAX_CHARS:
        raise HTTPException(status_code=400, detail="Text is too long")

    try:
        operation_id = str(payload.get("operation_id") or "").strip()[:200]
        if not operation_id:
            operation_id = "tts-" + secrets.token_urlsafe(18)
        audio_bytes, mime, _usage_meta = await synthesize_tts_accounted(
            tts_text,
            user_id=str(user_id),
            feature="tts",
            operation_id=operation_id,
            operation_stage="http_synthesis",
        )
    except TtsUnavailable as e:
        raise HTTPException(status_code=e.status, detail=str(e))

    await asyncio.to_thread(
        record_bandwidth_usage, "tts_audio_response", len(audio_bytes), str(user_id),
        aggregate_key=f"user={str(user_id)[:120]}",
    )

    return Response(
        content=audio_bytes,
        media_type=mime or "audio/mpeg",
        headers={"Cache-Control": "no-store", "Content-Encoding": "identity"},
    )


@app.post("/api/video/view")
async def video_view(request: Request):
    user_id = require_page_user(request, "video_replay")
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        video_id = int(payload.get("video_id"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid video_id")

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(
            text(f"""INSERT INTO {TABLE_VIDEO_VIEWS} (video_id,user_id,viewed_at)
                SELECT :video_id,:user_id,:viewed_at
                WHERE NOT EXISTS (SELECT 1 FROM {TABLE_VIDEO_VIEWS}
                  WHERE video_id=:video_id AND user_id=:user_id
                    AND viewed_at>=:view_cutoff)"""),
            {"video_id": video_id, "user_id": user_id, "viewed_at": now,
             "view_cutoff": now - datetime.timedelta(hours=VIDEO_VIEW_DEDUPE_HOURS)},
        )

    return {"ok": True}


@app.post("/api/video/progress")
async def video_progress(request: Request):
    user_id = require_page_user(request, "video_replay")
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        video_id = int(payload.get("video_id"))
        watched_seconds = max(0, int(float(payload.get("watched_seconds") or 0)))
        duration_seconds = max(0, int(float(payload.get("duration_seconds") or 0)))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid progress payload")
    if (
        video_id <= 0
        or watched_seconds > VIDEO_PROGRESS_MAX_SECONDS
        or duration_seconds > VIDEO_PROGRESS_MAX_SECONDS
    ):
        raise HTTPException(status_code=400, detail="Invalid progress payload")
    if duration_seconds:
        watched_seconds = min(watched_seconds, duration_seconds)

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                INSERT INTO {TABLE_VIDEO_PROGRESS} (
                    video_id, user_id, watched_seconds, duration_seconds, updated_at
                )
                VALUES (
                    :video_id, :user_id, :watched_seconds, :duration_seconds, :updated_at
                )
                ON CONFLICT (video_id, user_id) DO UPDATE SET
                    watched_seconds = EXCLUDED.watched_seconds,
                    duration_seconds = EXCLUDED.duration_seconds,
                    updated_at = EXCLUDED.updated_at
                WHERE {TABLE_VIDEO_PROGRESS}.watched_seconds IS DISTINCT FROM EXCLUDED.watched_seconds
                   OR {TABLE_VIDEO_PROGRESS}.duration_seconds IS DISTINCT FROM EXCLUDED.duration_seconds
                """
            ),
            {
                "video_id": video_id,
                "user_id": user_id,
                "watched_seconds": watched_seconds,
                "duration_seconds": duration_seconds,
                "updated_at": now,
            },
        )

    return {"ok": True}


# ---------------------------------------------------------------------------
# Competition-day projector (big-screen display + operator control)
#
# These routes are backed by projector_state plus read-only match/debater data.
# Schema ownership lives in schema.py and compatibility creation runs at startup.
# The projector intentionally shows no timer: timing stays on the chairperson's
# own device.
# ---------------------------------------------------------------------------

PROJECTOR_DEFAULT_DISPLAY = "main"

# segment-id prefix -> debater position (1 主辯, 2 一副, 3 二副, 4 結辯).
# Only these single-speaker turns map to a named debater; prep / free-debate /
# cross-exam / 聯中三副 segments have no 1-4 slot and show role text only.
_SEG_POSITION = {"main": 1, "dep1": 2, "dep2": 3, "closing": 4}


def _seg_speaker_slot(seg_id: str):
    for prefix, pos in _SEG_POSITION.items():
        if seg_id.startswith(prefix + "_"):
            if seg_id.endswith("_pro"):
                return ("pro", pos)
            if seg_id.endswith("_con"):
                return ("con", pos)
    return None


def _active_side(seg_side: str):
    if seg_side == "正方":
        return "pro"
    if seg_side == "反方":
        return "con"
    return None  # 雙方 / 準備 — no single active side


def _resolve_projector_state(engine, display_key):
    """Turn the stored row into ready-to-render display JSON (motion, team
    names, current speaking role/name). All resolution happens here so the
    display page can stay dumb and just poll."""
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT match_id, debate_format, seg_index, visible "
                 "FROM projector_state WHERE display_key = :k"),
            {"k": display_key},
        ).fetchone()

    if row is None or not row._mapping.get("match_id"):
        return {"configured": False, "visible": False, "display_key": display_key}

    r = row._mapping
    match_id = r["match_id"]
    debate_format = r.get("debate_format") or "校園隨想"

    with engine.begin() as conn:
        m = conn.execute(
            text("""SELECT topic_text,pro_team,con_team,debate_format,
                           free_debate_minutes
                    FROM matches WHERE match_id=:id"""),
            {"id": match_id},
        ).fetchone()
        drows = conn.execute(
            text("SELECT side, position, debater_name FROM debaters WHERE match_id = :id"),
            {"id": match_id},
        ).fetchall()

    if m is None:
        return {
            "configured": False,
            "visible": False,
            "display_key": display_key,
            "error": "所選正式場次已不存在",
        }

    mm = m._mapping
    official_format = str(mm.get("debate_format") or "")
    if official_format in DEBATE_FORMATS:
        debate_format = official_format
    free_minutes = mm.get("free_debate_minutes")
    try:
        free_minutes = float(free_minutes) if free_minutes is not None else None
    except (TypeError, ValueError, OverflowError):
        free_minutes = None

    names = {(d._mapping["side"], d._mapping["position"]): d._mapping["debater_name"]
             for d in drows}

    seq = get_full_mock_sequence(debate_format, free_minutes)
    total = len(seq)
    idx = r.get("seg_index") or 0
    if total:
        idx = max(0, min(idx, total - 1))
    seg = seq[idx] if total else {"id": "", "label": "", "side": ""}
    slot = _seg_speaker_slot(seg["id"])
    speaker_name = names.get(slot) if slot else None

    return {
        "configured": True,
        "visible": bool(r.get("visible", True)),
        "display_key": display_key,
        "match_id": match_id,
        "motion": (mm.get("topic_text") or ""),
        "pro_team": (mm.get("pro_team") or ""),
        "con_team": (mm.get("con_team") or ""),
        "segment_label": seg["label"],
        "segment_side": seg["side"],
        "active_side": _active_side(seg["side"]),
        "speaker_name": speaker_name or "",
        "seg_index": idx,
        "seg_total": total,
        "debate_format": debate_format,
        "free_debate_minutes": free_minutes,
    }


@app.get("/projector")
async def projector_display_page():
    return FileResponse(BASE_DIR / "templates" / "projector_display.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/projector/control")
async def projector_control_page():
    return FileResponse(BASE_DIR / "templates" / "projector_control.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/api/projector/state")
async def projector_get_state(request: Request):
    display_key = request.query_params.get("display", PROJECTOR_DEFAULT_DISPLAY)
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    return _resolve_projector_state(engine, display_key)


@app.get("/api/projector/sequence")
async def projector_get_sequence(request: Request):
    match_id = str(request.query_params.get("match_id") or "").strip()
    debate_format = request.query_params.get("format", "校園隨想")
    free_minutes = None
    if match_id:
        engine = _get_db_engine()
        if engine is None:
            raise HTTPException(status_code=503, detail="Database is not configured")
        with engine.begin() as conn:
            row = conn.execute(
                text("""SELECT debate_format,free_debate_minutes
                        FROM matches WHERE match_id=:match_id"""),
                {"match_id": match_id},
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="找不到正式場次")
        debate_format = str(row._mapping.get("debate_format") or "校園隨想")
        raw_free = row._mapping.get("free_debate_minutes")
        try:
            free_minutes = float(raw_free) if raw_free is not None else None
        except (TypeError, ValueError, OverflowError):
            free_minutes = None
    if debate_format not in DEBATE_FORMATS:
        debate_format = DEBATE_FORMATS[0]
    seq = get_full_mock_sequence(debate_format, free_minutes)
    return {
        "format": debate_format,
        "free_debate_minutes": free_minutes,
        "segments": [
            {"id": s["id"], "label": s["label"], "side": s["side"]}
            for s in seq
        ],
    }


@app.get("/api/projector/matches")
async def projector_list_matches(request: Request):
    require_competition_staff(request)
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT match_id,match_date,match_time,topic_text,pro_team,con_team,"
            "debate_format,free_debate_minutes FROM matches "
            "ORDER BY CASE WHEN match_date IS NULL THEN 2 ELSE 0 END,"
            "ABS(COALESCE(match_date,CURRENT_DATE)-CURRENT_DATE),"
            "match_time ASC NULLS LAST,match_id ASC "
            "LIMIT :limit"
        ), {"limit": PROJECTOR_MATCH_LIMIT}).fetchall()
    return {"matches": [
        {
            "match_id": x._mapping["match_id"],
            "match_date": str(x._mapping["match_date"]) if x._mapping["match_date"] else "",
            "match_time": str(x._mapping["match_time"]) if x._mapping["match_time"] else "",
            "topic_text": x._mapping["topic_text"] or "",
            "pro_team": x._mapping["pro_team"] or "",
            "con_team": x._mapping["con_team"] or "",
            "debate_format": x._mapping.get("debate_format") or DEBATE_FORMATS[0],
            "free_debate_minutes": (
                float(x._mapping["free_debate_minutes"])
                if x._mapping.get("free_debate_minutes") is not None else None
            ),
        }
        for x in rows
    ]}


@app.post("/api/projector/state")
async def projector_set_state(request: Request):
    require_competition_staff(request)
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    display_key = str(payload.get("display") or PROJECTOR_DEFAULT_DISPLAY).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", display_key):
        raise HTTPException(status_code=400, detail="Invalid display key")
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    with engine.begin() as conn:
        current = conn.execute(
            text("SELECT match_id, debate_format, seg_index, visible "
                 "FROM projector_state WHERE display_key = :k"),
            {"k": display_key},
        ).fetchone()
        cur = current._mapping if current else {}

        # Partial update.  The official match row, never browser input, is the
        # source of truth for format and free-debate duration.
        match_id = payload.get("match_id", cur.get("match_id"))
        match_id = str(match_id or "").strip()
        if not match_id:
            raise HTTPException(status_code=400, detail="請先選擇正式場次")
        match_row = conn.execute(
            text("""SELECT match_id,topic_text,pro_team,con_team,debate_format,
                           free_debate_minutes
                    FROM matches WHERE match_id=:match_id"""),
            {"match_id": match_id},
        ).fetchone()
        if match_row is None:
            raise HTTPException(status_code=404, detail="找不到正式場次")
        match_map = match_row._mapping
        debate_format = str(match_map.get("debate_format") or DEBATE_FORMATS[0])
        if debate_format not in DEBATE_FORMATS:
            raise HTTPException(status_code=409, detail="正式場次賽制設定無效")
        raw_free = match_map.get("free_debate_minutes")
        try:
            free_minutes = float(raw_free) if raw_free is not None else None
        except (TypeError, ValueError, OverflowError):
            free_minutes = None
        sequence = get_full_mock_sequence(debate_format, free_minutes)
        seg_index = payload.get("seg_index", cur.get("seg_index") or 0)
        visible = payload.get("visible", cur.get("visible") if current else True)
        try:
            seg_index = int(seg_index)
        except Exception:
            seg_index = 0
        seg_index = max(0, min(seg_index, max(0, len(sequence) - 1)))
        visible = bool(visible)

        ai_schema_ready = bool(
            conn.execute(text("SELECT to_regclass('public.projector_ai_sessions') IS NOT NULL")).scalar()
        )
        if ai_schema_ready:
            active = conn.execute(
                text("""SELECT match_id FROM projector_ai_sessions
                        WHERE display_key=:display
                          AND status IN ('start_requested','recording','stop_requested','processing')
                        ORDER BY created_at DESC LIMIT 1"""),
                {"display": display_key},
            ).fetchone()
            if active is not None and str(active._mapping.get("match_id") or "") != match_id:
                raise HTTPException(
                    status_code=409,
                    detail="AI評判易進行期間不可轉換正式場次。",
                )

        conn.execute(
            text("""
                INSERT INTO projector_state
                    (display_key, match_id, debate_format, seg_index, visible, updated_at)
                VALUES (:k, :match_id, :debate_format, :seg_index, :visible, :now)
                ON CONFLICT (display_key) DO UPDATE SET
                    match_id = EXCLUDED.match_id,
                    debate_format = EXCLUDED.debate_format,
                    seg_index = EXCLUDED.seg_index,
                    visible = EXCLUDED.visible,
                    updated_at = EXCLUDED.updated_at
            """),
            {"k": display_key, "match_id": match_id, "debate_format": debate_format,
             "seg_index": seg_index, "visible": visible, "now": now},
        )

        old_index = int(cur.get("seg_index") or 0) if current else None
        old_match = str(cur.get("match_id") or "") if current else ""
        if ai_schema_ready and (old_index != seg_index or old_match != match_id):
            from api.projector_ai_api import record_projector_segment_change

            record_projector_segment_change(
                conn,
                display=display_key,
                match={
                    "match_id": match_id,
                    "debate_format": debate_format,
                    "free_debate_minutes": free_minutes,
                },
                seg_index=seg_index,
                now=now,
            )

    return _resolve_projector_state(engine, display_key)


# ---------------------------------------------------------------------------
# Appliance practice page (dedicated kiosk-authenticated hub)
#
# Additive and self-contained, same pattern as the projector above. Serves one
# static big-text page (templates/appliance_practice.html) meant for the
# dedicated-machine 日常練習 mode (PRACTICE_URL). It embeds the chairperson
# 叮叮 timer (all formats) and dedicated kiosk login shell, and links out to the
# existing AI-practice engine. The HTML shell remains reachable so an expired
# browser cookie can display its login form; server-side AI routes require the
# centrally defined kiosk page-access policy.
# ---------------------------------------------------------------------------


@app.get("/practice")
async def appliance_practice_page():
    return FileResponse(BASE_DIR / "templates" / "appliance_practice.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_NO_CACHE))


@app.get("/vote")
async def vote_page():
    """Primary HTML voting page."""
    return FileResponse(BASE_DIR / "frontend" / "vote" / "index.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/")
async def home_page():
    """Primary HTML home page, replacing Streamlit's former default route."""
    return FileResponse(BASE_DIR / "frontend" / "home" / "index.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/open_db")
@app.get("/open-db", include_in_schema=False)
async def open_db_page():
    """Primary HTML public topic bank; hyphenated path remains an alias."""
    return FileResponse(BASE_DIR / "frontend" / "open_db" / "index.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/bug-report")
async def bug_report_page():
    """Primary HTML committee bug-report page."""
    return FileResponse(BASE_DIR / "frontend" / "bug_report" / "index.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/registration")
async def registration_page():
    """Primary public HTML competition registration page."""
    return FileResponse(BASE_DIR / "frontend" / "registration" / "index.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/registration-admin")
@app.get("/registration_admin", include_in_schema=False)
async def registration_admin_page():
    """Primary HTML organiser registration-management page; underscore path is legacy alias."""
    return FileResponse(BASE_DIR / "frontend" / "registration_admin" / "index.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/video-replay")
async def video_replay_page():
    """Primary HTML committee replay page."""
    return FileResponse(BASE_DIR / "frontend" / "video_replay" / "index.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/video-admin")
@app.get("/video_admin", include_in_schema=False)
async def video_admin_page():
    """Primary HTML organiser video-management page; underscore path is legacy alias."""
    return FileResponse(BASE_DIR / "frontend" / "video_admin" / "index.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/match-photos")
async def match_photos_page():
    """Primary HTML committee match-photo gallery."""
    html = (BASE_DIR / "frontend" / "match_photos" / "index.html").read_text(
        encoding="utf-8"
    )
    html = html.replace("__APP_VERSION__", APP_VERSION)
    return Response(
        content=html, media_type="text/html", headers=_cache_headers(CACHE_HTML)
    )


@app.get("/team-roster")
async def team_roster_page():
    return FileResponse(BASE_DIR / "frontend" / "team_roster" / "index.html",
                        media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/match-info")
@app.get("/match_info", include_in_schema=False)
async def match_info_page():
    return FileResponse(BASE_DIR / "frontend" / "match_info" / "index.html",
                        media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/draw-match-schedule")
@app.get("/draw_match_schedule", include_in_schema=False)
async def draw_match_schedule_page():
    return FileResponse(BASE_DIR / "frontend" / "draw_match_schedule" / "index.html",
                        media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/management")
async def management_page():
    return FileResponse(BASE_DIR / "frontend" / "management" / "index.html",
                        media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/judging")
async def judging_page():
    return FileResponse(BASE_DIR / "frontend" / "judging" / "index.html",
                        media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/review")
async def review_page():
    return FileResponse(BASE_DIR / "frontend" / "review" / "index.html",
                        media_type="text/html", headers=_cache_headers(CACHE_HTML))

@app.get("/admin-hub")
async def admin_hub_page():
    return FileResponse(BASE_DIR / "frontend" / "admin_hub" / "index.html", media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/chairperson")
async def chairperson_page():
    return FileResponse(BASE_DIR / "frontend" / "chairperson" / "index.html", media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/ai-coach")
async def ai_coach_page():
    html = (BASE_DIR / "frontend" / "ai_coach" / "index.html").read_text(
        encoding="utf-8"
    )
    html = html.replace("__APP_VERSION__", APP_VERSION)
    return Response(
        content=html, media_type="text/html", headers=_cache_headers(CACHE_HTML)
    )


@app.get("/ai-coach/room/{code}")
async def ai_coach_room_page(code: str, request: Request):
    user_id = require_page_user(request, "ai_room")
    room = ROOMS.get((code or "").upper())
    if not room:
        return _practice_error_page("房間不存在", "房間已結束或不存在。", "/ai-coach")
    if room.phase == "ended" and user_id not in room.members:
        return _practice_error_page("無權查看", "只有原房間成員可查看完場逐字稿。", "/ai-coach")
    html = (BASE_DIR / "templates" / "room_debate.html").read_text(encoding="utf-8")
    html = html.replace("__ROOM_CODE__", json.dumps(room.code))
    html = html.replace("__ROOM_WS_BASE__", json.dumps(_get_proxy_secret("ROOM_WS_BASE", "") or ""))
    html = html.replace("__MODE__", json.dumps(room.mode))
    html = html.replace("__BELL_SRC__", json.dumps(_practice_bell_src()))
    return Response(
        content=html, media_type="text/html", headers=_cache_headers(CACHE_NO_STORE)
    )


@app.get("/ai-training")
async def ai_training_page():
    html = (BASE_DIR / "frontend" / "ai_training" / "index.html").read_text(encoding="utf-8")
    html = html.replace("__APP_VERSION__", APP_VERSION)
    # This shell and app.js share an exact consent/form DOM contract. Force
    # revalidation so a browser holding the 4.2.1 immutable script cannot keep
    # receiving a stale shell during the hotfix's stale-while-revalidate window.
    return Response(content=html, media_type="text/html", headers=_cache_headers(CACHE_NO_CACHE))


@app.get("/ai-training/app.js")
async def ai_training_script():
    return FileResponse(
        BASE_DIR / "frontend" / "ai_training" / "app.js",
        media_type="text/javascript",
        # The script is tightly coupled to consent/form DOM. Revalidate even
        # with a version query so a same-version hotfix cannot leave browsers
        # executing an incompatible immutable script.
        headers=_cache_headers(CACHE_NO_CACHE),
    )


@app.get("/db-mgmt")
@app.get("/db_mgmt", include_in_schema=False)
async def db_management_page():
    return FileResponse(BASE_DIR / "frontend" / "db_mgmt" / "index.html", media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/dev-settings")
@app.get("/dev_settings", include_in_schema=False)
async def developer_settings_page():
    html = (BASE_DIR / "frontend" / "dev_settings" / "index.html").read_text(
        encoding="utf-8"
    )
    html = html.replace("__APP_VERSION__", APP_VERSION)
    return Response(
        content=html, media_type="text/html", headers=_cache_headers(CACHE_HTML)
    )


@app.get("/dev-settings/lateness-managers.js")
async def developer_lateness_managers_script():
    return FileResponse(
        BASE_DIR / "frontend" / "dev_settings" / "lateness-managers.js",
        media_type="application/javascript",
        headers=_cache_headers(CACHE_NO_CACHE),
    )


@app.get("/lateness-fund")
@app.get("/lateness_fund", include_in_schema=False)
async def lateness_fund_page():
    return FileResponse(BASE_DIR / "frontend" / "lateness_fund" / "index.html", media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/ai-fund")
@app.get("/ai_fund", include_in_schema=False)
async def ai_fund_page():
    return FileResponse(BASE_DIR / "frontend" / "ai_fund" / "index.html", media_type="text/html", headers=_cache_headers(CACHE_HTML))


@lru_cache(maxsize=1)
def _practice_bell_version() -> str:
    path = BASE_DIR / "assets" / "bell.mp3"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12] if path.exists() else ""


@app.get("/api/practice/bell")
async def appliance_practice_bell(request: Request):
    version = _practice_bell_version()
    if version and request.query_params.get("v") != version:
        return RedirectResponse(
            url=f"/api/practice/bell?v={version}", status_code=307,
            headers=_cache_headers(CACHE_NO_CACHE),
        )
    return FileResponse(
        BASE_DIR / "assets" / "bell.mp3", media_type="audio/mpeg",
        headers=_binary_cache_headers(CACHE_BELL),
    )


@app.get("/api/practice/timer-config")
async def appliance_practice_timer_config(request: Request):
    """Serve the chairperson bell/timer schedule for a format so the static
    practice page can render the same 叮叮 timer without a Streamlit session.
    Pure computation (no DB, no auth)."""
    debate_format = request.query_params.get("format", DEBATE_FORMATS[0])
    if debate_format not in DEBATE_FORMATS:
        debate_format = DEBATE_FORMATS[0]

    def _opt_float(name):
        raw = request.query_params.get(name)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    free_minutes = _opt_float("free_minutes")
    prep_minutes = _opt_float("closing_prep_minutes")
    if free_minutes is not None:
        free_minutes = min(float(LIVE_FREE_MAX_MINUTES), max(2.0, free_minutes))
    if prep_minutes is not None:
        prep_minutes = min(10.0, max(0.5, prep_minutes))
    config = get_debate_timer_config(
        debate_format,
        free_debate_minutes=free_minutes,
        closing_prep_minutes=prep_minutes,
    )
    return {"format": debate_format, "formats": DEBATE_FORMATS, **config}


# ---------------------------------------------------------------------------
# Shared appliance / AI Coach practice renderer (direct browser Live)
#
# The kiosk 練習頁 and ordinary /ai-coach page both link here. They reuse the
# same Gemini Live engine (templates/live_debate.html), prompt builder and
# Start-time token endpoint. Access follows the central AI Coach account policy
# (ordinary members plus the dedicated kiosk identity), while the kiosk setup
# page and all other kiosk APIs remain kiosk-only. Token minting is rate-limited
# per authenticated user so neither entry point can burn AI budget unchecked.
# ---------------------------------------------------------------------------

# Backwards-compatible alias; the model choice itself is centrally managed.
FREE_DEBATE_LIVE_MODEL = GEMINI_LIVE_MODEL

# Only formats with a free-debate segment are offered for standalone Free De.
_PRACTICE_LIVE_FORMATS = list(FREE_DEBATE_FORMATS)

# In-process rate limit for token minting, keyed by authenticated AI Coach user.
# Persistent daily/weekly/monthly quotas remain authoritative across restarts.
_practice_live_hits: dict = {}
_PRACTICE_LIVE_MAX_PER_HOUR = PRACTICE_LIVE_MAX_PER_HOUR
_PRACTICE_LIVE_MIN_GAP_SEC = PRACTICE_LIVE_MIN_GAP_SECONDS
SOLO_LIVE_TOKEN_ISSUE_LOCK = asyncio.Lock()
_solo_live_token_response_cache: dict[
    tuple[str, str, int, str], tuple[str, float]
] = {}
_solo_live_token_response_cache_lock = threading.Lock()
_bandwidth_last_prune = None
_bandwidth_prune_lock = threading.Lock()


def _solo_live_claim_digest(claim: dict) -> str:
    """Bind an ephemeral-token retry entry to its canonical Live constraints."""
    canonical = {
        "mode": str(claim.get("mode") or ""),
        "session_seconds": [
            int(value) for value in (claim.get("session_seconds") or [])
        ],
        "system_prompt": str(claim.get("system_prompt") or ""),
    }
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _solo_live_token_cache_key(
    claim: dict, session_index: int,
) -> tuple[str, str, int, str]:
    return (
        str(claim.get("user_id") or ""),
        str(claim.get("practice_id") or ""),
        int(session_index),
        _solo_live_claim_digest(claim),
    )


def _prune_solo_live_token_response_cache(now: float | None = None) -> None:
    current = time.monotonic() if now is None else float(now)
    for key, (_token, expires_at) in list(_solo_live_token_response_cache.items()):
        if expires_at <= current:
            _solo_live_token_response_cache.pop(key, None)
    while len(_solo_live_token_response_cache) > LIVE_TOKEN_RESPONSE_CACHE_MAX_ENTRIES:
        oldest = min(
            _solo_live_token_response_cache,
            key=lambda key: _solo_live_token_response_cache[key][1],
        )
        _solo_live_token_response_cache.pop(oldest, None)


def _get_cached_solo_live_token(claim: dict, session_index: int) -> str:
    with _solo_live_token_response_cache_lock:
        _prune_solo_live_token_response_cache()
        entry = _solo_live_token_response_cache.get(
            _solo_live_token_cache_key(claim, session_index),
        )
        return str(entry[0]) if entry else ""


def _cache_solo_live_token(
    claim: dict, session_index: int, token: str, *, ttl_seconds: float | None = None,
) -> None:
    """Cache only while the provider's one-minute start window is usable."""
    ttl = (
        float(LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS)
        if ttl_seconds is None else float(ttl_seconds)
    )
    if not token or ttl <= 0:
        return
    with _solo_live_token_response_cache_lock:
        _prune_solo_live_token_response_cache()
        key = _solo_live_token_cache_key(claim, session_index)
        _solo_live_token_response_cache[key] = (
            str(token),
            time.monotonic() + min(
                ttl, float(LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS),
            ),
        )
        _prune_solo_live_token_response_cache()


def _clear_solo_live_token_response_cache() -> None:
    """Test/restart helper; cached values are ephemeral tokens, never API keys."""
    with _solo_live_token_response_cache_lock:
        _solo_live_token_response_cache.clear()

SOLO_LIMIT_MESSAGE = (
    f"為控制 Gemini Live 用量並確保所有委員均能使用服務，每位委員每日最多可進行"
    f"{SOLO_FREE_DAILY_LIMIT}次單人自由辯論，並且每星期最多可進行"
    f"{SOLO_MOCK_WEEKLY_LIMIT}次單人完整模擬練習。"
    "你已使用此類別的練習限額，請於下一個限額週期再試。"
)
GLOBAL_LIVE_LIMIT_MESSAGE = (
    "本月全系統的 Gemini Live 練習名額已用完。"
    "為確保一般系統功能維持正常，請於下月再試或聯絡系統管理員。"
)
SOLO_HK_COUNTRY_MESSAGE = (
    "香港網絡暫時無法直接連接 Google Gemini Live。請先連接至 Google 支援地區"
    "網絡／VPN，再按「重新檢查」。"
)
BANDWIDTH_STOP_MESSAGE = (
    f"由於本月全系統網絡傳輸量已達{BANDWIDTH_STOP_LIVE_BYTES / 1_000_000_000:g}GB"
    "預算上限，系統已停止Solo server TTS及新的聯機Live房間。"
    "Solo瀏覽器直連Gemini、一般功能、R2媒體及管理功能維持正常。"
)
BANDWIDTH_ESSENTIAL_MESSAGE = (
    f"由於本月全系統網絡傳輸量已達{BANDWIDTH_ESSENTIAL_ONLY_BYTES / 1_000_000_000:g}GB"
    "保護上限，本功能暫停使用。"
    "目前只保留一般HTML、JSON、R2媒體及管理功能。"
)


def _solo_live_country_status(request: Request) -> dict:
    """Use only Cloudflare's country code as a non-persistent Solo UX gate."""
    raw = str(request.headers.get("CF-IPCountry") or "").strip().upper()
    app_env = str(os.getenv("APP_ENV") or "").strip().lower()
    service_name = str(os.getenv("RENDER_SERVICE_NAME") or "").strip().lower()
    render_marker = bool(
        str(os.getenv("RENDER") or "").strip()
        or str(os.getenv("RENDER_SERVICE_ID") or "").strip()
        or str(os.getenv("RENDER_EXTERNAL_HOSTNAME") or "").strip()
        or str(os.getenv("EXTERNAL_HOSTNAME") or "").strip()
    )
    production_marker = (
        render_marker
        or app_env in {"production", "prod"}
        or "production" in service_name
        or service_name.endswith("-prod")
    )
    explicit_nonproduction = (
        not production_marker
        and (
            app_env in {"local", "development", "dev", "test", "testing", "staging"}
            or "staging" in service_name
            or (not app_env and not service_name and not render_marker)
        )
    )
    if not raw:
        # Host is attacker-controlled on a direct origin request.  Only an
        # explicit local/staging runtime marker may relax a missing CF header.
        if not explicit_nonproduction:
            return {
                "code": "", "status": "blocked", "supported": False,
                "message": "未能核實網絡地區，暫時不會簽發 Solo Live token。請重新檢查或聯絡管理員。",
            }
        return {
            "code": "", "status": "unknown", "supported": True,
            "message": "未收到地區資料；本地／測試環境會允許嘗試直接連線。",
        }
    if raw in {"XX", "T1"} or not re.fullmatch(r"[A-Z]{2}", raw):
        if production_marker:
            return {
                "code": "", "status": "blocked", "supported": False,
                "message": "未能核實網絡地區，暫時不會簽發 Solo Live token。請重新檢查或聯絡管理員。",
            }
        return {
            "code": "", "status": "unknown", "supported": True,
            "message": "地區資料不明，系統會允許嘗試直接連接 Google。",
        }
    if raw == "HK":
        return {
            "code": "HK", "status": "blocked", "supported": False,
            "message": SOLO_HK_COUNTRY_MESSAGE,
        }
    return {"code": raw, "status": "supported", "supported": True, "message": ""}


def _bandwidth_month_context(now: datetime.datetime | None = None):
    hk = ZoneInfo("Asia/Hong_Kong")
    if now is None:
        now_hk = datetime.datetime.now(hk)
    elif now.tzinfo is None:
        now_hk = now.replace(tzinfo=hk)
    else:
        now_hk = now.astimezone(hk)
    start_hk = now_hk.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        now_hk.strftime("%Y-%m"),
        start_hk.astimezone(datetime.timezone.utc).replace(tzinfo=None),
    )


def _bandwidth_write_context(now: datetime.datetime | None = None):
    """Return one UTC write timestamp and its matching Hong Kong month start."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    now_utc = now.astimezone(datetime.timezone.utc)
    return now_utc.replace(tzinfo=None), _bandwidth_month_context(now_utc)[1]


def bandwidth_budget_status(*, notify: bool = False) -> dict:
    """Return tracked high-bandwidth egress plus an optional Render baseline."""
    engine = _get_db_engine()
    period, start_utc = _bandwidth_month_context()
    tracked = 0
    if engine is not None:
        with engine.begin() as conn:
            tracked = int(conn.execute(text(f"""SELECT COALESCE(SUM(bytes_out),0)
                FROM {TABLE_BANDWIDTH_USAGE_LOGS} WHERE created_at>=:start"""),
                {"start": start_utc}).scalar() or 0)
    try:
        baseline = max(0, int(_get_proxy_secret("BANDWIDTH_MONTH_BASE_BYTES", "0") or 0))
    except ValueError:
        baseline = 0
    baseline_as_of = _get_proxy_secret("BANDWIDTH_BASELINE_AS_OF", "").strip()
    tracked_snapshot_raw = _get_proxy_secret("BANDWIDTH_BASELINE_TRACKED_BYTES", "").strip()
    baseline_snapshot_ready = bool(baseline_as_of and tracked_snapshot_raw)
    try:
        tracked_snapshot = max(0, int(tracked_snapshot_raw or 0))
    except ValueError:
        tracked_snapshot = 0
        baseline_snapshot_ready = False
    baseline_period_ok = True
    if baseline_as_of:
        try:
            parsed_as_of = datetime.datetime.fromisoformat(baseline_as_of)
            if parsed_as_of.tzinfo is None:
                parsed_as_of = parsed_as_of.replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
            baseline_period_ok = (
                parsed_as_of.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m") == period
            )
        except ValueError:
            baseline_period_ok = False
    if baseline and baseline_snapshot_ready and baseline_period_ok:
        tracked_after_baseline = max(0, tracked - tracked_snapshot)
    else:
        # Backward-compatible and deliberately conservative: an incomplete
        # snapshot may double count, but can never silently under-enforce.
        tracked_after_baseline = tracked
    effective_baseline = baseline if baseline_period_ok else 0
    total = effective_baseline + tracked_after_baseline
    stage = 4 if total >= BANDWIDTH_ESSENTIAL_ONLY_BYTES else 3.5 if total >= BANDWIDTH_STOP_LIVE_BYTES else 3 if total >= BANDWIDTH_WARN_BYTES else 0
    status = {
        "period": period, "baseline_bytes": effective_baseline,
        "baseline_as_of": baseline_as_of,
        "baseline_tracked_snapshot_bytes": tracked_snapshot,
        "baseline_snapshot_ready": baseline_snapshot_ready and baseline_period_ok,
        "tracked_bytes": tracked, "tracked_after_baseline_bytes": tracked_after_baseline,
        "total_bytes": total, "stage": stage,
        "warn_bytes": BANDWIDTH_WARN_BYTES,
        "stop_live_bytes": BANDWIDTH_STOP_LIVE_BYTES,
        "essential_only_bytes": BANDWIDTH_ESSENTIAL_ONLY_BYTES,
    }
    if notify:
        _send_bandwidth_warning_once(status)
    return status


def _send_bandwidth_warning_once(status: dict) -> None:
    if status["total_bytes"] < BANDWIDTH_WARN_BYTES:
        return
    engine = _get_db_engine()
    if engine is None:
        return
    marker = f"bandwidth_3gb_push_sent:{status['period']}"
    claimed = False
    now = datetime.datetime.now(datetime.timezone.utc)
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('bandwidth_3gb_push'))"))
        existing = get_configs_from_connection(conn, (marker,))
        if marker not in existing:
            set_configs_on_connection(conn, {
                marker: status["total_bytes"],
                "bandwidth_developer_warning": status,
            }, updated_at=now)
            claimed = True
    if not claimed:
        return
    logger.warning("Monthly bandwidth warning reached: %s", status)
    try:
        from core.push import notify_committee
        notify_committee(
            get_vote_db(), _get_vapid(), "⚠️ 系統網絡傳輸量提示",
            f"本月全系統網絡傳輸量已達{BANDWIDTH_WARN_BYTES / 1_000_000_000:g}GB。"
            f"為控制營運預算，達{BANDWIDTH_STOP_LIVE_BYTES / 1_000_000_000:g}GB後"
            "將停止Solo server TTS及新的聯機Live房間；Solo browser直連仍可使用。",
            tag=f"bandwidth-warning-{status['period']}", url="/",
        )
    except Exception:
        logger.exception("Failed to send committee bandwidth warning")


def record_bandwidth_usage(
    source: str, byte_count: int, user_id: str = "", details: str = "",
    *, aggregate_key: str = "",
) -> bool:
    global _bandwidth_last_prune
    count = max(0, int(byte_count or 0))
    if not count:
        return True
    engine = _get_db_engine()
    if engine is None:
        return False
    now, period_start = _bandwidth_write_context()
    source = str(source)[:80]
    user = str(user_id or "")[:200]
    details = str(details or "")[:500]
    aggregate_key = str(aggregate_key or "")[:400]
    with engine.begin() as conn:
        params = {
            "source": source, "user": user, "insert_user": user or None,
            "bytes": count, "details": details, "now": now,
            "period_start": period_start,
        }
        if aggregate_key:
            # A 30-second checkpoint must survive a Render crash, but one row per
            # checkpoint would turn a bandwidth safeguard into a storage leak.
            # Keep one accumulating row per live session during the current month.
            params["details"] = aggregate_key
            lock_key = f"bandwidth:{source}:{user}:{aggregate_key}"
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                         {"lock_key": lock_key})
            updated = conn.execute(text(f"""UPDATE {TABLE_BANDWIDTH_USAGE_LOGS}
                SET bytes_out=bytes_out+:bytes
                WHERE id=(SELECT id FROM {TABLE_BANDWIDTH_USAGE_LOGS}
                    WHERE source=:source AND COALESCE(user_id,'')=:user
                      AND details=:details AND created_at>=:period_start
                    ORDER BY id DESC LIMIT 1)"""), params)
            if updated.rowcount:
                params = None
        if params is not None:
            conn.execute(text(f"""INSERT INTO {TABLE_BANDWIDTH_USAGE_LOGS}
                (source,user_id,bytes_out,details,created_at)
                VALUES(:source,:insert_user,:bytes,:details,:now)"""), params)
    monotonic_now = time.monotonic()
    if (
        _bandwidth_last_prune is None
        or monotonic_now - _bandwidth_last_prune >= MAINTENANCE_PRUNE_INTERVAL_SECONDS
    ):
        with _bandwidth_prune_lock:
            if (
                _bandwidth_last_prune is None
                or monotonic_now - _bandwidth_last_prune >= MAINTENANCE_PRUNE_INTERVAL_SECONDS
            ):
                try:
                    with engine.begin() as conn:
                        conn.execute(text(
                            f"DELETE FROM {TABLE_BANDWIDTH_USAGE_LOGS} WHERE created_at<:cutoff"
                        ), {"cutoff": now - datetime.timedelta(days=BANDWIDTH_LOG_RETENTION_DAYS)})
                except Exception:
                    logger.exception("Bandwidth retention maintenance failed")
                _bandwidth_last_prune = monotonic_now
    try:
        _send_bandwidth_warning_once(bandwidth_budget_status())
    except Exception:
        logger.exception("Bandwidth usage was recorded but warning delivery failed")
    return True


def _bandwidth_live_gate_error() -> str | None:
    status = bandwidth_budget_status(notify=True)
    return BANDWIDTH_STOP_MESSAGE if status["total_bytes"] >= BANDWIDTH_STOP_LIVE_BYTES else None


def _bandwidth_essential_gate_error() -> str | None:
    status = bandwidth_budget_status(notify=True)
    return BANDWIDTH_ESSENTIAL_MESSAGE if status["total_bytes"] >= BANDWIDTH_ESSENTIAL_ONLY_BYTES else None


def _practice_live_rate_check(user_id: str):
    """Return an error message if this user is minting too fast, else None."""
    now = time.time()
    # A restarted worker naturally clears this local throttle.  While it stays
    # alive, prune every user's expired bucket so old committee IDs cannot turn
    # the dict into an unbounded process-lifetime cache.
    for key, values in list(_practice_live_hits.items()):
        recent = [timestamp for timestamp in values if now - timestamp < PRACTICE_LIVE_RATE_WINDOW_SECONDS]
        if recent:
            _practice_live_hits[key] = recent
        else:
            _practice_live_hits.pop(key, None)
    hits = [t for t in _practice_live_hits.get(user_id, []) if now - t < PRACTICE_LIVE_RATE_WINDOW_SECONDS]
    if hits and now - hits[-1] < _PRACTICE_LIVE_MIN_GAP_SEC:
        return "太快喇，請等幾秒再開始。"
    if len(hits) >= _PRACTICE_LIVE_MAX_PER_HOUR:
        return "練習次數已達每小時上限，請稍後再試。"
    hits.append(now)
    _practice_live_hits[user_id] = hits
    return None


def _solo_quota_boundaries(now_hk: datetime.datetime, is_mock: bool):
    """Return UTC-naive boundaries for the existing Live usage ledger."""
    month_start_hk = now_hk.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    user_start_hk = (
        (now_hk - datetime.timedelta(days=now_hk.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        if is_mock else now_hk.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    return (
        user_start_hk.astimezone(datetime.timezone.utc).replace(tzinfo=None),
        month_start_hk.astimezone(datetime.timezone.utc).replace(tzinfo=None),
    )


def _solo_quota_exempt(
    exemptions, user_id: str, mode: str,
    *, now_utc: datetime.datetime | None = None,
) -> bool:
    """Return a developer-granted, still-active per-user exemption.

    Stored timestamps must be timezone-aware.  This helper never affects the
    global monthly quota or any auth/country/bandwidth/rate safety gate.
    """
    if not isinstance(exemptions, dict):
        return False
    entry = exemptions.get(str(user_id))
    if not isinstance(entry, dict):
        return False
    allowed_mode = str(entry.get("mode") or "")
    if allowed_mode not in ("all", mode):
        return False
    try:
        expiry = datetime.datetime.fromisoformat(
            str(entry.get("expires_at") or "").replace("Z", "+00:00"),
        )
    except (TypeError, ValueError):
        return False
    if expiry.tzinfo is None:
        return False
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    now = now.astimezone(datetime.timezone.utc)
    expiry = expiry.astimezone(datetime.timezone.utc)
    return now < expiry <= now + datetime.timedelta(days=30)


def _solo_live_quota_error(user_id: str, mode: str) -> str | None:
    """Enforce persistent per-user and global quotas before minting Live tokens."""
    engine = _get_db_engine()
    if engine is None:
        return "Database is not configured"
    now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
    is_mock = mode == "mock"
    feature = "full_mock_live" if is_mock else "free_debate_live"
    user_start, month_start = _solo_quota_boundaries(now_hk, is_mock)
    user_limit = SOLO_MOCK_WEEKLY_LIMIT if is_mock else SOLO_FREE_DAILY_LIMIT
    global_limit = SOLO_MOCK_MONTHLY_LIMIT if is_mock else SOLO_FREE_MONTHLY_LIMIT
    with engine.begin() as conn:
        exemptions = get_configs_from_connection(
            conn, ("solo_quota_exemptions",),
        ).get("solo_quota_exemptions") or {}
        exempt = _solo_quota_exempt(
            exemptions, user_id, mode,
            now_utc=now_hk.astimezone(datetime.timezone.utc),
        )
        user_count = 0 if exempt else int(conn.execute(text(f"""SELECT COUNT(*) FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE user_id=:user AND feature=:feature AND status='success' AND created_at>=:start"""),
            {"user": user_id, "feature": feature, "start": user_start}).scalar() or 0)
        global_count = int(conn.execute(text(f"""SELECT COUNT(*) FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE feature=:feature AND status='success' AND created_at>=:start"""),
            {"feature": feature, "start": month_start}).scalar() or 0)
    if user_count >= user_limit:
        return SOLO_LIMIT_MESSAGE
    if global_count >= global_limit:
        return GLOBAL_LIVE_LIMIT_MESSAGE
    return None


def _solo_live_lifecycle_seconds(claim: dict) -> int:
    """Return the one server-authoritative wall-clock budget for a practice."""
    if str(claim.get("mode") or "") == "free":
        return LIVE_FREE_SESSION_MAX_SECONDS
    return (
        sum(int(value) for value in (claim.get("session_seconds") or []))
        + LIVE_MOCK_OVERALL_GRACE_SECONDS
    )


def _solo_live_ledger_marker(
    claim: dict, started_at: int, deadline_at: int, issued: set[int],
    *, last_issued_at: int,
) -> str:
    practice_id = str(claim.get("practice_id") or "")
    prefix = f"direct_practice:{practice_id}"[:450]
    tokens = ",".join(str(index) for index in sorted(issued))
    return (
        f"{prefix}|claim={_solo_live_claim_digest(claim)}"
        f"|started={int(started_at)}|deadline={int(deadline_at)}"
        f"|last_issued={int(last_issued_at)}|tokens={tokens}"
    )


def _parse_solo_live_ledger_state(claim: dict, value: str) -> dict | None:
    """Parse and authenticate state kept in the existing usage-log marker."""
    practice_id = str(claim.get("practice_id") or "")
    prefix = f"direct_practice:{practice_id}"[:450]
    raw = str(value or "")
    if not raw.startswith(prefix + "|"):
        return None
    fields = {}
    for part in raw[len(prefix) + 1:].split("|"):
        key, separator, item = part.partition("=")
        if separator and key not in fields:
            fields[key] = item
    try:
        started_at = int(fields.get("started") or 0)
        deadline_at = int(fields.get("deadline") or 0)
        last_issued_at = int(fields.get("last_issued") or 0)
        token_text = fields.get("tokens", "")
        if token_text and any(not item.isdigit() for item in token_text.split(",")):
            return None
    except (TypeError, ValueError):
        return None
    issued = {
        int(item) for item in fields.get("tokens", "").split(",")
        if item.isdigit()
    }
    if (
        started_at <= 0
        or deadline_at <= started_at
        or last_issued_at < started_at
        or last_issued_at >= deadline_at
    ):
        return None
    digest = str(fields.get("claim") or "")
    return {
        "started_at": started_at,
        "deadline_at": deadline_at,
        "last_issued_at": last_issued_at,
        "issued": issued,
        "lifecycle_matches": (
            deadline_at - started_at == _solo_live_lifecycle_seconds(claim)
        ),
        "claim_matches": bool(digest) and hmac.compare_digest(
            digest, _solo_live_claim_digest(claim),
        ),
    }


def _solo_live_practice_state(claim: dict) -> dict | None:
    engine = _get_db_engine()
    if engine is None:
        return None
    user_id = str(claim.get("user_id") or "")
    practice_id = str(claim.get("practice_id") or "")
    feature = "full_mock_live" if claim.get("mode") == "mock" else "free_debate_live"
    marker = f"direct_practice:{practice_id}"[:450]
    with engine.begin() as conn:
        row = conn.execute(text(f"""SELECT error_message FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE user_id=:user AND feature=:feature AND status='success'
              AND LEFT(error_message,LENGTH(:marker))=:marker LIMIT 1"""), {
            "user": user_id, "feature": feature, "marker": marker,
        }).fetchone()
    return _parse_solo_live_ledger_state(claim, row[0]) if row else None


def _solo_live_gate_from_state(
    claim: dict, session_index: int, state: dict | None, *, now_epoch: int,
) -> str | None:
    """Authorize only the next due Mock section inside one absolute deadline."""
    if not state:
        return "Mock初始練習尚未完成配額預留，請返回重新開始。"
    if not state.get("claim_matches"):
        return "練習憑證與已預留狀態不一致，請返回 AI Coach 重新開始。"
    if not state.get("lifecycle_matches"):
        return "練習時限狀態不一致，請返回 AI Coach 重新開始。"
    issued = set(state.get("issued") or set())
    index = int(session_index)
    sessions = [int(value) for value in (claim.get("session_seconds") or [])]
    if index < 0 or index >= len(sessions):
        return "Mock環節編號與已預留狀態不一致，請返回重新開始。"
    deadline_at = int(state.get("deadline_at") or 0)
    required_remaining = (
        sessions[index]
        + LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS
        + LIVE_TOKEN_EXPIRY_GRACE_SECONDS
    )
    if now_epoch + required_remaining > deadline_at:
        return "整場 Mock 已達伺服器時限，剩餘時間不足以安全完成下一環節。"
    if index in issued:
        return "這一節連線憑證已簽發，但安全重試時限已過。請返回 AI Coach 重新開始練習。"
    expected_issued = set(range(len(issued)))
    if issued != expected_issued or index != len(issued):
        return "Mock環節必須按伺服器記錄的次序逐節進行。"
    scheduled_not_before = int(state.get("started_at") or 0) + sum(
        sessions[:index]
    )
    chained_not_before = int(state.get("last_issued_at") or 0)
    if index > 0:
        chained_not_before += sessions[index - 1]
    not_before = max(scheduled_not_before, chained_not_before)
    if index > 0 and now_epoch < not_before:
        remaining = max(1, not_before - now_epoch)
        return f"下一節尚未到可簽發時間，請約{remaining}秒後再試。"
    return None


def _solo_live_token_gate(
    claim: dict, session_index: int, *, now_epoch: int | None = None,
) -> tuple[dict | None, str | None]:
    state = _solo_live_practice_state(claim)
    error = _solo_live_gate_from_state(
        claim, session_index, state,
        now_epoch=int(time.time()) if now_epoch is None else int(now_epoch),
    )
    return state, error


def _set_solo_live_ledger_timeouts(conn) -> None:
    """Bound every blocking DB step that follows a provider token mint."""
    timeout = f"{LIVE_TOKEN_LEDGER_DB_TIMEOUT_SECONDS * 1000}ms"
    conn.execute(
        text("SELECT set_config('lock_timeout', :timeout, TRUE)"),
        {"timeout": timeout},
    )
    conn.execute(
        text("SELECT set_config('statement_timeout', :timeout, TRUE)"),
        {"timeout": timeout},
    )


def _reserve_solo_live_slot(
    claim: dict, *, report_created: bool = False, started_at: int | None = None,
    before_insert=None, after_insert=None,
) -> str | None | tuple[str | None, bool]:
    """Atomically reserve one direct Solo practice before token disclosure.

    All Mock sections share ``practice_id`` and therefore consume one quota row.
    ``report_created`` lets the Start-time endpoint distinguish a new row from
    an existing cross-worker winner.  When supplied, ``before_insert`` runs
    only after the advisory lock and quota checks, so race losers never mint.
    """
    def result(error: str | None, created: bool):
        return (error, created) if report_created else error

    engine = _get_db_engine()
    if engine is None:
        return result("Database is not configured", False)
    user_id = str(claim.get("user_id") or "")
    mode = str(claim.get("mode") or "")
    practice_id = str(claim.get("practice_id") or "")
    if not user_id or mode not in ("free", "mock") or not practice_id:
        return result("練習授權資料無效，請返回重新開始。", False)
    is_mock = mode == "mock"
    feature = "full_mock_live" if is_mock else "free_debate_live"
    user_limit = SOLO_MOCK_WEEKLY_LIMIT if is_mock else SOLO_FREE_DAILY_LIMIT
    global_limit = SOLO_MOCK_MONTHLY_LIMIT if is_mock else SOLO_FREE_MONTHLY_LIMIT
    marker = f"direct_practice:{practice_id}"[:450]
    lifecycle_started_at = int(time.time()) if started_at is None else int(started_at)
    lifecycle_deadline_at = lifecycle_started_at + _solo_live_lifecycle_seconds(claim)
    duration_seconds = sum(int(value) for value in (claim.get("session_seconds") or []))
    duration_minutes = max(0.5, duration_seconds / 60)
    with engine.begin() as conn:
        _set_solo_live_ledger_timeouts(conn)
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('solo_live_quota'))"))
        now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
        user_start, month_start = _solo_quota_boundaries(now_hk, is_mock)
        now_utc = now_hk.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        already = conn.execute(text(f"""SELECT 1 FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE user_id=:user AND feature=:feature AND status='success'
              AND LEFT(error_message,LENGTH(:marker))=:marker LIMIT 1"""), {
            "user": user_id, "feature": feature, "marker": marker,
        }).fetchone()
        if already:
            return result(None, False)
        exemptions = get_configs_from_connection(
            conn, ("solo_quota_exemptions",),
        ).get("solo_quota_exemptions") or {}
        exempt = _solo_quota_exempt(
            exemptions, user_id, mode,
            now_utc=now_hk.astimezone(datetime.timezone.utc),
        )
        user_count = 0 if exempt else int(conn.execute(text(f"""SELECT COUNT(*) FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE user_id=:user AND feature=:feature AND status='success'
              AND created_at>=:start"""), {
            "user": user_id, "feature": feature, "start": user_start,
        }).scalar() or 0)
        if user_count >= user_limit:
            return result(SOLO_LIMIT_MESSAGE, False)
        global_count = int(conn.execute(text(f"""SELECT COUNT(*) FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE feature=:feature AND status='success' AND created_at>=:start"""), {
            "feature": feature, "start": month_start,
        }).scalar() or 0)
        if global_count >= global_limit:
            return result(GLOBAL_LIVE_LIMIT_MESSAGE, False)
        if before_insert is not None:
            provision_error = before_insert()
            if provision_error:
                return result(str(provision_error), False)
        # The deadline is Start-anchored, but section cadence begins only once
        # the provider token has actually returned under this advisory lock.
        lifecycle_issued_at = int(time.time())
        issued_marker = _solo_live_ledger_marker(
            claim, lifecycle_started_at, lifecycle_deadline_at, {0},
            last_issued_at=lifecycle_issued_at,
        )
        conn.execute(text(f"""INSERT INTO {TABLE_AI_FUND_USAGE_LOGS}
            (user_id,feature,model_label,provider,estimated_cost_usd,
             estimated_cost_hkd,input_tokens,output_tokens,audio_tokens,
            search_calls,cost_source,status,error_message,created_at)
            VALUES(:user,:feature,:model_label,:provider,:usd,:hkd,0,0,
                   :audio,0,'gemini_live_token_reservation','success',:marker,:now)"""), {
            "user": user_id, "feature": feature,
            "model_label": GEMINI_LIVE_MODEL_LABEL,
            "provider": GEMINI_LIVE_PROVIDER,
            "usd": round(duration_minutes * 0.01, 4),
            "hkd": round(duration_minutes * 0.078, 4),
            "audio": int(duration_minutes * 60 * 25),
            "marker": issued_marker, "now": now_utc,
        })
        if after_insert is not None:
            delivery_error = after_insert()
            if delivery_error:
                conn.rollback()
                return result(str(delivery_error), False)
    return result(None, True)


def _solo_live_practice_exists(claim: dict) -> bool:
    """Detect an existing reservation by signed practice identity only.

    The GET launch claim intentionally has no prompt/session plan, so its
    digest cannot match the enriched claim stored after Start.  Identity-only
    lookup is used solely to block reload/remint; JIT authorization remains
    bound to the full authenticated digest in ``_solo_live_practice_state``.
    """
    engine = _get_db_engine()
    if engine is None:
        return False
    user_id = str(claim.get("user_id") or "")
    practice_id = str(claim.get("practice_id") or "")
    mode = str(claim.get("mode") or "")
    if not user_id or not practice_id or mode not in ("free", "mock"):
        return False
    feature = "full_mock_live" if mode == "mock" else "free_debate_live"
    marker = f"direct_practice:{practice_id}"[:450]
    with engine.begin() as conn:
        row = conn.execute(text(f"""SELECT 1 FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE user_id=:user AND feature=:feature AND status='success'
              AND LEFT(error_message,LENGTH(:marker))=:marker LIMIT 1"""), {
            "user": user_id, "feature": feature, "marker": marker,
        }).fetchone()
    return bool(row)


def _solo_live_practice_reserved(claim: dict) -> bool:
    """Check that the initial Start-time token reservation exists."""
    state = _solo_live_practice_state(claim)
    return bool(
        state and state.get("claim_matches") and state.get("lifecycle_matches")
        and 0 in state.get("issued", set())
    )


def _solo_live_token_issued(claim: dict, session_index: int) -> bool:
    """Persistently detect duplicate initial/JIT token disclosure."""
    state = _solo_live_practice_state(claim)
    return bool(
        state and state.get("claim_matches")
        and int(session_index) in state.get("issued", set())
    )


def _mark_solo_live_token_issued(
    claim: dict, session_index: int, *, report_reason: bool = False,
    before_update=None, after_update=None,
) -> bool | tuple[bool, str | None, dict | None]:
    """Atomically append one disclosed Mock section to its quota ledger marker."""
    def result(ok: bool, error: str | None, state: dict | None):
        return (ok, error, state) if report_reason else ok

    engine = _get_db_engine()
    if engine is None:
        return result(False, "Database is not configured", None)
    user_id = str(claim.get("user_id") or "")
    practice_id = str(claim.get("practice_id") or "")
    feature = "full_mock_live" if claim.get("mode") == "mock" else "free_debate_live"
    marker = f"direct_practice:{practice_id}"[:450]
    with engine.begin() as conn:
        _set_solo_live_ledger_timeouts(conn)
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('solo_live_token_issue'))"))
        row = conn.execute(text(f"""SELECT error_message FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE user_id=:user AND feature=:feature AND status='success'
              AND LEFT(error_message,LENGTH(:marker))=:marker LIMIT 1"""), {
            "user": user_id, "feature": feature, "marker": marker,
        }).fetchone()
        if not row:
            return result(False, "Mock初始練習尚未完成配額預留，請返回重新開始。", None)
        state = _parse_solo_live_ledger_state(claim, row[0])
        now_epoch = int(time.time())
        gate_error = _solo_live_gate_from_state(
            claim, session_index, state, now_epoch=now_epoch,
        )
        if gate_error:
            return result(False, gate_error, state)
        if before_update is not None:
            provision_error = before_update()
            if provision_error:
                return result(False, str(provision_error), state)
            now_epoch = int(time.time())
            gate_error = _solo_live_gate_from_state(
                claim, session_index, state, now_epoch=now_epoch,
            )
            if gate_error:
                return result(False, gate_error, state)
        issued = set(state["issued"])
        index = int(session_index)
        issued.add(index)
        new_marker = _solo_live_ledger_marker(
            claim, state["started_at"], state["deadline_at"], issued,
            last_issued_at=now_epoch,
        )
        conn.execute(text(f"""UPDATE {TABLE_AI_FUND_USAGE_LOGS}
            SET error_message=:new_marker
            WHERE user_id=:user AND feature=:feature AND status='success'
              AND LEFT(error_message,LENGTH(:marker))=:marker"""), {
            "new_marker": new_marker, "user": user_id,
            "feature": feature, "marker": marker,
        })
        if after_update is not None:
            delivery_error = after_update()
            if delivery_error:
                conn.rollback()
                return result(False, str(delivery_error), state)
        state = {**state, "issued": issued}
    state = {**state, "issued": issued, "last_issued_at": now_epoch}
    return result(True, None, state)


def _practice_bell_src() -> str:
    # Do not inline the 38KB MP3 into every generated live/room page.  The
    # versioned, edge-cacheable endpoint transfers it once per browser instead.
    version = _practice_bell_version()
    return f"/api/practice/bell?v={version}" if version else ""


def _script_safe_json(value) -> str:
    """Serialize a JSON literal that cannot terminate an inline script block."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _live_token_now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _mint_gemini_live_token(
    duration_minutes: float,
    start_delay_minutes: float = 0,
    *,
    system_prompt: str = "",
    constrained_direct: bool = True,
    absolute_expire_at: int | float | datetime.datetime | None = None,
):
    """Create a short-lived, single-use Gemini Live ephemeral token.

    Solo browser tokens get a one-minute new-session window and a field-mask
    constrained setup. Backend-owned multiplayer connections retain their
    planned hand-off window and never disclose their token to a browser.
    """
    api_key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not api_key:
        return None, "未設定 GEMINI_API_KEY，未能開始練習。"
    try:
        from google import genai  # deferred: heavy import, cloud-only dependency
    except Exception:
        return None, "伺服器未安裝 Gemini SDK。"
    token_minutes = max(3, math.ceil(float(duration_minutes)))
    start_delay = max(0, float(start_delay_minutes or 0))
    now = _live_token_now_utc()
    if constrained_direct:
        new_session_expire = now + datetime.timedelta(
            seconds=LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS,
        )
        expire = now + datetime.timedelta(
            minutes=token_minutes,
            seconds=(
                LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS
                + LIVE_TOKEN_EXPIRY_GRACE_SECONDS
            ),
        )
        if absolute_expire_at is not None:
            if isinstance(absolute_expire_at, datetime.datetime):
                expire_cap = absolute_expire_at
                if expire_cap.tzinfo is None:
                    expire_cap = expire_cap.replace(tzinfo=datetime.timezone.utc)
                else:
                    expire_cap = expire_cap.astimezone(datetime.timezone.utc)
            else:
                expire_cap = datetime.datetime.fromtimestamp(
                    float(absolute_expire_at), tz=datetime.timezone.utc,
                )
            expire = min(expire, expire_cap)
            if expire <= new_session_expire + datetime.timedelta(
                seconds=LIVE_TOKEN_EXPIRY_GRACE_SECONDS,
            ):
                return None, "本節剩餘安全連線時間不足，請返回重新開始練習。"
    else:
        # Existing server-owned room tokens are minted as one initial resource.
        new_session_expire = now + datetime.timedelta(minutes=start_delay + 5)
        expire = now + datetime.timedelta(minutes=start_delay + token_minutes + 5)
    config = {
        "uses": 1,
        "expire_time": expire,
        "new_session_expire_time": new_session_expire,
        "http_options": {"api_version": "v1alpha"},
    }
    if constrained_direct:
        config.update({
            "live_connect_constraints": {
                "model": FREE_DEBATE_LIVE_MODEL,
                "config": {
                    "response_modalities": ["AUDIO"],
                    "system_instruction": {
                        "parts": [{"text": _bounded_live_system_prompt(system_prompt)}],
                    },
                    "session_resumption": {},
                    "context_window_compression": {
                        "trigger_tokens": LIVE_CONTEXT_COMPRESSION_TRIGGER_TOKENS,
                        "sliding_window": {
                            "target_tokens": LIVE_CONTEXT_COMPRESSION_TARGET_TOKENS,
                        },
                    },
                    "realtime_input_config": {
                        "automatic_activity_detection": {"disabled": True},
                    },
                    "input_audio_transcription": {},
                    "output_audio_transcription": {},
                },
            },
            # An empty list asks google-genai for a field mask over only the
            # explicit constraints above; browser speech settings may remain.
            "lock_additional_fields": [],
        })
    try:
        client = genai.Client(api_key=api_key, http_options={
            "api_version": "v1alpha",
            "timeout": LIVE_TOKEN_MINT_TIMEOUT_SECONDS * 1000,
        })
        token = client.auth_tokens.create(config=config)
    except Exception as e:
        # SDK errors may contain an authenticated request representation.
        logger.warning("Gemini Live token mint failed (%s)", type(e).__name__)
        return None, "Gemini 未能建立練習連線，請稍後再試。"
    token_name = getattr(token, "name", None)
    if not token_name:
        return None, "Gemini 未回傳 token。"
    if constrained_direct:
        completed_at = _live_token_now_utc()
        if completed_at >= new_session_expire or completed_at >= expire:
            return None, "Gemini 建立練習連線逾時，請立即再試。"
    return token_name, None


def _render_live_debate_html(
    token, prompt, live_minutes, bell_schedule, ai_starts, *, segments=None,
    tokens=None, session_labels=None, session_label="自由辯論",
    practice_id="", session_max_seconds=LIVE_FREE_SESSION_MAX_SECONDS,
):
    """Server-render templates/live_debate.html the same way ai_coach does, so the
    kiosk gets the identical Live engine for Free De and multi-session Mock.

    ``token``/``tokens`` remain in the private call signature for compatibility
    but are deliberately never injected: every section is minted by the
    authenticated endpoint only after the user presses Start/Next.
    """
    html = (BASE_DIR / "templates" / "live_debate.html").read_text(encoding="utf-8")
    # Rebrand the static UI copy BEFORE injecting the JSON payloads: the system
    # prompt, runtime prompts, segment labels and research brief legitimately
    # contain「自由辯論」and must not be rewritten (prompts.py documents this
    # contract).
    if session_label != "自由辯論":
        html = html.replace("自由辯論", session_label)
    replacements = {
        "__LIVE_MODEL__": _script_safe_json(FREE_DEBATE_LIVE_MODEL),
        "__LIVE_PROMPT__": _script_safe_json(prompt),
        "__LIVE_MINUTES__": _script_safe_json(float(live_minutes or 2.5)),
        "__BELL_SRC__": _script_safe_json(_practice_bell_src()),
        "__BELL_SCHEDULE__": _script_safe_json(bell_schedule or []),
        "__MOCK_SEGMENTS__": _script_safe_json(segments or []),
        "__MOCK_SESSION_LABELS__": _script_safe_json(session_labels or []),
        "__LIVE_PRACTICE_ID__": _script_safe_json(practice_id),
        "__LIVE_TOKEN_URL__": _script_safe_json("/api/ai-coach/live-token"),
        "__LIVE_CONTEXT_TRIGGER_TOKENS__": _script_safe_json(LIVE_CONTEXT_COMPRESSION_TRIGGER_TOKENS),
        "__LIVE_CONTEXT_TARGET_TOKENS__": _script_safe_json(LIVE_CONTEXT_COMPRESSION_TARGET_TOKENS),
        "__LIVE_SESSION_MAX_SECONDS__": _script_safe_json(int(session_max_seconds)),
        "__AI_STARTS__": _script_safe_json(bool(ai_starts)),
        "__LIVE_PROMPTS__": _script_safe_json(LIVE_RUNTIME_PROMPTS),
    }
    # Substitute the original template in one pass. Sequential ``str.replace``
    # would rescan user-authored prompt text and corrupt it whenever a debate
    # topic happened to contain one of the remaining placeholder names.
    pattern = re.compile("|".join(re.escape(key) for key in replacements))
    return pattern.sub(lambda match: replacements[match.group(0)], html)


def _practice_error_page(
    title: str, message: str, back: str = "/practice/ai-debate", *, retry: bool = False,
) -> Response:
    retry_link = '<a href="">重新檢查</a>' if retry else ""
    body = f"""<!DOCTYPE html><html lang="zh-HK"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{xml_escape(title)}</title>
<style>
html,body{{margin:0;height:100%;background:#0e0f13;color:#eef1f6;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans HK","PingFang HK",sans-serif;
display:flex;align-items:center;justify-content:center;text-align:center}}
.box{{max-width:760px;padding:40px}}
h1{{font-size:clamp(28px,4vw,48px);margin:0 0 18px}}
p{{font-size:clamp(18px,2.2vw,26px);color:#9aa3b2;line-height:1.6;margin:0 0 30px}}
a{{display:inline-block;font-size:clamp(20px,2.4vw,30px);font-weight:800;color:#fff;
background:#3b82f6;border-radius:16px;padding:16px 44px;text-decoration:none}}
</style></head><body><div class="box">
<h1>{xml_escape(title)}</h1><p>{xml_escape(message)}</p>
{retry_link}<a href="{xml_escape(back)}">◀ 返回</a></div></body></html>"""
    return Response(
        content=body, media_type="text/html", status_code=200,
        headers={"Cache-Control": CACHE_NO_STORE},
    )


@app.get("/practice/ai-debate")
async def appliance_ai_debate_page(request: Request):
    try:
        require_kiosk_user(request)
    except HTTPException:
        return RedirectResponse(
            url="/practice?next=ai-debate", status_code=307,
            headers={"Cache-Control": CACHE_NO_STORE},
        )
    return FileResponse(BASE_DIR / "templates" / "appliance_ai_debate.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/practice/ai-debate/live")
async def appliance_ai_debate_live(request: Request):
    # Serialize same-process page rendering with the persistent used-claim
    # check. Token issuance itself happens only at the authenticated Start-time
    # endpoint and is protected by the same process lock plus a DB marker.
    async with SOLO_LIVE_TOKEN_ISSUE_LOCK:
        return await _appliance_ai_debate_live_locked(request)


async def _appliance_ai_debate_live_locked(request: Request):
    try:
        # This Live renderer is shared by the dedicated practice appliance and
        # the ordinary /ai-coach page.  Both identities are already covered by
        # the central AI Coach policy; requiring the kiosk-only policy here
        # incorrectly rejects every signed-in committee member after
        # /api/ai-coach/prepare-live has issued their member-bound claim.
        user_id = require_page_user(request, "ai_coach")
    except HTTPException:
        from_coach = request.query_params.get("source") == "coach"
        return _practice_error_page(
            "需要登入",
            "請先登入可使用 AI 辯論練習的帳戶。",
            "/ai-coach" if from_coach else "/practice/ai-debate",
        )

    q = request.query_params
    topic = (q.get("topic") or "").strip()
    side = (q.get("side") or "正方").strip()
    debate_format = (q.get("format") or _PRACTICE_LIVE_FORMATS[0]).strip()
    mode = (q.get("mode") or "free").strip()
    if mode not in ("free", "mock"):
        mode = "free"
    country = _solo_live_country_status(request)
    if not country["supported"]:
        return _practice_error_page(
            "請重新檢查網絡地區", country["message"], retry=True,
        )
    if not topic:
        return _practice_error_page("未有辯題", "請先輸入辯題再開始。")

    supplied_claim = str(q.get("practice_id") or "")
    if supplied_claim:
        launch_claim = _verify_live_practice_claim(
            supplied_claim, expected_user_id=user_id, expected_mode=mode,
        )
        if not launch_claim:
            return _practice_error_page("練習連結已失效", "請返回 AI Coach 重新開始練習。")
    else:
        supplied_claim = _new_live_practice_claim(user_id, mode)
        if not supplied_claim:
            return _practice_error_page("未能開始", "伺服器未能簽發練習授權，請稍後再試。")
        return RedirectResponse(
            url=str(request.url.include_query_params(practice_id=supplied_claim)),
            status_code=307,
            headers={"Cache-Control": CACHE_NO_STORE},
        )

    already_reserved = await asyncio.to_thread(_solo_live_practice_exists, launch_claim)
    if already_reserved:
        return _practice_error_page(
            "練習憑證已簽發",
            "為避免重複建立可計費連線，這個練習連結不可重新載入。練習頁會在連線中斷時自動用同一憑證及 session handle 恢復；如已關閉頁面，請返回 AI Coach 重新開始。",
        )
    # Rate/quota checks are authoritative at the Start-time token endpoint.
    # Rendering this page must neither consume a rate-limit hit nor reserve a
    # paid practice before the user explicitly presses Start.
    quota_error = await asyncio.to_thread(_solo_live_quota_error, user_id, mode)
    if quota_error:
        return _practice_error_page("練習限額已用完", quota_error)
    budget_error = _bandwidth_essential_gate_error()
    if budget_error:
        return _practice_error_page("本月網絡用量已達上限", budget_error)

    brief_id = q.get("brief_id")
    if side not in ("正方", "反方"):
        side = "正方"
    allowed_formats = DEBATE_FORMATS if mode == "mock" else _PRACTICE_LIVE_FORMATS
    if debate_format not in allowed_formats:
        debate_format = allowed_formats[0]

    if debate_format == "聯中":
        try:
            live_minutes = float(q.get("minutes") or 5)
        except (TypeError, ValueError):
            live_minutes = 5.0
        live_minutes = min(float(LIVE_FREE_MAX_MINUTES), max(0.5, live_minutes))
    else:
        live_minutes = 2.5

    if mode == "mock":
        live_minutes = max(2.0, live_minutes)
        segments = get_full_mock_sequence(
            debate_format,
            free_debate_minutes=live_minutes if debate_format == "聯中" else None,
        )
        sessions = split_mock_into_sessions(segments)
        total_minutes = full_mock_total_seconds(segments) / 60
        from api.ai_coach_api import consume_live_brief
        research_brief = consume_live_brief(brief_id, user_id)
        prompt = _bounded_live_system_prompt(build_full_mock_live_prompt(
            topic, side, debate_format,
            free_debate_minutes=live_minutes if debate_format == "聯中" else None,
            research_brief=research_brief,
        ))
        session_seconds = [int(session["planned_seconds"]) for session in sessions]
        practice_claim = _planned_live_practice_claim(launch_claim, session_seconds, prompt)
        flat = [
            {**segment, "session": index}
            for index, session in enumerate(sessions)
            for segment in session["segments"]
        ]
        html = _render_live_debate_html(
            "", prompt, total_minutes, [], False,
            segments=flat, tokens=[],
            session_labels=[session["label"] for session in sessions],
            session_label="Mock", practice_id=practice_claim,
            session_max_seconds=sum(session_seconds) + 10 * 60,
        )
        return Response(
            content=html, media_type="text/html",
            headers={"Cache-Control": CACHE_NO_STORE},
        )

    bell_schedule = get_debate_timer_config(
        debate_format, free_debate_minutes=live_minutes,
    )["bell_schedules"].get("free", [])
    from api.ai_coach_api import consume_live_brief
    research_brief = consume_live_brief(brief_id, user_id)
    prompt = _bounded_live_system_prompt(
        build_free_debate_live_prompt(topic, side, research_brief),
    )
    speech_seconds = int(math.ceil(live_minutes * 2 * 60))
    practice_claim = _planned_live_practice_claim(launch_claim, [speech_seconds], prompt)
    html = _render_live_debate_html(
        "", prompt, live_minutes, bell_schedule, side == "反方",
        practice_id=practice_claim,
        session_max_seconds=LIVE_FREE_SESSION_MAX_SECONDS,
    )
    return Response(
        content=html, media_type="text/html",
        headers={"Cache-Control": CACHE_NO_STORE},
    )


GEMINI_LIVE_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained"
)


# ---------------------------------------------------------------------------
# Networked practice rooms (聯機打 Free De / Mock)
#
# Additive and self-contained, same discipline as the projector block above:
# these routes are registered BEFORE the catch-all proxy routes (WS at the very
# bottom, HTTP after) — Starlette matches in declaration order, so a route
# declared after the catch-all would be swallowed and proxied to Streamlit.
#
# Rooms live in an in-memory dict in this single uvicorn process (the deployment
# is a single Render instance). WebSocket objects cannot be shared across
# processes, and audio fan-out is far too high-frequency for the DB-polling
# pattern the projector uses — so in-memory is both correct and simplest here.
# No Streamlit page, DB schema, or existing route is touched.
#
# Two modes:
#   A  真人對真人 1v1 — two committee members debate by voice; the server fans
#      out each active speaker's PCM frames to the peer. AI is only a 評判.
#   B  多人對 AI — a team shares ONE server-owned Gemini Live session and
#      takes turns; the server forwards the active speaker's audio to Gemini and
#      broadcasts Gemini's audio/transcript to everyone. (Gemini leg: phase 2.)
# ---------------------------------------------------------------------------

ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no O/0/I/1
ROOM_CODE_LEN = 5
ROOM_EMPTY_GRACE_MS = ROOM_EMPTY_GRACE_SECONDS * 1000
ROOM_MAX_AGE_MS = ROOM_MAX_AGE_SECONDS * 1000
ROOM_JUDGEMENT_MODELS = model_slugs_for_feature("room_judgement")

ROOMS = {}  # code -> Room
ROOMS_LOCK = asyncio.Lock()

PRACTICE_DAILY_LIMIT_MESSAGE = (
    "由於系統每月可用的網絡傳輸量有限，為控制營運預算並確保所有委員均能使用服務，"
    "每位委員每日只可進行一次聯機自由辯論及一次聯機完整模擬練習。"
    "你今日已使用此類別的練習限額，請於翌日再試。"
)


def _practice_kind(structure: str) -> str:
    return "multiplayer_mock" if structure == "mock" else "multiplayer_free"


def _reserve_practice_daily_slot(user_id: str, structure: str, room_code: str) -> bool:
    """Atomically consume a Hong Kong calendar-day slot.

    Reconnecting to the same room remains allowed; a different room in the same
    category on the same day is rejected.
    """
    return _reserve_room_practice_slots([user_id], structure, room_code)


def _reserve_room_practice_slots(user_ids, structure: str, room_code: str) -> bool:
    """Atomically reserve the whole verified roster immediately before start."""
    users = list(dict.fromkeys(str(user).strip() for user in user_ids if str(user).strip()))
    if not users:
        return False
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
    usage_date = now_hk.date()
    kind = _practice_kind(structure)
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('practice_room_monthly_quota'))"))
        conn.execute(text(f"DELETE FROM {TABLE_PRACTICE_DAILY_USAGE} WHERE usage_date<:month_start"),
                     {"month_start": usage_date.replace(day=1)})
        room_exists = bool(conn.execute(text(
            f"SELECT 1 FROM {TABLE_PRACTICE_DAILY_USAGE} WHERE room_code=:room LIMIT 1"
        ), {"room": room_code}).fetchone())
        if not room_exists:
            month_start = usage_date.replace(day=1)
            limit = MULTIPLAYER_MOCK_MONTHLY_ROOMS if structure == "mock" else MULTIPLAYER_FREE_MONTHLY_ROOMS
            room_count = int(conn.execute(text(f"""SELECT COUNT(DISTINCT room_code)
                FROM {TABLE_PRACTICE_DAILY_USAGE}
                WHERE practice_kind=:kind AND usage_date>=:start"""), {
                "kind": kind, "start": month_start,
            }).scalar() or 0)
            if room_count >= limit:
                return False
        for user_id in users:
            existing = conn.execute(text(f"""SELECT room_code
                FROM {TABLE_PRACTICE_DAILY_USAGE}
                WHERE user_id=:user AND practice_kind=:kind AND usage_date=:day"""), {
                "user": user_id, "kind": kind, "day": usage_date,
            }).scalar()
            if existing and str(existing) != str(room_code):
                return False
        for user_id in users:
            conn.execute(text(f"""INSERT INTO {TABLE_PRACTICE_DAILY_USAGE}
                (user_id,practice_kind,usage_date,room_code,created_at)
                VALUES(:user,:kind,:day,:room,:now)
                ON CONFLICT(user_id,practice_kind,usage_date) DO NOTHING"""), {
                "user": user_id, "kind": kind, "day": usage_date,
                "room": room_code, "now": now_hk.replace(tzinfo=None),
            })
    return True


def _release_practice_daily_slot(user_id: str, structure: str, room_code: str) -> None:
    """Release only this creator's provisional slot after room setup fails."""
    engine = _get_db_engine()
    if engine is None:
        return
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('practice_room_monthly_quota'))"))
        conn.execute(text(f"""DELETE FROM {TABLE_PRACTICE_DAILY_USAGE}
            WHERE user_id=:user AND practice_kind=:kind AND room_code=:room"""), {
            "user": user_id, "kind": _practice_kind(structure),
            "room": room_code,
        })


def _release_room_practice_slots(user_ids, structure: str, room_code: str) -> None:
    for user_id in list(dict.fromkeys(str(user) for user in user_ids)):
        _release_practice_daily_slot(user_id, structure, room_code)


def _practice_daily_slot_available(user_id: str, structure: str) -> bool:
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    usage_date = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    with engine.begin() as conn:
        used = conn.execute(text(f"""SELECT 1 FROM {TABLE_PRACTICE_DAILY_USAGE}
            WHERE user_id=:user AND practice_kind=:kind AND usage_date=:day"""), {
            "user": user_id, "kind": _practice_kind(structure), "day": usage_date,
        }).fetchone()
    return used is None


def _practice_monthly_room_available(structure: str) -> bool:
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    today = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    month_start = today.replace(day=1)
    limit = MULTIPLAYER_MOCK_MONTHLY_ROOMS if structure == "mock" else MULTIPLAYER_FREE_MONTHLY_ROOMS
    with engine.begin() as conn:
        used = int(conn.execute(text(f"""SELECT COUNT(DISTINCT room_code)
            FROM {TABLE_PRACTICE_DAILY_USAGE}
            WHERE practice_kind=:kind AND usage_date>=:start"""), {
            "kind": _practice_kind(structure), "start": month_start,
        }).scalar() or 0)
    return used < limit


def _now_ms():
    return int(time.time() * 1000)


def _build_room_segments(structure, debate_format, free_minutes):
    """Server is authoritative over the segment/timer sequence. Reuses the same
    pure helpers the Streamlit tabs use (already imported at top)."""
    if structure == "mock":
        return get_full_mock_sequence(debate_format, free_debate_minutes=free_minutes)
    seconds = int(round(float(free_minutes or 2.5) * 60))
    warn = max(0, seconds - 30)
    bells = [
        {"t": 0, "rings": 1, "label": "開始 — 1 叮"},
        {"t": warn, "rings": 1, "label": "完結前 30 秒 — 1 叮"},
        {"t": seconds, "rings": 2, "label": "時間到 — 2 叮"},
    ]
    return [{"id": "free", "label": "自由辯論", "side": "雙方", "seconds": seconds, "bells": bells}]


def _room_segment_position(room, seg):
    """Return the assigned human position for a single-speaker mock segment."""
    if not seg:
        return None
    seg_id = str(seg.get("id") or "")
    if seg_id.startswith("main_"):
        return 1
    if seg_id.startswith("dep1_"):
        return 2
    if seg_id.startswith("dep2_"):
        return 3
    if seg_id.startswith("dep3_"):
        return 4
    if seg_id.startswith("closing_"):
        return 1 if room.debate_format in ("聯中", "星島") else 4
    return None


def _fill_live_runtime_prompt(template, values):
    text_value = str(template or "")
    for key, value in values.items():
        text_value = text_value.replace("{" + key + "}", str(value))
    return text_value


class RoomMember:
    def __init__(self, user_id, ws):
        self.user_id = user_id
        self.ws = ws
        self.connection_generation = 1
        self.role = None          # "正方"/"反方" (A) or claimed side (B)
        self.position = None      # mode B mock: positions 1-4 assigned before start
        self.name = user_id
        self.connected = True
        self.joined_at = _now_ms()
        self.last_test_audio_ms = 0
        # One bucket follows the authenticated room member across reconnects;
        # replacing a socket must not refresh an attacker's audio allowance.
        self.audio_rate_tokens = float(ROOM_AUDIO_RATE_BURST_BYTES)
        self.audio_rate_updated_ms = self.joined_at
        self.audio_message_tokens = float(ROOM_AUDIO_RATE_BURST_MESSAGES)
        self.audio_message_updated_ms = self.joined_at
        self.control_rate_tokens = float(ROOM_CONTROL_RATE_BURST_MESSAGES)
        self.control_rate_updated_ms = self.joined_at
        self.pending_test_audio = {}
        self.last_test_received_ms = 0


class Room:
    def __init__(self, code, mode, created_by, debate_format, topic,
                 structure, free_minutes, capacity):
        self.code = code
        self.mode = mode                 # "A" | "B"
        self.created_by = created_by
        self.created_at = _now_ms()
        self.started_ms = None
        self.hard_deadline_ms = None
        self.ended_at_ms = None
        self.ended_retain_until_ms = 0
        self.phase = "lobby"             # lobby | starting | active | ending | ended
        self.activation_ready = False
        self.debate_format = debate_format
        self.topic = topic
        self.structure = structure       # free | mock
        self.free_minutes = free_minutes
        self.capacity = capacity
        self.segments = _build_room_segments(structure, debate_format, free_minutes)
        self.seg_index = 0
        self.seg_started_ms = None
        self.side_elapsed_ms = {"正方": 0, "反方": 0}
        self.active_turn_user = None
        self.active_turn_side = None
        self.active_turn_started_ms = None
        self.free_first_done = False
        self.precheck_id = None
        self.precheck_results = {}
        self.members = {}                # user_id -> RoomMember
        self.transcript = []             # {speaker, side, seg, text}
        self.transcript_revision = 0
        self.judgement = ""
        self.judgement_revision = -1
        self.empty_since = None
        self.terminal_requested = False
        self.creator_side = None         # mode A: side the host picked at create
        # mode B / judge (Gemini leg wired in phase 2)
        self.human_side = None           # 正方/反方 the humans take in mode B
        self.gemini = None               # {tokens, sigs, prompt, model, session_labels}
        self.gemini_session_index = 0
        self.gemini_ws = None
        self.gemini_task = None
        self.gemini_resume_handle = ""
        self.gemini_generation = 0
        self.gemini_connect_epoch = 0
        self.gemini_resume_attempts = 0
        self.gemini_setup_future = None
        self.gemini_turn_state = None
        self.ai_audio_remainder_ms = 0.0
        self.free_opening_ai_cued = False
        self.free_opening_ai_complete = False
        self.tick_task = None
        self.lifecycle_task = None
        self.judgement_task = None
        self.empty_cleanup_task = None
        self.quota_users = []
        # Approximate Render egress for this room.  We count successful
        # websocket fan-out plus payloads sent from Render to Gemini, then write
        # one aggregate row when the room ends or is garbage-collected.
        self.bandwidth_bytes = 0
        self.bandwidth_flushed_bytes = 0
        self.bandwidth_recorded = False
        self.bandwidth_lock = threading.Lock()
        self.last_bandwidth_checkpoint_ms = _now_ms()
        # Server-side TTS: when on, the pump synthesizes the AI's transcript via
        # _synthesize_tts and broadcasts one audio blob to the whole room (synced,
        # one call/turn), keeping Gemini native audio as fallback. Set at gemini start.
        self.tts_enabled = False
        self.lock = asyncio.Lock()
        self.activation_lock = asyncio.Lock()
        self.gemini_connect_lock = asyncio.Lock()
        self.judgement_lock = asyncio.Lock()
        self.segment_lock = asyncio.Lock()
        self.end_complete_event = asyncio.Event()

    def position_labels(self):
        if self.debate_format == "聯中":
            return {1: "主辯／結辯", 2: "一副", 3: "二副", 4: "三副"}
        if self.debate_format == "星島":
            return {1: "主辯／結辯", 2: "一副", 3: "二副"}
        return {1: "主辯", 2: "一副", 3: "二副", 4: "結辯"}

    def required_positions(self):
        return tuple(self.position_labels().keys())

    def roster(self):
        labels = self.position_labels()
        return [
            {"user_id": m.user_id, "name": m.name, "role": m.role,
             "position": m.position, "position_label": labels.get(m.position, ""),
             "connected": m.connected, "is_host": m.user_id == self.created_by}
            for m in self.members.values()
        ]

    def connected_user_ids(self):
        return [m.user_id for m in self.members.values() if m.connected]

    def current_segment(self):
        if 0 <= self.seg_index < len(self.segments):
            return self.segments[self.seg_index]
        return None

    def active_speaker(self):
        """The single member allowed to speak this segment, or None when the
        segment is open (雙方 free debate) or silent (準備)."""
        seg = self.current_segment()
        if not seg:
            return None
        side = seg.get("side")
        if self.mode == "B":
            if self.structure == "mock" and side == self.human_side:
                position = _room_segment_position(self, seg)
                if position:
                    for m in self.members.values():
                        if m.connected and m.position == position:
                            return m.user_id
            return None
        if side in ("正方", "反方"):
            for m in self.members.values():
                if m.role == side:
                    return m.user_id
        return None

    def expected_turn_side(self):
        if self.mode == "B":
            # In Free mode 正方 still opens.  When the human team is 正方 every
            # member shares that role; when AI is 正方 the server separately
            # blocks humans until the AI's opening turn completes.
            if (
                self.structure == "free"
                and self.human_side == "正方"
                and not self.free_first_done
            ):
                return "正方"
            return None
        seg = self.current_segment()
        if self.phase == "active" and seg and seg.get("side") == "雙方" and not self.free_first_done:
            return "正方"
        return None

    def is_open_free_segment(self):
        """Whether the current segment is a timed, alternating free debate.

        A full Mock also contains a ``雙方`` free-debate segment.  The old code
        checked ``structure == 'free'`` and therefore skipped side timers and
        Gemini activity handling during that Mock segment.
        """
        seg = self.current_segment()
        return bool(self.phase == "active" and seg and seg.get("side") == "雙方")

    def state_msg(self):
        seg = self.current_segment()
        now = _now_ms()
        side_elapsed_ms = dict(self.side_elapsed_ms)
        return {
            "type": "state",
            "phase": self.phase,
            "seg_index": self.seg_index,
            "seg_total": len(self.segments),
            "seg_label": seg.get("label") if seg else "",
            "side": seg.get("side") if seg else "",
            "seconds": seg.get("seconds") if seg else 0,
            "bells": seg.get("bells") if seg else [],
            "active_speaker": self.active_speaker(),
            "seg_started_ms": self.seg_started_ms,
            "server_now_ms": now,
            "side_elapsed_ms": side_elapsed_ms,
            "active_turn_user": self.active_turn_user,
            "active_turn_side": self.active_turn_side,
            "active_turn_started_ms": self.active_turn_started_ms,
            "expected_turn_side": self.expected_turn_side(),
        }


def _checkpoint_room_bandwidth(room, final: bool = False):
    with room.bandwidth_lock:
        if room.bandwidth_recorded:
            return
        snapshot = int(room.bandwidth_bytes)
        delta = max(0, snapshot - int(room.bandwidth_flushed_bytes))
        if delta and record_bandwidth_usage(
            f"multiplayer_{room.structure}", delta, room.created_by,
            aggregate_key=f"room={room.code};mode={room.mode}",
        ):
            room.bandwidth_flushed_bytes = snapshot
        if final and room.bandwidth_flushed_bytes >= snapshot:
            room.bandwidth_recorded = True


def _record_room_bandwidth_once(room):
    _checkpoint_room_bandwidth(room, final=True)


def _gc_rooms():
    def dispose(code, room, reason):
        if room.phase == "ended":
            ROOMS.pop(code, None)
            return
        if room.terminal_requested:
            return
        # Registration checks this flag synchronously, closing the scheduling
        # gap before the async terminal transition acquires activation_lock.
        room.terminal_requested = True
        try:
            asyncio.get_running_loop().create_task(
                _room_end_and_remove(room, reason),
            )
        except RuntimeError:
            # Defensive fallback for a future synchronous maintenance caller.
            ROOMS.pop(code, None)
            room.phase = "ended"
            _record_room_bandwidth_once(room)
            for task in (
                room.tick_task, room.lifecycle_task, room.gemini_task,
                room.judgement_task,
                room.empty_cleanup_task,
            ):
                if task is not None and not task.done():
                    task.cancel()

    now = _now_ms()
    for code in list(ROOMS.keys()):
        room = ROOMS.get(code)
        if room is None:
            continue
        if room.phase == "ended":
            judgement_pending = (
                room.judgement_task is not None
                and not room.judgement_task.done()
            )
            if judgement_pending or now < room.ended_retain_until_ms:
                continue
            ROOMS.pop(code, None)
            continue
        if now - room.created_at > ROOM_MAX_AGE_MS:
            dispose(code, room, "ttl")
            continue
        if any(m.connected for m in room.members.values()):
            _room_cancel_empty_cleanup(room)
        else:
            if room.empty_since is None:
                _room_schedule_empty_cleanup(room)
            elif now - room.empty_since > ROOM_EMPTY_GRACE_MS:
                dispose(code, room, "empty")


def _active_room_count():
    return len([r for r in ROOMS.values() if r.phase != "ended"])


def _room_prune_lobby_offline_members(room, *, limit=None):
    """Drop a bounded batch of lobby ghosts before admitting another socket.

    Active rooms retain offline members for reconnect.  Lobby members, however,
    have no durable state yet; retaining failed sockets would let a long-lived
    lobby's member dictionary grow even though capacity counts only connected
    sockets.
    """
    if room.phase != "lobby":
        return 0
    batch = max(1, int(limit or ROOM_MAX_CAPACITY * 2))
    removed = 0
    for user_id, member in list(room.members.items()):
        if removed >= batch:
            break
        if member.connected:
            continue
        room.members.pop(user_id, None)
        room.precheck_results.pop(user_id, None)
        removed += 1
    return removed


def _room_cancel_empty_cleanup(room):
    room.empty_since = None
    task = room.empty_cleanup_task
    room.empty_cleanup_task = None
    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None
    if (
        task is not None and task is not current
        and not task.done()
    ):
        task.cancel()


async def _room_empty_cleanup_after_grace(room, marked_empty_ms):
    try:
        await asyncio.sleep(ROOM_EMPTY_GRACE_SECONDS)
        async with room.lock:
            still_empty = (
                room.empty_since == marked_empty_ms
                and not any(member.connected for member in room.members.values())
                and room.phase not in ("ending", "ended")
                and not room.terminal_requested
            )
            if still_empty:
                room.terminal_requested = True
        if not still_empty:
            return
        # End first so a socket that already captured this Room can no longer
        # reconnect into an orphan between registry removal and phase change.
        await _room_end_and_remove(room, "empty")
    except asyncio.CancelledError:
        raise
    finally:
        if room.empty_cleanup_task is asyncio.current_task():
            room.empty_cleanup_task = None


def _room_schedule_empty_cleanup(room):
    if room.empty_cleanup_task is not None and not room.empty_cleanup_task.done():
        return
    room.empty_since = _now_ms()
    room.empty_cleanup_task = asyncio.create_task(
        _room_empty_cleanup_after_grace(room, room.empty_since),
    )


async def _room_broadcast(room, msg, exclude=None):
    text = json.dumps(msg, ensure_ascii=False)
    recipients = [
        (member, member.ws, member.connection_generation)
        for member in list(room.members.values())
        if member.connected and not (exclude and member.user_id == exclude)
    ]

    async def send(member, websocket, generation):
        try:
            # A stalled mobile client must not block audio fan-out to everyone
            # else.  The next reconnect rehydrates room state and transcript.
            await asyncio.wait_for(
                websocket.send_text(text), timeout=ROOM_WS_SEND_TIMEOUT_SECONDS,
            )
            room.bandwidth_bytes += len(text.encode("utf-8"))
            return None
        except Exception:
            return member, websocket, generation

    if recipients:
        results = await asyncio.gather(*(send(*recipient) for recipient in recipients))
        failed = []
        for result in results:
            if result is None:
                continue
            member, websocket, generation = result
            async with room.lock:
                if (
                    member.ws is not websocket
                    or member.connection_generation != generation
                    or not member.connected
                ):
                    continue
                member.connected = False
                if room.phase == "lobby":
                    room.members.pop(member.user_id, None)
                    room.precheck_results.pop(member.user_id, None)
                failed.append(member)
        for member in failed:
            if room.active_turn_user == member.user_id:
                await _room_handle_turn(room, member, False)
        if (
            failed and not room.connected_user_ids()
            and not room.terminal_requested
        ):
            _room_schedule_empty_cleanup(room)


async def _room_gemini_send(room, gws, payload):
    encoded = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    await gws.send(encoded)
    room.bandwidth_bytes += (
        len(encoded) if isinstance(encoded, bytes) else len(encoded.encode("utf-8"))
    )


async def _room_tick(room):
    try:
        while room.phase == "active" and ROOMS.get(room.code) is room:
            await asyncio.sleep(1)
            if room.phase != "active" or ROOMS.get(room.code) is not room:
                break
            now = _now_ms()
            if room.hard_deadline_ms and now >= room.hard_deadline_ms:
                await _room_end(room, "server_time_limit")
                break
            seg = room.current_segment()
            seg_seconds = int((seg or {}).get("seconds") or 0)
            if (
                room.structure == "free"
                and seg and seg.get("side") == "雙方"
                and seg_seconds > 0
            ):
                budget_ms = seg_seconds * 1000
                active_side = room.active_turn_side
                active_user = room.active_turn_user
                active_used = room.side_elapsed_ms.get(active_side, 0)
                if active_side in room.side_elapsed_ms and room.active_turn_started_ms is not None:
                    active_used += max(0, now - room.active_turn_started_ms)
                if active_user and active_used >= budget_ms:
                    member = room.members.get(active_user)
                    if member is not None:
                        await _room_handle_turn(room, member, False)
                if all(
                    room.side_elapsed_ms.get(side, 0) >= budget_ms
                    for side in ("正方", "反方")
                ):
                    await _room_end(room, "server_side_budgets_complete")
                    break
            elif (seg and room.seg_started_ms and seg_seconds > 0
                    and now - room.seg_started_ms >= seg_seconds * (2 if seg.get("side") == "雙方" else 1) * 1000):
                if room.seg_index >= len(room.segments) - 1:
                    await _room_end(room, "server_segment_limit")
                    break
                current_index = room.seg_index
                await _room_advance_segment(
                    room, current_index + 1, expected_from=current_index,
                )
            await _room_broadcast(room, room.state_msg())
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("room tick failed (%s): %s", room.code, e)
        # Losing the only authoritative timer must never leave an active room
        # running without deadline or segment enforcement.
        try:
            if room.phase == "active" and ROOMS.get(room.code) is room:
                await _room_end(room, "server_timer_failure")
        except Exception as cleanup_exc:
            logger.exception(
                "room tick safe-end failed (%s): %s",
                room.code, cleanup_exc,
            )


async def _room_lifecycle(room):
    """Enforce room TTL and egress gates in every non-terminal phase.

    The active tick owns debate timers only.  Keeping lifecycle enforcement in
    one creation-time task also covers a connected lobby/slow activation and
    avoids duplicate bandwidth checkpoints once a room becomes active.
    """
    try:
        while (
            ROOMS.get(room.code) is room
            and room.phase not in ("ending", "ended")
            and not room.terminal_requested
        ):
            remaining_ms = ROOM_MAX_AGE_MS - (_now_ms() - room.created_at)
            if remaining_ms <= 0:
                await _room_end(room, "ttl")
                return
            await asyncio.sleep(min(
                float(BANDWIDTH_CHECKPOINT_SECONDS),
                max(0.05, remaining_ms / 1000),
            ))
            if (
                ROOMS.get(room.code) is not room
                or room.phase in ("ending", "ended")
                or room.terminal_requested
            ):
                return
            now = _now_ms()
            if now - room.created_at >= ROOM_MAX_AGE_MS:
                await _room_end(room, "ttl")
                return
            await asyncio.to_thread(_checkpoint_room_bandwidth, room, False)
            room.last_bandwidth_checkpoint_ms = now
            if await asyncio.to_thread(_bandwidth_live_gate_error):
                await _room_end(room, "monthly_bandwidth_limit")
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("room lifecycle failed (%s): %s", room.code, exc)
        try:
            if (
                ROOMS.get(room.code) is room
                and room.phase not in ("ending", "ended")
                and not room.terminal_requested
            ):
                await _room_end(room, "server_lifecycle_failure")
        except Exception as cleanup_exc:
            logger.exception(
                "room lifecycle safe-end failed (%s): %s",
                room.code, cleanup_exc,
            )
    finally:
        if room.lifecycle_task is asyncio.current_task():
            room.lifecycle_task = None


def _room_ensure_tick(room):
    if room.tick_task is None or room.tick_task.done():
        room.tick_task = asyncio.create_task(_room_tick(room))


def _room_ensure_lifecycle(room):
    if room.lifecycle_task is None or room.lifecycle_task.done():
        room.lifecycle_task = asyncio.create_task(_room_lifecycle(room))


def _room_precheck_msg(room, msg_type="precheck_status"):
    users = room.connected_user_ids()
    return {
        "type": msg_type,
        "check_id": room.precheck_id,
        "members": users,
        "results": {u: room.precheck_results.get(u) for u in users},
    }


def _room_connected_roster_signature(room):
    return tuple(
        (member.user_id, member.connection_generation)
        for member in room.members.values() if member.connected
    )


def _room_precheck_snapshot_matches(room, check_id, roster_signature):
    if not check_id or room.precheck_id != check_id:
        return False
    if _room_connected_roster_signature(room) != tuple(roster_signature or ()):
        return False
    users = [user_id for user_id, _generation in roster_signature or ()]
    return bool(
        users
        and all(user_id in room.precheck_results for user_id in users)
        and all(room.precheck_results[user_id].get("ok") for user_id in users)
    )


def _room_mint_gemini_tokens(room) -> str | None:
    if room.mode != "B" or room.gemini is None:
        return None
    durations = list(room.gemini.get("session_minutes") or [])
    if not durations:
        durations = [max(3.0, full_mock_total_seconds(room.segments) / 60 + 2)]
    # Mint only the immediately-needed session.  Later Mock sections are minted
    # just in time under gemini_connect_lock so their one-use start window cannot
    # expire during a long 聯中 practice.
    token, error = _mint_gemini_live_token(
        max(3, float(durations[0])), constrained_direct=False,
    )
    if error:
        return error
    room.gemini["tokens"] = [token] + [""] * (len(durations) - 1)
    return None


def _room_failed_activation_state(room):
    """Restore a retryable lobby after any start failure."""
    if room.phase not in ("ending", "ended") and not room.terminal_requested:
        room.phase = "lobby"
    room.started_ms = None
    room.seg_started_ms = None
    room.hard_deadline_ms = None
    room.precheck_id = None
    room.precheck_results = {}
    room.active_turn_user = None
    room.active_turn_side = None
    room.active_turn_started_ms = None
    room.activation_ready = False
    if room.gemini is not None:
        room.gemini["tokens"] = []


async def _room_rollback_activation(
    room, users, released_message, *, close_gemini=False,
):
    """Reset a failed activation even when quota release itself fails.

    A database outage during rollback must not strand the in-memory room in
    ``starting``.  When release cannot be confirmed we retain the affected user
    list for diagnostics/retry and return wording that does not falsely promise
    that today's slot was restored.
    """
    if close_gemini:
        try:
            await _room_close_gemini(room)
        except Exception as exc:
            logger.warning(
                "room activation upstream cleanup failed (%s, %s)",
                room.code, type(exc).__name__,
            )
    released = False
    try:
        await asyncio.to_thread(
            _release_room_practice_slots,
            users, room.structure, room.code,
        )
        released = True
    except Exception as exc:
        logger.warning(
            "room quota rollback failed (%s, %s)",
            room.code, type(exc).__name__,
        )
    finally:
        async with room.lock:
            room.quota_users = [] if released else list(users)
            _room_failed_activation_state(room)
    if released:
        return released_message
    return (
        "房間未能開始；練習限額回滾暫時未能確認，"
        "今日名額可能仍被佔用，請稍後再試或聯絡管理員。"
    )


async def _room_start_active(
    room, *, expected_precheck_id=None, expected_roster_signature=None,
) -> str | None:
    # Multiple final precheck messages can arrive in the same event-loop tick.
    # Only the activation owner may reserve quota, mint or connect upstream.
    async with room.activation_lock:
        async with room.lock:
            if room.terminal_requested:
                return "房間正在結束，未有扣除練習限額。"
            if room.phase == "active":
                return None
            if expected_precheck_id is not None and (
                room.phase != "lobby"
                or not _room_precheck_snapshot_matches(
                    room, expected_precheck_id, expected_roster_signature,
                )
            ):
                return "開始前檢查已失效，請由主持重新開始連線測試。"
            if room.phase not in ("lobby", "starting"):
                return "房間目前狀態不可開始練習。"
            room.phase = "starting"

        budget_error = await asyncio.to_thread(_bandwidth_live_gate_error)
        if budget_error:
            _room_failed_activation_state(room)
            return budget_error

        # Revalidate the authoritative roster after the last precheck message.
        # A socket may have disconnected while the 3.5 GB gate ran.
        async with room.lock:
            start_error = _room_start_blocker(room)
            users = room.connected_user_ids()
            roster_signature = _room_connected_roster_signature(room)
            precheck_changed = bool(
                expected_precheck_id is not None
                and not _room_precheck_snapshot_matches(
                    room, expected_precheck_id, expected_roster_signature,
                )
            )
        if start_error or precheck_changed:
            _room_failed_activation_state(room)
            return start_error or (
                "成員名單或連線在檢查後有變，未有扣除練習限額；"
                "請重新進行連線測試。"
            )
        try:
            reserved = await asyncio.to_thread(
                _reserve_room_practice_slots, users, room.structure, room.code,
            )
        except Exception as exc:
            logger.warning(
                "room quota reservation failed (%s, %s)",
                room.code, type(exc).__name__,
            )
            _room_failed_activation_state(room)
            return "練習限額暫時未能確認，未有扣除限額，請稍後再試。"
        if not reserved:
            _room_failed_activation_state(room)
            return PRACTICE_DAILY_LIMIT_MESSAGE
        room.quota_users = users
        # The transactional reservation itself runs in a worker thread. A
        # reconnect/disconnect during that await invalidates the precheck; roll
        # the reservation back before minting a single-use provider token.
        async with room.lock:
            roster_changed = (
                room.phase != "starting"
                or room.terminal_requested
                or _room_connected_roster_signature(room) != roster_signature
                or bool(_room_start_blocker(room))
                or (
                    expected_precheck_id is not None
                    and not _room_precheck_snapshot_matches(
                        room, expected_precheck_id, expected_roster_signature,
                    )
                )
            )
        if roster_changed:
            return await _room_rollback_activation(room, users, (
                "成員名單在配額確認期間有變，已撤銷扣除；"
                "請重新進行連線測試。"
            ))
        try:
            mint_error = await asyncio.to_thread(_room_mint_gemini_tokens, room)
        except Exception as exc:
            logger.warning(
                "room token mint failed (%s, %s)", room.code, type(exc).__name__,
            )
            mint_error = "AI 對手暫時未能建立，未有扣除練習限額。"
        if mint_error:
            return await _room_rollback_activation(room, users, mint_error)

        # Token minting yields to a worker thread. Recheck before the upstream
        # connect, but deliberately retain ``starting`` so browsers cannot send
        # debate audio while setupComplete is still pending.
        roster_changed = False
        async with room.lock:
            roster_changed = (
                room.phase != "starting"
                or room.terminal_requested
                or _room_connected_roster_signature(room) != roster_signature
                or bool(_room_start_blocker(room))
            )
            if not roster_changed:
                room.seg_index = 0
                room.started_ms = _now_ms()
                room.seg_started_ms = room.started_ms
                total_seconds = full_mock_total_seconds(room.segments)
                # Preserve the pre-existing multiplayer Free hard stop. Solo's
                # separate browser-direct session owns the 30-minute deadline.
                if room.structure == "free":
                    total_seconds = min(10 * 60, total_seconds)
                room.hard_deadline_ms = (
                    room.started_ms + max(30, int(total_seconds)) * 1000
                )
                room.side_elapsed_ms = {"正方": 0, "反方": 0}
                room.active_turn_user = None
                room.active_turn_side = None
                room.active_turn_started_ms = None
                room.free_first_done = False
                room.free_opening_ai_cued = False
                room.free_opening_ai_complete = False
                room.ai_audio_remainder_ms = 0.0
                room.gemini_turn_state = None
        if roster_changed:
            return await _room_rollback_activation(
                room, users,
                "成員名單在開始前有變，已撤銷扣除；請重新進行連線測試。",
            )
        gemini_ready = await _room_start_gemini_if_needed(room)
        if room.mode == "B" and not gemini_ready:
            return await _room_rollback_activation(
                room, users,
                "AI 對手連線失敗，已撤銷扣除；請重新測試後再開始。",
                close_gemini=True,
            )

        # setupComplete can take several seconds.  A final lock-protected roster
        # check closes the disconnect window and atomically publishes active.
        async with room.lock:
            roster_changed = (
                room.phase != "starting"
                or room.terminal_requested
                or _room_connected_roster_signature(room) != roster_signature
                or bool(_room_start_blocker(room))
                or (room.mode == "B" and room.gemini_ws is None)
            )
            if not roster_changed:
                room.phase = "active"
                room.activation_ready = False
                room.precheck_id = None
                room.precheck_results = {}
        if roster_changed:
            return await _room_rollback_activation(
                room, users,
                "成員名單在開始前有變，已撤銷扣除；請重新進行連線測試。",
                close_gemini=True,
            )
        if room.mode == "B":
            announced = await _room_announce_gemini_ready(room, initial=True)
            if not announced:
                return await _room_rollback_activation(
                    room, users,
                    "AI 對手未能開始第一段發言，已撤銷扣除；請重新測試。",
                    close_gemini=True,
                )
        async with room.lock:
            roster_changed = (
                room.phase != "active"
                or room.terminal_requested
                or _room_connected_roster_signature(room) != roster_signature
                or bool(_room_start_blocker(room))
                or (room.mode == "B" and room.gemini_ws is None)
            )
            if not roster_changed:
                room.activation_ready = True
        if roster_changed:
            return await _room_rollback_activation(
                room, users,
                "成員在開始提示期間離線，已撤銷扣除；請重新測試。",
                close_gemini=True,
            )
        _room_ensure_tick(room)
        await _room_broadcast(room, room.state_msg())
        return None


async def _room_begin_precheck(room):
    async with room.lock:
        if room.phase != "lobby":
            return
        start_error = _room_start_blocker(room)
        if not start_error and room.precheck_id:
            return
        if not start_error:
            room.precheck_id = secrets.token_hex(6)
            room.precheck_results = {}
    if start_error:
        await _room_broadcast(room, {"type": "error", "message": start_error})
        return
    await _room_broadcast(room, _room_precheck_msg(room, "precheck_request"))
    await _room_broadcast(room, _room_precheck_msg(room))


async def _room_reset_precheck_if_current(room, check_id):
    """Atomically make a completed failed check retryable by the host."""
    async with room.lock:
        if room.phase != "lobby" or room.precheck_id != check_id:
            return
        room.precheck_id = None
        room.precheck_results = {}


async def _room_handle_precheck_result(room, member, msg):
    async with room.lock:
        if room.phase != "lobby" or not room.precheck_id:
            return
        if msg.get("check_id") != room.precheck_id:
            return
        room.precheck_results[member.user_id] = {
            "ok": bool(msg.get("ok")),
            "message": str(msg.get("message") or "")[:800],
        }
        users = room.connected_user_ids()
        ready = bool(
            users
            and all(user in room.precheck_results for user in users)
            and all(room.precheck_results[user].get("ok") for user in users)
        )
        complete = bool(
            users and all(user in room.precheck_results for user in users)
        )
        completed_precheck_id = room.precheck_id
        completed_roster_signature = _room_connected_roster_signature(room)
        status_msg = _room_precheck_msg(room)
        failed_msg = {**status_msg, "type": "precheck_failed"}
    await _room_broadcast(room, status_msg)

    if not complete:
        return
    if ready:
        start_error = _room_start_blocker(room)
        if start_error:
            await _room_reset_precheck_if_current(
                room, completed_precheck_id,
            )
            await _room_broadcast(room, {"type": "error", "message": start_error})
            await _room_broadcast(room, failed_msg)
            return
        activation_error = await _room_start_active(
            room, expected_precheck_id=completed_precheck_id,
            expected_roster_signature=completed_roster_signature,
        )
        if activation_error:
            await _room_reset_precheck_if_current(
                room, completed_precheck_id,
            )
            await _room_broadcast(room, {"type": "error", "message": activation_error})
            await _room_broadcast(room, failed_msg)
    else:
        await _room_reset_precheck_if_current(room, completed_precheck_id)
        await _room_broadcast(room, failed_msg)


def _room_start_blocker(room):
    if room.mode == "A":
        members = [m for m in room.members.values() if m.connected]
        if len(members) != 2 or {m.role for m in members} != {"正方", "反方"}:
            return "真人對真人練習必須兩位委員在線，並分別擔任正方及反方。"
    if room.mode == "B" and room.structure == "mock":
        members = [m for m in room.members.values() if m.connected]
        required_positions = room.required_positions()
        required_count = len(required_positions)
        if len(members) != required_count:
            return f"完整 Mock 必須啱啱好 {required_count} 位隊員在線；目前有 {len(members)} 位。"
        positions = [m.position for m in members]
        missing = [pos for pos in required_positions if pos not in positions]
        duplicate = len([p for p in positions if p]) != len(set(p for p in positions if p))
        if missing or duplicate:
            labels = room.position_labels()
            missing_text = "、".join(labels.get(pos, str(pos)) for pos in missing)
            if missing_text:
                return f"請先分配 {required_count} 個辯位；尚欠：{missing_text}。"
            return f"請先確保 {required_count} 位隊員各自選擇不同辯位。"
    return None


def _audio_fields(msg):
    """Accept either the Gemini realtimeInput shape or a flat {data,mimeType}."""
    if isinstance(msg.get("realtimeInput"), dict):
        a = msg["realtimeInput"].get("audio") or {}
        return a.get("data"), a.get("mimeType")
    return msg.get("data"), msg.get("mimeType")


def _validated_room_pcm(data, mime, *, max_bytes):
    """Return canonical PCM base64 only for a small, strict inbound frame."""
    if not isinstance(data, str) or mime != "audio/pcm;rate=16000":
        return None
    if len(data) > ((int(max_bytes) + 2) // 3) * 4:
        return None
    try:
        decoded = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        return None
    if not decoded or len(decoded) > int(max_bytes):
        return None
    return base64.b64encode(decoded).decode("ascii")


def _room_member_audio_rate_allowed(member, canonical_data, *, now_ms=None):
    """Consume decoded PCM bytes from a reconnect-stable member token bucket."""
    if not isinstance(canonical_data, str):
        return False
    padding = 2 if canonical_data.endswith("==") else (
        1 if canonical_data.endswith("=") else 0
    )
    decoded_bytes = max(0, (len(canonical_data) * 3) // 4 - padding)
    now = _now_ms() if now_ms is None else int(now_ms)
    previous = int(member.audio_rate_updated_ms)
    elapsed_ms = max(0, now - previous)
    member.audio_rate_tokens = min(
        float(ROOM_AUDIO_RATE_BURST_BYTES),
        float(member.audio_rate_tokens)
        + elapsed_ms * float(ROOM_AUDIO_RATE_BYTES_PER_SECOND) / 1000,
    )
    member.audio_rate_updated_ms = max(previous, now)
    if decoded_bytes <= 0 or decoded_bytes > member.audio_rate_tokens:
        return False
    member.audio_rate_tokens -= decoded_bytes
    return True


def _room_member_audio_message_rate_allowed(member, *, now_ms=None):
    """Bound valid and invalid audio-shaped messages before base64 validation."""
    now = _now_ms() if now_ms is None else int(now_ms)
    previous = int(member.audio_message_updated_ms)
    elapsed_ms = max(0, now - previous)
    member.audio_message_tokens = min(
        float(ROOM_AUDIO_RATE_BURST_MESSAGES),
        float(member.audio_message_tokens)
        + elapsed_ms * float(ROOM_AUDIO_RATE_MESSAGES_PER_SECOND) / 1000,
    )
    member.audio_message_updated_ms = max(previous, now)
    if member.audio_message_tokens < 1:
        return False
    member.audio_message_tokens -= 1
    return True


def _room_member_control_rate_allowed(member, *, now_ms=None):
    """Consume one reconnect-stable token for a non-audio client message."""
    now = _now_ms() if now_ms is None else int(now_ms)
    previous = int(member.control_rate_updated_ms)
    elapsed_ms = max(0, now - previous)
    member.control_rate_tokens = min(
        float(ROOM_CONTROL_RATE_BURST_MESSAGES),
        float(member.control_rate_tokens)
        + elapsed_ms * float(ROOM_CONTROL_RATE_MESSAGES_PER_SECOND) / 1000,
    )
    member.control_rate_updated_ms = max(previous, now)
    if member.control_rate_tokens < 1:
        return False
    member.control_rate_tokens -= 1
    return True


# --- Gemini leg (mode B): server-owned Gemini Live sessions per room ---------
#
# The server (not any browser) owns the single upstream Gemini Live socket, so
# the ephemeral token never leaves the server, the whole team shares one AI
# context, and a member dropping does not kill the AI. Same connect/pump shape
# Here the proxy itself is the authenticated Gemini client. Render's egress is
# only used for multiplayer; Solo audio never crosses this server.

async def _room_start_gemini_if_needed(
    room, *, resume=False, expected_generation=None,
):
    if room.mode != "B" or room.gemini is None:
        return True
    async with room.gemini_connect_lock:
        connect_epoch = room.gemini_connect_epoch
        requested_session_index = room.gemini_session_index

        def connection_is_current():
            phase = getattr(room, "phase", "active")
            allowed_phases = {"active"} if resume else {"starting", "active"}
            return (
                phase in allowed_phases
                and not getattr(room, "terminal_requested", False)
                and room.gemini_connect_epoch == connect_epoch
                and room.gemini_session_index == requested_session_index
                and (
                    expected_generation is None
                    or room.gemini_generation == expected_generation
                )
            )

        if (
            expected_generation is not None
            and room.gemini_generation != expected_generation
        ):
            return False
        if room.gemini_ws is not None:
            return True
        if not connection_is_current():
            return False
        tokens = list(room.gemini.get("tokens") or [])
        durations = list(room.gemini.get("session_minutes") or [])
        required_slots = max(1, len(durations), room.gemini_session_index + 1)
        if len(tokens) < required_slots:
            tokens.extend([""] * (required_slots - len(tokens)))
        session_index = min(room.gemini_session_index, max(0, len(tokens) - 1))
        token = tokens[session_index] if tokens else ""
        model = room.gemini.get("model") or ""
        resume_handle = room.gemini_resume_handle if resume else ""
        if not token and not resume:
            duration = (
                durations[session_index]
                if session_index < len(durations)
                else max(3.0, full_mock_total_seconds(room.segments) / 60 + 2)
            )
            token, mint_error = await asyncio.to_thread(
                _mint_gemini_live_token,
                max(3, float(duration)),
                constrained_direct=False,
            )
            if mint_error or not token or not connection_is_current():
                await _room_broadcast(room, {
                    "type": "error",
                    "message": "下一節 AI 連線憑證未能建立，房間會安全結束。",
                })
                return False
            tokens[session_index] = token
            room.gemini["tokens"] = tokens
        if not token or not model or (resume and not resume_handle):
            await _room_broadcast(room, {
                "type": "error",
                "message": "未有可恢復的 AI 連線資料，房間會安全結束。",
            })
            return False

        backend_url = f"{GEMINI_LIVE_WS_URL}?access_token={quote_plus(token)}"
        try:
            gws = await websockets.connect(
                backend_url,
                max_size=GEMINI_WS_MAX_SIZE,
                max_queue=GEMINI_WS_MAX_QUEUE,
                compression=None,
                ping_interval=None,
            )
        except Exception as exc:
            # The URL contains an ephemeral credential; log type only.
            logger.warning(
                "room Gemini connect failed (%s, %s)",
                room.code, type(exc).__name__,
            )
            return False

        if not connection_is_current():
            await gws.close()
            return False

        prompt = room.gemini.get("prompt") or ""
        if session_index and room.transcript and not resume:
            recent = room.transcript[-24:]
            continuity = "\n".join(
                f"{item.get('side') or item.get('speaker')}：{item.get('text')}"
                for item in recent if item.get("text")
            )
            if continuity:
                prompt += (
                    "\n\n## 上一節接力內容\n"
                    "以下係之前環節逐字稿，請延續同一場辯論：\n" + continuity
                )
        prompt = _bounded_live_system_prompt(prompt)
        setup = {
            "setup": {
                "model": "models/" + model,
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {"voiceName": "Kore"},
                        },
                    },
                },
                "systemInstruction": {"parts": [{"text": prompt}]},
                "realtimeInputConfig": {
                    "automaticActivityDetection": {"disabled": True},
                },
                "inputAudioTranscription": {},
                "outputAudioTranscription": {},
                "contextWindowCompression": {
                    "triggerTokens": LIVE_CONTEXT_COMPRESSION_TRIGGER_TOKENS,
                    "slidingWindow": {
                        "targetTokens": LIVE_CONTEXT_COMPRESSION_TARGET_TOKENS,
                    },
                },
                "sessionResumption": (
                    {"handle": resume_handle} if resume else {}
                ),
            },
        }
        try:
            await _room_gemini_send(room, gws, setup)
        except Exception as exc:
            logger.warning(
                "room Gemini setup failed (%s, %s)",
                room.code, type(exc).__name__,
            )
            try:
                await gws.close()
            except Exception:
                pass
            return False

        if not connection_is_current():
            await gws.close()
            return False

        room.gemini_generation += 1
        generation = room.gemini_generation
        room.gemini_ws = gws
        setup_future = asyncio.get_running_loop().create_future()
        room.gemini_setup_future = setup_future
        room.tts_enabled = (
            tts_provider_configured()
            and _get_proxy_secret("ROOM_TTS_ENABLED", "1").strip() != "0"
        )
        room.gemini_task = asyncio.create_task(
            _room_gemini_pump(room, gws, generation, resuming=resume),
        )
        try:
            setup_complete = await asyncio.wait_for(
                asyncio.shield(setup_future),
                timeout=ROOM_GEMINI_SETUP_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            await _room_close_gemini(room)
            raise
        except asyncio.TimeoutError:
            setup_complete = False
            await _room_broadcast(room, {
                "type": "error",
                "message": "AI 連線設定逾時，未收到完成確認。",
            })
        finally:
            if room.gemini_setup_future is setup_future:
                room.gemini_setup_future = None
        if (
            not setup_complete
            or room.gemini_ws is not gws
            or room.gemini_generation != generation
        ):
            await _room_close_gemini(room)
            return False
        return True


async def _room_resume_gemini(
    room, generation, reason, *, session_index=None, connect_epoch=None,
):
    expected_session = (
        room.gemini_session_index if session_index is None else session_index
    )
    expected_epoch = (
        room.gemini_connect_epoch if connect_epoch is None else connect_epoch
    )

    def still_current():
        return (
            room.phase == "active"
            and room.gemini_generation == generation
            and room.gemini_session_index == expected_session
            and room.gemini_connect_epoch == expected_epoch
        )

    if not still_current():
        return
    if not room.gemini_resume_handle:
        await _room_broadcast(room, {
            "type": "error",
            "message": "AI 連線中斷，而且未收到可恢復代碼；房間會安全結束。",
        })
        await _room_end(room, "ai_connection_lost")
        return
    for attempt in range(
        room.gemini_resume_attempts + 1,
        ROOM_GEMINI_RESUME_MAX_ATTEMPTS + 1,
    ):
        room.gemini_resume_attempts = attempt
        if ROOM_GEMINI_RESUME_DELAY_SECONDS:
            await asyncio.sleep(ROOM_GEMINI_RESUME_DELAY_SECONDS * attempt)
        if not still_current():
            return
        if await _room_start_gemini_if_needed(
            room, resume=True, expected_generation=generation,
        ):
            await _room_broadcast(room, {
                "type": "ai_reconnected", "reason": reason,
            })
            return
    await _room_broadcast(room, {
        "type": "error",
        "message": "AI 對手重新連線失敗，房間會安全結束；請返回建立新房間。",
    })
    await _room_end(room, "ai_reconnect_failed")


# --- Server-side TTS for the room (mode B) -----------------------------------
#
# When room.tts_enabled, the pump re-voices the AI: instead of forwarding
# Gemini's native audio, it synthesizes the AI's transcript once (via the shared
# _synthesize_tts) and broadcasts the same audio blob to every member — synced,
# and one synth per sentence for the whole room (not per-member). If synth fails
# mid-turn it flips to broadcasting Gemini's buffered native audio (Kore) so the
# room never goes silent. State is a per-turn dict, reset on turnComplete.

_TTS_SENTENCE_END = "。！？!?…\n"


def _tts_new_turn_state():
    # pending: transcript not yet synthesized; native: raw serverContents with
    # audio held in reserve for fallback; fallback: Azure gave up this turn.
    return {
        "pending": "", "native": [], "native_bytes": 0,
        "fallback": False, "transcript_seen": False,
        "operation_id": "tts-room-" + secrets.token_urlsafe(18),
    }


def _tts_take_sentences(buf, force=False):
    """Split a transcript buffer into (ready_sentences, remainder). Sentences end
    on _TTS_SENTENCE_END; on force (turn end) the trailing remainder flushes too."""
    chunks, start = [], 0
    for i, ch in enumerate(buf):
        if ch in _TTS_SENTENCE_END:
            piece = buf[start:i + 1].strip()
            if piece:
                chunks.append(piece)
            start = i + 1
    remainder = buf[start:]
    if force:
        piece = remainder.strip()
        if piece:
            chunks.append(piece)
        remainder = ""
    return chunks, remainder


def _strip_audio_parts(sc):
    """Copy serverContent with inlineData audio parts removed, so interrupt/turn
    signalling still reaches clients without the native audio playing."""
    mt = sc.get("modelTurn")
    if not isinstance(mt, dict):
        return sc
    parts = mt.get("parts") or []
    kept = [p for p in parts if not (p.get("inlineData") or {}).get("data")]
    if len(kept) == len(parts):
        return sc
    new_sc = dict(sc)
    new_mt = dict(mt)
    new_mt["parts"] = kept
    new_sc["modelTurn"] = new_mt
    return new_sc


async def _room_tts_fallback(room, state):
    """Give up TTS for the rest of the turn; replay the native audio held so far
    so the room hears the AI's own voice."""
    state["fallback"] = True
    await _room_broadcast(room, {"type": "tts_fallback_hint"})
    for sc in state["native"]:
        await _room_broadcast(room, {"type": "serverContent", "serverContent": sc})
    state["native"] = []
    state["native_bytes"] = 0


async def _room_tts_synth(room, chunk, state):
    """Synthesize one sentence and broadcast it to the whole room. On failure,
    flip to native-audio fallback. Returns False once fallback is active."""
    try:
        audio_bytes, mime, _usage_meta = await synthesize_tts_accounted(
            chunk,
            user_id=str(room.created_by),
            feature="tts",
            operation_id=str(state.get("operation_id") or "")
            or ("tts-room-" + secrets.token_urlsafe(18)),
            operation_stage="room_synthesis",
        )
    except Exception as exc:
        logger.info(
            "room TTS synth failed (%s, %s); using native audio",
            room.code, type(exc).__name__,
        )
        await _room_tts_fallback(room, state)
        return False
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    await _room_broadcast(room, {"type": "tts_audio", "data": b64, "mime": mime})
    return True


async def _room_pump_tts(room, sc, state, final=False):
    """Handle one serverContent under server-side TTS. Broadcasts audio-stripped
    serverContent (for interrupt/turn signals), buffers native audio for
    fallback, and synthesizes complete sentences from the transcript."""
    if state["fallback"]:
        await _room_broadcast(room, {"type": "serverContent", "serverContent": sc})
    elif sc.get("interrupted"):
        # barge-in: drop pending TTS and reserved native audio for this turn
        state["pending"] = ""
        state["native"] = []
        state["native_bytes"] = 0
        await _room_broadcast(room, {"type": "serverContent", "serverContent": _strip_audio_parts(sc)})
    else:
        parts = ((sc.get("modelTurn") or {}).get("parts")) or []
        if any((p.get("inlineData") or {}).get("data") for p in parts):
            state["native"].append(sc)
            state["native_bytes"] += len(json.dumps(sc, ensure_ascii=False))
            if state["native_bytes"] > ROOM_NATIVE_AUDIO_BUFFER_MAX_BYTES:
                await _room_tts_fallback(room, state)
                return
        ot = sc.get("outputTranscription") or {}
        if ot.get("text"):
            state["transcript_seen"] = True
            state["pending"] = (state["pending"] + str(ot["text"]))[-ROOM_PENDING_TRANSCRIPT_MAX_CHARS:]
        if (
            final and state["native"] and not state["fallback"]
            and not state["transcript_seen"]
        ):
            # Replay the native audio before forwarding turnComplete; otherwise
            # clients may tear down playback as soon as they see the final flag.
            await _room_tts_fallback(room, state)
            await _room_broadcast(
                room,
                {"type": "serverContent", "serverContent": _strip_audio_parts(sc)},
            )
            return
        stripped_message = {
            "type": "serverContent", "serverContent": _strip_audio_parts(sc),
        }
        if not final:
            await _room_broadcast(room, stripped_message)
        chunks, state["pending"] = _tts_take_sentences(state["pending"], force=final)
        for chunk in chunks:
            if not await _room_tts_synth(room, chunk, state):
                break
        if final:
            await _room_broadcast(room, stripped_message)


def _pcm_audio_bytes_and_rate(data, mime_type):
    mime = str(mime_type or "")
    if not re.match(r"^audio/(?:pcm|l16)\b", mime, re.IGNORECASE):
        return b"", 0, 0
    rate_match = re.search(r"rate=(\d+)", mime, re.IGNORECASE)
    channels_match = re.search(r"channels=(\d+)", mime, re.IGNORECASE)
    rate = int(rate_match.group(1)) if rate_match else 24_000
    channels = int(channels_match.group(1)) if channels_match else 1
    if not 8_000 <= rate <= 192_000 or not 1 <= channels <= 8:
        return b"", 0, 0
    try:
        raw = base64.b64decode(str(data or ""), validate=False)
    except Exception:
        return b"", 0, 0
    return raw, rate, channels


def _room_limit_and_account_ai_audio(room, sc):
    """Clamp Mode-B Free AI PCM to its authoritative per-side budget."""
    seg = room.current_segment()
    if (
        room.mode != "B"
        or not seg
        or seg.get("side") != "雙方"
        or room.phase not in ("starting", "active")
    ):
        return sc, False, True
    ai_side = "反方" if room.human_side == "正方" else "正方"
    budget_ms = max(0, int(seg.get("seconds") or 0) * 1000)
    if not budget_ms:
        return sc, False, True
    used_before = min(budget_ms, float(room.side_elapsed_ms.get(ai_side, 0)))
    prior_fraction = float(room.ai_audio_remainder_ms or 0.0)
    remaining_ms = max(0.0, budget_ms - used_before - prior_fraction)
    model_turn = sc.get("modelTurn")
    if not isinstance(model_turn, dict):
        return sc, False, remaining_ms > 0

    changed = False
    forwarded_audio = False
    new_parts = []
    counted_ms = 0.0
    for original in model_turn.get("parts") or []:
        part = dict(original)
        inline = part.get("inlineData") or {}
        data = inline.get("data")
        raw, rate, channels = _pcm_audio_bytes_and_rate(
            data, inline.get("mimeType"),
        ) if data else (b"", 0, 0)
        if data and (not raw or not rate):
            changed = True
            part.pop("inlineData", None)
            if part:
                new_parts.append(part)
            continue
        if not raw or not rate:
            new_parts.append(part)
            continue
        bytes_per_second = rate * channels * 2
        allowed = min(
            len(raw),
            max(0, int((remaining_ms - counted_ms) * bytes_per_second / 1000)),
        )
        if allowed <= 0:
            changed = True
            part.pop("inlineData", None)
            if part:
                new_parts.append(part)
            continue
        forwarded_audio = True
        if allowed < len(raw):
            changed = True
            clipped = dict(inline)
            clipped["data"] = base64.b64encode(raw[:allowed]).decode("ascii")
            part["inlineData"] = clipped
        new_parts.append(part)
        counted_ms += allowed * 1000 / bytes_per_second

    exact_ms = used_before + prior_fraction + counted_ms
    if budget_ms - exact_ms < 0.001:
        room.side_elapsed_ms[ai_side] = budget_ms
        room.ai_audio_remainder_ms = 0.0
    else:
        room.side_elapsed_ms[ai_side] = int(exact_ms)
        room.ai_audio_remainder_ms = exact_ms - int(exact_ms)
    exhausted = room.side_elapsed_ms.get(ai_side, 0) >= budget_ms
    newly_exhausted = exhausted and used_before < budget_ms
    if changed:
        new_sc = dict(sc)
        new_turn = dict(model_turn)
        new_turn["parts"] = new_parts
        new_sc["modelTurn"] = new_turn
        sc = new_sc
    return sc, newly_exhausted, forwarded_audio


async def _room_cue_ai_free_opening(room):
    gws = room.gemini_ws
    if gws is None:
        return False
    try:
        await _room_gemini_send(room, gws, {"clientContent": {
            "turns": [{
                "role": "user",
                "parts": [{"text": (
                    "自由辯論由正方先開始。你是正方，請立即作第一輪精簡、有內容的粵語發言，"
                    "然後停下等待反方回應。"
                )}],
            }],
            "turnComplete": True,
        }})
        await _room_broadcast(
            room, {"type": "speaking", "user_id": "AI", "speaking": True},
        )
        return True
    except Exception:
        return False


async def _room_announce_gemini_ready(room, *, initial=False):
    await _room_broadcast(room, {"type": "ai_ready"})
    ai_side = "反方" if room.human_side == "正方" else "正方"
    if room.structure == "mock" and initial:
        segment = room.current_segment()
        if segment and segment.get("side") == ai_side:
            return await _room_cue_ai_segment(room, segment)
    elif (
        room.structure == "free"
        and ai_side == "正方"
        and not room.free_opening_ai_cued
    ):
        if not await _room_cue_ai_free_opening(room):
            return False
        room.free_opening_ai_cued = True
    return True


async def _room_gemini_pump(room, gws, generation, *, resuming=False):
    """Read the room's Gemini Live socket and fan its serverContent out to every
    member. When room.tts_enabled, re-voice the AI via server-side TTS (synced,
    shared, with native-audio fallback); otherwise forward native audio verbatim.
    Also accumulate the AI's transcript into room.transcript so 評判 covers the AI."""
    pump_session_index = room.gemini_session_index
    pump_connect_epoch = room.gemini_connect_epoch
    ai_side = "反方" if room.human_side == "正方" else "正方"
    carry = room.gemini_turn_state
    if not isinstance(carry, dict):
        carry = {"ai_text": "", "tts": _tts_new_turn_state()}
        room.gemini_turn_state = carry
    ai_buffer = {"text": str(carry.get("ai_text") or "")}
    tts_state = carry.get("tts")
    if not isinstance(tts_state, dict):
        tts_state = _tts_new_turn_state()
        carry["tts"] = tts_state
    resume_reason = "upstream_closed"
    try:
        async for raw in gws:
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except Exception:
                    continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            update = msg.get("sessionResumptionUpdate")
            if isinstance(update, dict):
                handle = str(update.get("newHandle") or "")
                if update.get("resumable") and handle:
                    if (
                        room.gemini_ws is gws
                        and room.gemini_generation == generation
                    ):
                        room.gemini_resume_handle = handle
                elif update.get("resumable") is False and (
                    room.gemini_ws is gws
                    and room.gemini_generation == generation
                ):
                    room.gemini_resume_handle = ""
                continue
            if "goAway" in msg:
                resume_reason = "go_away"
                await _room_broadcast(room, {
                    "type": "ai_reconnecting",
                    "message": "AI 連線將重設，正在安全恢復同一場對話。",
                })
                break
            # NB: Gemini sends setupComplete as an empty object {}, which is
            # falsy in Python — must test membership, not truthiness.
            if "setupComplete" in msg:
                room.gemini_resume_attempts = 0
                setup_future = room.gemini_setup_future
                if (
                    room.gemini_ws is gws
                    and room.gemini_generation == generation
                    and setup_future is not None
                    and not setup_future.done()
                ):
                    setup_future.set_result(True)
                # Initial activation keeps phase=starting until the roster is
                # revalidated.  Do not emit/cue any AI output before that atomic
                # publish; resumption already occurs in an active room.
                if room.phase == "active":
                    announced = await _room_announce_gemini_ready(
                        room, initial=not resuming,
                    )
                    if not announced:
                        await _room_broadcast(room, {
                            "type": "error",
                            "message": "AI 未能恢復本節發言，房間會安全結束。",
                        })
                        await _room_end(room, "ai_ready_cue_failed")
                        return
                continue
            sc = msg.get("serverContent")
            if sc is None:
                continue
            sc, ai_budget_reached, forwarded_ai_audio = (
                _room_limit_and_account_ai_audio(room, sc)
            )
            turn_complete = bool(sc.get("turnComplete"))
            seg = room.current_segment()
            ai_budget_exhausted = bool(
                seg and seg.get("side") == "雙方"
                and room.side_elapsed_ms.get(ai_side, 0)
                >= int((seg.get("seconds") or 0) * 1000)
            )
            if room.tts_enabled and (forwarded_ai_audio or not ai_budget_exhausted):
                await _room_pump_tts(room, sc, tts_state, final=turn_complete)
            else:
                await _room_broadcast(
                    room,
                    {"type": "serverContent", "serverContent": _strip_audio_parts(sc)
                     if room.tts_enabled else sc},
                )
            if ai_budget_reached:
                try:
                    # Manual activity start is the Live API barge-in signal and
                    # stops further model audio without closing the resumable session.
                    await _room_gemini_send(
                        room, gws, {"realtimeInput": {"activityStart": {}}},
                    )
                except Exception:
                    pass
                await _room_broadcast(room, {
                    "type": "side_budget_exhausted", "side": ai_side,
                    "message": f"{ai_side}已用完本節發言時間。",
                })
                await _room_broadcast(room, room.state_msg())
            ot = sc.get("outputTranscription") or {}
            if ot.get("text"):
                ai_buffer["text"] = (ai_buffer["text"] + str(ot["text"]))[-ROOM_PENDING_TRANSCRIPT_MAX_CHARS:]
                carry["ai_text"] = ai_buffer["text"]
            if turn_complete:
                if room.tts_enabled:
                    tts_state = _tts_new_turn_state()
                    carry["tts"] = tts_state
                text_value = ai_buffer["text"].strip()
                ai_buffer["text"] = ""
                carry["ai_text"] = ""
                if text_value:
                    item = {
                        "speaker": "AI", "side": ai_side, "seg": room.seg_index,
                        "label": (room.current_segment() or {}).get("label", ""),
                        "text": text_value[:ROOM_TRANSCRIPT_ITEM_MAX_CHARS], "created_ms": _now_ms(),
                    }
                    room.transcript.append(item)
                    room.transcript = room.transcript[-ROOM_TRANSCRIPT_MAX_ITEMS:]
                    room.transcript_revision += 1
                    await _room_broadcast(room, {"type": "transcript", "item": item})
                if (
                    room.structure == "free"
                    and ai_side == "正方"
                    and room.free_opening_ai_cued
                    and not room.free_opening_ai_complete
                ):
                    room.free_opening_ai_complete = True
                    room.free_first_done = True
                await _room_broadcast(room, {"type": "speaking", "user_id": "AI",
                                             "speaking": False})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        resume_reason = "unexpected_close"
        logger.info(
            "room Gemini pump ended (%s, %s)", room.code, type(exc).__name__,
        )
    finally:
        is_current = (
            room.gemini_ws is gws
            and room.gemini_generation == generation
        )
        if is_current:
            setup_future = room.gemini_setup_future
            if setup_future is not None and not setup_future.done():
                setup_future.set_result(False)
            room.gemini_ws = None
            if room.gemini_task is asyncio.current_task():
                room.gemini_task = None
        try:
            await gws.close()
        except Exception:
            pass
        if is_current and room.phase == "active":
            await _room_resume_gemini(
                room, generation, resume_reason,
                session_index=pump_session_index,
                connect_epoch=pump_connect_epoch,
            )


async def _room_forward_audio_to_gemini(room, member, data, mime):
    gws = room.gemini_ws
    if gws is None or not data:
        return
    try:
        await _room_gemini_send(room, gws, {
            "realtimeInput": {"audio": {"data": data,
                                        "mimeType": mime or "audio/pcm;rate=16000"}}
        })
    except Exception:
        pass


async def _room_close_gemini(room):
    room.gemini_connect_epoch += 1
    setup_future = room.gemini_setup_future
    room.gemini_setup_future = None
    if setup_future is not None and not setup_future.done():
        setup_future.set_result(False)
    task = room.gemini_task
    room.gemini_task = None
    ws = room.gemini_ws
    room.gemini_ws = None
    if (
        task is not None and task is not asyncio.current_task()
        and not task.done()
    ):
        task.cancel()
    if ws is not None:
        try:
            await ws.close()
        except Exception:
            pass


def _human_transcript_since_last_ai(room):
    """The human speeches accumulated since the AI last spoke — used as context
    when cueing the AI for its next mock segment."""
    lines = []
    for item in reversed(room.transcript):
        if item.get("speaker") == "AI":
            break
        lines.append(item)
    lines.reverse()
    return "\n".join(f"{i.get('side') or i.get('speaker')}：{i.get('text')}" for i in lines)


async def _room_cue_ai_segment(room, seg):
    """Mock structure: tell the server-owned Gemini session to deliver the
    current AI-side segment as a full speech, grounded in the latest human
    transcript. (Free structure uses audio streaming instead — see
    _room_handle_audio / _room_handle_turn.)"""
    gws = room.gemini_ws
    if gws is None or not seg:
        return False
    ai_side = "反方" if room.human_side == "正方" else "正方"
    context = _human_transcript_since_last_ai(room)
    ctx = f"\n\n對手（{room.human_side}）剛才的發言重點：\n{context}" if context else ""
    seg_secs = int(seg.get("seconds") or 0)
    word_min = round((seg_secs / 60) * 250)
    word_max = round((seg_secs / 60) * 300)
    cue = _fill_live_runtime_prompt(LIVE_RUNTIME_PROMPTS["segment_announce"], {
        "label": seg.get("label") or "",
        "side": seg.get("side") or "",
        "secs": seg_secs,
        "word_min": word_min,
        "word_max": word_max,
    }) + ctx
    try:
        await _room_gemini_send(room, gws, {"clientContent": {
            "turns": [{"role": "user", "parts": [{"text": cue}]}],
            "turnComplete": True,
        }})
        await _room_broadcast(room, {"type": "speaking", "user_id": "AI",
                                     "speaking": True})
        return True
    except Exception:
        return False


async def _room_on_segment_enter(room):
    """When the host advances to a new mock segment that belongs to the AI, cue
    Gemini to speak it. No-op for mode A / free structure."""
    if room.mode != "B" or room.structure != "mock":
        return
    seg = room.current_segment()
    if not seg:
        return
    target_session = int(seg.get("session") or 0)
    if target_session != room.gemini_session_index:
        await _room_close_gemini(room)
        room.gemini_session_index = target_session
        room.gemini_resume_handle = ""
        room.gemini_resume_attempts = 0
        room.gemini_turn_state = None
        connected = False
        for attempt in range(1, ROOM_GEMINI_RESUME_MAX_ATTEMPTS + 1):
            if attempt > 1 and ROOM_GEMINI_RESUME_DELAY_SECONDS:
                await asyncio.sleep(ROOM_GEMINI_RESUME_DELAY_SECONDS * attempt)
            if room.phase != "active" or room.gemini_session_index != target_session:
                return
            connected = await _room_start_gemini_if_needed(room)
            if connected:
                break
            # A one-use token may have been consumed by a failed handshake.
            # Clear only this planned section so the retry mints a fresh JIT token.
            tokens = list(room.gemini.get("tokens") or [])
            if target_session < len(tokens):
                tokens[target_session] = ""
                room.gemini["tokens"] = tokens
        if not connected:
            await _room_broadcast(room, {
                "type": "error",
                "message": "下一節 AI 連線未能建立，房間會安全結束。",
            })
            await _room_end(room, "ai_section_connect_failed")
        return
    ai_side = "反方" if room.human_side == "正方" else "正方"
    if seg.get("side") == ai_side:
        if not await _room_cue_ai_segment(room, seg):
            await _room_broadcast(room, {
                "type": "error",
                "message": "AI 未能開始本節發言，房間會安全結束。",
            })
            await _room_end(room, "ai_segment_cue_failed")


async def _room_advance_segment(room, index: int, *, expected_from=None):
    """Move the authoritative server timer without trusting client clocks."""
    async with room.segment_lock:
        if room.phase != "active" or not room.activation_ready:
            return
        if expected_from is not None and room.seg_index != expected_from:
            return
        now = _now_ms()
        if room.active_turn_side in room.side_elapsed_ms and room.active_turn_started_ms is not None:
            room.side_elapsed_ms[room.active_turn_side] += max(0, now - room.active_turn_started_ms)
        room.seg_index = max(0, min(int(index), len(room.segments) - 1))
        room.seg_started_ms = now
        room.active_turn_user = None
        room.active_turn_side = None
        room.active_turn_started_ms = None
        room.free_first_done = False
        room.free_opening_ai_cued = False
        room.free_opening_ai_complete = False
        room.ai_audio_remainder_ms = 0.0
        if room.current_segment() and room.current_segment().get("side") == "雙方":
            room.side_elapsed_ms = {"正方": 0, "反方": 0}
        await _room_broadcast(room, room.state_msg())
        await _room_on_segment_enter(room)
        if room.phase == "active" and room.seg_index == max(
            0, min(int(index), len(room.segments) - 1),
        ):
            # Handoff/setup latency is not debate speech time.
            room.seg_started_ms = _now_ms()
            await _room_broadcast(room, room.state_msg())


async def _room_end(room, reason: str = "host"):
    # Serialize the terminal transition with activation.  Otherwise a host end
    # arriving while quota/token work is in flight can be overwritten by the
    # activation coroutine setting the room back to ``active``.
    room.terminal_requested = True
    async with room.activation_lock:
        if room.phase in ("ending", "ended"):
            return
        room.phase = "ending"
    current = asyncio.current_task()
    if room.tick_task is not None and room.tick_task is not current and not room.tick_task.done():
        room.tick_task.cancel()
    if (
        room.lifecycle_task is not None
        and room.lifecycle_task is not current
        and not room.lifecycle_task.done()
    ):
        room.lifecycle_task.cancel()
    _room_cancel_empty_cleanup(room)
    await _room_close_gemini(room)
    # Final transcript judgement is a single shared provider call.  Preserve an
    # in-flight request, or start one before clients are told the room ended.
    if (
        room.transcript
        and room.judgement_revision != room.transcript_revision
    ):
        existing = room.judgement_task
        if existing is None or existing.done():
            task = asyncio.create_task(_room_request_judgement(room))
        else:
            # Queue a final-revision request behind the in-flight lock.  It will
            # reuse the result if that request already covered this revision.
            task = asyncio.create_task(_room_request_judgement(room))
        room.judgement_task = task
        if task is not current:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=ROOM_FINAL_JUDGEMENT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                await _room_broadcast(room, {
                    "type": "judgement_pending",
                    "message": "完場評判仍在處理，可稍後重新開啟逐字稿查看結果。",
                })
            except Exception as exc:
                logger.warning(
                    "room final judgement failed (%s, %s)",
                    room.code, type(exc).__name__,
                )
    room.phase = "ended"
    room.ended_at_ms = _now_ms()
    room.ended_retain_until_ms = max(
        room.ended_retain_until_ms,
        room.ended_at_ms + ROOM_EMPTY_GRACE_MS,
    )
    try:
        await _room_broadcast(room, {"type": "ended", "reason": reason})
        try:
            await asyncio.to_thread(_record_room_bandwidth_once, room)
        except Exception as exc:
            logger.warning(
                "room final bandwidth checkpoint failed (%s, %s)",
                room.code, type(exc).__name__,
            )
        sockets = [member.ws for member in room.members.values() if member.connected]
        if sockets:
            await asyncio.gather(*(
                socket.close(code=1000, reason="practice ended") for socket in sockets
            ), return_exceptions=True)
    finally:
        room.end_complete_event.set()


async def _room_end_and_remove(room, reason):
    """Finish a GC/empty room while retaining its member-only result window."""
    await _room_end(room, reason)
    if room.phase != "ended":
        try:
            await asyncio.wait_for(
                asyncio.shield(room.end_complete_event.wait()),
                timeout=ROOM_FINAL_JUDGEMENT_TIMEOUT_SECONDS + 5,
            )
        except asyncio.TimeoutError:
            return
    if room.phase == "ended":
        room.ended_retain_until_ms = max(
            room.ended_retain_until_ms,
            _now_ms() + ROOM_EMPTY_GRACE_MS,
        )


# --- message handling ------------------------------------------------------

def _parse_room_client_text(value) -> dict | None:
    """Reject oversized/non-object room messages before business dispatch."""
    if not isinstance(value, str) or len(value) > ROOM_WS_TEXT_MAX_BYTES:
        return None
    if len(value.encode("utf-8")) > ROOM_WS_TEXT_MAX_BYTES:
        return None
    try:
        message = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return message if isinstance(message, dict) else None


async def _room_handle_audio(room, member, msg):
    if room.phase != "active" or not room.activation_ready:
        return
    seg = room.current_segment()
    if not seg or seg.get("side") == "準備":
        return
    # Mode B: humans only speak on their own side or the free (雙方) segment; the
    # AI owns the other side's segments (server cues Gemini there — no human mic).
    if room.mode == "B" and seg.get("side") not in (room.human_side, "雙方"):
        return
    active = room.active_speaker()
    if active is not None and member.user_id != active:
        return  # defense-in-depth: drop non-active speaker's audio
    if room.active_turn_user != member.user_id:
        return  # audio always requires a server-accepted active turn
    if room.is_open_free_segment() and member.role in room.side_elapsed_ms:
        used = room.side_elapsed_ms.get(member.role, 0)
        if room.active_turn_side == member.role and room.active_turn_started_ms is not None:
            used += max(0, _now_ms() - room.active_turn_started_ms)
        if used >= int((seg.get("seconds") or 0) * 1000):
            return
    data, mime = _audio_fields(msg)
    data = _validated_room_pcm(
        data, mime, max_bytes=ROOM_AUDIO_FRAME_MAX_BYTES,
    )
    if data is None:
        return
    if not _room_member_audio_rate_allowed(member, data):
        return
    await _room_broadcast(
        room,
        {"type": "peer_audio", "from": member.user_id, "data": data,
         "mimeType": mime or "audio/pcm;rate=16000"},
        exclude=member.user_id,
    )
    # Free structure streams the human's audio to Gemini (the AI hears them).
    # Mock structure is text-cue driven (see _room_cue_ai_segment) — the AI is
    # cued from the SpeechRecognition transcript, not the raw audio.
    if room.mode == "B" and room.is_open_free_segment():
        await _room_forward_audio_to_gemini(room, member, data, mime)


async def _room_handle_turn(room, member, speaking):
    if room.phase != "active" or not room.activation_ready:
        return
    now = _now_ms()
    if speaking:
        if not member.connected:
            return
        if (
            room.mode == "B"
            and room.structure == "free"
            and room.human_side == "反方"
            and not room.free_opening_ai_complete
        ):
            await member.ws.send_text(json.dumps({
                "type": "turn_rejected",
                "message": "自由辯論由正方先發言，請先等待 AI 完成開場。",
            }, ensure_ascii=False))
            return
        if room.active_turn_user == member.user_id:
            return
        if room.active_turn_user and room.active_turn_user != member.user_id:
            await member.ws.send_text(json.dumps({
                "type": "turn_rejected",
                "message": "已有成員發言中，請等待對方停止後再開始。",
            }, ensure_ascii=False))
            return
        seg = room.current_segment()
        if room.mode == "B" and seg and seg.get("side") not in (room.human_side, "雙方"):
            await member.ws.send_text(json.dumps({
                "type": "turn_rejected",
                "message": "呢段輪到 AI 發言。",
            }, ensure_ascii=False))
            return
        active = room.active_speaker()
        if active is not None and member.user_id != active:
            await member.ws.send_text(json.dumps({
                "type": "turn_rejected",
                "message": "呢段未輪到你嘅辯位發言。",
            }, ensure_ascii=False))
            return
        expected_side = room.expected_turn_side()
        if expected_side and member.role != expected_side:
            await member.ws.send_text(json.dumps({
                "type": "error",
                "message": f"自由辯論由{expected_side}先發言。",
            }, ensure_ascii=False))
            return
        if room.is_open_free_segment() and member.role in room.side_elapsed_ms:
            if room.side_elapsed_ms.get(member.role, 0) >= int((seg.get("seconds") or 0) * 1000):
                return
        room.active_turn_user = member.user_id
        room.active_turn_side = member.role
        room.active_turn_started_ms = now
    else:
        if room.active_turn_user != member.user_id:
            return
        if room.active_turn_side in room.side_elapsed_ms and room.active_turn_started_ms is not None:
            elapsed = max(0, now - room.active_turn_started_ms)
            if room.is_open_free_segment():
                limit_ms = int((room.current_segment() or {}).get("seconds") or 0) * 1000
                room.side_elapsed_ms[room.active_turn_side] = min(
                    limit_ms,
                    room.side_elapsed_ms.get(room.active_turn_side, 0) + elapsed,
                )
            else:
                room.side_elapsed_ms[room.active_turn_side] += elapsed
        if (room.current_segment() or {}).get("side") == "雙方" and room.active_turn_side == "正方":
            room.free_first_done = True
        room.active_turn_user = None
        room.active_turn_side = None
        room.active_turn_started_ms = None
    await _room_broadcast(
        room, {"type": "speaking", "user_id": member.user_id, "speaking": speaking},
    )
    await _room_broadcast(room, room.state_msg())
    # Free structure: bracket the human's streamed audio with activity markers so
    # Gemini generates a rebuttal after each turn.
    if room.mode == "B" and room.is_open_free_segment() and room.gemini_ws is not None:
        try:
            key = "activityStart" if speaking else "activityEnd"
            ai_side = "反方" if room.human_side == "正方" else "正方"
            ai_limit = int((room.current_segment() or {}).get("seconds") or 0) * 1000
            if speaking or room.side_elapsed_ms.get(ai_side, 0) < ai_limit:
                await _room_gemini_send(
                    room, room.gemini_ws, {"realtimeInput": {key: {}}},
                )
        except Exception:
            pass
    # Mock structure: after a human finishes a 雙方 (free-debate) turn, cue the AI
    # to give a short rebuttal from the transcript.
    if (room.mode == "B" and room.structure == "mock" and not speaking
            and room.is_open_free_segment()):
        await _room_cue_ai_segment(room, room.current_segment())


async def _room_handle_transcript(room, member, msg):
    if room.phase != "active" or not room.activation_ready:
        return
    # Browser speech recognition is accepted only from the authoritative active
    # speaker.  This prevents an idle member from poisoning/evicting the bounded
    # judgement transcript with fabricated entries.
    if room.active_turn_user != member.user_id:
        return
    text_value = str(msg.get("text") or "").strip()
    if not text_value:
        return
    item = {
        "speaker": member.user_id,
        "side": member.role or "",
        "seg": room.seg_index,
        "label": (room.current_segment() or {}).get("label", ""),
        "text": text_value[:ROOM_TRANSCRIPT_ITEM_MAX_CHARS],
        "created_ms": _now_ms(),
    }
    room.transcript.append(item)
    room.transcript = room.transcript[-ROOM_TRANSCRIPT_MAX_ITEMS:]
    room.transcript_revision += 1
    await _room_broadcast(room, {"type": "transcript", "item": item})


async def _room_request_judgement(room):
    async with room.judgement_lock:
        try:
            if (
                room.judgement
                and getattr(room, "judgement_revision", -1)
                == getattr(room, "transcript_revision", 0)
            ):
                await _room_broadcast(
                    room, {"type": "judgement", "text": room.judgement},
                )
                return
            await _room_request_judgement_unlocked(room)
        finally:
            # A final judgement may complete after _room_end's bounded wait.
            # Give members a full retrieval window from that late completion,
            # instead of letting the next room-create GC immediately erase it.
            if getattr(room, "phase", "active") in ("ending", "ended"):
                room.ended_retain_until_ms = max(
                    getattr(room, "ended_retain_until_ms", 0),
                    _now_ms() + ROOM_EMPTY_GRACE_MS,
                )


def _log_room_judgement_attempt(
    room,
    model,
    success,
    *,
    operation_id,
    operation_stage,
    response_data=None,
    error_message="",
):
    """Best-effort AI-fund row for one real final-judgement HTTP attempt."""
    try:
        from core.funds_logic import log_ai_usage

        try:
            model_label, config = get_model_by_slug(model)
        except KeyError:
            # Tests and emergency centrally supplied overrides still get a
            # transparent zero-rate provider-call row instead of disappearing.
            model_label = str(model or "unknown")[:200]
            config = {"provider": "gemini"}
        actual = _provider_usage(response_data or {}, "gemini") if response_data else {}
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
            getattr(room, "created_by", None),
            "full_mock_live" if getattr(room, "structure", "") == "mock" else "free_debate_live",
            success,
            usage={
                "model_label": model_label,
                "provider": config.get("provider") or "gemini",
                "estimated_cost_usd": usd,
                "estimated_cost_hkd": usd * 7.8,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "audio_tokens": audio_tokens,
                "search_calls": 0,
                "cost_source": actual.get("cost_source") or (
                    "provider_attempt_unknown_usage" if not success else "estimate"
                ),
                "operation_id": str(operation_id or "")[:200],
                "operation_stage": str(operation_stage or "")[:80],
            },
            error_message=str(error_message or "")[:300],
            db=get_vote_db(),
        )
    except Exception as exc:
        logger.warning(
            "Room judgement usage ledger write failed: %s", type(exc).__name__
        )


async def _room_request_judgement_unlocked(room):
    target_revision = getattr(room, "transcript_revision", 0)
    budget_error = _bandwidth_essential_gate_error()
    if budget_error:
        room.judgement = budget_error
        room.judgement_revision = target_revision
        await _room_broadcast(room, {"type": "judgement", "text": budget_error})
        return
    if not room.transcript:
        result = "暫時未有逐字稿，AI 評判未能判定哪一方勝出。請先完成發言，或使用支援語音轉文字的瀏覽器。"
        room.judgement = result
        room.judgement_revision = target_revision
        await _room_broadcast(room, {"type": "judgement", "text": result})
        return

    api_key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not api_key:
        result = "未設定 GEMINI_API_KEY，暫時無法使用 AI 評判。"
        room.judgement = result
        room.judgement_revision = target_revision
        await _room_broadcast(room, {"type": "judgement", "text": result})
        return

    await _room_broadcast(room, {"type": "judgement_pending"})
    prompt_text = build_room_judgement_prompt(
        room.topic,
        room.debate_format,
        room.structure,
        room.transcript,
    )[:AI_PROVIDER_PROMPT_MAX_CHARS]
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt_text}],
        }],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200},
    }
    last_error = ""
    result = ""
    operation_id = (
        "room-judgement-"
        + str(getattr(room, "code", "room"))[:40]
        + "-"
        + str(target_revision)
        + "-"
        + secrets.token_urlsafe(12)
    )
    try:
        async with httpx.AsyncClient(timeout=ROOM_JUDGEMENT_TIMEOUT_SECONDS) as client:
            for attempt_number, model in enumerate(ROOM_JUDGEMENT_MODELS, 1):
                operation_stage = f"judgement_attempt_{attempt_number}"
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent"
                )
                try:
                    data = await post_json_bounded(
                        client, url, max_bytes=AI_PROVIDER_RESPONSE_MAX_BYTES,
                        headers={"x-goog-api-key": api_key}, json=payload,
                    )
                except httpx.TimeoutException:
                    last_error = f"{model}：AI服務逾時"
                    logger.warning("Room judgement Gemini failed %s", last_error)
                    _log_room_judgement_attempt(
                        room,
                        model,
                        False,
                        operation_id=operation_id,
                        operation_stage=operation_stage,
                        error_message=last_error,
                    )
                    continue
                except httpx.HTTPStatusError as exc:
                    last_error = f"{model}：AI服務HTTP {exc.response.status_code}錯誤"
                    logger.warning("Room judgement Gemini failed %s", last_error)
                    _log_room_judgement_attempt(
                        room,
                        model,
                        False,
                        operation_id=operation_id,
                        operation_stage=operation_stage,
                        error_message=last_error,
                    )
                    continue
                except httpx.HTTPError as exc:
                    last_error = f"{model}：AI服務連線錯誤"
                    logger.warning(
                        "Room judgement Gemini transport failed (%s)",
                        type(exc).__name__,
                    )
                    _log_room_judgement_attempt(
                        room,
                        model,
                        False,
                        operation_id=operation_id,
                        operation_stage=operation_stage,
                        error_message=last_error,
                    )
                    continue
                except ValueError as exc:
                    detail = str(exc)
                    if "exceeds server limit" in detail:
                        detail = "AI回應超過2MiB伺服器上限"
                    elif "empty" in detail:
                        detail = "AI回應為空"
                    else:
                        detail = "AI回應格式無效"
                    last_error = f"{model}：{detail}"
                    logger.warning("Room judgement Gemini failed %s", last_error)
                    _log_room_judgement_attempt(
                        room,
                        model,
                        False,
                        operation_id=operation_id,
                        operation_stage=operation_stage,
                        error_message=last_error,
                    )
                    continue
                except Exception as exc:
                    _log_room_judgement_attempt(
                        room,
                        model,
                        False,
                        operation_id=operation_id,
                        operation_stage=operation_stage,
                        error_message=f"{model}：AI服務發生非預期錯誤",
                    )
                    raise
                candidates = data.get("candidates") or []
                if not candidates or not isinstance(candidates[0], dict):
                    last_error = f"{model}：AI未有回傳候選結果"
                    _log_room_judgement_attempt(
                        room,
                        model,
                        False,
                        operation_id=operation_id,
                        operation_stage=operation_stage,
                        response_data=data,
                        error_message=last_error,
                    )
                    continue
                content = candidates[0].get("content") or {}
                if not isinstance(content, dict):
                    content = {}
                parts = content.get("parts") or []
                result = "\n".join(
                    str(part.get("text", ""))
                    for part in parts
                    if isinstance(part, dict)
                ).strip()
                if result:
                    _log_room_judgement_attempt(
                        room,
                        model,
                        True,
                        operation_id=operation_id,
                        operation_stage=operation_stage,
                        response_data=data,
                    )
                    break
                last_error = f"{model}：AI回應為空"
                _log_room_judgement_attempt(
                    room,
                    model,
                    False,
                    operation_id=operation_id,
                    operation_stage=operation_stage,
                    response_data=data,
                    error_message=last_error,
                )
            else:
                result = (
                    "AI 評判暫時失敗。"
                    + (f"\n原因：{last_error}" if last_error else "")
                    + "\n請檢查 GEMINI_API_KEY、模型權限或稍後再試。"
                )
    except Exception as e:
        # Provider exceptions can include request URLs or headers.  Neither the
        # room transcript nor server logs should ever receive those details.
        logger.warning("Room judgement failed (%s)", type(e).__name__)
        result = (
            "AI 評判暫時無法連線。"
            "\n原因：上游服務連線錯誤。"
            "\n請檢查伺服器網絡或 GEMINI_API_KEY。"
        )

    room.judgement = result
    room.judgement_revision = target_revision
    await _room_broadcast(room, {"type": "judgement", "text": result})


async def _room_handle_message(
    room, member, msg, *, websocket=None, generation=None,
):
    if websocket is not None and (
        member.ws is not websocket
        or member.connection_generation != generation
        or not member.connected
    ):
        return
    mtype = msg.get("type")
    if mtype == "audio" or "realtimeInput" in msg:
        if not _room_member_audio_message_rate_allowed(member):
            return
        await _room_handle_audio(room, member, msg)
        return
    if not _room_member_control_rate_allowed(member):
        return

    is_host = member.user_id == room.created_by

    if mtype == "claim_role":
        side = msg.get("side")
        if room.phase == "lobby" and room.mode == "A" and side in ("正方", "反方"):
            if all(m.role != side or m.user_id == member.user_id
                   for m in room.members.values()):
                member.role = side
                await _room_broadcast(room, {
                    "type": "roster", "roster": room.roster(),
                    "position_labels": room.position_labels(),
                    "required_positions": room.required_positions(),
                })
        return

    if mtype == "claim_position":
        if room.mode == "B" and room.structure == "mock" and room.phase == "lobby":
            try:
                position = int(msg.get("position") or 0)
            except Exception:
                position = 0
            if position in room.required_positions():
                if all(not m.connected or m.position != position or m.user_id == member.user_id
                       for m in room.members.values()):
                    member.position = position
                    await _room_broadcast(room, {
                        "type": "roster", "roster": room.roster(),
                        "position_labels": room.position_labels(),
                        "required_positions": room.required_positions(),
                    })
                else:
                    await member.ws.send_text(json.dumps({
                        "type": "error",
                        "message": "呢個辯位已經有人選咗。",
                    }, ensure_ascii=False))
        return

    if mtype == "start" and is_host:
        await _room_begin_precheck(room)
        return

    if mtype in ("next_segment", "set_segment") and is_host:
        if mtype == "set_segment":
            try:
                idx = int(msg.get("index", room.seg_index))
            except Exception:
                idx = room.seg_index
        else:
            idx = room.seg_index + 1
        await _room_advance_segment(room, idx)
        return

    if mtype == "end" and is_host:
        await _room_end(room, "host")
        return

    if mtype in ("turn_begin", "turn_end"):
        await _room_handle_turn(room, member, mtype == "turn_begin")
        return

    if mtype == "precheck_result":
        await _room_handle_precheck_result(room, member, msg)
        return

    if mtype == "transcript":
        await _room_handle_transcript(room, member, msg)
        return

    if mtype == "request_judgement" and is_host:
        if (
            room.judgement
            and room.judgement_revision == room.transcript_revision
        ):
            await member.ws.send_text(json.dumps({
                "type": "judgement", "text": room.judgement,
            }, ensure_ascii=False))
        elif room.judgement_task is None or room.judgement_task.done():
            room.judgement_task = asyncio.create_task(_room_request_judgement(room))
        return

    if mtype == "test_ping":
        client_ts = msg.get("client_ts")
        if (
            isinstance(client_ts, bool)
            or not isinstance(client_ts, (int, float))
            or not math.isfinite(client_ts)
            or client_ts < 0
            or client_ts > 10**16
        ):
            return
        await member.ws.send_text(json.dumps({
            "type": "test_pong",
            "client_ts": client_ts,
            "server_now_ms": _now_ms(),
        }, ensure_ascii=False))
        return

    if mtype == "heartbeat":
        await member.ws.send_text(json.dumps({"type": "heartbeat_ack", "server_now_ms": _now_ms()}))
        return

    if mtype == "test_audio":
        if room.phase != "lobby":
            return
        data, mime = _audio_fields(msg)
        # The real 0.7-second test tone is 22,400 bytes. Reject large or
        # malformed payloads before fan-out so a lobby cannot amplify arbitrary
        # near-frame-limit base64 blobs to every peer.
        data = _validated_room_pcm(
            data, mime, max_bytes=ROOM_TEST_AUDIO_MAX_BYTES,
        )
        if data is None:
            return
        now = _now_ms()
        if now - member.last_test_audio_ms < ROOM_TEST_AUDIO_COOLDOWN_MS:
            return
        member.last_test_audio_ms = now
        # A lobby can otherwise stay connected forever and amplify small input
        # into per-peer egress without the active debate tick ever running.
        try:
            await asyncio.to_thread(_checkpoint_room_bandwidth, room, False)
            room.last_bandwidth_checkpoint_ms = now
            budget_error = await asyncio.to_thread(_bandwidth_live_gate_error)
        except Exception as exc:
            logger.warning(
                "room lobby bandwidth check failed (%s, %s)",
                room.code, type(exc).__name__,
            )
            await _room_end(room, "server_lifecycle_failure")
            return
        if budget_error:
            await _room_end(room, "monthly_bandwidth_limit")
            return
        test_id = secrets.token_hex(8)
        expires_ms = now + ROOM_TEST_AUDIO_ACK_TTL_MS
        for recipient in list(room.members.values()):
            if not recipient.connected or recipient.user_id == member.user_id:
                continue
            for pending_id, (_source, expiry) in list(
                recipient.pending_test_audio.items()
            ):
                if expiry < now:
                    recipient.pending_test_audio.pop(pending_id, None)
            while len(recipient.pending_test_audio) >= ROOM_TEST_AUDIO_PENDING_MAX:
                recipient.pending_test_audio.pop(
                    next(iter(recipient.pending_test_audio)), None,
                )
            recipient.pending_test_audio[test_id] = (
                member.user_id, expires_ms,
            )
        await _room_broadcast(
            room,
            {"type": "test_audio", "from": member.user_id,
             "test_id": test_id,
             "data": data,
             "mimeType": mime},
            exclude=member.user_id,
        )
        return

    if mtype == "test_received":
        if room.phase != "lobby":
            return
        now = _now_ms()
        if now - member.last_test_received_ms < ROOM_TEST_RECEIVED_COOLDOWN_MS:
            return
        source = msg.get("source")
        test_id = msg.get("test_id")
        if not isinstance(source, str) or not isinstance(test_id, str):
            return
        pending = member.pending_test_audio.get(test_id)
        if pending is None:
            return
        expected_source, expires_ms = pending
        source_member = room.members.get(expected_source)
        if (
            source != expected_source
            or source == member.user_id
            or now > expires_ms
            or source_member is None
            or not source_member.connected
        ):
            return
        member.pending_test_audio.pop(test_id, None)
        member.last_test_received_ms = now
        await _room_broadcast(
            room,
            {"type": "test_received", "from": member.user_id,
             "source": expected_source},
        )
        return

    if mtype == "chat":
        if room.phase in ("ending", "ended"):
            return
        await _room_broadcast(room, {"type": "chat", "from": member.user_id,
                                     "text": str(msg.get("text", ""))[:500]})
        return


def _build_room_plan(
    code, mode, user_id, debate_format, topic, structure, free_minutes,
    capacity, payload,
):
    """Build one lobby without mutating the global room registry."""
    room = Room(
        code, mode, user_id, debate_format, topic, structure,
        free_minutes, capacity,
    )
    if mode == "A":
        side = payload.get("side")
        room.creator_side = side if side in ("正方", "反方") else "正方"
        return room

    human_side = payload.get("human_side")
    room.human_side = human_side if human_side in ("正方", "反方") else "正方"
    prompt = (
        build_full_mock_live_prompt(
            topic, room.human_side, debate_format,
            free_debate_minutes=free_minutes if debate_format == "聯中" else None,
        )
        if structure == "mock"
        else build_free_debate_live_prompt(topic, room.human_side, "")
    )
    if structure == "mock":
        sessions = split_mock_into_sessions(room.segments)
        room.segments = [
            {**segment, "session": session_index}
            for session_index, session in enumerate(sessions)
            for segment in session["segments"]
        ]
        session_labels = [session["label"] for session in sessions]
        session_minutes = [
            max(3, full_mock_total_seconds(session["segments"]) / 60 + 2)
            for session in sessions
        ]
    else:
        session_labels = []
        session_minutes = [
            min(
                float(LIVE_FREE_MAX_MINUTES) + 2,
                full_mock_total_seconds(room.segments) / 60 + 2,
            )
        ]
    room.gemini = {
        "tokens": [], "prompt": prompt, "model": FREE_DEBATE_LIVE_MODEL,
        "session_labels": session_labels, "session_minutes": session_minutes,
    }
    return room


@app.post("/api/room/create")
async def room_create(request: Request):
    user_id = require_page_user(request, "ai_room")
    budget_error = _bandwidth_live_gate_error()
    if budget_error:
        raise HTTPException(status_code=429, detail=budget_error)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    mode = str(payload.get("mode") or "A").upper()
    if mode not in ("A", "B"):
        mode = "A"
    debate_format = str(payload.get("debate_format") or DEBATE_FORMATS[0])
    if debate_format not in DEBATE_FORMATS:
        debate_format = DEBATE_FORMATS[0]
    structure = str(payload.get("structure") or ("mock" if mode == "B" else "free"))
    if structure not in ("free", "mock"):
        structure = "free"
    if not _practice_daily_slot_available(user_id, structure):
        raise HTTPException(status_code=429, detail=PRACTICE_DAILY_LIMIT_MESSAGE)
    if not _practice_monthly_room_available(structure):
        raise HTTPException(status_code=429, detail=GLOBAL_LIVE_LIMIT_MESSAGE)
    if structure == "free" and debate_format not in FREE_DEBATE_FORMATS:
        raise HTTPException(status_code=400, detail=f"{debate_format}不設自由辯論，請改用完整 Mock。")
    topic = str(payload.get("topic") or "").strip()[:500]
    try:
        free_minutes = float(payload.get("free_minutes") or 2.5)
    except Exception:
        free_minutes = 2.5
    # The browser is not authoritative over practice duration.  Without this
    # clamp a crafted room-create request could keep a Free De room running far
    # beyond the advertised ten-minute cap and consume the monthly budget.
    free_minutes = min(float(LIVE_FREE_MAX_MINUTES), max(0.5, free_minutes))
    if mode == "A":
        capacity = 2
    elif structure == "mock":
        capacity = min(ROOM_MAX_CAPACITY, 3 if debate_format == "星島" else 4)
    else:
        try:
            capacity = max(1, min(ROOM_MAX_CAPACITY, int(payload.get("capacity") or ROOM_MAX_CAPACITY)))
        except Exception:
            capacity = ROOM_MAX_CAPACITY

    async with ROOMS_LOCK:
        _gc_rooms()
        if _active_room_count() >= MAX_ROOMS:
            raise HTTPException(status_code=429, detail="太多練習房，請稍後再試")
        code = None
        for _ in range(20):
            candidate = "".join(
                secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LEN)
            )
            if candidate not in ROOMS:
                code = candidate
                break
        if code is None:
            raise HTTPException(status_code=503, detail="未能產生房間代碼，請再試。")
        room = _build_room_plan(
            code, mode, user_id, debate_format, topic, structure,
            free_minutes, capacity, payload,
        )
        ROOMS[code] = room
        _room_ensure_lifecycle(room)
    return JSONResponse(
        {"ok": True, "code": code, "mode": mode},
        headers={"Cache-Control": CACHE_NO_STORE},
    )


@app.get("/api/room/{code}")
async def room_info(code: str, request: Request):
    require_page_user(request, "ai_room")
    room = ROOMS.get((code or "").upper())
    if not room or room.phase == "ended":
        raise HTTPException(status_code=404, detail="房間不存在或已結束")
    return JSONResponse({
        "ok": True, "code": room.code, "mode": room.mode, "phase": room.phase,
        "debate_format": room.debate_format, "topic": room.topic,
        "structure": room.structure, "capacity": room.capacity,
        "human_side": room.human_side, "roster": room.roster(),
        "position_labels": room.position_labels(),
        "required_positions": room.required_positions(),
    }, headers={"Cache-Control": CACHE_NO_STORE})


@app.post("/api/room/{code}/leave")
async def room_leave(code: str, request: Request):
    user_id = require_page_user(request, "ai_room")
    room = ROOMS.get((code or "").upper())
    if room and user_id in room.members:
        m = room.members[user_id]
        if m.connected and room.active_turn_user == user_id:
            await _room_handle_turn(room, m, False)
        m.connected = False
        try:
            await m.ws.close()
        except Exception:
            pass
        if room.phase == "lobby":
            room.members.pop(user_id, None)
            room.precheck_results.pop(user_id, None)
        await _room_broadcast(room, {
            "type": "roster", "roster": room.roster(),
            "position_labels": room.position_labels(),
            "required_positions": room.required_positions(),
        })
        if not room.connected_user_ids():
            _room_schedule_empty_cleanup(room)
    return JSONResponse(
        {"ok": True}, headers={"Cache-Control": CACHE_NO_STORE},
    )


@app.get("/api/room/{code}/transcript")
async def room_transcript(code: str, request: Request):
    user_id = require_page_user(request, "ai_room")
    room = ROOMS.get((code or "").upper())
    if not room:
        raise HTTPException(status_code=404, detail="房間不存在")
    if user_id not in room.members:
        raise HTTPException(status_code=403, detail="只有房間成員可查看逐字稿")
    return JSONResponse({
        "ok": True, "topic": room.topic, "debate_format": room.debate_format,
        "phase": room.phase,
        "transcript": room.transcript, "judgement": room.judgement,
        "transcript_revision": room.transcript_revision,
        "judgement_revision": room.judgement_revision,
        "judgement_pending": bool(
            room.transcript
            and room.judgement_revision != room.transcript_revision
        ),
    }, headers={"Cache-Control": CACHE_NO_STORE})


async def _room_register_socket(room, user_id, websocket):
    """Atomically enforce lobby capacity and replace a stale member socket."""
    stale_websocket = None
    async with room.lock:
        if room.phase in ("ending", "ended") or room.terminal_requested:
            return None, "房間已結束。", 1008
        _room_prune_lobby_offline_members(room)
        existing = room.members.get(user_id)
        if existing is None:
            if room.phase != "lobby":
                return None, "練習開始後不可加入新成員。", 1008
            if len(room.connected_user_ids()) >= room.capacity:
                return None, "房間已滿", 1013
            member = RoomMember(user_id, websocket)
            if room.mode == "A":
                if user_id == room.created_by and room.creator_side:
                    member.role = room.creator_side
                else:
                    taken = {m.role for m in room.members.values() if m.connected}
                    for side in ("正方", "反方"):
                        if side not in taken:
                            member.role = side
                            break
            else:
                member.role = room.human_side
            room.members[user_id] = member
        else:
            if not existing.connected and len(room.connected_user_ids()) >= room.capacity:
                return None, "房間已滿", 1013
            if existing.position and any(
                m.connected and m.user_id != existing.user_id
                and m.position == existing.position
                for m in room.members.values()
            ):
                existing.position = None
            if existing.ws is not websocket:
                stale_websocket = existing.ws
                existing.connection_generation += 1
                room.precheck_results.pop(user_id, None)
            existing.ws = websocket
            existing.connected = True
            member = existing
        _room_cancel_empty_cleanup(room)
    if stale_websocket is not None:
        try:
            await stale_websocket.close(code=1000, reason="connection replaced")
        except Exception:
            pass
    return member, "", 0


@app.websocket("/room/{code}")
async def room_ws(websocket: WebSocket, code: str):
    # Authenticate before accept.  The same-origin HttpOnly cookie is the only
    # browser credential, so signed member tokens never enter URLs or storage.
    user_id = _verify_committee_token(websocket.cookies.get("committee_user") or "")
    if not user_id or not account_can_access(user_id, "ai_room"):
        await websocket.close(code=1008)
        return

    code = (code or "").upper()
    room = ROOMS.get(code)
    if not room or room.phase == "ended":
        await websocket.close(code=1008)
        return

    await websocket.accept()

    member, registration_error, close_code = await _room_register_socket(
        room, user_id, websocket,
    )
    if registration_error:
        try:
            await websocket.send_text(json.dumps(
                {"type": "error", "message": registration_error}, ensure_ascii=False,
            ))
        except Exception:
            pass
        await websocket.close(code=close_code)
        return
    socket_generation = member.connection_generation

    try:
        await websocket.send_text(json.dumps({
            "type": "roster", "you": user_id, "mode": room.mode,
            "roster": room.roster(), "topic": room.topic,
            "debate_format": room.debate_format, "structure": room.structure,
            "position_labels": room.position_labels(),
            "required_positions": room.required_positions(),
            "is_host": user_id == room.created_by,
            "transcript": room.transcript,
            "judgement": room.judgement,
        }, ensure_ascii=False))
        await websocket.send_text(json.dumps(room.state_msg(), ensure_ascii=False))
        await _room_broadcast(room, {
            "type": "roster", "roster": room.roster(),
            "position_labels": room.position_labels(),
            "required_positions": room.required_positions(),
        }, exclude=user_id)
        while True:
            raw = await websocket.receive()
            if (
                member.ws is not websocket
                or member.connection_generation != socket_generation
            ):
                break
            if raw.get("type") == "websocket.disconnect":
                break
            text = raw.get("text")
            if text is None:
                # Room clients have no binary protocol.  Closing prevents an
                # authenticated peer from repeatedly making Uvicorn retain
                # large binary frames without touching either member bucket.
                await websocket.close(code=1009, reason="text JSON required")
                break
            # A valid 64 KiB PCM frame is under 90 KiB after base64 + JSON.
            # The parser helper rejects larger/non-object messages before they
            # can use the 4 MiB Uvicorn ceiling as an allocation budget.
            if (
                len(text) > ROOM_WS_TEXT_MAX_BYTES
                or len(text.encode("utf-8")) > ROOM_WS_TEXT_MAX_BYTES
            ):
                await websocket.close(code=1009, reason="room message too large")
                break
            msg = _parse_room_client_text(text)
            if msg is None:
                await websocket.close(code=1007, reason="invalid room JSON")
                break
            await _room_handle_message(
                room, member, msg,
                websocket=websocket, generation=socket_generation,
            )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("room_ws error (%s): %s", code, e)
    finally:
        # A reconnect replaces ``member.ws`` before the old receive loop gets
        # its disconnect event.  Only the currently registered socket may mark
        # the member offline; otherwise the old loop drops the new connection
        # and audio appears to disappear until another reconnect happens.
        if (
            member.ws is websocket
            and member.connection_generation == socket_generation
        ):
            if room.active_turn_user == user_id:
                await _room_handle_turn(room, member, False)
            async with room.lock:
                is_current = (
                    member.ws is websocket
                    and member.connection_generation == socket_generation
                )
                if is_current:
                    member.connected = False
                    if room.phase == "lobby":
                        room.members.pop(user_id, None)
                        room.precheck_results.pop(user_id, None)
            if is_current:
                await _room_broadcast(room, {"type": "peer_left", "user_id": user_id})
                await _room_broadcast(room, {
                    "type": "roster", "roster": room.roster(),
                    "position_labels": room.position_labels(),
                    "required_positions": room.required_positions(),
                })
                if not room.connected_user_ids() and not room.terminal_requested:
                    _room_schedule_empty_cleanup(room)
        _gc_rooms()


@app.websocket("/{path:path}")
async def websocket_not_found(websocket: WebSocket, path: str):
    await websocket.close(code=1008, reason="Unknown WebSocket route")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def http_not_found(request: Request, path: str):
    return Response(content=json.dumps({"detail": "Not Found"}), status_code=404, media_type="application/json")
