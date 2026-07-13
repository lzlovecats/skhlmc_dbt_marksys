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
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.gzip import GZipMiddleware
from starlette.websockets import WebSocketDisconnect

from schema import (
    CREATE_AI_FUND_USAGE_LOGS,
    CREATE_BANDWIDTH_USAGE_LOGS,
    CREATE_PRACTICE_DAILY_USAGE,
    CREATE_PUSH_SUBSCRIPTIONS,
    CREATE_VIDEO_PROGRESS,
    CREATE_VIDEO_VIEWS,
    MEDIA_R2_STARTUP_MIGRATIONS,
    RUNTIME_OWNED_STARTUP_DDL,
    TABLE_AI_FUND_USAGE_LOGS,
    TABLE_BANDWIDTH_USAGE_LOGS,
    TABLE_PRACTICE_DAILY_USAGE,
    TABLE_PUSH_SUBSCRIPTIONS,
    TABLE_VIDEO_PROGRESS,
    TABLE_VIDEO_VIEWS,
)
from core.config_store import (
    get_configs_from_connection,
    migrate_legacy_config,
    set_configs_on_connection,
)
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
from ai_model_config import ROOM_JUDGEMENT_MODEL_LABELS, model_slugs_for_labels
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
from version import APP_VERSION
from system_limits import (
    BANDWIDTH_CHECKPOINT_SECONDS, BANDWIDTH_ESSENTIAL_ONLY_BYTES,
    BANDWIDTH_LOG_RETENTION_DAYS, BANDWIDTH_STOP_LIVE_BYTES,
    BANDWIDTH_WARN_BYTES, CACHE_HTML_MAX_AGE_SECONDS, CACHE_HTML_STALE_SECONDS,
    CACHE_MANIFEST_MAX_AGE_SECONDS, CACHE_SHARED_MAX_AGE_SECONDS,
    CACHE_SHARED_STALE_SECONDS, CACHE_STATIC_MAX_AGE_SECONDS, GEMINI_RELAY_MAX_BYTES,
    GEMINI_RELAY_MAX_SECONDS, GEMINI_RELAY_MIN_SECONDS,
    GEMINI_RELAY_SIGNATURE_TTL_SECONDS, GEMINI_WS_MAX_SIZE,
    GZIP_COMPRESS_LEVEL, GZIP_MINIMUM_SIZE, LIVE_FREE_MAX_MINUTES,
    MAINTENANCE_PRUNE_INTERVAL_SECONDS, MAX_HTTP_BODY_BYTES, MAX_ROOMS,
    MODEL_DEPLOYABLE_CACHE_TTL_SECONDS, MULTIPLAYER_FREE_MONTHLY_ROOMS,
    MULTIPLAYER_MOCK_MONTHLY_ROOMS, PRACTICE_LIVE_MAX_PER_HOUR,
    PRACTICE_LIVE_MIN_GAP_SECONDS, PRACTICE_LIVE_RATE_WINDOW_SECONDS,
    PROJECTOR_MATCH_LIMIT,
    PUSH_ACTIVE_DEVICES_PER_USER, PUSH_ENDPOINT_MAX_CHARS,
    PUSH_INACTIVE_RETENTION_DAYS, PUSH_KEY_MAX_CHARS,
    PUSH_SUBSCRIPTION_MAX_BYTES,
    REQUEST_BODY_BUFFER_CONCURRENCY, ROOM_EMPTY_GRACE_SECONDS,
    ROOM_JUDGEMENT_TIMEOUT_SECONDS,
    ROOM_MAX_AGE_SECONDS, ROOM_MAX_CAPACITY, ROOM_NATIVE_AUDIO_BUFFER_MAX_BYTES,
    ROOM_PENDING_TRANSCRIPT_MAX_CHARS, ROOM_TRANSCRIPT_ITEM_MAX_CHARS,
    ROOM_TRANSCRIPT_MAX_ITEMS, ROOM_WS_SEND_TIMEOUT_SECONDS, SOLO_FREE_MONTHLY_LIMIT,
    SOLO_MOCK_MONTHLY_LIMIT, TTS_CONCURRENCY, TTS_LEXICON_CACHE_TTL_SECONDS, TTS_LEXICON_LIMIT,
    TTS_MAX_RESPONSE_BYTES, TTS_PROVIDER_CONNECT_TIMEOUT_SECONDS,
    TTS_PROVIDER_TIMEOUT_SECONDS, TTS_TEXT_MAX_CHARS, VIDEO_PROGRESS_MAX_SECONDS,
    VIDEO_VIEW_DEDUPE_HOURS,
)


BASE_DIR = Path(__file__).resolve().parents[1]

CACHE_NO_CACHE = "no-cache"
CACHE_HTML = f"public, max-age={CACHE_HTML_MAX_AGE_SECONDS}, stale-while-revalidate={CACHE_HTML_STALE_SECONDS}"
CACHE_MANIFEST = f"public, max-age={CACHE_MANIFEST_MAX_AGE_SECONDS}"
CACHE_STATIC = f"public, max-age={CACHE_STATIC_MAX_AGE_SECONDS}, immutable"
CACHE_SHARED = f"public, max-age={CACHE_SHARED_MAX_AGE_SECONDS}, stale-while-revalidate={CACHE_SHARED_STALE_SECONDS}"

@asynccontextmanager
async def _lifespan(_app):
    try:
        run_safe_startup_migrations()
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
    "/api/tts/azure",
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
        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        try:
            declared = int(headers.get(b"content-length", b"0") or b"0")
        except ValueError:
            declared = self.max_bytes + 1
        if declared > self.max_bytes:
            await self._reject(send)
            return

        # At most a few 5MB buffers may be assembled simultaneously.  This is
        # separate from Uvicorn's endpoint concurrency because the complete
        # request has to be verified before Pydantic/endpoint code can run.
        async with self.buffer_slots:
            buffered = []
            received = 0
            while True:
                message = await receive()
                buffered.append(message)
                if message.get("type") == "http.request":
                    received += len(message.get("body") or b"")
                    if received > self.max_bytes:
                        await self._reject(send)
                        return
                    if not message.get("more_body", False):
                        break
                elif message.get("type") == "http.disconnect":
                    break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.inner(scope, replay_receive, send)


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
logger = logging.getLogger("skh_proxy")
_DDL_LOCK = threading.Lock()
_DDL_READY_ENGINES = set()


def _run_proxy_ddl_once(conn, schema_key: str, statements) -> None:
    """Run compatibility DDL once per real engine, while keeping fakes simple."""

    engine = getattr(conn, "engine", None)
    cache_key = (engine, schema_key) if engine is not None else None
    if cache_key is not None and cache_key in _DDL_READY_ENGINES:
        return
    with _DDL_LOCK:
        if cache_key is not None and cache_key in _DDL_READY_ENGINES:
            return
        for statement in statements:
            conn.execute(text(statement))
        if cache_key is not None:
            _DDL_READY_ENGINES.add(cache_key)


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


def run_safe_startup_migrations():
    """Apply small, idempotent compatibility migrations before serving traffic."""
    engine = _get_db_engine()
    if engine is None:
        logger.warning("Skipping startup migrations: database is not configured")
        return
    with engine.begin() as conn:
        config_result = migrate_legacy_config(conn)
        if config_result["unknown"]:
            logger.warning(
                "Migrated %s unregistered legacy config keys; classify them before "
                "removing system_config",
                config_result["unknown"],
            )
        for ddl in MEDIA_R2_STARTUP_MIGRATIONS:
            conn.execute(text(ddl))
        for ddl in RUNTIME_OWNED_STARTUP_DDL:
            conn.execute(text(ddl))


def get_vote_db():
    """The DB executor passed to ``core.vote_logic`` from the API handlers."""
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return RuntimeDb(engine)


def _verify_committee_token(token: str):
    """Verify a signed ``user_id:sig`` token against the shared cookie secret."""
    if not token or ":" not in token:
        return None

    engine = _get_db_engine()
    if engine is None:
        return None

    user_id, sig = token.rsplit(":", 1)
    with engine.begin() as conn:
        configs = get_configs_from_connection(
            conn, ("cookie_secret", "login_disabled_accounts")
        )
    if "cookie_secret" not in configs:
        return None
    disabled_accounts = configs.get("login_disabled_accounts") or []
    if isinstance(disabled_accounts, list) and user_id in disabled_accounts:
        return None

    secret = configs["cookie_secret"]
    expected = hmac.new(str(secret).encode(), user_id.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected):
        return user_id
    return None


def _verify_committee_cookie(request: Request):
    return _verify_committee_token(request.cookies.get("committee_user") or "")


_relay_cookie_secret = None


def _get_relay_cookie_secret():
    """Read (and cache) the shared cookie_secret used to sign Gemini Live relay
    tokens. Cached in-process because it rarely changes and this is hit on every
    relay connection attempt."""
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


def _relay_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _relay_b64decode(value: str) -> bytes:
    raw = value.encode("ascii")
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


