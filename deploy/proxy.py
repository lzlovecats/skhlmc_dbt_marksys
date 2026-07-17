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
from urllib.parse import quote_plus, urlsplit
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

import httpx
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
    TABLE_MONTHLY_RESOURCE_LIMITS,
    TABLE_PUSH_SUBSCRIPTIONS,
    TABLE_MATCHES,
    TABLE_VIDEO_PROGRESS,
    TABLE_VIDEO_VIEWS,
)
from core.config_store import get_configs_from_connection
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
from api.community_api import router as community_router
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
    REGISTRATION_ADMIN_SESSION_TTL_SECONDS,
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
    PRACTICE_LIVE_MIN_GAP_SECONDS, PRACTICE_LIVE_RATE_WINDOW_SECONDS,
    PROJECTOR_MATCH_LIMIT,
    PUSH_ACTIVE_DEVICES_PER_USER, PUSH_ENDPOINT_MAX_CHARS,
    PUSH_INACTIVE_RETENTION_DAYS, PUSH_KEY_MAX_CHARS,
    PUSH_SUBSCRIPTION_MAX_BYTES,
    REQUEST_BODY_BUFFER_CONCURRENCY,
    ROOM_CRITICAL_RATE_BURST_MESSAGES,
    ROOM_CRITICAL_RATE_MESSAGES_PER_SECOND,
    ROOM_CONTROL_RATE_BURST_MESSAGES, ROOM_CONTROL_RATE_MESSAGES_PER_SECOND,
    ROOM_EMPTY_GRACE_SECONDS, ROOM_ENDED_RETENTION_SECONDS,
    ROOM_FREE_HARD_GRACE_SECONDS,
    ROOM_JUDGEMENT_CONCURRENCY, ROOM_JUDGEMENT_TIMEOUT_SECONDS,
    ROOM_LOBBY_TTL_SECONDS, ROOM_MAX_CAPACITY,
    ROOM_MANUAL_TURN_FINALIZE_TIMEOUT_SECONDS,
    ROOM_MOCK_HARD_GRACE_SECONDS, ROOM_RETAINED_ENDED_MAX,
    ROOM_TRANSCRIPT_ITEM_MAX_CHARS, ROOM_TRANSCRIPT_MAX_ITEMS,
    ROOM_TRANSCRIPT_TOTAL_MAX_CHARS, ROOM_TURN_FINALIZE_TIMEOUT_SECONDS,
    ROOM_WS_SEND_TIMEOUT_SECONDS,
    ROOM_WS_TEXT_MAX_BYTES,
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
    sync_task = None
    if _get_proxy_secret("RENDER_API_KEY") and _get_proxy_secret("RENDER_SERVICE_ID"):
        sync_task = asyncio.create_task(_render_bandwidth_sync_loop())
    try:
        yield
    finally:
        if sync_task is not None:
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass
        dispose_db_engine()