def _sign_relay_token(
    token: str, user_id: str, practice_kind: str, max_seconds: int,
    practice_id: str,
) -> str:
    """Bind a relay token to its member, quota category and server deadline."""
    secret = _get_relay_cookie_secret()
    if not secret or practice_kind not in ("solo_free", "solo_mock"):
        return ""
    payload = {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "user_id": str(user_id),
        "practice_kind": practice_kind,
        "practice_id": str(practice_id),
        "max_seconds": max(GEMINI_RELAY_MIN_SECONDS, min(int(max_seconds), GEMINI_RELAY_MAX_SECONDS)),
        "exp": int(time.time()) + GEMINI_RELAY_SIGNATURE_TTL_SECONDS,
    }
    encoded = _relay_b64(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    signature = hmac.new(secret.encode(), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_relay_b64(signature)}"


def _verify_relay_signature(token: str, signed_claim: str) -> dict | None:
    """Verify and return the server-authoritative Gemini relay claim."""
    if not token or not signed_claim:
        return None
    secret = _get_relay_cookie_secret()
    if not secret:
        return None
    try:
        encoded, supplied = signed_claim.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_relay_b64(expected), supplied):
            return None
        payload = json.loads(_relay_b64decode(encoded))
        if payload.get("token_sha256") != hashlib.sha256(token.encode()).hexdigest():
            return None
        if payload.get("practice_kind") not in ("solo_free", "solo_mock"):
            return None
        if not str(payload.get("user_id") or "") or not str(payload.get("practice_id") or ""):
            return None
        if int(payload.get("exp") or 0) < int(time.time()):
            return None
        seconds = int(payload.get("max_seconds") or 0)
        if not GEMINI_RELAY_MIN_SECONDS <= seconds <= GEMINI_RELAY_MAX_SECONDS:
            return None
        return payload
    except Exception:
        return None


def _sign_committee_token(user_id: str):
    """Mint a signed ``user_id:sig`` committee token, matching
    _verify_committee_token / auth._sign_cookie. Used by the committee login API
    to set the shared ``committee_user`` cookie. Returns None if no secret."""
    secret = _get_relay_cookie_secret()
    if not secret:
        return None
    sig = hmac.new(str(secret).encode(), user_id.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}:{sig}"


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


def _sign_judging_token(match_id: str):
    """Mint a session token restricted to one judge-accessible match."""
    secret = _get_relay_cookie_secret()
    if not secret or not match_id:
        return None
    subject = f"judging:{match_id}"
    sig = hmac.new(str(secret).encode(), subject.encode(), hashlib.sha256).hexdigest()
    return f"{subject}:{sig}"


def _verify_judging_token(token: str):
    if not token or ":" not in token:
        return None
    subject, sig = token.rsplit(":", 1)
    if not subject.startswith("judging:"):
        return None
    match_id = subject[len("judging:"):]
    secret = _get_relay_cookie_secret()
    if not secret or not match_id:
        return None
    expected = hmac.new(str(secret).encode(), subject.encode(), hashlib.sha256).hexdigest()
    return match_id if hmac.compare_digest(sig, expected) else None


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


def _ensure_push_subscriptions_table(conn):
    _run_proxy_ddl_once(conn, "push-subscriptions", (
        CREATE_PUSH_SUBSCRIPTIONS,
    ))


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


def _ensure_video_tracking_tables(conn):
    _run_proxy_ddl_once(conn, "video-tracking", (
        CREATE_VIDEO_VIEWS,
        CREATE_VIDEO_PROGRESS,
    ))


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
    user_id = _require_committee_user(request)
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
        _ensure_push_subscriptions_table(conn)
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
    user_id = _require_committee_user(request)
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
        _ensure_push_subscriptions_table(conn)
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
        _ensure_push_subscriptions_table(conn)

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
    voice = xml_escape(voice or "zh-HK-HiuMaanNeural", {'"': "&quot;"})
    rate = xml_escape(rate or "0%", {'"': "&quot;"})
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


def tts_provider_configured() -> bool:
    """Whether the active TTS provider has the secrets it needs. Drives whether
    live-room server-side TTS turns on (else the room stays on Gemini native audio)."""
    provider = (_get_proxy_secret("TTS_PROVIDER", "azure").strip() or "azure").lower()
    if provider == "custom":
        model_id = _get_proxy_secret("CUSTOM_TTS_MODEL_VERSION").strip()
        return bool(
            _get_proxy_secret("CUSTOM_TTS_URL").strip()
            and _get_proxy_secret("CUSTOM_TTS_API_KEY").strip()
            and model_id and _model_is_deployable(model_id, "tts")
        )
    return bool(
        _get_proxy_secret("AZURE_SPEECH_KEY").strip()
        and _get_proxy_secret("AZURE_SPEECH_REGION").strip()
    )


_lexicon_cache = {"rows": None, "at": 0.0}
_deployable_model_cache = {"values": {}, "at": 0.0}
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
    now = time.monotonic()
    key = (model_id, model_type)
    if now - _deployable_model_cache["at"] < MODEL_DEPLOYABLE_CACHE_TTL_SECONDS and key in _deployable_model_cache["values"]:
        return bool(_deployable_model_cache["values"][key])
    allowed = False
    try:
        engine = _get_db_engine()
        with engine.connect() as conn:
            allowed = conn.execute(text("""SELECT EXISTS(SELECT 1 FROM ai_model_versions
                WHERE model_id=:model AND model_type=:type AND status='deployable')"""),
                {"model": model_id, "type": model_type}).scalar()
    except Exception as exc:
        logger.info("model deployable gate unavailable: %s", exc)
    _deployable_model_cache["values"][key] = bool(allowed)
    _deployable_model_cache["at"] = now
    return bool(allowed)


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


async def _synthesize_azure(text_value: str) -> tuple[bytes, str]:
    speech_key = _get_proxy_secret("AZURE_SPEECH_KEY").strip()
    speech_region = _get_proxy_secret("AZURE_SPEECH_REGION").strip()
    if not speech_key or not speech_region:
        raise TtsUnavailable("Azure TTS is not configured", status=503)

    voice = _get_proxy_secret("AZURE_TTS_VOICE", "zh-HK-HiuMaanNeural").strip() or "zh-HK-HiuMaanNeural"
    rate = _get_proxy_secret("AZURE_TTS_RATE", "0%").strip() or "0%"
    output_format = (
        _get_proxy_secret("AZURE_TTS_OUTPUT_FORMAT", "audio-24khz-48kbitrate-mono-mp3").strip()
        or "audio-24khz-48kbitrate-mono-mp3"
    )
    ssml = _build_azure_tts_ssml(text_value, voice, rate)
    endpoint = f"https://{speech_region}.tts.speech.microsoft.com/cognitiveservices/v1"

    try:
        async with httpx.AsyncClient(timeout=TTS_PROVIDER_TIMEOUT_SECONDS) as client:
            async with client.stream(
                "POST",
                endpoint,
                content=ssml.encode("utf-8"),
                headers={
                    "Ocp-Apim-Subscription-Key": speech_key,
                    "Content-Type": "application/ssml+xml; charset=utf-8",
                    "X-Microsoft-OutputFormat": output_format,
                    "User-Agent": "skhlmc-dbt-marksys",
                },
            ) as azure_response:
                if azure_response.status_code != 200:
                    logger.warning("Azure TTS returned %s", azure_response.status_code)
                    raise TtsUnavailable("Azure TTS request failed", status=502)
                audio = await _read_bounded_audio(azure_response)
                mime = azure_response.headers.get("content-type") or "audio/mpeg"
    except httpx.HTTPError as e:
        logger.warning("Azure TTS request failed: %s", e)
        raise TtsUnavailable("Azure TTS request failed", status=502)

    return audio, mime


async def _synthesize_custom(text_value: str) -> tuple[bytes, str]:
    """Call the authenticated custom TTS service using the stable wire contract."""
    custom_url = _get_proxy_secret("CUSTOM_TTS_URL").strip()
    api_key = _get_proxy_secret("CUSTOM_TTS_API_KEY").strip()
    model_version = _get_proxy_secret("CUSTOM_TTS_MODEL_VERSION").strip()
    if not custom_url or not api_key or not model_version:
        raise TtsUnavailable("Custom TTS is not configured", status=503)
    if not _model_is_deployable(model_version, "tts"):
        raise TtsUnavailable("Custom TTS model has not passed the deployable gate", status=503)
    request_id = secrets.token_urlsafe(12)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(
            TTS_PROVIDER_TIMEOUT_SECONDS, connect=TTS_PROVIDER_CONNECT_TIMEOUT_SECONDS,
        )) as client:
            async with client.stream("POST", custom_url, headers={
                    "Authorization": f"Bearer {api_key}", "Accept": "audio/*",
                    "X-Request-ID": request_id,
                }, json={"text": text_value, "model_version": model_version,
                         "request_id": request_id}) as response:
                response.raise_for_status()
                mime = (response.headers.get("content-type") or "audio/wav").split(";", 1)[0]
                if not mime.startswith("audio/"):
                    raise TtsUnavailable("Custom TTS returned invalid audio", status=502)
                audio = await _read_bounded_audio(response)
                response_model = response.headers.get("x-model-version") or model_version
    except httpx.HTTPError as exc:
        logger.warning("custom TTS failed request_id=%s: %s", request_id, exc)
        raise TtsUnavailable("Custom TTS request failed", status=502) from exc
    logger.info("custom TTS success request_id=%s model=%s elapsed_ms=%d bytes=%d",
                request_id, response_model,
                int((time.monotonic() - started) * 1000), len(audio))
    return audio, mime


async def _synthesize_tts(text_value: str) -> tuple[bytes, str]:
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
    provider = (_get_proxy_secret("TTS_PROVIDER", "azure").strip() or "azure").lower()
    async with TTS_SEMAPHORE:
        if provider == "custom":
            try:
                return await _synthesize_custom(processed)
            except TtsUnavailable as exc:
                logger.warning("custom TTS unavailable; falling back to Azure: %s", exc)
                return await _synthesize_azure(processed)
        return await _synthesize_azure(processed)