app = FastAPI(lifespan=_lifespan)
ESSENTIAL_ONLY_BLOCKED_PATHS = {
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
app.include_router(community_router)
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
    """Mint an expiring organiser session bound to the current admin password."""
    material = _registration_admin_auth_material()
    if material is None:
        return None
    secret, password_hash = material
    issued_at = int(time.time())
    payload = {
        "v": 1,
        "sub": "registration_admin",
        "iat": issued_at,
        "exp": issued_at + REGISTRATION_ADMIN_SESSION_TTL_SECONDS,
        "cred": _registration_admin_credential_fingerprint(secret, password_hash),
    }
    encoded = _claim_b64(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signature = _claim_b64(
        hmac.new(
            secret.encode(), f"ra1.{encoded}".encode("ascii"), hashlib.sha256,
        ).digest()
    )
    token = f"ra1.{encoded}.{signature}"
    return token if len(token) <= COMMITTEE_SESSION_TOKEN_MAX_CHARS else None


def _registration_admin_auth_material() -> tuple[str, str] | None:
    engine = _get_db_engine()
    if engine is None:
        return None
    try:
        with engine.begin() as conn:
            configs = get_configs_from_connection(
                conn, ("cookie_secret", "admin_password"),
            )
    except Exception:
        return None
    secret = str(configs.get("cookie_secret") or "")
    password_hash = str(configs.get("admin_password") or "")
    if not secret or not password_hash:
        return None
    return secret, password_hash


def _registration_admin_credential_fingerprint(
    secret: str, password_hash: str,
) -> str:
    material = f"registration-admin-credential-v1\0{password_hash}".encode()
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()


def _verify_registration_admin_token(token: str) -> bool:
    value = str(token or "")
    if not value or len(value) > COMMITTEE_SESSION_TOKEN_MAX_CHARS:
        return False
    try:
        prefix, encoded, signature = value.split(".", 2)
        if prefix != "ra1":
            return False
        payload = json.loads(_claim_b64decode(encoded))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            return False
        subject = str(payload.get("sub") or "")
        issued_at = int(payload.get("iat"))
        expires_at = int(payload.get("exp"))
        credential = str(payload.get("cred") or "")
    except (
        OverflowError, TypeError, ValueError, UnicodeError, json.JSONDecodeError,
    ):
        return False
    now = int(time.time())
    if (
        subject != "registration_admin"
        or issued_at > now + COMMITTEE_SESSION_CLOCK_SKEW_SECONDS
        or expires_at <= now
        or expires_at <= issued_at
        or expires_at - issued_at > REGISTRATION_ADMIN_SESSION_TTL_SECONDS
    ):
        return False
    material = _registration_admin_auth_material()
    if material is None:
        return False
    secret, password_hash = material
    expected_signature = _claim_b64(
        hmac.new(
            secret.encode(), f"ra1.{encoded}".encode("ascii"), hashlib.sha256,
        ).digest()
    )
    expected_credential = _registration_admin_credential_fingerprint(
        secret, password_hash,
    )
    return hmac.compare_digest(signature, expected_signature) and hmac.compare_digest(
        credential, expected_credential,
    )


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
    """讀音字典前處理。合成前把 tts_lexicon 嘅
    term → reading 覆寫。單人 `/api/tts/azure` 及賽事評判 TTS 都經呢度。
    將來可喺呢度加 G2P (ToJyutping/PyCantonese)。"""
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
    """統一 TTS 入口：單人 `/api/tts/azure` 及 custom model 共用。"""
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

    if str(user_id) == KIOSK_ACCOUNT_ID:
        from api.projector_ai_api import validate_kiosk_display_lease

        validate_kiosk_display_lease(
            request,
            display=str(payload.get("display") or ""),
        )

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
    html = (BASE_DIR / "frontend" / "video_replay" / "index.html").read_text(
        encoding="utf-8"
    )
    return Response(
        html.replace("__APP_VERSION__", APP_VERSION),
        media_type="text/html",
        headers=_cache_headers(CACHE_HTML),
    )


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


@app.get("/recent-matches")
async def recent_matches_page():
    html = (BASE_DIR / "frontend" / "recent_matches" / "index.html").read_text(
        encoding="utf-8"
    )
    return Response(
        html.replace("__APP_VERSION__", APP_VERSION),
        media_type="text/html",
        headers=_cache_headers(CACHE_HTML),
    )


@app.get("/ghost-forum")
async def ghost_forum_page():
    html = (BASE_DIR / "frontend" / "ghost_forum" / "index.html").read_text(
        encoding="utf-8"
    )
    return Response(
        html.replace("__APP_VERSION__", APP_VERSION),
        media_type="text/html",
        headers=_cache_headers(CACHE_HTML),
    )


@app.get("/team-history")
async def team_history_page():
    html = (BASE_DIR / "frontend" / "team_history" / "index.html").read_text(
        encoding="utf-8"
    )
    return Response(
        html.replace("__APP_VERSION__", APP_VERSION),
        media_type="text/html",
        headers=_cache_headers(CACHE_HTML),
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
    html = (BASE_DIR / "frontend" / "judging" / "index.html").read_text(
        encoding="utf-8"
    )
    return Response(
        html.replace("__APP_VERSION__", APP_VERSION),
        media_type="text/html",
        headers=_cache_headers(CACHE_HTML),
    )


@app.get("/review")
async def review_page():
    return FileResponse(BASE_DIR / "frontend" / "review" / "index.html",
                        media_type="text/html", headers=_cache_headers(CACHE_HTML))

@app.get("/admin-hub")
async def admin_hub_page():
    return FileResponse(BASE_DIR / "frontend" / "admin_hub" / "index.html", media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/chairperson")
async def chairperson_page():
    html = (BASE_DIR / "frontend" / "chairperson" / "index.html").read_text(
        encoding="utf-8"
    )
    return Response(
        html.replace("__APP_VERSION__", APP_VERSION),
        media_type="text/html",
        headers=_cache_headers(CACHE_HTML),
    )


@app.get("/ai-coach")
async def ai_coach_page():
    html = (BASE_DIR / "frontend" / "ai_coach" / "index.html").read_text(
        encoding="utf-8"
    )
    html = html.replace("__APP_VERSION__", APP_VERSION)
    return Response(
        content=html, media_type="text/html", headers=_cache_headers(CACHE_NO_CACHE)
    )


@app.get("/ai-coach/room/{code}")
async def ai_coach_room_page(code: str, request: Request):
    user_id = require_page_user(request, "ai_room")
    room = ROOMS.get((code or "").upper())
    if not room:
        return _practice_error_page("房間不存在", "房間已結束或不存在。", "/ai-coach")
    access_error = _room_nonmember_access_error(room, user_id)
    if access_error:
        _status, message = access_error
        title = "房間已滿" if _status == 409 else "無權查看"
        return _practice_error_page(title, message, "/ai-coach")
    html = (BASE_DIR / "templates" / "room_debate.html").read_text(encoding="utf-8")
    html = html.replace("__ROOM_CODE__", _script_safe_json(room.code))
    html = html.replace(
        "__ROOM_WS_BASE__",
        _script_safe_json(_get_proxy_secret("ROOM_WS_BASE", "") or ""),
    )
    html = html.replace("__MODE__", _script_safe_json(room.mode))
    html = html.replace("__BELL_SRC__", _script_safe_json(_practice_bell_src()))
    # This placeholder lives inside a quoted script URL, rather than inside
    # executable JavaScript.  APP_VERSION is a server-owned cache-buster.
    html = html.replace("__APP_VERSION__", APP_VERSION)
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
# per authenticated user to suppress accidental repeated token minting.
# ---------------------------------------------------------------------------

# Backwards-compatible alias; the model choice itself is centrally managed.
FREE_DEBATE_LIVE_MODEL = GEMINI_LIVE_MODEL

# Only formats with a free-debate segment are offered for standalone Free De.
_PRACTICE_LIVE_FORMATS = list(FREE_DEBATE_FORMATS)

# In-process rate limit for token minting, keyed by authenticated AI Coach user.
# This is a three-second duplicate-mint safety guard, not a usage quota.
_practice_live_hits: dict = {}
_PRACTICE_LIVE_MIN_GAP_SEC = PRACTICE_LIVE_MIN_GAP_SECONDS
SOLO_LIVE_TOKEN_ISSUE_LOCK = asyncio.Lock()
_solo_live_token_response_cache: dict[
    tuple[str, str, int, str], tuple[str, float]
] = {}
_solo_live_token_response_cache_lock = threading.Lock()
_bandwidth_last_prune = None
_bandwidth_prune_lock = threading.Lock()
_bandwidth_read_cache = {}
_bandwidth_read_cache_lock = threading.Lock()


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

SOLO_HK_COUNTRY_MESSAGE = (
    "香港網絡暫時無法直接連接 Google Gemini Live。請先連接至 Google 支援地區"
    "網絡／VPN，再按「重新檢查」。"
)
BANDWIDTH_STOP_MESSAGE = (
    f"由於本月全系統網絡傳輸量已達{BANDWIDTH_STOP_LIVE_BYTES / 1_000_000_000:g}GB"
    "預算上限，系統已停止新的AI Coach錄音分析傳輸及server TTS。"
    "Mode A真人P2P、Solo瀏覽器直連Gemini、文字AI、R2媒體及管理功能維持正常。"
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


def _parse_render_time(value):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            parsed = datetime.datetime.fromtimestamp(float(value), datetime.timezone.utc)
        else:
            raw = str(value).strip().replace("Z", "+00:00")
            parsed = datetime.datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        return None


def _render_bandwidth_buckets(
    payload, service_id: str, *, default_unit: str = "bytes",
) -> list[dict]:
    """Normalise Render metric series to byte-valued, hourly ledger rows."""
    buckets = []

    unit_multipliers = {
        "b": 1,
        "byte": 1,
        "bytes": 1,
        "kb": 1000,
        "mb": 1000 ** 2,
        "gb": 1000 ** 3,
        "tb": 1000 ** 4,
        "kib": 1024,
        "mib": 1024 ** 2,
        "gib": 1024 ** 3,
        "tib": 1024 ** 4,
    }

    def labels_dict(raw):
        if isinstance(raw, dict):
            return {str(key): str(value) for key, value in raw.items()}
        if isinstance(raw, list):
            result = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                field = item.get("field") or item.get("name") or item.get("key")
                if field not in (None, "") and item.get("value") not in (None, ""):
                    result[str(field)] = str(item["value"])
            return result
        return {}

    def byte_value(amount, unit):
        try:
            numeric = max(0.0, float(amount or 0))
        except (TypeError, ValueError, OverflowError):
            return None
        multiplier = unit_multipliers.get(str(unit or default_unit).strip().lower())
        if multiplier is None:
            return None
        return int(round(numeric * multiplier))

    def walk(value, inherited=None):
        inherited = dict(inherited or {})
        if isinstance(value, list):
            for item in value:
                walk(item, inherited)
            return
        if not isinstance(value, dict):
            return
        labels = value.get("labels") or value.get("label") or {}
        inherited.update(labels_dict(labels))
        if value.get("unit") not in (None, ""):
            inherited["unit"] = str(value["unit"])
        for key in ("category", "trafficCategory", "traffic_category", "type"):
            if value.get(key) not in (None, ""):
                inherited["category"] = str(value[key])
        points = value.get("values") or value.get("points") or value.get("dataPoints")
        if isinstance(points, list):
            for point in points:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    start = _parse_render_time(point[0])
                    amount = point[1]
                    item = {}
                elif isinstance(point, dict):
                    start = _parse_render_time(
                        point.get("time") or point.get("timestamp")
                        or point.get("startTime") or point.get("start")
                    )
                    amount = point.get("value")
                    if amount is None:
                        amount = point.get("bytes") or point.get("bandwidth")
                    item = point
                else:
                    continue
                byte_count = byte_value(
                    amount, item.get("unit") or inherited.get("unit") or default_unit,
                )
                if byte_count is None:
                    continue
                if start is None:
                    continue
                end = _parse_render_time(item.get("endTime") or item.get("end"))
                if end is None:
                    end = start + datetime.timedelta(hours=1)
                category = str(
                    item.get("category") or item.get("trafficCategory")
                    or inherited.get("category") or inherited.get("trafficSource")
                    or inherited.get("traffic")
                    or "total"
                )[:120]
                digest = hashlib.sha256(
                    f"{service_id}|{category}|{start.isoformat()}|{end.isoformat()}".encode()
                ).hexdigest()
                buckets.append({
                    "id": digest, "category": category, "start": start,
                    "end": end, "bytes": byte_count,
                })
        for key, child in value.items():
            if key not in {
                "values", "points", "dataPoints", "labels", "label", "unit",
            }:
                walk(child, inherited)

    walk(payload)
    unique = {item["id"]: item for item in buckets}
    return sorted(unique.values(), key=lambda item: (item["start"], item["category"]))


async def sync_render_bandwidth_metrics() -> dict:
    api_key = _get_proxy_secret("RENDER_API_KEY").strip()
    service_id = _get_proxy_secret("RENDER_SERVICE_ID").strip()
    if not api_key or not service_id:
        raise RuntimeError("未設定 RENDER_API_KEY 或 RENDER_SERVICE_ID")
    now = datetime.datetime.now(datetime.timezone.utc)
    _period, month_start = _bandwidth_month_context(now)
    start = month_start.replace(tzinfo=datetime.timezone.utc)
    params = {
        "resource": service_id,
        "startTime": int(start.timestamp()),
        "endTime": int(now.timestamp()),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            "https://api.render.com/v1/metrics/bandwidth",
            params=params,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        source_response = await client.get(
            "https://api.render.com/v1/metrics/bandwidth-sources",
            params=params,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
    total_buckets = _render_bandwidth_buckets(payload, service_id)
    source_buckets = []
    if source_response.is_success:
        # Render's traffic-source response currently omits a unit field. Its
        # values use the same GB unit as the canonical bandwidth metric.
        source_buckets = _render_bandwidth_buckets(
            source_response.json(), service_id, default_unit="GB",
        )
    else:
        logger.warning(
            "Render bandwidth source breakdown unavailable: status=%s",
            source_response.status_code,
        )
    # The source response contains its own `total` series. Keep the canonical
    # /bandwidth total and only add non-total categories, otherwise every hour
    # would be counted twice at the system-wide gate.
    buckets = total_buckets + [
        item for item in source_buckets if item["category"].lower() != "total"
    ]
    complete_before = now.replace(tzinfo=None) - datetime.timedelta(minutes=65)
    complete = [item for item in buckets if item["end"] <= complete_before]
    engine = _get_db_engine()
    if engine is None:
        raise RuntimeError("database unavailable")
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('render_bandwidth_sync'))"))
        for item in complete:
            conn.execute(text(f"""INSERT INTO {TABLE_BANDWIDTH_USAGE_LOGS}
                (source,user_id,bytes_out,details,official_bucket_id,
                 traffic_category,bucket_start,bucket_end,official_complete,created_at)
                VALUES('render_official',NULL,:bytes,:details,:bucket_id,
                       :category,:start,:end,TRUE,:end)
                ON CONFLICT(official_bucket_id) DO UPDATE SET
                  bytes_out=EXCLUDED.bytes_out,traffic_category=EXCLUDED.traffic_category,
                  bucket_start=EXCLUDED.bucket_start,bucket_end=EXCLUDED.bucket_end,
                  official_complete=TRUE"""), {
                "bytes": item["bytes"], "details": f"service={service_id}",
                "bucket_id": item["id"], "category": item["category"],
                "start": item["start"], "end": item["end"],
            })
    status = bandwidth_budget_status(notify=True)
    return {"received": len(buckets), "stored_complete": len(complete), "status": status}


async def _render_bandwidth_sync_loop():
    while True:
        try:
            await sync_render_bandwidth_metrics()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Render bandwidth metrics sync failed")
        await asyncio.sleep(60 * 60)


def bandwidth_budget_status(
    *, notify: bool = False, _connection=None, _limit_overrides=None,
) -> dict:
    """Combine complete official buckets with only later local checkpoints."""
    engine = _get_db_engine()
    period, start_utc = _bandwidth_month_context()
    tracked = 0
    official = 0
    official_through = None
    official_from = None
    ledger_read = False
    if _connection is not None:
        official_row = _connection.execute(text(f"""SELECT
                COALESCE(SUM(bytes_out) FILTER (
                    WHERE LOWER(COALESCE(traffic_category,'total'))='total'
                ),0) AS bytes,
                MIN(bucket_start) AS first_bucket, MAX(bucket_end) AS through
            FROM {TABLE_BANDWIDTH_USAGE_LOGS}
            WHERE official_complete=TRUE AND bucket_start>=:start"""),
            {"start": start_utc}).mappings().one()
        official = int(official_row["bytes"] or 0)
        official_from = official_row["first_bucket"]
        official_through = official_row["through"]
        local_start = official_through or start_utc
        tracked = int(_connection.execute(text(f"""SELECT COALESCE(SUM(bytes_out),0)
            FROM {TABLE_BANDWIDTH_USAGE_LOGS}
            WHERE official_bucket_id IS NULL AND created_at>=:start"""),
            {"start": local_start}).scalar() or 0)
        ledger_read = True
    elif engine is not None:
        try:
            with engine.begin() as conn:
                official_row = conn.execute(text(f"""SELECT
                        COALESCE(SUM(bytes_out) FILTER (
                            WHERE LOWER(COALESCE(traffic_category,'total'))='total'
                        ),0) AS bytes,
                        MIN(bucket_start) AS first_bucket, MAX(bucket_end) AS through
                    FROM {TABLE_BANDWIDTH_USAGE_LOGS}
                    WHERE official_complete=TRUE AND bucket_start>=:start"""),
                    {"start": start_utc}).mappings().one()
                official = int(official_row["bytes"] or 0)
                official_from = official_row["first_bucket"]
                official_through = official_row["through"]
                local_start = official_through or start_utc
                tracked = int(conn.execute(text(f"""SELECT COALESCE(SUM(bytes_out),0)
                    FROM {TABLE_BANDWIDTH_USAGE_LOGS}
                    WHERE official_bucket_id IS NULL AND created_at>=:start"""),
                    {"start": local_start}).scalar() or 0)
            ledger_read = True
        except Exception:
            # One-release transition support: before the migration lands the
            # existing ledger has no official-bucket columns. Treat all rows as
            # local checkpoints instead of breaking every AI gate.
            try:
                with engine.begin() as conn:
                    tracked = int(conn.execute(text(f"""SELECT COALESCE(SUM(bytes_out),0)
                        FROM {TABLE_BANDWIDTH_USAGE_LOGS}
                        WHERE created_at>=:start"""), {"start": start_utc}).scalar() or 0)
                ledger_read = True
            except Exception:
                pass
    cache_key = str(period)
    if ledger_read:
        with _bandwidth_read_cache_lock:
            _bandwidth_read_cache[cache_key] = {
                "official": official, "official_from": official_from,
                "official_through": official_through, "tracked": tracked,
            }
    else:
        with _bandwidth_read_cache_lock:
            cached_read = dict(_bandwidth_read_cache.get(cache_key) or {})
        if cached_read:
            official = int(cached_read.get("official") or 0)
            official_from = cached_read.get("official_from")
            official_through = cached_read.get("official_through")
            tracked = int(cached_read.get("tracked") or 0)
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
    if official_through is not None:
        tracked_after_baseline = tracked
        # A manual rollout baseline covers the missing month prefix only.
        effective_baseline = baseline if (
            baseline_period_ok and official_from and official_from > start_utc
        ) else 0
        total = effective_baseline + official + tracked
    elif baseline and baseline_snapshot_ready and baseline_period_ok:
        tracked_after_baseline = max(0, tracked - tracked_snapshot)
        effective_baseline = baseline
        total = effective_baseline + tracked_after_baseline
    else:
        # Backward-compatible and deliberately conservative: an incomplete
        # snapshot may double count, but can never silently under-enforce.
        tracked_after_baseline = tracked
        effective_baseline = baseline if baseline_period_ok else 0
        total = effective_baseline + tracked_after_baseline
    warn_bytes = BANDWIDTH_WARN_BYTES
    stop_bytes = BANDWIDTH_STOP_LIVE_BYTES
    hard_bytes = BANDWIDTH_ESSENTIAL_ONLY_BYTES
    if _limit_overrides is not None:
        warn_bytes, stop_bytes, hard_bytes = (
            int(value) for value in _limit_overrides
        )
    elif engine is not None:
        try:
            from core.resource_limits import get_monthly_limit
            row = get_monthly_limit(get_vote_db(), "render_bandwidth")
            warn_bytes = int(row.get("warning_value") or warn_bytes)
            stop_bytes = int(row.get("stop_value") or stop_bytes)
            hard_bytes = int(row.get("hard_value") or hard_bytes)
        except Exception:
            pass
    stage = 4 if total >= hard_bytes else 3.5 if total >= stop_bytes else 3 if total >= warn_bytes else 0
    status = {
        "period": period, "baseline_bytes": effective_baseline,
        "baseline_as_of": baseline_as_of,
        "baseline_tracked_snapshot_bytes": tracked_snapshot,
        "baseline_snapshot_ready": baseline_snapshot_ready and baseline_period_ok,
        "official_bytes": official,
        "official_from": official_from.isoformat() if official_from else "",
        "official_through": official_through.isoformat() if official_through else "",
        "tracked_bytes": tracked, "tracked_after_baseline_bytes": tracked_after_baseline,
        "total_bytes": total, "stage": stage,
        "warn_bytes": warn_bytes,
        "stop_live_bytes": stop_bytes,
        "essential_only_bytes": hard_bytes,
    }
    if notify:
        _send_bandwidth_warning_once(status)
    return status


def _send_bandwidth_warning_once(status: dict) -> None:
    warn_bytes = int(status.get("warn_bytes") or BANDWIDTH_WARN_BYTES)
    stop_bytes = int(status.get("stop_live_bytes") or BANDWIDTH_STOP_LIVE_BYTES)
    if status["total_bytes"] < warn_bytes:
        return
    engine = _get_db_engine()
    if engine is None:
        return
    period_month = datetime.date.fromisoformat(f"{status['period']}-01")
    claim = secrets.token_urlsafe(18)
    claimed = False
    now = datetime.datetime.now(datetime.timezone.utc)
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('bandwidth_3gb_push'))"))
        conn.execute(text(f"""INSERT INTO {TABLE_MONTHLY_RESOURCE_LIMITS}
            (period_month,limit_key,unit,warning_value,stop_value,hard_value)
            VALUES(:month,'render_bandwidth','bytes',:warning,:stop,:hard)
            ON CONFLICT(period_month,limit_key) DO NOTHING"""), {
            "month": period_month, "warning": warn_bytes, "stop": stop_bytes,
            "hard": int(status.get("essential_only_bytes") or BANDWIDTH_ESSENTIAL_ONLY_BYTES),
        })
        audit = json.dumps({
            "claim": claim, "state": "sending", "claimed_at": now.isoformat(),
            "total_bytes": int(status["total_bytes"]),
            "warning_bytes": warn_bytes, "stop_bytes": stop_bytes,
        }, ensure_ascii=False)
        changed = conn.execute(text(f"""UPDATE {TABLE_MONTHLY_RESOURCE_LIMITS}
            SET notification_audit=jsonb_set(
                    COALESCE(notification_audit,'{{}}'::jsonb),
                    '{{bandwidth_warning}}',CAST(:audit AS jsonb),TRUE
                ),updated_at=:now
            WHERE period_month=:month AND limit_key='render_bandwidth'
              AND NOT (COALESCE(notification_audit,'{{}}'::jsonb)
                       ? 'bandwidth_warning')"""), {
            "month": period_month, "audit": audit, "now": now,
        })
        claimed = bool(changed.rowcount)
    if not claimed:
        return
    logger.warning("Monthly bandwidth warning reached: %s", status)
    sent = 0
    delivery_error = ""
    try:
        from core.push import notify_committee
        sent = notify_committee(
            get_vote_db(), _get_vapid(), "⚠️ 系統網絡傳輸量提示",
            f"本月全系統網絡傳輸量已達{warn_bytes / 1_000_000_000:g}GB。"
            f"達{stop_bytes / 1_000_000_000:g}GB後會停止新錄音分析及server TTS；"
            "Mode A P2P真人練習仍可使用。",
            tag=f"bandwidth-warning-{status['period']}", url="/",
        )
    except Exception as exc:
        delivery_error = type(exc).__name__
        logger.exception("Failed to send committee bandwidth warning")
    finished = datetime.datetime.now(datetime.timezone.utc)
    final_audit = json.dumps({
        "claim": claim, "state": "sent" if sent > 0 else "failed",
        "claimed_at": now.isoformat(), "completed_at": finished.isoformat(),
        "successful_deliveries": int(sent), "error": delivery_error,
        "total_bytes": int(status["total_bytes"]),
        "warning_bytes": warn_bytes, "stop_bytes": stop_bytes,
    }, ensure_ascii=False)
    try:
        with engine.begin() as conn:
            conn.execute(text(f"""UPDATE {TABLE_MONTHLY_RESOURCE_LIMITS}
                SET notification_audit=jsonb_set(
                        notification_audit,'{{bandwidth_warning}}',
                        CAST(:audit AS jsonb),TRUE
                    ),updated_at=:now
                WHERE period_month=:month AND limit_key='render_bandwidth'
                  AND notification_audit->'bandwidth_warning'->>'claim'=:claim"""), {
                "month": period_month, "audit": final_audit,
                "now": finished, "claim": claim,
            })
    except Exception:
        logger.exception("Failed to finalize monthly bandwidth warning audit")


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


def reserve_bandwidth_transfer(operation_id: str, declared_bytes: int) -> int | None:
    """Atomically reserve raw provider-transfer bytes below the 3.5GB gate."""
    engine = _get_db_engine()
    if engine is None:
        return None
    declared = max(1, int(declared_bytes))
    reserved = declared + max(64 * 1024, int(declared * 0.05))
    now, _period_start = _bandwidth_write_context()
    # Resolve DB-backed thresholds and send a possible warning before taking
    # the reservation lock.  Holding one pool connection while the status
    # helper borrowed another could exhaust the production pool at concurrency 3.
    initial = bandwidth_budget_status(notify=True)
    limits = (
        int(initial.get("warn_bytes") or BANDWIDTH_WARN_BYTES),
        int(initial.get("stop_live_bytes") or BANDWIDTH_STOP_LIVE_BYTES),
        int(initial.get("essential_only_bytes") or BANDWIDTH_ESSENTIAL_ONLY_BYTES),
    )
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('bandwidth_transfer_reservation'))"))
        status = bandwidth_budget_status(
            _connection=conn, _limit_overrides=limits,
        )
        if status["total_bytes"] + reserved >= int(status["stop_live_bytes"]):
            return None
        row = conn.execute(text(f"""INSERT INTO {TABLE_BANDWIDTH_USAGE_LOGS}
            (source,user_id,bytes_out,details,created_at)
            VALUES('bandwidth_reservation',NULL,:bytes,:details,:now)
            RETURNING id"""), {
            "bytes": reserved, "details": str(operation_id)[:500], "now": now,
        }).scalar()
    return int(row)


def settle_bandwidth_transfer(reservation_id: int, actual_bytes: int, *, success: bool) -> None:
    engine = _get_db_engine()
    if engine is None:
        return
    with engine.begin() as conn:
        conn.execute(text(f"""UPDATE {TABLE_BANDWIDTH_USAGE_LOGS}
            SET source=:source,bytes_out=:bytes
            WHERE id=:id AND source='bandwidth_reservation'"""), {
            "id": int(reservation_id),
            "bytes": max(0, int(actual_bytes)),
            "source": (
                "ai_coach_audio_provider" if success
                else "ai_coach_audio_provider_failed"
            ),
        })


def _bandwidth_live_gate_error() -> str | None:
    status = bandwidth_budget_status(notify=True)
    return BANDWIDTH_STOP_MESSAGE if status["total_bytes"] >= int(status["stop_live_bytes"]) else None


def _bandwidth_essential_gate_error() -> str | None:
    status = bandwidth_budget_status(notify=True)
    return BANDWIDTH_ESSENTIAL_MESSAGE if status["total_bytes"] >= int(status["essential_only_bytes"]) else None


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
    hits.append(now)
    _practice_live_hits[user_id] = hits
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
        return "Mock初始練習尚未完成開始記錄，請返回重新開始。"
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

    All Mock sections share ``practice_id`` and therefore one lifecycle row.
    ``report_created`` lets the Start-time endpoint distinguish a new row from
    an existing cross-worker winner.  When supplied, ``before_insert`` runs
    only after the advisory lock and duplicate check, so race losers never mint.
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
    marker = f"direct_practice:{practice_id}"[:450]
    lifecycle_started_at = int(time.time()) if started_at is None else int(started_at)
    lifecycle_deadline_at = lifecycle_started_at + _solo_live_lifecycle_seconds(claim)
    duration_seconds = sum(int(value) for value in (claim.get("session_seconds") or []))
    duration_minutes = max(0.5, duration_seconds / 60)
    with engine.begin() as conn:
        _set_solo_live_ledger_timeouts(conn)
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('solo_live_lifecycle'))"))
        now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
        now_utc = now_hk.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        already = conn.execute(text(f"""SELECT 1 FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE user_id=:user AND feature=:feature AND status='success'
              AND LEFT(error_message,LENGTH(:marker))=:marker LIMIT 1"""), {
            "user": user_id, "feature": feature, "marker": marker,
        }).fetchone()
        if already:
            return result(None, False)
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
    """Atomically append one disclosed Mock section to its lifecycle marker."""
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
            return result(False, "Mock初始練習尚未完成開始記錄，請返回重新開始。", None)
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
                        headers=_cache_headers(CACHE_NO_CACHE))


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
    # Rate and lifecycle checks are authoritative at the Start-time endpoint.
    # Rendering this page must neither consume a rate-limit hit nor reserve a
    # paid practice before the user explicitly presses Start.
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
# Rooms live in an in-memory dict in this single uvicorn process. Render carries
# authenticated control/signalling/transcript traffic only; two committee
# members exchange Opus audio directly over STUN-only WebRTC. There is no TURN,
# SFU, Render audio fallback, or multiplayer-vs-AI mode.
# ---------------------------------------------------------------------------

ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no O/0/I/1
ROOM_CODE_LEN = 5
ROOM_EMPTY_GRACE_MS = ROOM_EMPTY_GRACE_SECONDS * 1000
ROOM_LOBBY_TTL_MS = ROOM_LOBBY_TTL_SECONDS * 1000
ROOM_ENDED_RETENTION_MS = ROOM_ENDED_RETENTION_SECONDS * 1000
ROOM_JUDGEMENT_MODELS = model_slugs_for_feature("room_judgement")
ROOM_JUDGEMENT_SEMAPHORE = asyncio.Semaphore(ROOM_JUDGEMENT_CONCURRENCY)

ROOMS = {}  # code -> Room
ROOMS_LOCK = asyncio.Lock()

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


class RoomMember:
    def __init__(self, user_id, ws):
        self.user_id = user_id
        self.ws = ws
        self.connection_generation = 1
        self.role = None          # "正方" / "反方"
        self.name = user_id
        self.connected = True
        self.rtc_status = "new"
        # An active control-socket replacement is a real disconnect boundary:
        # preserve the pause timestamp until the replacement has received its
        # bootstrap state, then start the single bounded ICE-restart window.
        self.restart_required = None
        self.leave_token = secrets.token_urlsafe(24)
        self.joined_at = _now_ms()
        self.control_rate_tokens = float(ROOM_CONTROL_RATE_BURST_MESSAGES)
        self.control_rate_updated_ms = self.joined_at
        self.critical_rate_tokens = float(ROOM_CRITICAL_RATE_BURST_MESSAGES)
        self.critical_rate_updated_ms = self.joined_at
        self.send_lock = asyncio.Lock()


class Room:
    def __init__(self, code, mode, created_by, debate_format, topic,
                 structure, free_minutes, capacity):
        self.code = code
        self.mode = "A"
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
        self.active_turn_id = None
        self.active_turn_request_id = None
        self.turn_transcript_chunks = {}
        self.free_next_side = "正方"
        self.precheck_id = None
        self.precheck_results = {}
        self.members = {}                # user_id -> RoomMember
        self.transcript = []             # {speaker, side, seg, text}
        self.transcript_chars = 0
        self.transcript_revision = 0
        self.judgement = ""
        self.judgement_revision = -1
        self.judgement_request_started = False
        self.judgement_request_revision = -1
        self.judge_enabled = True
        self.judge_disabled_reason = ""
        self.roster_generation = 0
        self.state_sequence = 0
        self.rtc_pause_started_ms = None
        self.rtc_restart_task = None
        self.empty_since = None
        self.terminal_requested = False
        self.turn_stop_pending = False
        self.segment_transitioning = False
        self.creator_side = None
        self.tick_task = None
        self.lifecycle_task = None
        self.judgement_task = None
        self.empty_cleanup_task = None
        self.ended_cleanup_task = None
        self.manual_stop_task = None
        self.control_tasks = {}
        self.segment_generation = 0
        self.fired_bell_keys = set()
        # Approximate Render egress for this room. We count successful control,
        # signalling and transcript WebSocket fan-out only; media never enters
        # this channel. The aggregate is checkpointed for the live tracker.
        self.bandwidth_bytes = 0
        self.bandwidth_flushed_bytes = 0
        self.bandwidth_recorded = False
        self.bandwidth_lock = threading.Lock()
        self.last_bandwidth_checkpoint_ms = _now_ms()
        self.lock = asyncio.Lock()
        self.activation_lock = asyncio.Lock()
        self.judgement_lock = asyncio.Lock()
        self.segment_lock = asyncio.Lock()
        self.turn_finalize_lock = asyncio.Lock()
        self.end_complete_event = asyncio.Event()

    def roster(self):
        return [
            {"user_id": m.user_id, "name": m.name, "role": m.role,
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
        if side in ("正方", "反方"):
            for m in self.members.values():
                if m.role == side:
                    return m.user_id
        return None

    def expected_turn_side(self):
        seg = self.current_segment()
        if self.phase != "active" or not seg or seg.get("side") != "雙方":
            return None
        expected = self.free_next_side if self.free_next_side in ("正方", "反方") else "正方"
        other = "反方" if expected == "正方" else "正方"
        limit_ms = max(0, int(seg.get("seconds") or 0) * 1000)
        if limit_ms and self.side_elapsed_ms.get(expected, 0) >= limit_ms:
            if self.side_elapsed_ms.get(other, 0) < limit_ms:
                return other
            return None
        return expected

    def is_open_free_segment(self):
        """Whether the current segment is a timed, alternating free debate.

        A full Mock also contains a ``雙方`` free-debate segment.  The old code
        checked ``structure == 'free'`` and therefore skipped side timers and
        Gemini activity handling during that Mock segment.
        """
        seg = self.current_segment()
        return bool(self.phase == "active" and seg and seg.get("side") == "雙方")

    def timer_now_ms(self, now_ms=None):
        now = _now_ms() if now_ms is None else int(now_ms)
        if self.rtc_pause_started_ms is not None:
            return min(now, self.rtc_pause_started_ms)
        return now

    def side_elapsed_snapshot(self, now_ms=None):
        timer_now = self.timer_now_ms(now_ms)
        state = self.turn_transcript_chunks.get(self.active_turn_id) or {}
        for cutoff_key in ("stop_intent_ms", "forced_stop_ms"):
            cutoff = state.get(cutoff_key)
            if cutoff is not None:
                timer_now = min(timer_now, int(cutoff))
        elapsed = dict(self.side_elapsed_ms)
        if (
            self.active_turn_side in elapsed
            and self.active_turn_started_ms is not None
        ):
            elapsed[self.active_turn_side] += max(
                0, timer_now - self.active_turn_started_ms,
            )
        return elapsed

    def segment_elapsed_ms(self, now_ms=None):
        if self.seg_started_ms is None:
            return 0
        return max(0, self.timer_now_ms(now_ms) - self.seg_started_ms)

    def state_msg(self):
        seg = self.current_segment()
        now = _now_ms()
        side_elapsed_ms = self.side_elapsed_snapshot(now)
        seg_elapsed_ms = self.segment_elapsed_ms(now)
        self.state_sequence += 1
        return {
            "type": "state",
            "state_sequence": self.state_sequence,
            "phase": self.phase,
            "seg_index": self.seg_index,
            "seg_total": len(self.segments),
            "seg_label": seg.get("label") if seg else "",
            "side": seg.get("side") if seg else "",
            "seconds": seg.get("seconds") if seg else 0,
            "bells": seg.get("bells") if seg else [],
            "active_speaker": self.active_speaker(),
            "started_ms": self.started_ms,
            "hard_deadline_ms": self.hard_deadline_ms,
            "seg_started_ms": self.seg_started_ms,
            "seg_elapsed_ms": seg_elapsed_ms,
            "segment_generation": self.segment_generation,
            "server_now_ms": now,
            "side_elapsed_ms": side_elapsed_ms,
            "active_turn_user": self.active_turn_user,
            "active_turn_side": self.active_turn_side,
            "active_turn_started_ms": self.active_turn_started_ms,
            "active_turn_id": self.active_turn_id,
            "active_turn_request_id": self.active_turn_request_id,
            "turn_stop_pending": self.turn_stop_pending,
            "expected_turn_side": self.expected_turn_side(),
            "judge_enabled": self.judge_enabled,
            "judge_disabled_reason": self.judge_disabled_reason,
            "rtc_paused": self.rtc_pause_started_ms is not None,
            "roster_generation": self.roster_generation,
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


def _room_expiry_deadline_ms(room):
    """Return the fixed wall-clock expiry for the room's current lifecycle."""
    if room.phase in ("lobby", "starting"):
        return int(room.created_at) + ROOM_LOBBY_TTL_MS
    if room.phase in ("active", "ending") and room.hard_deadline_ms:
        return int(room.hard_deadline_ms)
    return None


def _room_extend_ended_retention(room, *, now_ms=None):
    """Give members a fresh result window after end or late judgement work."""
    now = _now_ms() if now_ms is None else int(now_ms)
    room.ended_retain_until_ms = max(
        int(getattr(room, "ended_retain_until_ms", 0) or 0),
        now + ROOM_ENDED_RETENTION_MS,
    )
    if getattr(room, "phase", "") == "ended":
        _room_schedule_ended_cleanup(room)


def _gc_rooms():
    """Prune/schedule expired rooms while the caller holds ``ROOMS_LOCK``."""
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
                room.tick_task, room.lifecycle_task,
                room.judgement_task, room.rtc_restart_task,
                room.empty_cleanup_task, room.ended_cleanup_task,
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
        expiry_ms = _room_expiry_deadline_ms(room)
        if expiry_ms is not None and now >= expiry_ms:
            dispose(
                code, room,
                "lobby_ttl" if room.phase in ("lobby", "starting")
                else "server_time_limit",
            )
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


def _retained_ended_room_count():
    now = _now_ms()
    return sum(
        1 for room in ROOMS.values()
        if room.phase == "ended"
        and (
            now < room.ended_retain_until_ms
            or (
                room.judgement_task is not None
                and not room.judgement_task.done()
            )
        )
    )


def _room_nonmember_access_error(room, user_id):
    """Return an admission error for page/info reads by a non-member."""
    members = getattr(room, "members", {})
    if user_id in members:
        return None
    if room.phase != "lobby":
        return 403, "練習開始後只有原房間成員可查看。"
    connected = (
        room.connected_user_ids()
        if hasattr(room, "connected_user_ids")
        else [
            member for member in members.values()
            if getattr(member, "connected", False)
        ]
    )
    if len(connected) >= int(getattr(room, "capacity", ROOM_MAX_CAPACITY)):
        return 409, "房間已滿。"
    connected_ids = set(connected)
    capacity = int(getattr(room, "capacity", ROOM_MAX_CAPACITY))
    creator_id = str(getattr(room, "created_by", "") or "")
    if (
        user_id != creator_id
        and creator_id not in connected_ids
        and len(connected_ids) >= max(0, capacity - 1)
    ):
        return 409, "房間正預留一個位置畀主持。"
    return None


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
        _room_bump_roster_generation(room)
        _room_invalidate_precheck(room)
        removed += 1
    return removed


def _room_invalidate_precheck(room):
    """Discard media-test evidence after any role or roster mutation."""
    room.precheck_id = None
    room.precheck_results = {}


def _room_bump_roster_generation(room):
    """Advance the signaling epoch and invalidate active RTC confirmations."""
    room.roster_generation += 1
    if room.phase == "active":
        for member in room.members.values():
            if member.connected:
                member.rtc_status = "new"


def _room_control_task_active(room, key):
    task = room.control_tasks.get(key)
    return bool(task is not None and not task.done())


def _room_schedule_control_task(room, key, coroutine_factory):
    """Run one bounded control transition without blocking a WS receive loop."""
    existing = room.control_tasks.get(key)
    if existing is not None and not existing.done():
        return existing
    try:
        task = asyncio.create_task(coroutine_factory())
    except Exception:
        return None
    room.control_tasks[key] = task

    def completed(done_task):
        if room.control_tasks.get(key) is done_task:
            room.control_tasks.pop(key, None)
        if done_task.cancelled():
            return
        try:
            error = done_task.exception()
        except (asyncio.CancelledError, Exception):
            return
        if error is not None:
            logger.error(
                "room control task failed (%s, %s, %s)",
                room.code, key, type(error).__name__,
            )

    task.add_done_callback(completed)
    return task


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


async def _room_ended_cleanup(room):
    """Remove one terminal room once its member-result window has elapsed."""
    try:
        while ROOMS.get(room.code) is room and room.phase == "ended":
            judgement_pending = (
                room.judgement_task is not None
                and not room.judgement_task.done()
            )
            remaining_ms = room.ended_retain_until_ms - _now_ms()
            if not judgement_pending and remaining_ms <= 0:
                async with ROOMS_LOCK:
                    if (
                        ROOMS.get(room.code) is room
                        and room.phase == "ended"
                        and (
                            room.judgement_task is None
                            or room.judgement_task.done()
                        )
                        and _now_ms() >= room.ended_retain_until_ms
                    ):
                        ROOMS.pop(room.code, None)
                return
            await asyncio.sleep(
                1.0 if judgement_pending else max(0.01, remaining_ms / 1000),
            )
    except asyncio.CancelledError:
        raise
    finally:
        if room.ended_cleanup_task is asyncio.current_task():
            room.ended_cleanup_task = None


def _room_schedule_ended_cleanup(room):
    if room.phase != "ended":
        return
    if room.ended_cleanup_task is not None and not room.ended_cleanup_task.done():
        return
    room.ended_cleanup_task = asyncio.create_task(_room_ended_cleanup(room))


async def _room_send_member(
    room, member, message, *, websocket=None, generation=None,
    encoded_text=None, close_on_failure=True,
):
    """Bound one member send and account successful room-control egress."""
    target = websocket if websocket is not None else member.ws
    expected_generation = (
        member.connection_generation if generation is None else generation
    )
    text_value = (
        encoded_text
        if encoded_text is not None
        else json.dumps(message, ensure_ascii=False)
    )
    try:
        async with member.send_lock:
            # Revalidate after waiting: a replacement may have advanced the
            # generation while another state/roster send held this lock.
            if (
                member.ws is not target
                or member.connection_generation != expected_generation
                or not member.connected
            ):
                return False
            await asyncio.wait_for(
                target.send_text(text_value), timeout=ROOM_WS_SEND_TIMEOUT_SECONDS,
            )
            room.bandwidth_bytes += len(text_value.encode("utf-8"))
            return True
    except Exception:
        if close_on_failure:
            try:
                await asyncio.wait_for(
                    target.close(code=1011, reason="room control send failed"),
                    timeout=ROOM_WS_SEND_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
        return False


async def _room_broadcast(room, msg, exclude=None):
    text = json.dumps(msg, ensure_ascii=False)
    recipients = [
        (member, member.ws, member.connection_generation)
        for member in list(room.members.values())
        if member.connected and not (exclude and member.user_id == exclude)
    ]

    async def send(member, websocket, generation):
        if await _room_send_member(
            room, member, None,
            websocket=websocket, generation=generation,
            encoded_text=text, close_on_failure=False,
        ):
            return None
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
                pause_started_ms = _room_claim_control_disconnect_pause_locked(
                    room, member,
                )
                member.connected = False
                member.connection_generation += 1
                _room_bump_roster_generation(room)
                _room_invalidate_precheck(room)
                if room.phase == "lobby":
                    room.members.pop(member.user_id, None)
                failed.append((member, websocket, pause_started_ms))
        if failed:
            await asyncio.gather(*(
                asyncio.wait_for(
                    websocket.close(
                        code=1011, reason="room control send failed",
                    ),
                    timeout=ROOM_WS_SEND_TIMEOUT_SECONDS,
                )
                for _member, websocket, _pause_started_ms in failed
            ), return_exceptions=True)
        for member, _websocket, _pause_started_ms in failed:
            await _room_broadcast(room, {
                "type": "peer_left", "user_id": member.user_id,
                "roster_generation": room.roster_generation,
            })
        if failed:
            await _room_broadcast(room, {
                "type": "roster", "roster": room.roster(),
                "roster_generation": room.roster_generation,
            })
        for _member, _websocket, pause_started_ms in failed:
            if pause_started_ms is not None:
                _room_schedule_control_task(
                    room, f"rtc_pause:{pause_started_ms}",
                    lambda stamp=pause_started_ms: _room_complete_disconnect_pause(
                        room, stamp, "control_send_failure",
                    ),
                )
        if (
            failed and not room.connected_user_ids()
            and not room.terminal_requested
        ):
            _room_schedule_empty_cleanup(room)


def _room_claim_disconnect_pause_locked(room, member, *, now_ms=None):
    """Freeze active debate clocks once; caller must hold ``room.lock``."""
    member.rtc_status = "disconnected"
    now = _now_ms() if now_ms is None else int(now_ms)
    if (
        room.phase == "active"
        and not room.terminal_requested
        and room.rtc_pause_started_ms is None
    ):
        room.rtc_pause_started_ms = now
        return now
    return None


def _room_claim_control_disconnect_pause_locked(room, member, *, now_ms=None):
    """Claim an offline control boundary, including a pending replacement.

    A replacement socket can fail or explicitly leave after registration but
    before its bootstrap consumes ``restart_required``.  In that race the room
    is already paused, so a fresh claim returns ``None``; carrying forward the
    saved stamp ensures the required restart timeout is still started exactly
    once.
    """
    claimed_pause_started_ms = _room_claim_disconnect_pause_locked(
        room, member, now_ms=now_ms,
    )
    pause_started_ms = (
        member.restart_required
        if member.restart_required is not None
        else claimed_pause_started_ms
    )
    member.restart_required = None
    return pause_started_ms


async def _room_complete_disconnect_pause(room, pause_started_ms, reason):
    """Finalize speech and start the one bounded reconnect window."""
    if pause_started_ms is None:
        return False
    await _room_request_turn_finalization(room, reason)
    restart_started = False
    async with room.lock:
        if (
            room.phase == "active"
            and not room.terminal_requested
            and room.rtc_pause_started_ms == pause_started_ms
            and (
                room.rtc_restart_task is None
                or room.rtc_restart_task.done()
            )
        ):
            room.rtc_restart_task = asyncio.create_task(
                _room_rtc_restart_timeout(room, pause_started_ms),
            )
            restart_started = True
    if not restart_started:
        return False
    await _room_broadcast(room, room.state_msg())
    await _room_broadcast(room, {
        "type": "rtc_status", "status": "restart",
        "roster_generation": room.roster_generation,
    })
    return True


async def _room_rtc_restart_timeout(room, pause_started_ms):
    """End a room if one server-observed ICE restart does not recover in 10s."""
    try:
        await asyncio.sleep(10)
        if (
            room.phase == "active"
            and room.rtc_pause_started_ms == pause_started_ms
            and not room.terminal_requested
        ):
            await _room_end(room, "p2p_ice_restart_timeout")
    except asyncio.CancelledError:
        raise
    finally:
        if room.rtc_restart_task is asyncio.current_task():
            room.rtc_restart_task = None


async def _room_emit_due_bells(room, *, now_ms=None, allow_paused=False):
    """Publish each authoritative bell threshold once per segment run/side."""
    if (
        room.phase != "active"
        or not room.activation_ready
        or (room.rtc_pause_started_ms is not None and not allow_paused)
    ):
        return
    seg = room.current_segment()
    if not seg:
        return
    side = ""
    if seg.get("side") == "雙方":
        side = room.active_turn_side or ""
        if side not in ("正方", "反方"):
            return
        elapsed_ms = room.side_elapsed_snapshot(now_ms).get(side, 0)
    else:
        elapsed_ms = room.segment_elapsed_ms(now_ms)
    due = []
    for bell_index, bell in enumerate(seg.get("bells") or []):
        key = (side, bell_index)
        threshold_ms = max(0, int(float(bell.get("t") or 0) * 1000))
        if key in room.fired_bell_keys or elapsed_ms < threshold_ms:
            continue
        room.fired_bell_keys.add(key)
        due.append({
            "type": "bell",
            "segment_generation": room.segment_generation,
            "seg_index": room.seg_index,
            "side": side,
            "bell_index": bell_index,
            "t": bell.get("t") or 0,
            "rings": bell.get("rings") or 1,
            "label": str(bell.get("label") or "")[:200],
        })
    for payload in due:
        await _room_broadcast(room, payload)


async def _room_tick(room):
    try:
        while room.phase == "active" and ROOMS.get(room.code) is room:
            await asyncio.sleep(1)
            if room.phase != "active" or ROOMS.get(room.code) is not room:
                break
            now = _now_ms()
            # The fixed overall deadline includes RTC/restart and handoff grace;
            # only the segment and per-side speech clocks pause.
            if room.hard_deadline_ms and now >= room.hard_deadline_ms:
                await _room_end(room, "server_time_limit")
                break
            if room.rtc_pause_started_ms is not None:
                await _room_broadcast(room, room.state_msg())
                continue
            await _room_emit_due_bells(room, now_ms=now)
            seg = room.current_segment()
            seg_seconds = int((seg or {}).get("seconds") or 0)
            if seg and seg.get("side") == "雙方" and seg_seconds > 0:
                budget_ms = seg_seconds * 1000
                active_side = room.active_turn_side
                active_user = room.active_turn_user
                active_used = room.side_elapsed_snapshot(now).get(active_side, 0)
                if active_user and active_used >= budget_ms:
                    await _room_request_turn_finalization(
                        room, "side_time_limit",
                    )
                if all(
                    room.side_elapsed_ms.get(side, 0) >= budget_ms
                    for side in ("正方", "反方")
                ):
                    if room.structure == "free" or room.seg_index >= len(room.segments) - 1:
                        await _room_end(room, "server_side_budgets_complete")
                        break
                    current_index = room.seg_index
                    await _room_advance_segment(
                        room, current_index + 1, expected_from=current_index,
                    )
            elif (
                seg and room.seg_started_ms and seg_seconds > 0
                and room.segment_elapsed_ms(now) >= seg_seconds * 1000
            ):
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
            expiry_ms = _room_expiry_deadline_ms(room)
            if expiry_ms is None:
                await _room_end(room, "server_lifecycle_failure")
                return
            remaining_ms = expiry_ms - _now_ms()
            if remaining_ms <= 0:
                await _room_end(
                    room,
                    "lobby_ttl" if room.phase in ("lobby", "starting")
                    else "server_time_limit",
                )
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
            expiry_ms = _room_expiry_deadline_ms(room)
            if expiry_ms is None or now >= expiry_ms:
                await _room_end(
                    room,
                    "lobby_ttl" if room.phase in ("lobby", "starting")
                    else "server_time_limit",
                )
                return
            await asyncio.to_thread(_checkpoint_room_bandwidth, room, False)
            room.last_bandwidth_checkpoint_ms = now
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
    """Bind a precheck to identities, sockets, roles and roster revision."""
    return (
        int(room.roster_generation),
        tuple(sorted(
            (
                member.user_id,
                int(member.connection_generation),
                str(member.role or ""),
                str(member.rtc_status or ""),
            )
            for member in room.members.values() if member.connected
        )),
    )


def _room_precheck_snapshot_matches(room, check_id, roster_signature):
    if not check_id or room.precheck_id != check_id:
        return False
    if _room_connected_roster_signature(room) != tuple(roster_signature or ()):
        return False
    try:
        _generation, roster = roster_signature
        users = [user_id for user_id, _connection, _role, _rtc in roster]
    except (TypeError, ValueError):
        return False
    return bool(
        users
        and all(user_id in room.precheck_results for user_id in users)
        and all(room.precheck_results[user_id].get("ok") for user_id in users)
    )


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
    room.active_turn_id = None
    room.active_turn_request_id = None
    room.activation_ready = False


async def _room_rollback_activation(room, users, released_message):
    """Reset a failed activation to a retryable lobby."""
    async with room.lock:
        _room_failed_activation_state(room)
    return released_message


async def _room_start_active_impl(
    room, *, expected_precheck_id=None, expected_roster_signature=None,
) -> str | None:
    # Multiple final precheck messages can arrive in the same event-loop tick.
    # Only one activation may publish the authoritative timer state.
    async with room.activation_lock:
        async with room.lock:
            if room.terminal_requested:
                return "房間正在結束。"
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

        # Revalidate the authoritative roster after the last precheck message.
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
                "成員名單或連線在檢查後有變；"
                "請重新進行連線測試。"
            )
        try:
            bandwidth = await asyncio.to_thread(
                bandwidth_budget_status, notify=True,
            )
        except Exception as exc:
            logger.warning(
                "room activation budget check failed (%s, %s)",
                room.code, type(exc).__name__,
            )
            return await _room_rollback_activation(
                room, users,
                "暫時未能完成資源檢查；房間未有開始，請稍後再試。",
            )
        if bandwidth["total_bytes"] >= int(bandwidth["essential_only_bytes"]):
            await _room_disable_judge(
                room, "本月 Render 傳輸量已達 4GB；真人 P2P 練習可繼續，但 AI 評判及 Web Speech 逐字稿已停用。",
            )
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
                "成員名單在開始期間有變；"
                "請重新進行連線測試。"
            ))
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
                room.segment_generation += 1
                room.fired_bell_keys.clear()
                room.started_ms = _now_ms()
                room.seg_started_ms = room.started_ms
                if room.structure == "free":
                    side_seconds = int(
                        (room.current_segment() or {}).get("seconds") or 0
                    )
                    total_seconds = side_seconds * 2
                    grace_seconds = ROOM_FREE_HARD_GRACE_SECONDS
                else:
                    total_seconds = full_mock_total_seconds(room.segments)
                    grace_seconds = ROOM_MOCK_HARD_GRACE_SECONDS
                room.hard_deadline_ms = (
                    room.started_ms
                    + (max(0, int(total_seconds)) + grace_seconds) * 1000
                )
                room.side_elapsed_ms = {"正方": 0, "反方": 0}
                room.active_turn_user = None
                room.active_turn_side = None
                room.active_turn_started_ms = None
                room.active_turn_id = None
                room.active_turn_request_id = None
                room.free_next_side = "正方"
                room.turn_stop_pending = False
        if roster_changed:
            return await _room_rollback_activation(
                room, users,
                "成員名單在開始前有變；請重新進行連線測試。",
            )
        async with room.lock:
            roster_changed = (
                room.phase != "starting"
                or room.terminal_requested
                or _room_connected_roster_signature(room) != roster_signature
                or bool(_room_start_blocker(room))
            )
            if not roster_changed:
                room.phase = "active"
                room.activation_ready = False
                room.precheck_id = None
                room.precheck_results = {}
        if roster_changed:
            return await _room_rollback_activation(
                room, users,
                "成員名單在開始前有變；請重新進行連線測試。",
            )
        async with room.lock:
            roster_changed = (
                room.phase != "active"
                or room.terminal_requested
                or _room_connected_roster_signature(room) != roster_signature
                or bool(_room_start_blocker(room))
            )
            if not roster_changed:
                room.activation_ready = True
        if roster_changed:
            return await _room_rollback_activation(
                room, users,
                "成員在開始期間離線；請重新測試。",
            )
        _room_ensure_tick(room)
        await _room_broadcast(room, room.state_msg())
        await _room_emit_due_bells(room)
        return None


async def _room_start_active(
    room, *, expected_precheck_id=None, expected_roster_signature=None,
) -> str | None:
    """Fail closed to a retryable lobby for unexpected activation errors."""
    try:
        return await _room_start_active_impl(
            room,
            expected_precheck_id=expected_precheck_id,
            expected_roster_signature=expected_roster_signature,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "room activation failed (%s): %s", room.code, type(exc).__name__,
        )
        if room.phase == "active" and room.activation_ready:
            # Activation has already committed its authoritative state.  Keep
            # the timer alive instead of publishing a contradictory lobby.
            try:
                _room_ensure_tick(room)
                return None
            except Exception:
                await _room_end(room, "server_timer_failure")
                return "房間計時器未能啟動，練習已安全結束。"
        if room.phase not in ("ending", "ended") and not room.terminal_requested:
            async with room.lock:
                _room_failed_activation_state(room)
        return "開始房間時發生伺服器錯誤；房間未有開始，請重新測試。"


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


async def _room_disable_judge(room, reason: str):
    """Permanently disable provider judgement while leaving P2P practice live."""
    if not room.judge_enabled:
        return
    room.judge_enabled = False
    room.judge_disabled_reason = str(reason or "AI 評價目前不可用")[:500]
    await _room_broadcast(room, {
        "type": "judge_disabled", "reason": room.judge_disabled_reason,
    })


async def _room_handle_precheck_result(room, member, msg):
    async with room.lock:
        if room.phase != "lobby" or not room.precheck_id:
            return
        if msg.get("check_id") != room.precheck_id:
            return
        rtc_connected = member.rtc_status == "connected"
        media_ok = bool(msg.get("media_ok", msg.get("ok"))) and rtc_connected
        room.precheck_results[member.user_id] = {
            "ok": media_ok,
            "media_ok": media_ok,
            "message": (
                str(msg.get("message") or "")[:800]
                if rtc_connected
                else "P2P RTC 連線未建立或已中斷。"
            ),
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
    members = [m for m in room.members.values() if m.connected]
    if len(members) != 2 or {m.role for m in members} != {"正方", "反方"}:
        return "真人對真人練習必須兩位委員在線，並分別擔任正方及反方。"
    if any(member.rtc_status != "connected" for member in members):
        return "開始前必須兩位委員均完成並維持 P2P RTC 連線。"
    return None


def _room_member_control_rate_allowed(member, *, now_ms=None):
    """Consume one reconnect-stable token for a client message."""
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


def _room_member_critical_rate_allowed(member, *, now_ms=None):
    """Consume the small reserve for a validated safety-critical message."""
    now = _now_ms() if now_ms is None else int(now_ms)
    previous = int(member.critical_rate_updated_ms)
    elapsed_ms = max(0, now - previous)
    member.critical_rate_tokens = min(
        float(ROOM_CRITICAL_RATE_BURST_MESSAGES),
        float(member.critical_rate_tokens)
        + elapsed_ms * float(ROOM_CRITICAL_RATE_MESSAGES_PER_SECOND) / 1000,
    )
    member.critical_rate_updated_ms = max(previous, now)
    if member.critical_rate_tokens < 1:
        return False
    member.critical_rate_tokens -= 1
    return True


def _room_message_is_safety_critical(room, member, mtype, msg):
    """Return true only for state-bound operations that safely stop progress."""
    if mtype == "transcript_commit":
        return bool(
            room.phase == "active"
            and room.active_turn_user == member.user_id
            and room.active_turn_id
            and isinstance(msg.get("turn_id"), str)
            and msg.get("turn_id") == room.active_turn_id
        )
    if mtype in {"turn_end", "turn_stop_intent"}:
        return _room_message_matches_active_turn(room, member, msg)
    if mtype == "end":
        return bool(
            member.user_id == room.created_by
            and room.phase not in ("ending", "ended")
            and not room.terminal_requested
        )
    if mtype == "rtc_status" and room.phase == "active":
        try:
            generation = int(msg.get("roster_generation"))
        except (TypeError, ValueError):
            return False
        return bool(
            generation == room.roster_generation
            and str(msg.get("status") or "") in {"disconnected", "failed"}
        )
    return False


def _room_message_matches_active_turn(room, member, msg):
    if (
        room.phase != "active"
        or room.active_turn_user != member.user_id
        or not room.active_turn_id
    ):
        return False
    turn_id = msg.get("turn_id")
    request_id = msg.get("request_id")
    matched_identifier = False
    if turn_id is not None:
        if not (
            isinstance(turn_id, str)
            and 0 < len(turn_id) <= 80
            and turn_id == room.active_turn_id
        ):
            return False
        matched_identifier = True
    if request_id is not None:
        if not (
            isinstance(request_id, str)
            and 0 < len(request_id) <= 80
            and request_id == room.active_turn_request_id
        ):
            return False
        matched_identifier = True
    return matched_identifier


async def _room_run_scheduled_segment_advance(room, index, expected_from):
    try:
        await _room_advance_segment(
            room, index, expected_from=expected_from,
        )
    finally:
        room.segment_transitioning = False


async def _room_advance_segment(room, index: int, *, expected_from=None):
    """Move the authoritative server timer without trusting client clocks."""
    async with room.segment_lock:
        if (
            room.phase != "active"
            or not room.activation_ready
            or room.terminal_requested
            or room.rtc_pause_started_ms is not None
        ):
            return
        if expected_from is not None and room.seg_index != expected_from:
            return
        target = max(0, min(int(index), len(room.segments) - 1))
        if target == room.seg_index:
            return
        room.segment_transitioning = True
        try:
            if room.active_turn_user:
                await _room_request_turn_finalization(room, "segment_advance")
            if (
                room.phase != "active"
                or room.terminal_requested
                or room.rtc_pause_started_ms is not None
            ):
                return
            now = _now_ms()
            room.seg_index = target
            room.segment_generation += 1
            room.fired_bell_keys.clear()
            room.seg_started_ms = now
            room.active_turn_user = None
            room.active_turn_side = None
            room.active_turn_started_ms = None
            room.active_turn_id = None
            room.active_turn_request_id = None
            room.free_next_side = "正方"
            if room.current_segment() and room.current_segment().get("side") == "雙方":
                room.side_elapsed_ms = {"正方": 0, "反方": 0}
            await _room_broadcast(room, room.state_msg())
            if (
                room.phase == "active"
                and not room.terminal_requested
                and room.seg_index == target
            ):
                # Handoff/setup latency is not debate speech time.
                room.seg_started_ms = _now_ms()
                await _room_broadcast(room, room.state_msg())
                await _room_emit_due_bells(room)
        finally:
            room.segment_transitioning = False


async def _room_end(room, reason: str = "host"):
    # Serialize the terminal transition with activation.  Otherwise a host end
    # arriving while activation work is in flight can be overwritten by the
    # activation coroutine setting the room back to ``active``.
    # Block new turns and segment changes before yielding for STT finalization.
    room.terminal_requested = True
    async with room.activation_lock:
        if room.phase in ("ending", "ended"):
            return
        if room.phase == "active" and room.active_turn_user:
            await _room_request_turn_finalization(room, "room_end")
        room.phase = "ending"
    current = asyncio.current_task()
    if room.tick_task is not None and room.tick_task is not current and not room.tick_task.done():
        room.tick_task.cancel()
    if (
        room.rtc_restart_task is not None
        and room.rtc_restart_task is not current
        and not room.rtc_restart_task.done()
    ):
        room.rtc_restart_task.cancel()
    room.rtc_restart_task = None
    if (
        room.lifecycle_task is not None
        and room.lifecycle_task is not current
        and not room.lifecycle_task.done()
    ):
        room.lifecycle_task.cancel()
    _room_cancel_empty_cleanup(room)
    room.phase = "ended"
    room.activation_ready = False
    room.ended_at_ms = _now_ms()
    if room.members:
        _room_extend_ended_retention(room, now_ms=room.ended_at_ms)
    else:
        room.ended_retain_until_ms = room.ended_at_ms
    try:
        await _room_broadcast(room, {"type": "ended", "reason": reason})
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_record_room_bandwidth_once, room),
                timeout=ROOM_WS_SEND_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "room final bandwidth checkpoint failed (%s, %s)",
                room.code, type(exc).__name__,
            )
        sockets = [member.ws for member in room.members.values() if member.connected]
        if sockets:
            await asyncio.gather(*(
                asyncio.wait_for(
                    socket.close(code=1000, reason="practice ended"),
                    timeout=ROOM_WS_SEND_TIMEOUT_SECONDS,
                )
                for socket in sockets
            ), return_exceptions=True)
    finally:
        room.end_complete_event.set()
        _room_schedule_ended_cleanup(room)


async def _room_end_and_remove(room, reason):
    """Finish a GC/empty room while retaining its member-only result window."""
    await _room_end(room, reason)
    if room.phase != "ended":
        try:
            await asyncio.wait_for(
                asyncio.shield(room.end_complete_event.wait()),
                timeout=ROOM_TURN_FINALIZE_TIMEOUT_SECONDS + 5,
            )
        except asyncio.TimeoutError:
            return
    if room.phase == "ended":
        if room.members:
            _room_extend_ended_retention(room)
        else:
            _room_schedule_ended_cleanup(room)


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


def _room_store_turn_transcript(room, state, *, partial):
    """Commit one bounded server-owned turn state exactly once."""
    if not state or state.get("committed"):
        return None
    text_value = " ".join(state.get("chunks") or []).strip()
    if not text_value:
        return None
    stored_chars = int(getattr(room, "transcript_chars", 0) or 0)
    if stored_chars <= 0 and getattr(room, "transcript", None):
        stored_chars = sum(
            len(str(item.get("text") or "")) for item in room.transcript
        )
    remaining = max(0, ROOM_TRANSCRIPT_TOTAL_MAX_CHARS - stored_chars)
    accepted = text_value[:min(ROOM_TRANSCRIPT_ITEM_MAX_CHARS, remaining)]
    state["committed"] = True
    state["chunks"] = []
    if not accepted:
        return None
    item_revision = room.transcript_revision + 1
    item = {
        "revision": item_revision,
        "turn_id": str(state.get("turn_id") or ""),
        "speaker": str(state.get("user_id") or ""),
        "side": str(state.get("side") or ""),
        "seg": int(state.get("seg_index") or 0),
        "label": str(state.get("label") or "")[:300],
        "text": accepted,
        "partial": bool(
            partial or state.get("truncated") or len(accepted) < len(text_value)
        ),
        "created_ms": _now_ms(),
    }
    room.transcript.append(item)
    if len(room.transcript) > ROOM_TRANSCRIPT_MAX_ITEMS:
        room.transcript = room.transcript[-ROOM_TRANSCRIPT_MAX_ITEMS:]
    room.transcript_chars = sum(
        len(str(existing.get("text") or "")) for existing in room.transcript
    )
    room.transcript_revision = item_revision
    state["item"] = item
    return item


async def _room_finish_turn(room, turn_id, *, partial_fallback):
    """Close the current turn once, accounting time and preserving chunks."""
    if not turn_id or room.active_turn_id != turn_id:
        return False
    user_id = room.active_turn_user
    side = room.active_turn_side
    state = room.turn_transcript_chunks.get(turn_id) or {}
    now = room.timer_now_ms()
    for cutoff_key in ("stop_intent_ms", "forced_stop_ms"):
        cutoff = state.get(cutoff_key)
        if cutoff is not None:
            now = min(now, int(cutoff))
    # Capture threshold bells while the authoritative side/turn identity still
    # exists.  Clearing it first would make a manual stop just after the bank
    # limit silently lose the final two-ring bell.
    await _room_emit_due_bells(room, now_ms=now, allow_paused=True)
    if room.active_turn_id != turn_id:
        return False
    if side in room.side_elapsed_ms and room.active_turn_started_ms is not None:
        elapsed = max(0, now - room.active_turn_started_ms)
        if room.is_open_free_segment():
            limit_ms = int((room.current_segment() or {}).get("seconds") or 0) * 1000
            room.side_elapsed_ms[side] = min(
                limit_ms,
                room.side_elapsed_ms.get(side, 0) + elapsed,
            )
        else:
            room.side_elapsed_ms[side] += elapsed
    if (room.current_segment() or {}).get("side") == "雙方" and side in ("正方", "反方"):
        room.free_next_side = "反方" if side == "正方" else "正方"
    item = _room_store_turn_transcript(
        room, state,
        partial=bool(partial_fallback or state.get("force_partial")),
    )
    finalized = state.get("finalized_event")
    if isinstance(finalized, asyncio.Event):
        finalized.set()
    room.active_turn_user = None
    room.active_turn_side = None
    room.active_turn_started_ms = None
    room.active_turn_id = None
    room.active_turn_request_id = None
    room.turn_stop_pending = False
    room.turn_transcript_chunks.pop(turn_id, None)
    manual_stop_task = room.manual_stop_task
    room.manual_stop_task = None
    if (
        manual_stop_task is not None
        and manual_stop_task is not asyncio.current_task()
        and not manual_stop_task.done()
    ):
        manual_stop_task.cancel()
    if item is not None:
        await _room_broadcast(room, {"type": "transcript", "item": item})
    await _room_broadcast(
        room, {"type": "speaking", "user_id": user_id, "speaking": False},
    )
    await _room_broadcast(room, room.state_msg())
    return True


async def _room_request_turn_finalization(room, reason):
    """Ask for a final commit, then persist received chunks after ~1 second."""
    # ``_room_finish_turn`` deliberately clears ``active_turn_id`` before it
    # broadcasts the final transcript/state.  A failed send during one of
    # those broadcasts can synchronously enter the disconnect-pause path and
    # request finalization again.  Avoid waiting on our own non-reentrant lock
    # in that case.  A room pause/transition blocks a new turn from starting,
    # and the locked check below still protects all other races.
    if not room.active_turn_id:
        return
    async with room.turn_finalize_lock:
        turn_id = room.active_turn_id
        if not turn_id:
            return
        state = room.turn_transcript_chunks.get(turn_id) or {}
        state["force_partial"] = True
        forced_stop_ms = room.timer_now_ms()
        if state.get("stop_intent_ms") is not None:
            forced_stop_ms = min(forced_stop_ms, int(state["stop_intent_ms"]))
        state["forced_stop_ms"] = forced_stop_ms
        existing_item = state.get("item")
        item_was_upgraded = bool(
            isinstance(existing_item, dict)
            and not existing_item.get("partial")
        )
        if item_was_upgraded:
            existing_item["partial"] = True
            room.transcript_revision += 1
            existing_item["revision"] = room.transcript_revision
            await _room_broadcast(
                room, {"type": "transcript", "item": existing_item},
            )
        user_id = room.active_turn_user
        member = room.members.get(user_id)
        deadline_ms = _now_ms() + ROOM_TURN_FINALIZE_TIMEOUT_SECONDS * 1000
        room.turn_stop_pending = True
        delivered = False
        try:
            if member is not None and member.connected:
                delivered = await _room_send_member(room, member, {
                    "type": "turn_stop_requested",
                    "user_id": user_id,
                    "turn_id": turn_id,
                    "reason": str(reason or "server_transition")[:80],
                    "deadline_ms": deadline_ms,
                })
            finalized = state.get("finalized_event")
            remaining = max(0, deadline_ms - _now_ms()) / 1000
            if delivered and isinstance(finalized, asyncio.Event) and remaining > 0:
                try:
                    await asyncio.wait_for(finalized.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
            await _room_finish_turn(
                room, turn_id, partial_fallback=True,
            )
        finally:
            if room.active_turn_id in {None, turn_id}:
                room.turn_stop_pending = False


async def _room_manual_stop_watchdog(room, turn_id):
    try:
        await asyncio.sleep(ROOM_MANUAL_TURN_FINALIZE_TIMEOUT_SECONDS)
        if room.active_turn_id == turn_id:
            state = room.turn_transcript_chunks.get(turn_id) or {}
            finalized = state.get("finalized_event")
            if state.get("committed") or (
                isinstance(finalized, asyncio.Event) and finalized.is_set()
            ):
                await _room_finish_turn(
                    room, turn_id, partial_fallback=False,
                )
            else:
                await _room_request_turn_finalization(
                    room, "manual_stop_timeout",
                )
    except asyncio.CancelledError:
        raise
    finally:
        if room.manual_stop_task is asyncio.current_task():
            room.manual_stop_task = None


async def _room_publish_turn_stop_intent(room, turn_id, stop_intent_ms):
    """Publish ordered stop UI events without occupying the owner's WS loop."""
    if room.active_turn_id != turn_id:
        return
    await _room_emit_due_bells(
        room, now_ms=stop_intent_ms, allow_paused=True,
    )
    state = room.turn_transcript_chunks.get(turn_id) or {}
    if (
        room.active_turn_id == turn_id
        and state.get("stop_intent_ms") == stop_intent_ms
    ):
        await _room_broadcast(room, room.state_msg())


async def _room_handle_turn_stop_intent(room, member, msg):
    """Freeze speech time at server receipt while the browser drains STT."""
    if room.phase != "active" or not room.activation_ready:
        return
    if room.active_turn_user != member.user_id or not room.active_turn_id:
        return
    if not _room_message_matches_active_turn(room, member, msg):
        return
    state = room.turn_transcript_chunks.get(room.active_turn_id) or {}
    if state.get("stop_intent_ms") is None:
        state["stop_intent_ms"] = room.timer_now_ms()
        room.turn_stop_pending = True
        if room.manual_stop_task is None or room.manual_stop_task.done():
            room.manual_stop_task = asyncio.create_task(
                _room_manual_stop_watchdog(room, room.active_turn_id),
            )
        turn_id = room.active_turn_id
        stop_intent_ms = state["stop_intent_ms"]
        _room_schedule_control_task(
            room, f"stop_intent:{turn_id}",
            lambda: _room_publish_turn_stop_intent(
                room, turn_id, stop_intent_ms,
            ),
        )


async def _room_handle_turn(room, member, speaking, *, request_id=""):
    if room.phase != "active" or not room.activation_ready:
        return
    now = room.timer_now_ms()
    if speaking:
        if (
            room.terminal_requested
            or room.segment_transitioning
            or room.turn_stop_pending
            or room.rtc_pause_started_ms is not None
        ):
            return
        if not member.connected:
            return
        rtc_members = [
            candidate for candidate in room.members.values()
            if candidate.connected
        ]
        if (
            len(rtc_members) != room.capacity
            or any(candidate.rtc_status != "connected" for candidate in rtc_members)
        ):
            await _room_send_member(room, member, {
                "type": "turn_rejected",
                "message": "雙方 RTC 連線未準備好，請等待重新連線完成。",
            })
            return
        if room.active_turn_user == member.user_id:
            return
        if room.active_turn_user and room.active_turn_user != member.user_id:
            await _room_send_member(room, member, {
                "type": "turn_rejected",
                "message": "已有成員發言中，請等待對方停止後再開始。",
            })
            return
        seg = room.current_segment()
        if not seg or seg.get("side") == "準備":
            await _room_send_member(room, member, {
                "type": "turn_rejected",
                "message": "準備環節不設發言，請等待下一個正式發言環節。",
            })
            return
        active = room.active_speaker()
        if active is not None and member.user_id != active:
            await _room_send_member(room, member, {
                "type": "turn_rejected",
                "message": "呢段未輪到你嘅辯位發言。",
            })
            return
        expected_side = room.expected_turn_side()
        if expected_side and member.role != expected_side:
            await _room_send_member(room, member, {
                "type": "turn_rejected",
                "message": f"自由辯論而家輪到{expected_side}發言。",
            })
            return
        if room.is_open_free_segment() and member.role in room.side_elapsed_ms:
            if room.side_elapsed_ms.get(member.role, 0) >= int(
                (seg.get("seconds") or 0) * 1000
            ):
                await _room_send_member(room, member, {
                    "type": "turn_rejected",
                    "message": f"{member.role}嘅自由辯論時間已用完。",
                })
                return
        room.active_turn_user = member.user_id
        room.active_turn_side = member.role
        room.active_turn_started_ms = now
        room.active_turn_id = secrets.token_urlsafe(12)
        room.active_turn_request_id = (
            request_id
            if isinstance(request_id, str) and 0 < len(request_id) <= 80
            else ""
        )
        room.turn_transcript_chunks[room.active_turn_id] = {
            "turn_id": room.active_turn_id,
            "user_id": member.user_id,
            "side": member.role or "",
            "seg_index": room.seg_index,
            "label": seg.get("label", ""),
            "next_sequence": 0,
            "chunks": [],
            "total_chars": 0,
            "committed": False,
            "force_partial": False,
            "stop_intent_ms": None,
            "truncated": False,
            "finalized_event": asyncio.Event(),
        }
        await _room_broadcast(
            room, {"type": "speaking", "user_id": member.user_id, "speaking": True},
        )
        await _room_broadcast(room, room.state_msg())
        await _room_emit_due_bells(room)
        return
    if room.active_turn_user != member.user_id:
        return
    await _room_finish_turn(
        room, room.active_turn_id, partial_fallback=True,
    )


async def _room_handle_transcript(room, member, msg):
    """Accept ordered final SpeechRecognition chunks for the active turn."""
    if room.phase != "active" or not room.activation_ready:
        return
    turn_id = str(msg.get("turn_id") or "")
    state = room.turn_transcript_chunks.get(turn_id)
    if (
        room.active_turn_user != member.user_id
        or turn_id != room.active_turn_id
        or not state or state.get("user_id") != member.user_id
        or state.get("committed")
    ):
        return
    try:
        sequence = int(msg.get("sequence"))
    except (TypeError, ValueError):
        return
    if sequence != int(state.get("next_sequence") or 0):
        return
    raw_text = msg.get("text")
    if not isinstance(raw_text, str):
        return
    text_value = raw_text.strip()
    if not text_value:
        return
    used = max(0, int(state.get("total_chars") or 0))
    separator_chars = 1 if state.get("chunks") else 0
    remaining = max(
        0, ROOM_TRANSCRIPT_ITEM_MAX_CHARS - used - separator_chars,
    )
    if remaining:
        accepted = text_value[:remaining]
        state["chunks"].append(accepted)
        state["total_chars"] = used + separator_chars + len(accepted)
    if len(text_value) > remaining:
        state["truncated"] = True
    # Continue ordered protocol progress at the ceiling so a final sequence can
    # commit the bounded server copy without accepting unbounded text.
    state["next_sequence"] = sequence + 1


async def _room_commit_transcript(room, member, msg):
    if room.phase != "active" or not room.activation_ready:
        return
    turn_id = str(msg.get("turn_id") or "")
    state = room.turn_transcript_chunks.get(turn_id)
    if (
        room.active_turn_user != member.user_id
        or turn_id != room.active_turn_id
        or not state or state.get("user_id") != member.user_id
    ):
        return
    finalized = state.get("finalized_event")
    if state.get("committed"):
        if isinstance(finalized, asyncio.Event):
            finalized.set()
        return
    try:
        final_sequence = int(msg.get("final_sequence"))
    except (TypeError, ValueError):
        final_sequence = -1
    if final_sequence != int(state.get("next_sequence") or 0) or not state.get("chunks"):
        return
    item = _room_store_turn_transcript(
        room, state,
        partial=bool(state.get("force_partial") or msg.get("partial") is True),
    )
    if isinstance(finalized, asyncio.Event):
        finalized.set()
    if item is not None:
        await _room_broadcast(room, {"type": "transcript", "item": item})


async def _room_request_judgement(room):
    async with room.judgement_lock:
        try:
            if not getattr(room, "judge_enabled", True):
                return
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Room judgement setup failed (%s)", type(exc).__name__,
            )
            room.judgement = (
                "本次 AI 評判未能啟動。"
                "\n本房評判要求已完成；請聯絡管理員檢查伺服器設定。"
            )
            room.judgement_revision = getattr(room, "transcript_revision", 0)
            try:
                await _room_broadcast(
                    room, {"type": "judgement", "text": room.judgement},
                )
            except Exception:
                pass
        finally:
            if getattr(room, "phase", "active") in ("ending", "ended"):
                _room_extend_ended_retention(room)


def _room_judgement_transcript_error(room) -> str:
    transcript_sides = {
        str(item.get("side") or "")
        for item in getattr(room, "transcript", [])
        if str(item.get("text") or "").strip()
    }
    missing_sides = [side for side in ("正方", "反方") if side not in transcript_sides]
    if not missing_sides:
        return ""
    return (
        "暫未能要求 AI 評價："
        + "、".join(missing_sides)
        + "未有逐字稿。請雙方先完成至少一次發言。"
    )


def _log_room_judgement_attempt_sync(
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


async def _log_room_judgement_attempt(
    room,
    model,
    success,
    *,
    operation_id,
    operation_stage,
    response_data=None,
    error_message="",
):
    await asyncio.to_thread(
        _log_room_judgement_attempt_sync,
        room,
        model,
        success,
        operation_id=operation_id,
        operation_stage=operation_stage,
        response_data=response_data,
        error_message=error_message,
    )


async def _room_request_judgement_unlocked(room):
    target_revision = getattr(room, "transcript_revision", 0)
    if not getattr(room, "judge_enabled", True):
        result = (
            "本房 AI 評判已停用："
            + str(getattr(room, "judge_disabled_reason", "AI 評價目前不可用。"))
        )
        room.judgement = result
        room.judgement_revision = target_revision
        await _room_broadcast(room, {"type": "judgement", "text": result})
        return
    transcript_error = _room_judgement_transcript_error(room)
    if transcript_error:
        room.judgement = transcript_error
        room.judgement_revision = target_revision
        await _room_broadcast(
            room, {"type": "judgement", "text": transcript_error},
        )
        return
    budget_error = await asyncio.to_thread(_bandwidth_essential_gate_error)
    if budget_error:
        room.judgement = budget_error
        room.judgement_revision = target_revision
        await _room_broadcast(room, {"type": "judgement", "text": budget_error})
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
        "generationConfig": {"temperature": 0.2},
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
        async with (
            ROOM_JUDGEMENT_SEMAPHORE,
            httpx.AsyncClient(timeout=ROOM_JUDGEMENT_TIMEOUT_SECONDS) as client,
        ):
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
                    await _log_room_judgement_attempt(
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
                    await _log_room_judgement_attempt(
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
                    await _log_room_judgement_attempt(
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
                    await _log_room_judgement_attempt(
                        room,
                        model,
                        False,
                        operation_id=operation_id,
                        operation_stage=operation_stage,
                        error_message=last_error,
                    )
                    continue
                except Exception as exc:
                    await _log_room_judgement_attempt(
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
                    await _log_room_judgement_attempt(
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
                    await _log_room_judgement_attempt(
                        room,
                        model,
                        True,
                        operation_id=operation_id,
                        operation_stage=operation_stage,
                        response_data=data,
                    )
                    break
                last_error = f"{model}：AI回應為空"
                await _log_room_judgement_attempt(
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
                    "本次 AI 評判未能完成。"
                    + (f"\n原因：{last_error}" if last_error else "")
                    + "\n本房評判要求已完成；如需跟進，請聯絡管理員檢查模型權限。"
                )
    except Exception as e:
        # Provider exceptions can include request URLs or headers.  Neither the
        # room transcript nor server logs should ever receive those details.
        logger.warning("Room judgement failed (%s)", type(e).__name__)
        result = (
            "本次 AI 評判未能連線。"
            "\n原因：上游服務連線錯誤。"
            "\n本房評判要求已完成；請聯絡管理員檢查伺服器網絡或設定。"
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
    # Every syntactically valid JSON object consumes normal control capacity.
    # A small independently bounded reserve prevents a transcript commit or
    # stop/disconnect operation from being silently dropped after a chunk burst.
    normal_rate_allowed = _room_member_control_rate_allowed(member)
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype or len(mtype) > 80:
        return
    if not normal_rate_allowed:
        critical = _room_message_is_safety_critical(
            room, member, mtype, msg,
        )
        if not critical or not _room_member_critical_rate_allowed(member):
            if critical and websocket is not None:
                try:
                    await asyncio.wait_for(
                        websocket.close(
                            code=1008, reason="room control rate exceeded",
                        ),
                        timeout=ROOM_WS_SEND_TIMEOUT_SECONDS,
                    )
                except Exception:
                    pass
            return
    if mtype in {"audio", "test_audio", "test_received"} or "realtimeInput" in msg:
        # Breaking change: Render no longer accepts room media frames.
        return

    is_host = member.user_id == room.created_by

    if mtype in {"rtc_offer", "rtc_answer", "rtc_ice"}:
        try:
            roster_generation = int(msg.get("roster_generation"))
        except (TypeError, ValueError):
            return
        if roster_generation != room.roster_generation or room.phase in ("ending", "ended"):
            return
        field = "candidate" if mtype == "rtc_ice" else "description"
        payload = msg.get(field)
        if not isinstance(payload, dict):
            return
        limit = 4_096 if mtype == "rtc_ice" else 48_000
        if len(json.dumps(payload, separators=(",", ":"))) > limit:
            return
        peers = [peer for peer in room.members.values()
                 if peer.connected and peer.user_id != member.user_id]
        if len(peers) != 1:
            return
        await _room_broadcast(room, {
            "type": mtype, "from": member.user_id, field: payload,
            "roster_generation": room.roster_generation,
        }, exclude=member.user_id)
        return

    if mtype == "rtc_status":
        status = str(msg.get("status") or "")
        try:
            roster_generation = int(msg.get("roster_generation"))
        except (TypeError, ValueError):
            return
        if roster_generation != room.roster_generation:
            return
        now = _now_ms()
        if status == "preflight_ready" and room.phase == "lobby":
            await _room_broadcast(room, {
                "type": "rtc_status", "status": "preflight_ready",
                "from": member.user_id,
                "roster_generation": room.roster_generation,
            }, exclude=member.user_id)
        elif status in {"disconnected", "failed"} and room.phase == "starting":
            member.rtc_status = status
            _room_invalidate_precheck(room)
            await _room_broadcast(room, {
                "type": "precheck_failed",
                "message": "開始期間 P2P RTC 連線中斷；請重新進行連線測試。",
            })
        elif status == "disconnected" and room.phase == "active":
            async with room.lock:
                pause_started_ms = _room_claim_disconnect_pause_locked(
                    room, member, now_ms=now,
                )
            if pause_started_ms is not None:
                _room_schedule_control_task(
                    room, f"rtc_pause:{pause_started_ms}",
                    lambda stamp=pause_started_ms: _room_complete_disconnect_pause(
                        room, stamp, "rtc_disconnect",
                    ),
                )
        elif status == "connected" and room.rtc_pause_started_ms is not None:
            member.rtc_status = "connected"
            connected_members = [
                peer for peer in room.members.values() if peer.connected
            ]
            if len(connected_members) == room.capacity and all(
                peer.rtc_status == "connected" for peer in connected_members
            ):
                restart_task = room.rtc_restart_task
                room.rtc_restart_task = None
                if restart_task is not None and not restart_task.done():
                    restart_task.cancel()
                paused = max(0, now - room.rtc_pause_started_ms)
                for attr in ("seg_started_ms", "active_turn_started_ms"):
                    value = getattr(room, attr, None)
                    if value is not None:
                        setattr(room, attr, value + paused)
                room.rtc_pause_started_ms = None
                await _room_broadcast(room, room.state_msg())
        elif status == "connected":
            member.rtc_status = "connected"
        elif status in ("disconnected", "failed") and room.phase == "lobby":
            member.rtc_status = status
            _room_invalidate_precheck(room)
            await _room_broadcast(room, _room_precheck_msg(room))
        elif status == "failed" and room.phase not in ("ending", "ended"):
            room.terminal_requested = True
            _room_schedule_control_task(
                room, "end",
                lambda: _room_end(room, "p2p_ice_failed"),
            )
        return

    if mtype == "claim_role":
        side = msg.get("side")
        if room.phase == "lobby" and room.mode == "A" and side in ("正方", "反方"):
            if all(m.role != side or m.user_id == member.user_id
                   for m in room.members.values()):
                if member.role == side:
                    return
                member.role = side
                _room_bump_roster_generation(room)
                _room_invalidate_precheck(room)
                await _room_broadcast(room, {
                    "type": "roster", "roster": room.roster(),
                    "roster_generation": room.roster_generation,
                })
        return

    if mtype == "start" and is_host:
        await _room_begin_precheck(room)
        return

    if mtype in ("next_segment", "set_segment") and is_host:
        if (
            room.rtc_pause_started_ms is not None
            or room.segment_transitioning
            or _room_control_task_active(room, "segment")
        ):
            return
        expected_from = room.seg_index
        if mtype == "set_segment":
            try:
                idx = int(msg.get("index", room.seg_index))
            except Exception:
                idx = room.seg_index
        else:
            idx = room.seg_index + 1
        room.segment_transitioning = True
        task = _room_schedule_control_task(
            room, "segment",
            lambda: _room_run_scheduled_segment_advance(
                room, idx, expected_from,
            ),
        )
        if task is None:
            room.segment_transitioning = False
        return

    if mtype == "end" and is_host:
        if room.phase not in ("ending", "ended") and not room.terminal_requested:
            room.terminal_requested = True
            _room_schedule_control_task(
                room, "end", lambda: _room_end(room, "host"),
            )
        return

    if mtype in ("turn_begin", "turn_end"):
        request_id = msg.get("request_id")
        if mtype == "turn_end" and not _room_message_matches_active_turn(
            room, member, msg,
        ):
            return
        await _room_handle_turn(
            room, member, mtype == "turn_begin",
            request_id=request_id if isinstance(request_id, str) else "",
        )
        return

    if mtype == "turn_stop_intent":
        await _room_handle_turn_stop_intent(room, member, msg)
        return

    if mtype == "precheck_result":
        await _room_handle_precheck_result(room, member, msg)
        return

    if mtype == "transcript_chunk":
        await _room_handle_transcript(room, member, msg)
        return

    if mtype == "transcript_commit":
        await _room_commit_transcript(room, member, msg)
        return

    if mtype == "request_judgement":
        await _room_send_member(room, member, {
            "type": "error",
            "message": "AI 評判只可由主持在完場後透過結果頁要求一次。",
        })
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
        await _room_send_member(room, member, {
            "type": "test_pong",
            "client_ts": client_ts,
            "server_now_ms": _now_ms(),
        })
        return

    if mtype == "heartbeat":
        await _room_send_member(room, member, {
            "type": "heartbeat_ack", "server_now_ms": _now_ms(),
        })
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
    side = payload.get("side")
    room.creator_side = side if side in ("正方", "反方") else "正方"
    return room


@app.post("/api/room/create")
async def room_create(request: Request):
    user_id = require_page_user(request, "ai_room")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    raw_mode = payload.get("mode")
    if not isinstance(raw_mode, str) or not raw_mode.strip():
        raise HTTPException(status_code=400, detail="練習模式無效。")
    mode = raw_mode.strip().upper()
    if mode == "B":
        raise HTTPException(status_code=400, detail="多人一隊對 AI（Mode B）已移除。")
    if mode != "A":
        raise HTTPException(status_code=400, detail="只支援 Mode A 真人 P2P 練習。")
    raw_format = payload.get("debate_format")
    if not isinstance(raw_format, str):
        raise HTTPException(status_code=400, detail="賽制無效。")
    debate_format = raw_format.strip()
    if debate_format not in DEBATE_FORMATS:
        raise HTTPException(status_code=400, detail="不支援的賽制。")
    raw_structure = payload.get("structure")
    if not isinstance(raw_structure, str):
        raise HTTPException(status_code=400, detail="練習結構無效。")
    structure = raw_structure.strip()
    if structure not in ("free", "mock"):
        raise HTTPException(status_code=400, detail="只支援自由辯論或完整 Mock。")
    if structure == "free" and debate_format not in FREE_DEBATE_FORMATS:
        raise HTTPException(status_code=400, detail=f"{debate_format}不設自由辯論，請改用完整 Mock。")
    raw_side = payload.get("side")
    if not isinstance(raw_side, str) or raw_side.strip() not in ("正方", "反方"):
        raise HTTPException(status_code=400, detail="立場必須為正方或反方。")
    side = raw_side.strip()
    raw_topic = payload.get("topic")
    if not isinstance(raw_topic, str) or not raw_topic.strip():
        raise HTTPException(status_code=400, detail="請輸入辯題。")
    topic = raw_topic.strip()
    if len(topic) > 500:
        raise HTTPException(status_code=400, detail="辯題不可超過500字。")
    raw_minutes = payload.get("free_minutes")
    if isinstance(raw_minutes, bool):
        raise HTTPException(status_code=400, detail="自由辯論時間無效。")
    try:
        free_minutes = float(raw_minutes)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail="自由辯論時間無效。") from exc
    minimum_minutes = 2.0 if structure == "mock" else 0.5
    if (
        not math.isfinite(free_minutes)
        or not minimum_minutes <= free_minutes <= float(LIVE_FREE_MAX_MINUTES)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"自由辯論每邊時間必須為{minimum_minutes:g}至"
                f"{float(LIVE_FREE_MAX_MINUTES):g}分鐘。"
            ),
        )
    capacity = 2

    async with ROOMS_LOCK:
        _gc_rooms()
        if _active_room_count() >= MAX_ROOMS:
            raise HTTPException(status_code=429, detail="太多練習房，請稍後再試")
        if _retained_ended_room_count() >= ROOM_RETAINED_ENDED_MAX:
            raise HTTPException(
                status_code=429,
                detail="完場結果暫存已滿，請稍後再建立房間。",
            )
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
            free_minutes, capacity, {**payload, "side": side},
        )
        ROOMS[code] = room
        _room_ensure_lifecycle(room)
        _room_schedule_empty_cleanup(room)
    return JSONResponse(
        {"ok": True, "code": code, "mode": mode},
        headers={"Cache-Control": CACHE_NO_STORE},
    )


@app.get("/api/room/{code}")
async def room_info(code: str, request: Request):
    user_id = require_page_user(request, "ai_room")
    room = ROOMS.get((code or "").upper())
    if not room:
        raise HTTPException(status_code=404, detail="房間不存在")
    access_error = _room_nonmember_access_error(room, user_id)
    if access_error:
        status_code, detail = access_error
        raise HTTPException(status_code=status_code, detail=detail)
    return JSONResponse({
        "ok": True, "code": room.code, "mode": room.mode, "phase": room.phase,
        "debate_format": room.debate_format, "topic": room.topic,
        "structure": room.structure, "capacity": room.capacity,
        "roster": room.roster(),
    }, headers={"Cache-Control": CACHE_NO_STORE})


@app.post("/api/room/{code}/leave")
async def room_leave(code: str, request: Request):
    user_id = require_page_user(request, "ai_room")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    leave_token = payload.get("leave_token") if isinstance(payload, dict) else None
    room = ROOMS.get((code or "").upper())
    if room and user_id in room.members:
        websocket = None
        changed = False
        pause_started_ms = None
        async with room.lock:
            member = room.members.get(user_id)
            if member is not None and member.connected:
                if (
                    not isinstance(leave_token, str)
                    or not 0 < len(leave_token) <= 128
                    or not secrets.compare_digest(member.leave_token, leave_token)
                ):
                    return JSONResponse({
                        "ok": False,
                        "detail": "離開憑證已過期；目前連線未有中斷。",
                    }, status_code=409, headers={
                        "Cache-Control": CACHE_NO_STORE,
                    })
                member.leave_token = secrets.token_urlsafe(24)
                pause_started_ms = _room_claim_control_disconnect_pause_locked(
                    room, member,
                )
                websocket = member.ws
                member.connected = False
                member.connection_generation += 1
                _room_bump_roster_generation(room)
                _room_invalidate_precheck(room)
                if room.phase == "lobby":
                    room.members.pop(user_id, None)
                changed = True
        if websocket is not None:
            try:
                await asyncio.wait_for(
                    websocket.close(code=1000, reason="member left room"),
                    timeout=ROOM_WS_SEND_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
        if changed:
            await _room_broadcast(room, {
                "type": "peer_left", "user_id": user_id,
                "roster_generation": room.roster_generation,
            })
            await _room_broadcast(room, {
                "type": "roster", "roster": room.roster(),
                "roster_generation": room.roster_generation,
            })
            await _room_complete_disconnect_pause(
                room, pause_started_ms, "room_leave",
            )
            if not room.connected_user_ids() and not room.terminal_requested:
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
    judgement_pending = bool(
        room.judgement_request_started
        and room.judgement_task is not None
        and not room.judgement_task.done()
    )
    transcript_error = _room_judgement_transcript_error(room)
    return JSONResponse({
        "ok": True, "topic": room.topic, "debate_format": room.debate_format,
        "phase": room.phase,
        "host": room.created_by,
        "is_host": user_id == room.created_by,
        "roster": room.roster(),
        "transcript": room.transcript, "judgement": room.judgement,
        "transcript_revision": room.transcript_revision,
        "judgement_revision": room.judgement_revision,
        "judge_enabled": room.judge_enabled,
        "judge_disabled_reason": room.judge_disabled_reason,
        "judgement_requested": room.judgement_request_started,
        "judgement_pending": judgement_pending,
        "can_request_judgement": bool(
            room.phase == "ended"
            and user_id == room.created_by
            and room.judge_enabled
            and not room.judgement_request_started
            and not transcript_error
        ),
    }, headers={"Cache-Control": CACHE_NO_STORE})


@app.post("/api/room/{code}/judgement")
async def room_request_judgement(code: str, request: Request):
    """Atomically launch the host's one explicit post-match judgement."""
    user_id = require_page_user(request, "ai_room")
    room = ROOMS.get((code or "").upper())
    if not room:
        raise HTTPException(status_code=404, detail="房間不存在")
    if user_id != room.created_by or user_id not in room.members:
        raise HTTPException(status_code=403, detail="只有原房主持可要求 AI 評判。")
    if room.phase != "ended":
        raise HTTPException(status_code=409, detail="只可在完場後要求 AI 評判。")
    if not room.judge_enabled:
        raise HTTPException(
            status_code=409,
            detail=room.judge_disabled_reason or "本房 AI 評判已停用。",
        )
    transcript_error = _room_judgement_transcript_error(room)
    if transcript_error:
        # Missing evidence is retryable and does not consume the once-only
        # judgement claim or create any provider task.
        raise HTTPException(status_code=409, detail=transcript_error)

    async with ROOMS_LOCK:
        if ROOMS.get(room.code) is not room:
            raise HTTPException(status_code=404, detail="房間結果已過期。")
        async with room.lock:
            if not room.judgement_request_started:
                room.judgement_request_started = True
                room.judgement_request_revision = room.transcript_revision
                _room_extend_ended_retention(room)
                room.judgement_task = asyncio.create_task(
                    _room_request_judgement(room),
                )
            task = room.judgement_task
            pending = bool(task is not None and not task.done())
            judgement = room.judgement
    return JSONResponse({
        "ok": True,
        "judgement": judgement,
        "judgement_pending": pending,
        "judgement_requested": True,
        "judge_enabled": room.judge_enabled,
        "judge_disabled_reason": room.judge_disabled_reason,
    }, status_code=202 if pending else 200, headers={
        "Cache-Control": CACHE_NO_STORE,
    })


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
            connected_ids = set(room.connected_user_ids())
            if len(connected_ids) >= room.capacity:
                return None, "房間已滿", 1013
            if (
                user_id != room.created_by
                and room.created_by not in connected_ids
                and len(connected_ids) >= max(0, room.capacity - 1)
            ):
                return None, "房間正預留一個位置畀主持。", 1013
            member = RoomMember(user_id, websocket)
            if user_id == room.created_by and room.creator_side:
                member.role = room.creator_side
            else:
                taken = {m.role for m in room.members.values() if m.connected}
                if room.created_by not in connected_ids and room.creator_side:
                    taken.add(room.creator_side)
                for side in ("正方", "反方"):
                    if side not in taken:
                        member.role = side
                        break
            room.members[user_id] = member
            _room_bump_roster_generation(room)
            _room_invalidate_precheck(room)
        else:
            if not existing.connected and len(room.connected_user_ids()) >= room.capacity:
                return None, "房間已滿", 1013
            if existing.ws is not websocket:
                stale_websocket = existing.ws
                replacement_pause_started_ms = _room_claim_disconnect_pause_locked(
                    room, existing,
                )
                if replacement_pause_started_ms is not None:
                    existing.restart_required = replacement_pause_started_ms
                existing.connection_generation += 1
            existing.ws = websocket
            existing.connected = True
            existing.rtc_status = "new"
            existing.leave_token = secrets.token_urlsafe(24)
            member = existing
            _room_bump_roster_generation(room)
            _room_invalidate_precheck(room)
        _room_cancel_empty_cleanup(room)
    if stale_websocket is not None:
        try:
            await asyncio.wait_for(
                stale_websocket.close(code=1000, reason="connection replaced"),
                timeout=ROOM_WS_SEND_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
    return member, "", 0


def _room_websocket_origin_allowed(websocket):
    """Require a browser WebSocket Origin matching the request Host."""
    headers = getattr(websocket, "headers", {})
    origin = str(headers.get("origin") or "").strip()
    host = str(headers.get("host") or "").strip()
    if not origin or not host:
        return False
    forwarded_proto = str(headers.get("x-forwarded-proto") or "").split(",", 1)[0]
    forwarded_proto = forwarded_proto.strip().lower()
    if forwarded_proto in {"http", "https"}:
        public_scheme = forwarded_proto
    else:
        request_scheme = str(
            getattr(websocket, "scope", {}).get("scheme") or ""
        ).lower()
        public_scheme = {"ws": "http", "wss": "https"}.get(request_scheme)
    if public_scheme not in {"http", "https"}:
        return False
    try:
        parsed_origin = urlsplit(origin)
        parsed_host = urlsplit(f"//{host}")
        if (
            parsed_origin.scheme not in {"http", "https"}
            or parsed_origin.scheme != public_scheme
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            return False
        origin_hostname = (parsed_origin.hostname or "").lower()
        host_hostname = (parsed_host.hostname or "").lower()
        origin_port = parsed_origin.port or (
            443 if parsed_origin.scheme == "https" else 80
        )
        host_port = parsed_host.port or (
            443 if public_scheme == "https" else 80
        )
    except (TypeError, ValueError):
        return False
    return bool(
        origin_hostname
        and origin_hostname == host_hostname
        and origin_port == host_port
    )


@app.websocket("/room/{code}")
async def room_ws(websocket: WebSocket, code: str):
    # Authenticate before accept.  The same-origin HttpOnly cookie is the only
    # browser credential, so signed member tokens never enter URLs or storage.
    user_id = _verify_committee_token(websocket.cookies.get("committee_user") or "")
    if not user_id or not account_can_access(user_id, "ai_room"):
        await websocket.close(code=1008)
        return
    if not _room_websocket_origin_allowed(websocket):
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
            await asyncio.wait_for(
                websocket.send_text(json.dumps(
                    {"type": "error", "message": registration_error},
                    ensure_ascii=False,
                )),
                timeout=ROOM_WS_SEND_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
        await websocket.close(code=close_code)
        return
    socket_generation = member.connection_generation

    try:
        if not await _room_send_member(room, member, {
            "type": "roster", "you": user_id, "mode": room.mode,
            "roster": room.roster(), "topic": room.topic,
            "debate_format": room.debate_format, "structure": room.structure,
            "is_host": user_id == room.created_by,
            "transcript": room.transcript,
            "judgement": room.judgement,
            "judge_enabled": room.judge_enabled,
            "judge_disabled_reason": room.judge_disabled_reason,
            "leave_token": member.leave_token,
            "roster_generation": room.roster_generation,
        }, websocket=websocket, generation=socket_generation):
            return
        # Publish the new signaling generation to the existing peer before any
        # paused state can trigger an ICE-restart offer from this socket.
        await _room_broadcast(room, {
            "type": "roster", "roster": room.roster(),
            "roster_generation": room.roster_generation,
        }, exclude=user_id)
        if not await _room_send_member(
            room, member, room.state_msg(),
            websocket=websocket, generation=socket_generation,
        ):
            return
        replacement_pause_started_ms = None
        async with room.lock:
            if (
                member.ws is websocket
                and member.connection_generation == socket_generation
                and member.connected
                and member.restart_required is not None
            ):
                replacement_pause_started_ms = member.restart_required
                member.restart_required = None
        if replacement_pause_started_ms is not None:
            _room_schedule_control_task(
                room, f"rtc_pause:{replacement_pause_started_ms}",
                lambda stamp=replacement_pause_started_ms: (
                    _room_complete_disconnect_pause(
                        room, stamp, "control_replaced",
                    )
                ),
            )
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
            # Bound signaling/control JSON independently of Uvicorn's ceiling.
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
        # and corrupts the P2P roster generation.
        is_current = False
        pause_started_ms = None
        async with room.lock:
            is_current = (
                member.ws is websocket
                and member.connection_generation == socket_generation
                and member.connected
            )
            if is_current:
                pause_started_ms = _room_claim_control_disconnect_pause_locked(
                    room, member,
                )
                member.connected = False
                member.connection_generation += 1
                _room_bump_roster_generation(room)
                _room_invalidate_precheck(room)
                if room.phase == "lobby":
                    room.members.pop(user_id, None)
        if is_current:
            await _room_broadcast(room, {
                "type": "peer_left", "user_id": user_id,
                "roster_generation": room.roster_generation,
            })
            await _room_broadcast(room, {
                "type": "roster", "roster": room.roster(),
                "roster_generation": room.roster_generation,
            })
            await _room_complete_disconnect_pause(
                room, pause_started_ms, "control_disconnect",
            )
            if not room.connected_user_ids() and not room.terminal_requested:
                _room_schedule_empty_cleanup(room)


@app.websocket("/{path:path}")
async def websocket_not_found(websocket: WebSocket, path: str):
    await websocket.close(code=1008, reason="Unknown WebSocket route")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def http_not_found(request: Request, path: str):
    return Response(content=json.dumps({"detail": "Not Found"}), status_code=404, media_type="application/json")