@app.post("/api/tts/azure")
async def azure_tts(request: Request):
    user_id = _require_committee_user(request)
    budget_error = _bandwidth_essential_gate_error()
    if budget_error:
        raise HTTPException(status_code=429, detail=budget_error)

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
        audio_bytes, mime = await _synthesize_tts(tts_text)
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
    user_id = _require_committee_user(request)
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
        _ensure_video_tracking_tables(conn)
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
    user_id = _require_committee_user(request)
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
        _ensure_video_tracking_tables(conn)
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
            text("SELECT topic_text, pro_team, con_team FROM matches WHERE match_id = :id"),
            {"id": match_id},
        ).fetchone()
        drows = conn.execute(
            text("SELECT side, position, debater_name FROM debaters WHERE match_id = :id"),
            {"id": match_id},
        ).fetchall()

    names = {(d._mapping["side"], d._mapping["position"]): d._mapping["debater_name"]
             for d in drows}

    seq = get_full_mock_sequence(debate_format)
    total = len(seq)
    idx = r.get("seg_index") or 0
    if total:
        idx = max(0, min(idx, total - 1))
    seg = seq[idx] if total else {"id": "", "label": "", "side": ""}
    slot = _seg_speaker_slot(seg["id"])
    speaker_name = names.get(slot) if slot else None

    mm = m._mapping if m else {}
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
    debate_format = request.query_params.get("format", "校園隨想")
    seq = get_full_mock_sequence(debate_format)
    return {"format": debate_format,
            "segments": [{"label": s["label"], "side": s["side"]} for s in seq]}


@app.get("/api/projector/matches")
async def projector_list_matches(request: Request):
    _require_committee_user(request)
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT match_id, match_date, match_time, topic_text, pro_team, con_team "
            "FROM matches ORDER BY match_date DESC NULLS LAST, match_time DESC NULLS LAST "
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
        }
        for x in rows
    ]}


@app.post("/api/projector/state")
async def projector_set_state(request: Request):
    _require_committee_user(request)
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    display_key = str(payload.get("display") or PROJECTOR_DEFAULT_DISPLAY)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    with engine.begin() as conn:
        current = conn.execute(
            text("SELECT match_id, debate_format, seg_index, visible "
                 "FROM projector_state WHERE display_key = :k"),
            {"k": display_key},
        ).fetchone()
        cur = current._mapping if current else {}

        # partial update: keep existing values for any field not supplied
        match_id = payload.get("match_id", cur.get("match_id"))
        debate_format = payload.get("debate_format", cur.get("debate_format") or "校園隨想")
        seg_index = payload.get("seg_index", cur.get("seg_index") or 0)
        visible = payload.get("visible", cur.get("visible") if current else True)
        try:
            seg_index = int(seg_index)
        except Exception:
            seg_index = 0
        visible = bool(visible)

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

    return _resolve_projector_state(engine, display_key)


# ---------------------------------------------------------------------------
# Appliance practice page (login-free kiosk hub)
#
# Additive and self-contained, same pattern as the projector above. Serves one
# static big-text page (templates/appliance_practice.html) meant for the
# dedicated-machine 日常練習 mode (PRACTICE_URL). It embeds the chairperson
# 叮叮 timer (all formats) — pure client-side, no login — and links out to the
# existing /ai-coach for AI practice (the kiosk browser stays logged in as the
# appliance's own committee account, so the token-signed Gemini Live relay keeps
# working). No Streamlit page, schema, or existing route is touched.
# ---------------------------------------------------------------------------


@app.get("/practice")
async def appliance_practice_page():
    return FileResponse(BASE_DIR / "templates" / "appliance_practice.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


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
    return FileResponse(BASE_DIR / "frontend" / "match_photos" / "index.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


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
    return FileResponse(BASE_DIR / "frontend" / "ai_coach" / "index.html", media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/ai-coach/room/{code}")
async def ai_coach_room_page(code: str, request: Request):
    _require_committee_user(request)
    room = ROOMS.get((code or "").upper())
    if not room or room.phase == "ended":
        return _practice_error_page("房間不存在", "房間已結束或不存在。", "/ai-coach")
    html = (BASE_DIR / "templates" / "room_debate.html").read_text(encoding="utf-8")
    html = html.replace("__ROOM_CODE__", json.dumps(room.code))
    html = html.replace("__ROOM_WS_BASE__", json.dumps(_get_proxy_secret("ROOM_WS_BASE", "") or ""))
    html = html.replace("__MODE__", json.dumps(room.mode))
    html = html.replace("__BELL_SRC__", json.dumps(_practice_bell_src()))
    return Response(content=html, media_type="text/html")


@app.get("/ai-training")
async def ai_training_page():
    html = (BASE_DIR / "frontend" / "ai_training" / "index.html").read_text(encoding="utf-8")
    html = html.replace("__APP_VERSION__", APP_VERSION)
    return Response(content=html, media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/ai-training/app.js")
async def ai_training_script():
    return FileResponse(
        BASE_DIR / "frontend" / "ai_training" / "app.js",
        media_type="text/javascript",
        headers=_cache_headers(CACHE_STATIC),
    )


@app.get("/db-mgmt")
@app.get("/db_mgmt", include_in_schema=False)
async def db_management_page():
    return FileResponse(BASE_DIR / "frontend" / "db_mgmt" / "index.html", media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/dev-settings")
@app.get("/dev_settings", include_in_schema=False)
async def developer_settings_page():
    return FileResponse(BASE_DIR / "frontend" / "dev_settings" / "index.html", media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/dev-settings/lateness-managers.js")
async def developer_lateness_managers_script():
    return FileResponse(BASE_DIR / "frontend" / "dev_settings" / "lateness-managers.js", media_type="application/javascript", headers=_cache_headers(CACHE_STATIC))


@app.get("/lateness-fund")
@app.get("/lateness_fund", include_in_schema=False)
async def lateness_fund_page():
    return FileResponse(BASE_DIR / "frontend" / "lateness_fund" / "index.html", media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/ai-fund")
@app.get("/ai_fund", include_in_schema=False)
async def ai_fund_page():
    return FileResponse(BASE_DIR / "frontend" / "ai_fund" / "index.html", media_type="text/html", headers=_cache_headers(CACHE_HTML))


@app.get("/api/practice/bell")
async def appliance_practice_bell():
    return FileResponse(BASE_DIR / "assets" / "bell.mp3", media_type="audio/mpeg",
                        headers=_binary_cache_headers(CACHE_STATIC))


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
# Appliance AI free-debate practice (login-free kiosk page, committee-gated mint)
#
# The kiosk 練習頁 links here. Reuses the SAME Gemini Live engine
# (templates/live_debate.html), prompt builder and relay as the committee AI
# coach (ai_coach.py) so behaviour stays consistent — the only differences are a
# big-text setup page and that the ephemeral-token mint happens here in the
# proxy (which has no Streamlit `st.secrets`) instead of in ai_coach. Minting is
# gated on the committee cookie (the kiosk logs in as its own committee account)
# and rate-limited so the appliance can't be abused into burning AI budget.
# ---------------------------------------------------------------------------

# Keep in sync with ai_coach_helpers.FREE_DEBATE_LIVE_MODEL.
FREE_DEBATE_LIVE_MODEL = "gemini-3.1-flash-live-preview"

# Only formats with a free-debate segment are offered for standalone Free De.
_PRACTICE_LIVE_FORMATS = list(FREE_DEBATE_FORMATS)

# In-process rate limit for token minting, keyed by committee user. Single-kiosk
# scale, so a plain dict that resets on restart is enough.
_practice_live_hits: dict = {}
_PRACTICE_LIVE_MAX_PER_HOUR = PRACTICE_LIVE_MAX_PER_HOUR
_PRACTICE_LIVE_MIN_GAP_SEC = PRACTICE_LIVE_MIN_GAP_SECONDS
_bandwidth_last_prune = 0.0
_bandwidth_prune_lock = threading.Lock()

SOLO_LIMIT_MESSAGE = (
    "由於系統每月可用的網絡傳輸量有限，為控制營運預算並確保所有委員均能使用服務，"
    "每位委員每日只可進行一次單人自由辯論，並且每星期只可進行一次單人完整模擬練習。"
    "你已使用此類別的練習限額，請於下一個限額週期再試。"
)
GLOBAL_LIVE_LIMIT_MESSAGE = (
    "由於本月全系統的網絡傳輸量預算有限，Gemini Live練習名額已用完。"
    "為確保一般系統功能維持正常，請於下月再試或聯絡系統管理員。"
)
BANDWIDTH_STOP_MESSAGE = (
    f"由於本月全系統網絡傳輸量已達{BANDWIDTH_STOP_LIVE_BYTES / 1_000_000_000:g}GB"
    "預算上限，系統已停止建立新的Gemini Live"
    "練習及聯機房間。一般功能、R2媒體及管理功能維持正常。"
)
BANDWIDTH_ESSENTIAL_MESSAGE = (
    f"由於本月全系統網絡傳輸量已達{BANDWIDTH_ESSENTIAL_ONLY_BYTES / 1_000_000_000:g}GB"
    "保護上限，本功能暫停使用。"
    "目前只保留一般HTML、JSON、R2媒體及管理功能。"
)


def _bandwidth_month_context():
    now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
    start_hk = now_hk.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        now_hk.strftime("%Y-%m"),
        start_hk.astimezone(datetime.timezone.utc).replace(tzinfo=None),
    )


def bandwidth_budget_status(*, notify: bool = False) -> dict:
    """Return tracked high-bandwidth egress plus an optional Render baseline."""
    engine = _get_db_engine()
    period, start_utc = _bandwidth_month_context()
    tracked = 0
    if engine is not None:
        with engine.begin() as conn:
            conn.execute(text(CREATE_BANDWIDTH_USAGE_LOGS))
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
            "將停止新的Gemini Live及聯機房間。",
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
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    source = str(source)[:80]
    user = str(user_id or "")[:200]
    details = str(details or "")[:500]
    aggregate_key = str(aggregate_key or "")[:400]
    with engine.begin() as conn:
        conn.execute(text(CREATE_BANDWIDTH_USAGE_LOGS))
        params = {
            "source": source, "user": user, "insert_user": user or None,
            "bytes": count, "details": details, "now": now,
            "period_start": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
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
    if monotonic_now - _bandwidth_last_prune >= MAINTENANCE_PRUNE_INTERVAL_SECONDS:
        with _bandwidth_prune_lock:
            if monotonic_now - _bandwidth_last_prune >= MAINTENANCE_PRUNE_INTERVAL_SECONDS:
                with engine.begin() as conn:
                    conn.execute(text(f"DELETE FROM {TABLE_BANDWIDTH_USAGE_LOGS} WHERE created_at<:cutoff"),
                                 {"cutoff": now - datetime.timedelta(days=BANDWIDTH_LOG_RETENTION_DAYS)})
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
    """Return UTC-naive user and month boundaries for HKT calendar quotas."""
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


def _solo_live_quota_error(user_id: str, mode: str) -> str | None:
    """Enforce persistent per-user and global quotas before minting Live tokens."""
    budget_error = _bandwidth_live_gate_error()
    if budget_error:
        return budget_error
    engine = _get_db_engine()
    if engine is None:
        return "Database is not configured"
    now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
    is_mock = mode == "mock"
    feature = "full_mock_live" if is_mock else "free_debate_live"
    # created_at is stored as UTC-naive. Convert Hong Kong calendar boundaries
    # to UTC before comparing so midnight/week/month edges cannot bypass quota.
    user_start, month_start = _solo_quota_boundaries(now_hk, is_mock)
    global_limit = SOLO_MOCK_MONTHLY_LIMIT if is_mock else SOLO_FREE_MONTHLY_LIMIT
    with engine.begin() as conn:
        conn.execute(text(CREATE_AI_FUND_USAGE_LOGS))
        user_count = int(conn.execute(text(f"""SELECT COUNT(*) FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE user_id=:user AND feature=:feature AND status='success' AND created_at>=:start"""),
            {"user": user_id, "feature": feature, "start": user_start}).scalar() or 0)
        global_count = int(conn.execute(text(f"""SELECT COUNT(*) FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE feature=:feature AND status='success' AND created_at>=:start"""),
            {"feature": feature, "start": month_start}).scalar() or 0)
    if user_count >= 1:
        return SOLO_LIMIT_MESSAGE
    if global_count >= global_limit:
        return GLOBAL_LIVE_LIMIT_MESSAGE
    return None


def _reserve_solo_live_slot(claim: dict) -> str | None:
    """Consume a solo quota only after the upstream WebSocket has connected.

    All Mock chapter tokens share ``practice_id`` and therefore consume one
    quota row.  An issued token that is never used creates no database record.
    """
    budget_error = _bandwidth_live_gate_error()
    if budget_error:
        return budget_error
    engine = _get_db_engine()
    if engine is None:
        return "Database is not configured"
    user_id = str(claim.get("user_id") or "")
    practice_kind = str(claim.get("practice_kind") or "")
    practice_id = str(claim.get("practice_id") or "")
    is_mock = practice_kind == "solo_mock"
    feature = "full_mock_live" if is_mock else "free_debate_live"
    global_limit = SOLO_MOCK_MONTHLY_LIMIT if is_mock else SOLO_FREE_MONTHLY_LIMIT
    now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
    user_start, month_start = _solo_quota_boundaries(now_hk, is_mock)
    now_utc = now_hk.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    marker = f"relay_session:{practice_id}"[:500]
    duration_minutes = max(0.5, int(claim.get("max_seconds") or 30) / 60)
    with engine.begin() as conn:
        conn.execute(text(CREATE_AI_FUND_USAGE_LOGS))
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('solo_live_quota'))"))
        already = conn.execute(text(f"""SELECT 1 FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE user_id=:user AND feature=:feature AND status='success'
              AND error_message=:marker LIMIT 1"""), {
            "user": user_id, "feature": feature, "marker": marker,
        }).fetchone()
        if already:
            return None
        user_count = int(conn.execute(text(f"""SELECT COUNT(*) FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE user_id=:user AND feature=:feature AND status='success'
              AND created_at>=:start"""), {
            "user": user_id, "feature": feature, "start": user_start,
        }).scalar() or 0)
        if user_count >= 1:
            return SOLO_LIMIT_MESSAGE
        global_count = int(conn.execute(text(f"""SELECT COUNT(*) FROM {TABLE_AI_FUND_USAGE_LOGS}
            WHERE feature=:feature AND status='success' AND created_at>=:start"""), {
            "feature": feature, "start": month_start,
        }).scalar() or 0)
        if global_count >= global_limit:
            return GLOBAL_LIVE_LIMIT_MESSAGE
        conn.execute(text(f"""INSERT INTO {TABLE_AI_FUND_USAGE_LOGS}
            (user_id,feature,model_label,provider,estimated_cost_usd,
             estimated_cost_hkd,input_tokens,output_tokens,audio_tokens,
             search_calls,cost_source,status,error_message,created_at)
            VALUES(:user,:feature,'Gemini Live','gemini',:usd,:hkd,0,0,
                   :audio,0,'relay','success',:marker,:now)"""), {
            "user": user_id, "feature": feature,
            "usd": round(duration_minutes * 0.01, 4),
            "hkd": round(duration_minutes * 0.078, 4),
            "audio": int(duration_minutes * 60 * 25),
            "marker": marker, "now": now_utc,
        })
    return None


def _practice_bell_src() -> str:
    # Do not inline the 38KB MP3 into every generated live/room page.  The
    # versioned, edge-cacheable endpoint transfers it once per browser instead.
    return "/api/practice/bell" if (BASE_DIR / "assets" / "bell.mp3").exists() else ""


def _mint_gemini_live_token(duration_minutes: float, start_delay_minutes: float = 0):
    """Create a single-use Gemini Live ephemeral token. Reimplements
    ai_coach_helpers.create_gemini_live_ephemeral_token without the Streamlit
    dependency. Returns (token_name, None) or (None, error_message)."""
    api_key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not api_key:
        return None, "未設定 GEMINI_API_KEY，未能開始練習。"
    try:
        from google import genai  # deferred: heavy import, cloud-only dependency
    except Exception:
        return None, "伺服器未安裝 Gemini SDK。"
    token_minutes = max(3, math.ceil(float(duration_minutes)))
    start_delay = max(0, float(start_delay_minutes or 0))
    now = datetime.datetime.now(datetime.timezone.utc)
    # Later Mock tokens are minted together with section 1.  Keep each token
    # valid until its planned hand-off; otherwise Google rejects the new Live
    # session before that chapter begins.  Five minutes is a pause/transition
    # allowance on top of the planned elapsed time.
    new_session_expire = now + datetime.timedelta(minutes=start_delay + 5)
    expire = now + datetime.timedelta(minutes=start_delay + token_minutes + 5)
    try:
        client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})
        token = client.auth_tokens.create(config={
            "uses": 1,
            "expire_time": expire,
            "new_session_expire_time": new_session_expire,
            "http_options": {"api_version": "v1alpha"},
        })
    except Exception as e:
        logger.warning("Gemini Live token mint failed: %s", e)
        return None, "Gemini 未能建立練習連線，請稍後再試。"
    token_name = getattr(token, "name", None)
    if not token_name:
        return None, "Gemini 未回傳 token。"
    return token_name, None


def _render_live_debate_html(
    token, prompt, live_minutes, bell_schedule, ai_starts, *, segments=None,
    tokens=None, session_labels=None, session_label="自由辯論",
    relay_user_id="", relay_practice_kind="solo_free", relay_practice_id="",
    relay_max_seconds_by_token=None,
):
    """Server-render templates/live_debate.html the same way ai_coach does, so the
    kiosk gets the identical Live engine for Free De and multi-session Mock."""
    html = (BASE_DIR / "templates" / "live_debate.html").read_text(encoding="utf-8")
    relay_ws_base = _get_proxy_secret("LIVE_RELAY_WS_BASE", "") or ""
    token_sigs = {}
    if relay_ws_base:
        # Mock reconnects with a fresh single-use token at every session boundary.
        # Sign every token up front; signing only the first makes section 2 fail
        # authentication whenever the Singapore relay is enabled.
        seconds_by_token = relay_max_seconds_by_token or {}
        default_seconds = max(30, min(int(float(live_minutes or 2.5) * 60), 30 * 60))
        for live_token in dict.fromkeys([token, *(tokens or [])]):
            sig = _sign_relay_token(
                live_token, relay_user_id, relay_practice_kind,
                int(seconds_by_token.get(live_token) or default_seconds),
                relay_practice_id,
            )
            if sig:
                token_sigs[live_token] = sig
    replacements = {
        "__RELAY_WS_BASE__": json.dumps(relay_ws_base),
        "__TOKEN_SIGS__": json.dumps(token_sigs),
        "__LIVE_TOKEN__": json.dumps(token),
        "__LIVE_MODEL__": json.dumps(FREE_DEBATE_LIVE_MODEL),
        "__LIVE_PROMPT__": json.dumps(prompt, ensure_ascii=False),
        "__LIVE_MINUTES__": json.dumps(float(live_minutes or 2.5)),
        "__BELL_SRC__": json.dumps(_practice_bell_src()),
        "__BELL_SCHEDULE__": json.dumps(bell_schedule or [], ensure_ascii=False),
        "__MOCK_SEGMENTS__": json.dumps(segments or [], ensure_ascii=False),
        "__MOCK_TOKENS__": json.dumps(tokens or [], ensure_ascii=False),
        "__MOCK_SESSION_LABELS__": json.dumps(session_labels or [], ensure_ascii=False),
        "__AI_STARTS__": json.dumps(bool(ai_starts)),
        "__LIVE_PROMPTS__": json.dumps(LIVE_RUNTIME_PROMPTS, ensure_ascii=False),
    }
    for key, value in replacements.items():
        html = html.replace(key, value)
    if session_label != "自由辯論":
        html = html.replace("自由辯論", session_label)
    return html


def _practice_error_page(title: str, message: str, back: str = "/practice/ai-debate") -> Response:
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
<a href="{xml_escape(back)}">◀ 返回</a></div></body></html>"""
    return Response(content=body, media_type="text/html", status_code=200)


@app.get("/practice/ai-debate")
async def appliance_ai_debate_page():
    return FileResponse(BASE_DIR / "templates" / "appliance_ai_debate.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/practice/ai-debate/live")
async def appliance_ai_debate_live(request: Request):
    # Top-level same-origin navigation, so the committee cookie is sent. Check it
    # directly (rather than _require_committee_user which raises a JSON 401) so we
    # can render a friendly big-text page on the kiosk instead.
    user_id = _verify_committee_cookie(request)
    if not user_id:
        return _practice_error_page(
            "需要委員登入",
            "AI 辯論練習需要委員帳戶。請喺部機用委員帳戶登入一次（cookie 會記住），再返嚟開始。",
        )

    rate_error = _practice_live_rate_check(user_id)
    if rate_error:
        return _practice_error_page("請稍等", rate_error)

    q = request.query_params
    topic = (q.get("topic") or "").strip()
    side = (q.get("side") or "正方").strip()
    debate_format = (q.get("format") or _PRACTICE_LIVE_FORMATS[0]).strip()
    mode = (q.get("mode") or "free").strip()
    if mode not in ("free", "mock"):
        mode = "free"
    quota_error = _solo_live_quota_error(user_id, mode)
    if quota_error:
        return _practice_error_page("練習限額已用完", quota_error)
    from api.ai_coach_api import consume_live_brief
    research_brief=consume_live_brief(q.get("brief_id"),user_id)
    if not topic:
        return _practice_error_page("未有辯題", "請先輸入辯題再開始。")
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
        live_minutes = min(10.0, max(0.5, live_minutes))
    else:
        live_minutes = 2.5

    if mode == "mock":
        segments = get_full_mock_sequence(debate_format, free_debate_minutes=live_minutes if debate_format == "聯中" else None)
        sessions = split_mock_into_sessions(segments)
        total_minutes = full_mock_total_seconds(segments) / 60
        tokens=[]
        relay_seconds = {}
        planned_elapsed_minutes = 0.0
        for session in sessions:
            session_minutes = full_mock_total_seconds(session["segments"]) / 60
            token,error=_mint_gemini_live_token(
                max(3, session_minutes + 2), start_delay_minutes=planned_elapsed_minutes
            )
            if error:return _practice_error_page("未能開始",error)
            tokens.append(token)
            relay_seconds[token] = max(60, min(30 * 60, int(session["planned_seconds"]) + 120))
            planned_elapsed_minutes += session_minutes
        flat=[]
        for index,session in enumerate(sessions):
            flat.extend({**segment,"session":index} for segment in session["segments"])
        prompt=build_full_mock_live_prompt(topic,side,debate_format,free_debate_minutes=live_minutes if debate_format=="聯中" else None,research_brief=research_brief)
        html=_render_live_debate_html(
            tokens[0],prompt,total_minutes,[],False,segments=flat,tokens=tokens,
            session_labels=[s["label"] for s in sessions],session_label="Mock",
            relay_user_id=user_id,relay_practice_kind="solo_mock",
            relay_practice_id=secrets.token_urlsafe(12),
            relay_max_seconds_by_token=relay_seconds,
        )
        return Response(content=html,media_type="text/html")

    bell_schedule = get_debate_timer_config(
        debate_format, free_debate_minutes=live_minutes,
    )["bell_schedules"].get("free", [])
    token_minutes = max(3, math.ceil(live_minutes * 2 + 2))

    token, mint_error = _mint_gemini_live_token(token_minutes)
    if mint_error:
        return _practice_error_page("未能開始", mint_error)

    prompt = build_free_debate_live_prompt(topic, side, research_brief)
    max_seconds = max(60, min(10 * 60, int(math.ceil(live_minutes * 2 * 60))))
    html = _render_live_debate_html(
        token, prompt, live_minutes, bell_schedule, side == "反方",
        relay_user_id=user_id,relay_practice_kind="solo_free",
        relay_practice_id=secrets.token_urlsafe(12),
        relay_max_seconds_by_token={token: max_seconds},
    )
    return Response(content=html, media_type="text/html")


GEMINI_LIVE_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained"
)


@app.websocket("/gemini-live")
async def gemini_live_relay(websocket: WebSocket):
    # 香港（及其他受限地區）用戶嘅瀏覽器無法直連 Google Gemini Live WS，會被
    # geo-block。呢個 relay 喺 Render(Singapore) 代連 Google，令 Google 睇到嘅
    # 係受支援地區嘅 IP。瀏覽器只需連呢度，token 照舊用 ?access_token= query 傳，
    # 唔使 subprotocol（同瀏覽器直連 Google 時一致）。
    token = websocket.query_params.get("access_token", "")
    sig = websocket.query_params.get("sig", "")
    # 授權：只服務由本 app（用 cookie_secret 簽發）嘅 token，防止外人白嫖 relay
    # 消耗連線／頻寬。喺 accept 之前 reject，唔會完成 WS handshake、亦唔會撥去
    # Google。
    relay_claim = _verify_relay_signature(token, sig)
    if not relay_claim:
        await websocket.close(code=1008)
        return
    if _bandwidth_live_gate_error():
        await websocket.close(code=1008, reason="本月網絡傳輸量已達Live保護上限")
        return

    backend_url = f"{GEMINI_LIVE_WS_URL}?access_token={quote_plus(token)}"

    try:
        # Gemini 音訊 frame 可以大過 websockets 預設 1MB；提升到受控的4MB，
        # 避免用 max_size=None 令異常 frame 無上限佔用 Starter RAM。
        # ping_interval=None：避免 keepalive pong timeout 喺長時間等待時誤斷線。
        backend = await websockets.connect(
            backend_url, max_size=GEMINI_WS_MAX_SIZE, ping_interval=None
        )
    except Exception as e:
        logger.exception("Gemini Live relay backend connect failed: %s", e)
        await websocket.close(code=1011)
        return

    # Both browser and upstream handshakes must succeed before quota is spent.
    try:
        await websocket.accept()
    except Exception:
        await backend.close(code=1001, reason="browser disconnected before accept")
        return
    try:
        quota_error = await asyncio.to_thread(_reserve_solo_live_slot, relay_claim)
    except Exception as exc:
        logger.exception("Gemini relay quota reservation failed: %s", exc)
        await backend.close(code=1011, reason="quota service unavailable")
        await websocket.close(code=1011, reason="練習限額服務暫時不可用")
        return
    if quota_error:
        await backend.close(code=1008, reason="practice quota unavailable")
        await websocket.close(code=1008, reason="練習限額已用完")
        return

    # The quota is consumed only after both the browser and upstream Gemini
    # WebSocket connections have succeeded.

    async with backend:
        relayed_bytes = 0
        bandwidth_flushed = 0
        deadline_reached = False

        def within_relay_budget(message) -> bool:
            nonlocal relayed_bytes
            relayed_bytes += len(message) if isinstance(message, bytes) else len(str(message).encode("utf-8"))
            return relayed_bytes <= GEMINI_RELAY_MAX_BYTES

        async def client_to_backend():
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    await backend.close()
                    break
                elif "text" in message and message["text"] is not None:
                    if not within_relay_budget(message["text"]):
                        await backend.close(code=1008, reason="monthly bandwidth protection")
                        break
                    await backend.send(message["text"])
                elif "bytes" in message and message["bytes"] is not None:
                    if not within_relay_budget(message["bytes"]):
                        await backend.close(code=1008, reason="monthly bandwidth protection")
                        break
                    await backend.send(message["bytes"])

        async def backend_to_client():
            async for message in backend:
                if not within_relay_budget(message):
                    await websocket.close(code=1008, reason="練習已達網絡傳輸量上限")
                    break
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)

        async def enforce_deadline():
            nonlocal deadline_reached
            await asyncio.sleep(int(relay_claim["max_seconds"]))
            deadline_reached = True
            await backend.close(code=1008, reason="server practice time limit")
            try:
                await websocket.close(code=1008, reason="練習已達伺服器時間上限")
            except RuntimeError:
                pass

        async def checkpoint_bandwidth():
            nonlocal bandwidth_flushed
            while True:
                await asyncio.sleep(BANDWIDTH_CHECKPOINT_SECONDS)
                snapshot = relayed_bytes
                delta = max(0, snapshot - bandwidth_flushed)
                if delta and await asyncio.to_thread(
                    record_bandwidth_usage, "solo_gemini_relay", delta,
                    str(relay_claim["user_id"]),
                    aggregate_key=(f"practice={relay_claim['practice_kind']};"
                                   f"id={relay_claim['practice_id']}"),
                ):
                    bandwidth_flushed = snapshot
                if await asyncio.to_thread(_bandwidth_live_gate_error):
                    await backend.close(code=1008, reason="monthly bandwidth protection")
                    try:
                        await websocket.close(code=1008, reason="本月Live網絡傳輸量已達保護上限")
                    except RuntimeError:
                        pass
                    return

        checkpoint_task = asyncio.create_task(checkpoint_bandwidth())
        tasks = [
            asyncio.create_task(client_to_backend()),
            asyncio.create_task(backend_to_client()),
            asyncio.create_task(enforce_deadline()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.exception("Gemini Live relay failed: %s", e)
        finally:
            checkpoint_task.cancel()
            try:
                await checkpoint_task
            except asyncio.CancelledError:
                pass

    final_delta = max(0, relayed_bytes - bandwidth_flushed)
    if final_delta:
        await asyncio.to_thread(
            record_bandwidth_usage, "solo_gemini_relay", final_delta,
            str(relay_claim["user_id"]),
            aggregate_key=(f"practice={relay_claim['practice_kind']};"
                           f"id={relay_claim['practice_id']}"),
        )

    # 把 Google 嘅 close code / reason 傳返畀瀏覽器，令前端 formatCloseMessage
    # 對 token 過期(1008)等情況嘅提示照樣有效。1005/1006 唔可以明文送出，改用
    # 合法碼；reason 限 123 bytes。
    code = backend.close_code or 1000
    if code in (1005, 1006):
        code = 1011 if code == 1006 else 1000
    reason = (backend.close_reason or "").encode("utf-8")[:123].decode("utf-8", "ignore")
    try:
        await websocket.close(code=code, reason=reason)
    except TypeError:
        try:
            await websocket.close(code=code)
        except RuntimeError:
            pass
    except RuntimeError:
        pass


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
ROOM_JUDGEMENT_MODELS = model_slugs_for_labels(ROOM_JUDGEMENT_MODEL_LABELS)

ROOMS = {}  # code -> Room

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
        conn.execute(text(CREATE_PRACTICE_DAILY_USAGE))
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
    usage_date = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    with engine.begin() as conn:
        conn.execute(text(f"""DELETE FROM {TABLE_PRACTICE_DAILY_USAGE}
            WHERE user_id=:user AND practice_kind=:kind AND usage_date=:day AND room_code=:room"""), {
            "user": user_id, "kind": _practice_kind(structure),
            "day": usage_date, "room": room_code,
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
        conn.execute(text(CREATE_PRACTICE_DAILY_USAGE))
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
        conn.execute(text(CREATE_PRACTICE_DAILY_USAGE))
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
        self.role = None          # "正方"/"反方" (A) or claimed side (B)
        self.position = None      # mode B mock: positions 1-4 assigned before start
        self.name = user_id
        self.connected = True
        self.joined_at = _now_ms()


class Room:
    def __init__(self, code, mode, created_by, debate_format, topic,
                 structure, free_minutes, capacity):
        self.code = code
        self.mode = mode                 # "A" | "B"
        self.created_by = created_by
        self.created_at = _now_ms()
        self.started_ms = None
        self.hard_deadline_ms = None
        self.phase = "lobby"             # lobby | active | ended
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
        self.judgement = ""
        self.empty_since = None
        self.creator_side = None         # mode A: side the host picked at create
        # mode B / judge (Gemini leg wired in phase 2)
        self.human_side = None           # 正方/反方 the humans take in mode B
        self.gemini = None               # {tokens, sigs, prompt, model, session_labels}
        self.gemini_session_index = 0
        self.gemini_ws = None
        self.gemini_task = None
        self.tick_task = None
        self.judgement_task = None
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
        # Mode B: humans all share one side and the AI plays the other — there is
        # no 正方-first-among-humans constraint, so any team member may open.
        if self.mode == "B":
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
        _record_room_bandwidth_once(room)
        ROOMS.pop(code, None)
        try:
            asyncio.get_running_loop().create_task(_room_end(room, reason))
        except RuntimeError:
            # Defensive fallback for a future synchronous maintenance caller.
            for task in (room.tick_task, room.gemini_task, room.judgement_task):
                if task is not None and not task.done():
                    task.cancel()

    now = _now_ms()
    for code in list(ROOMS.keys()):
        room = ROOMS.get(code)
        if room is None:
            continue
        if now - room.created_at > ROOM_MAX_AGE_MS:
            dispose(code, room, "ttl")
            continue
        if any(m.connected for m in room.members.values()):
            room.empty_since = None
        else:
            if room.empty_since is None:
                room.empty_since = now
            elif now - room.empty_since > ROOM_EMPTY_GRACE_MS:
                dispose(code, room, "empty")


def _active_room_count():
    return len([r for r in ROOMS.values() if r.phase != "ended"])


async def _room_broadcast(room, msg, exclude=None):
    text = json.dumps(msg, ensure_ascii=False)
    recipients = [m for m in list(room.members.values())
                  if m.connected and not (exclude and m.user_id == exclude)]

    async def send(member):
        try:
            # A stalled mobile client must not block audio fan-out to everyone
            # else.  The next reconnect rehydrates room state and transcript.
            await asyncio.wait_for(
                member.ws.send_text(text), timeout=ROOM_WS_SEND_TIMEOUT_SECONDS,
            )
            room.bandwidth_bytes += len(text.encode("utf-8"))
        except Exception:
            member.connected = False

    if recipients:
        await asyncio.gather(*(send(member) for member in recipients))


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
            if (seg and room.seg_started_ms and seg_seconds > 0
                    and now - room.seg_started_ms >= seg_seconds * (2 if seg.get("side") == "雙方" else 1) * 1000):
                if room.seg_index >= len(room.segments) - 1:
                    await _room_end(room, "server_segment_limit")
                    break
                await _room_advance_segment(room, room.seg_index + 1)
            if now - room.last_bandwidth_checkpoint_ms >= BANDWIDTH_CHECKPOINT_SECONDS * 1000:
                await asyncio.to_thread(_checkpoint_room_bandwidth, room, False)
                room.last_bandwidth_checkpoint_ms = now
                if await asyncio.to_thread(_bandwidth_live_gate_error):
                    await _room_end(room, "monthly_bandwidth_limit")
                    break
            await _room_broadcast(room, room.state_msg())
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("room tick failed (%s): %s", room.code, e)


def _room_ensure_tick(room):
    if room.tick_task is None or room.tick_task.done():
        room.tick_task = asyncio.create_task(_room_tick(room))


def _room_precheck_msg(room, msg_type="precheck_status"):
    users = room.connected_user_ids()
    return {
        "type": msg_type,
        "check_id": room.precheck_id,
        "members": users,
        "results": {u: room.precheck_results.get(u) for u in users},
    }


def _room_mint_gemini_tokens(room) -> str | None:
    if room.mode != "B" or room.gemini is None:
        return None
    durations = list(room.gemini.get("session_minutes") or [])
    if not durations:
        durations = [min(12.0, full_mock_total_seconds(room.segments) / 60 + 2)]
    tokens = []
    planned_elapsed = 0.0
    for duration in durations:
        token, error = _mint_gemini_live_token(
            max(3, float(duration)), start_delay_minutes=planned_elapsed,
        )
        if error:
            return error
        tokens.append(token)
        planned_elapsed += max(0.0, float(duration) - 2)
    room.gemini["tokens"] = tokens
    return None


async def _room_start_active(room) -> str | None:
    users = room.connected_user_ids()
    try:
        reserved = await asyncio.to_thread(
            _reserve_room_practice_slots, users, room.structure, room.code,
        )
    except Exception as exc:
        logger.exception("room quota reservation failed (%s): %s", room.code, exc)
        return "練習限額暫時未能確認，未有扣除限額，請稍後再試。"
    if not reserved:
        return PRACTICE_DAILY_LIMIT_MESSAGE
    room.quota_users = users
    try:
        mint_error = await asyncio.to_thread(_room_mint_gemini_tokens, room)
    except Exception as exc:
        logger.exception("room token mint failed (%s): %s", room.code, exc)
        mint_error = "AI 對手暫時未能建立，未有扣除練習限額。"
    if mint_error:
        await asyncio.to_thread(
            _release_room_practice_slots, users, room.structure, room.code,
        )
        room.quota_users = []
        return mint_error
    room.phase = "active"
    room.seg_index = 0
    room.started_ms = _now_ms()
    room.seg_started_ms = room.started_ms
    total_seconds = full_mock_total_seconds(room.segments)
    if room.structure == "free":
        total_seconds = min(10 * 60, total_seconds)
    room.hard_deadline_ms = room.started_ms + max(30, int(total_seconds)) * 1000
    room.side_elapsed_ms = {"正方": 0, "反方": 0}
    room.active_turn_user = None
    room.active_turn_side = None
    room.active_turn_started_ms = None
    room.free_first_done = False
    room.precheck_id = None
    room.precheck_results = {}
    gemini_ready = await _room_start_gemini_if_needed(room)
    if room.mode == "B" and not gemini_ready:
        room.phase = "lobby"
        room.started_ms = None
        room.hard_deadline_ms = None
        room.gemini["tokens"] = []
        await asyncio.to_thread(
            _release_room_practice_slots, users, room.structure, room.code,
        )
        room.quota_users = []
        return "AI 對手連線失敗，未有扣除練習限額；請重新測試後再開始。"
    _room_ensure_tick(room)
    await _room_broadcast(room, room.state_msg())
    return None


async def _room_begin_precheck(room):
    start_error = _room_start_blocker(room)
    if start_error:
        await _room_broadcast(room, {"type": "error", "message": start_error})
        return
    room.precheck_id = secrets.token_hex(6)
    room.precheck_results = {}
    await _room_broadcast(room, _room_precheck_msg(room, "precheck_request"))
    await _room_broadcast(room, _room_precheck_msg(room))


async def _room_handle_precheck_result(room, member, msg):
    if room.phase != "lobby" or not room.precheck_id:
        return
    if msg.get("check_id") != room.precheck_id:
        return
    room.precheck_results[member.user_id] = {
        "ok": bool(msg.get("ok")),
        "message": str(msg.get("message") or "")[:800],
    }
    await _room_broadcast(room, _room_precheck_msg(room))

    users = room.connected_user_ids()
    if not users or any(u not in room.precheck_results for u in users):
        return
    if all(room.precheck_results[u].get("ok") for u in users):
        start_error = _room_start_blocker(room)
        if start_error:
            await _room_broadcast(room, {"type": "error", "message": start_error})
            await _room_broadcast(room, _room_precheck_msg(room, "precheck_failed"))
            return
        activation_error = await _room_start_active(room)
        if activation_error:
            await _room_broadcast(room, {"type": "error", "message": activation_error})
            await _room_broadcast(room, _room_precheck_msg(room, "precheck_failed"))
    else:
        await _room_broadcast(room, _room_precheck_msg(room, "precheck_failed"))


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


# --- Gemini leg (mode B): server-owned Gemini Live sessions per room ---------
#
# The server (not any browser) owns the single upstream Gemini Live socket, so
# the ephemeral token never leaves the server, the whole team shares one AI
# context, and a member dropping does not kill the AI. Same connect/pump shape
# as gemini_live_relay above; here the proxy is the client. Render's Singapore
# egress means no geo-block, so no relay hop is needed for this leg.

async def _room_start_gemini_if_needed(room):
    if room.mode != "B" or room.gemini is None:
        return True
    if room.gemini_ws is not None:
        return True
    tokens = room.gemini.get("tokens") or []
    session_index = min(room.gemini_session_index, max(0, len(tokens) - 1))
    token = tokens[session_index] if tokens else ""
    model = room.gemini.get("model") or ""
    prompt = room.gemini.get("prompt") or ""
    if not token or not model:
        await _room_broadcast(room, {"type": "error",
                                     "message": "未有 AI 連線資料，AI 對手未能啟動。"})
        return False

    backend_url = f"{GEMINI_LIVE_WS_URL}?access_token={quote_plus(token)}"
    try:
        gws = await websockets.connect(
            backend_url, max_size=GEMINI_WS_MAX_SIZE, ping_interval=None
        )
    except Exception as e:
        logger.exception("room Gemini connect failed (%s): %s", room.code, e)
        await _room_broadcast(room, {"type": "error",
                                     "message": "AI 對手連線失敗，請重新建立房間。"})
        return False

    room.gemini_ws = gws
    # Decide once per session whether to re-voice the AI via server-side TTS.
    # Off by default when the provider is unconfigured or ROOM_TTS_ENABLED=0, so
    # the room degrades to Gemini native "Kore" audio (existing behaviour).
    room.tts_enabled = (
        tts_provider_configured()
        and _get_proxy_secret("ROOM_TTS_ENABLED", "1").strip() != "0"
    )
    if session_index and room.transcript:
        recent = room.transcript[-24:]
        continuity = "\n".join(
            f"{item.get('side') or item.get('speaker')}：{item.get('text')}"
            for item in recent if item.get("text")
        )
        if continuity:
            prompt += "\n\n## 上一節接力內容\n以下係之前環節逐字稿，請延續同一場辯論：\n" + continuity
    setup = {
        "setup": {
            "model": "models/" + model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}
                },
            },
            "systemInstruction": {"parts": [{"text": prompt}]},
            "realtimeInputConfig": {"automaticActivityDetection": {"disabled": True}},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
        }
    }
    try:
        await _room_gemini_send(room, gws, setup)
    except Exception as e:
        logger.exception("room Gemini setup failed (%s): %s", room.code, e)
        room.gemini_ws = None
        await gws.close()
        return False
    room.gemini_task = asyncio.create_task(_room_gemini_pump(room, gws))
    return True


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
    return {"pending": "", "native": [], "native_bytes": 0, "fallback": False}


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
        audio_bytes, mime = await _synthesize_tts(chunk)
    except Exception as e:
        logger.info("room TTS synth failed (%s), using native audio: %s", room.code, e)
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
        await _room_broadcast(room, {"type": "serverContent", "serverContent": _strip_audio_parts(sc)})
        ot = sc.get("outputTranscription") or {}
        if ot.get("text"):
            state["pending"] = (state["pending"] + str(ot["text"]))[-ROOM_PENDING_TRANSCRIPT_MAX_CHARS:]
        chunks, state["pending"] = _tts_take_sentences(state["pending"], force=final)
        for chunk in chunks:
            if not await _room_tts_synth(room, chunk, state):
                break


async def _room_gemini_pump(room, gws):
    """Read the room's Gemini Live socket and fan its serverContent out to every
    member. When room.tts_enabled, re-voice the AI via server-side TTS (synced,
    shared, with native-audio fallback); otherwise forward native audio verbatim.
    Also accumulate the AI's transcript into room.transcript so 評判 covers the AI."""
    ai_side = "反方" if room.human_side == "正方" else "正方"
    ai_buffer = {"text": ""}
    tts_state = _tts_new_turn_state()
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
            # NB: Gemini sends setupComplete as an empty object {}, which is
            # falsy in Python — must test membership, not truthiness.
            if "setupComplete" in msg:
                await _room_broadcast(room, {"type": "ai_ready"})
                # Mock: if the opening segment belongs to the AI, cue it now that
                # the session is ready (connect happened before this).
                if room.structure == "mock":
                    seg0 = room.current_segment()
                    if seg0 and seg0.get("side") == ai_side:
                        await _room_cue_ai_segment(room, seg0)
                continue
            sc = msg.get("serverContent")
            if sc is None:
                continue
            turn_complete = bool(sc.get("turnComplete"))
            if room.tts_enabled:
                await _room_pump_tts(room, sc, tts_state, final=turn_complete)
            else:
                await _room_broadcast(room, {"type": "serverContent", "serverContent": sc})
            ot = sc.get("outputTranscription") or {}
            if ot.get("text"):
                ai_buffer["text"] = (ai_buffer["text"] + str(ot["text"]))[-ROOM_PENDING_TRANSCRIPT_MAX_CHARS:]
            if turn_complete:
                if room.tts_enabled:
                    tts_state = _tts_new_turn_state()
                text_value = ai_buffer["text"].strip()
                ai_buffer["text"] = ""
                if text_value:
                    item = {
                        "speaker": "AI", "side": ai_side, "seg": room.seg_index,
                        "label": (room.current_segment() or {}).get("label", ""),
                        "text": text_value[:ROOM_TRANSCRIPT_ITEM_MAX_CHARS], "created_ms": _now_ms(),
                    }
                    room.transcript.append(item)
                    room.transcript = room.transcript[-ROOM_TRANSCRIPT_MAX_ITEMS:]
                    await _room_broadcast(room, {"type": "transcript", "item": item})
                await _room_broadcast(room, {"type": "speaking", "user_id": "AI",
                                             "speaking": False})
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.info("room Gemini pump ended (%s): %s", room.code, e)
    finally:
        if room.gemini_ws is gws:
            room.gemini_ws = None
        try:
            await gws.close()
        except Exception:
            pass


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
    task = room.gemini_task
    room.gemini_task = None
    ws = room.gemini_ws
    room.gemini_ws = None
    if task is not None and not task.done():
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
        return
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
    except Exception:
        pass


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
        await _room_start_gemini_if_needed(room)
        return
    ai_side = "反方" if room.human_side == "正方" else "正方"
    if seg.get("side") == ai_side:
        await _room_cue_ai_segment(room, seg)


async def _room_advance_segment(room, index: int):
    """Move the authoritative server timer without trusting client clocks."""
    now = _now_ms()
    if room.active_turn_side in room.side_elapsed_ms and room.active_turn_started_ms is not None:
        room.side_elapsed_ms[room.active_turn_side] += max(0, now - room.active_turn_started_ms)
    room.seg_index = max(0, min(int(index), len(room.segments) - 1))
    room.seg_started_ms = now
    room.active_turn_user = None
    room.active_turn_side = None
    room.active_turn_started_ms = None
    room.free_first_done = False
    if room.current_segment() and room.current_segment().get("side") == "雙方":
        room.side_elapsed_ms = {"正方": 0, "反方": 0}
    await _room_broadcast(room, room.state_msg())
    await _room_on_segment_enter(room)


async def _room_end(room, reason: str = "host"):
    if room.phase == "ended":
        return
    room.phase = "ended"
    current = asyncio.current_task()
    if room.tick_task is not None and room.tick_task is not current and not room.tick_task.done():
        room.tick_task.cancel()
    if room.judgement_task is not None and room.judgement_task is not current and not room.judgement_task.done():
        room.judgement_task.cancel()
    await _room_close_gemini(room)
    await _room_broadcast(room, {"type": "ended", "reason": reason})
    await asyncio.to_thread(_record_room_bandwidth_once, room)
    sockets = [member.ws for member in room.members.values() if member.connected]
    if sockets:
        await asyncio.gather(*(
            socket.close(code=1000, reason="practice ended") for socket in sockets
        ), return_exceptions=True)


# --- message handling ------------------------------------------------------

async def _room_handle_audio(room, member, msg):
    if room.phase != "active":
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
    if room.active_turn_user and room.active_turn_user != member.user_id:
        return  # only the accepted active turn may send audio
    if room.is_open_free_segment() and member.role in room.side_elapsed_ms:
        used = room.side_elapsed_ms.get(member.role, 0)
        if room.active_turn_side == member.role and room.active_turn_started_ms is not None:
            used += max(0, _now_ms() - room.active_turn_started_ms)
        if used >= int((seg.get("seconds") or 0) * 1000):
            return
    data, mime = _audio_fields(msg)
    if not data:
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
    if room.phase != "active":
        return
    now = _now_ms()
    if speaking:
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
            room.side_elapsed_ms[room.active_turn_side] += max(0, now - room.active_turn_started_ms)
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
    await _room_broadcast(room, {"type": "transcript", "item": item})


async def _room_request_judgement(room):
    budget_error = _bandwidth_essential_gate_error()
    if budget_error:
        room.judgement = budget_error
        await _room_broadcast(room, {"type": "judgement", "text": budget_error})
        return
    if not room.transcript:
        result = "暫時未有逐字稿，AI 評判未能判定哪一方勝出。請先完成發言，或使用支援語音轉文字的瀏覽器。"
        room.judgement = result
        await _room_broadcast(room, {"type": "judgement", "text": result})
        return

    api_key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not api_key:
        result = "未設定 GEMINI_API_KEY，暫時無法使用 AI 評判。"
        room.judgement = result
        await _room_broadcast(room, {"type": "judgement", "text": result})
        return

    await _room_broadcast(room, {"type": "judgement_pending"})
    prompt_text = build_room_judgement_prompt(
        room.topic,
        room.debate_format,
        room.structure,
        room.transcript,
    )
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt_text}],
        }],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200},
    }
    last_error = ""
    try:
        async with httpx.AsyncClient(timeout=ROOM_JUDGEMENT_TIMEOUT_SECONDS) as client:
            for model in ROOM_JUDGEMENT_MODELS:
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent"
                )
                resp = await client.post(
                    url,
                    params={"key": api_key},
                    json=payload,
                )
                if resp.status_code != 200:
                    try:
                        err_data = resp.json()
                        err_msg = err_data.get("error", {}).get("message") or resp.text
                    except Exception:
                        err_msg = resp.text
                    last_error = f"{model} HTTP {resp.status_code}: {str(err_msg)[:220]}"
                    logger.warning("Room judgement Gemini failed %s", last_error)
                    continue
                data = resp.json()
                parts = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [])
                )
                result = "\n".join(str(p.get("text", "")) for p in parts).strip()
                if result:
                    break
                last_error = f"{model}: empty response"
            else:
                result = (
                    "AI 評判暫時失敗。"
                    + (f"\n原因：{last_error}" if last_error else "")
                    + "\n請檢查 GEMINI_API_KEY、模型權限或稍後再試。"
                )
    except Exception as e:
        logger.exception("Room judgement failed: %s", e)
        result = (
            "AI 評判暫時無法連線。"
            f"\n原因：{type(e).__name__}: {str(e)[:220]}"
            "\n請檢查伺服器網絡或 GEMINI_API_KEY。"
        )

    room.judgement = result
    await _room_broadcast(room, {"type": "judgement", "text": result})


async def _room_handle_message(room, member, msg):
    mtype = msg.get("type")
    if mtype == "audio" or "realtimeInput" in msg:
        await _room_handle_audio(room, member, msg)
        return

    is_host = member.user_id == room.created_by

    if mtype == "claim_role":
        side = msg.get("side")
        if room.mode == "A" and side in ("正方", "反方"):
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
        if room.judgement:
            await member.ws.send_text(json.dumps({
                "type": "judgement", "text": room.judgement,
            }, ensure_ascii=False))
        elif room.judgement_task is None or room.judgement_task.done():
            room.judgement_task = asyncio.create_task(_room_request_judgement(room))
        return

    if mtype == "test_ping":
        await member.ws.send_text(json.dumps({
            "type": "test_pong",
            "client_ts": msg.get("client_ts"),
            "server_now_ms": _now_ms(),
        }, ensure_ascii=False))
        return

    if mtype == "heartbeat":
        await member.ws.send_text(json.dumps({"type": "heartbeat_ack", "server_now_ms": _now_ms()}))
        return

    if mtype == "test_audio":
        data, mime = _audio_fields(msg)
        if data:
            await _room_broadcast(
                room,
                {"type": "test_audio", "from": member.user_id, "data": data,
                 "mimeType": mime or "audio/pcm;rate=16000"},
                exclude=member.user_id,
            )
        return

    if mtype == "test_received":
        await _room_broadcast(
            room,
            {"type": "test_received", "from": member.user_id,
             "source": msg.get("source")},
        )
        return

    if mtype == "chat":
        await _room_broadcast(room, {"type": "chat", "from": member.user_id,
                                     "text": str(msg.get("text", ""))[:500]})
        return


@app.post("/api/room/create")
async def room_create(request: Request):
    user_id = _require_committee_user(request)
    budget_error = _bandwidth_live_gate_error()
    if budget_error:
        raise HTTPException(status_code=429, detail=budget_error)
    _gc_rooms()
    if _active_room_count() >= MAX_ROOMS:
        raise HTTPException(status_code=429, detail="太多練習房，請稍後再試")

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

    code = None
    for _ in range(20):
        candidate = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LEN))
        if candidate not in ROOMS:
            code = candidate
            break
    if code is None:
        raise HTTPException(status_code=503, detail="未能產生房間代碼，請再試。")

    room = Room(code, mode, user_id, debate_format, topic, structure, free_minutes, capacity)
    if mode == "A":
        side = payload.get("side")
        room.creator_side = side if side in ("正方", "反方") else "正方"
    else:
        hs = payload.get("human_side")
        room.human_side = hs if hs in ("正方", "反方") else "正方"
        # Lobby creation stores only a server-side plan.  Quota reservation and
        # Gemini token minting happen after every connected member passes the
        # precheck, so abandoned lobbies cost neither quota nor provider token.
        prompt = (
            build_full_mock_live_prompt(topic, room.human_side, debate_format,
                                        free_debate_minutes=free_minutes if debate_format == "聯中" else None)
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
            session_minutes = [min(float(LIVE_FREE_MAX_MINUTES) + 2, full_mock_total_seconds(room.segments) / 60 + 2)]
        room.gemini = {
            "tokens": [], "prompt": prompt, "model": FREE_DEBATE_LIVE_MODEL,
            "session_labels": session_labels, "session_minutes": session_minutes,
        }
    ROOMS[code] = room
    return {"ok": True, "code": code, "mode": mode}


@app.get("/api/room/{code}")
async def room_info(code: str, request: Request):
    _require_committee_user(request)
    room = ROOMS.get((code or "").upper())
    if not room or room.phase == "ended":
        raise HTTPException(status_code=404, detail="房間不存在或已結束")
    return {
        "ok": True, "code": room.code, "mode": room.mode, "phase": room.phase,
        "debate_format": room.debate_format, "topic": room.topic,
        "structure": room.structure, "capacity": room.capacity,
        "human_side": room.human_side, "roster": room.roster(),
        "position_labels": room.position_labels(),
        "required_positions": room.required_positions(),
    }


@app.post("/api/room/{code}/leave")
async def room_leave(code: str, request: Request):
    user_id = _require_committee_user(request)
    room = ROOMS.get((code or "").upper())
    if room and user_id in room.members:
        m = room.members[user_id]
        m.connected = False
        try:
            await m.ws.close()
        except Exception:
            pass
        await _room_broadcast(room, {
            "type": "roster", "roster": room.roster(),
            "position_labels": room.position_labels(),
            "required_positions": room.required_positions(),
        })
    return {"ok": True}


@app.get("/api/room/{code}/transcript")
async def room_transcript(code: str, request: Request):
    _require_committee_user(request)
    room = ROOMS.get((code or "").upper())
    if not room:
        raise HTTPException(status_code=404, detail="房間不存在")
    return {"ok": True, "topic": room.topic, "debate_format": room.debate_format,
            "transcript": room.transcript}


@app.websocket("/room/{code}")
async def room_ws(websocket: WebSocket, code: str):
    # Authenticate before accept.  The same-origin HttpOnly cookie is the only
    # browser credential, so signed member tokens never enter URLs or storage.
    user_id = _verify_committee_token(websocket.cookies.get("committee_user") or "")
    if not user_id:
        await websocket.close(code=1008)
        return

    code = (code or "").upper()
    room = ROOMS.get(code)
    if not room or room.phase == "ended":
        await websocket.close(code=1008)
        return

    await websocket.accept()

    existing = room.members.get(user_id)
    if existing is None:
        if room.phase != "lobby":
            try:
                await websocket.send_text(json.dumps(
                    {"type": "error", "message": "練習開始後不可加入新成員。"},
                    ensure_ascii=False,
                ))
            except Exception:
                pass
            await websocket.close(code=1008)
            return
        if len([m for m in room.members.values() if m.connected]) >= room.capacity:
            try:
                await websocket.send_text(json.dumps(
                    {"type": "error", "message": "房間已滿"}, ensure_ascii=False))
            except Exception:
                pass
            await websocket.close(code=1013)
            return
        member = RoomMember(user_id, websocket)
        if room.mode == "A":
            if user_id == room.created_by and room.creator_side:
                member.role = room.creator_side
            else:
                taken = {m.role for m in room.members.values()}
                for s in ("正方", "反方"):
                    if s not in taken:
                        member.role = s
                        break
        else:
            member.role = room.human_side
        room.members[user_id] = member
    else:
        # reconnect: replace the stale socket, keep the role
        if not existing.connected and len([m for m in room.members.values() if m.connected]) >= room.capacity:
            try:
                await websocket.send_text(json.dumps(
                    {"type": "error", "message": "房間已滿"}, ensure_ascii=False))
            except Exception:
                pass
            await websocket.close(code=1013)
            return
        if existing.position and any(
                m.connected and m.user_id != existing.user_id and m.position == existing.position
                for m in room.members.values()):
            existing.position = None
        existing.ws = websocket
        existing.connected = True
        member = existing
    room.empty_since = None

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

    try:
        while True:
            raw = await websocket.receive()
            if raw.get("type") == "websocket.disconnect":
                break
            text = raw.get("text")
            if text is None:
                continue
            try:
                msg = json.loads(text)
            except Exception:
                continue
            await _room_handle_message(room, member, msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("room_ws error (%s): %s", code, e)
    finally:
        # A reconnect replaces ``member.ws`` before the old receive loop gets
        # its disconnect event.  Only the currently registered socket may mark
        # the member offline; otherwise the old loop drops the new connection
        # and audio appears to disappear until another reconnect happens.
        if member.ws is websocket:
            member.connected = False
            await _room_broadcast(room, {"type": "peer_left", "user_id": user_id})
            await _room_broadcast(room, {
                "type": "roster", "roster": room.roster(),
                "position_labels": room.position_labels(),
                "required_positions": room.required_positions(),
            })
        _gc_rooms()


@app.websocket("/{path:path}")
async def websocket_not_found(websocket: WebSocket, path: str):
    await websocket.close(code=1008, reason="Unknown WebSocket route")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def http_not_found(request: Request, path: str):
    return Response(content=json.dumps({"detail": "Not Found"}), status_code=404, media_type="application/json")
