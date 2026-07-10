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
import time
from pathlib import Path
from urllib.parse import quote_plus
from xml.sax.saxutils import escape as xml_escape

import tomllib

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, Response
from sqlalchemy import create_engine, event, text
from starlette.websockets import WebSocketDisconnect

from schema import (
    CREATE_PUSH_SUBSCRIPTIONS,
    CREATE_VIDEO_PROGRESS,
    CREATE_VIDEO_VIEWS,
    TABLE_PUSH_SUBSCRIPTIONS,
    TABLE_VIDEO_PROGRESS,
    TABLE_VIDEO_VIEWS,
)
from debate_timing import (  # pure helpers, no side effects
    get_full_mock_sequence,
    get_debate_timer_config,
    FREE_DEBATE_FORMATS,
    DEBATE_FORMATS,
)
from ai_model_config import ROOM_JUDGEMENT_MODEL_LABELS, model_slugs_for_labels
from prompts import build_free_debate_live_prompt, LIVE_RUNTIME_PROMPTS  # pure, no streamlit
from prompts import build_room_judgement_prompt
from api.vote_api import router as vote_router


STREAMLIT_HTTP_URL = os.getenv("STREAMLIT_HTTP_URL", "http://127.0.0.1:8501")
STREAMLIT_WS_URL = os.getenv("STREAMLIT_WS_URL", "ws://127.0.0.1:8501")
BASE_DIR = Path(__file__).resolve().parents[1]

CACHE_NO_CACHE = "no-cache"
CACHE_HTML = "public, max-age=300, stale-while-revalidate=3600"
CACHE_MANIFEST = "public, max-age=86400"
CACHE_STATIC = "public, max-age=31536000, immutable"
STREAMLIT_STATIC_RE = re.compile(
    r"^(static|vendor)/|"
    r"\.(?:js|mjs|css|map|woff2?|ttf|otf|png|jpe?g|gif|webp|svg|ico)$",
    re.IGNORECASE,
)

PWA_HEAD = """
<!-- skh-pwa-head -->
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/app-icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="聖呂中辯">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="theme-color" content="#000000">
<meta name="color-scheme" content="dark">
<style>
html, body, #root {
    background: #000000 !important;
}
input, textarea, select {
    font-size: 16px !important;
}
</style>
<script>
if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
}
</script>
"""

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


app = FastAPI()
# JSON API for the HTML voting page. Registered before the catch-all proxy
# routes at the bottom of this file so /api/vote/* is served locally instead of
# being forwarded to Streamlit.
app.include_router(vote_router)
logger = logging.getLogger("skh_proxy")
_db_engine = None
_streamlit_secrets = None


def _proxy_headers(headers):
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
    }


def _response_headers(headers):
    blocked = HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in blocked
    }


def _cache_headers(cache_control):
    return {"Cache-Control": cache_control}


def _streamlit_cache_control(path, content_type):
    if not path or path == "/":
        return CACHE_NO_CACHE

    normalized = path.lstrip("/")
    lowered_type = (content_type or "").lower()
    if "text/html" in lowered_type:
        return CACHE_NO_CACHE
    if STREAMLIT_STATIC_RE.search(normalized):
        return CACHE_STATIC
    return None


def _websocket_headers(headers):
    blocked = HOP_BY_HOP_HEADERS | {
        "host",
        "origin",
        "sec-websocket-accept",
        "sec-websocket-extensions",
        "sec-websocket-key",
        "sec-websocket-protocol",
        "sec-websocket-version",
    }
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in blocked
    }


def _inject_pwa_head(content):
    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError:
        return content

    if "<!-- skh-pwa-head -->" in html or "</head>" not in html:
        return content

    # Streamlit ships its own viewport meta tag. iOS is sensitive to multiple
    # viewport/theme declarations, so strip them and inject one authoritative
    # PWA block before the page reaches the device.
    for meta_name in (
        "viewport",
        "theme-color",
        "color-scheme",
        "apple-mobile-web-app-capable",
        "mobile-web-app-capable",
        "apple-mobile-web-app-title",
        "apple-mobile-web-app-status-bar-style",
    ):
        html = re.sub(
            rf"\s*<meta\b(?=[^>]*\bname=[\"']{re.escape(meta_name)}[\"'])[^>]*>\s*",
            "\n",
            html,
            flags=re.IGNORECASE,
        )

    return html.replace("</head>", PWA_HEAD + "\n</head>", 1).encode("utf-8")


def _get_db_url():
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url

    secrets_path = BASE_DIR / ".streamlit" / "secrets.toml"
    if not secrets_path.exists():
        return None

    with secrets_path.open("rb") as f:
        secrets = tomllib.load(f)

    db = secrets.get("connections", {}).get("postgresql", {})
    if not db:
        return None

    dialect = db.get("dialect", "postgresql")
    username = quote_plus(str(db.get("username", "")))
    password = quote_plus(str(db.get("password", "")))
    host = db.get("host", "localhost")
    port = db.get("port", "5432")
    database = db.get("database", "")
    return f"{dialect}://{username}:{password}@{host}:{port}/{database}"


def _get_streamlit_secrets():
    global _streamlit_secrets
    if _streamlit_secrets is not None:
        return _streamlit_secrets

    secrets_path = BASE_DIR / ".streamlit" / "secrets.toml"
    if not secrets_path.exists():
        _streamlit_secrets = {}
        return _streamlit_secrets

    try:
        with secrets_path.open("rb") as f:
            _streamlit_secrets = tomllib.load(f)
    except Exception:
        logger.exception("Failed to read Streamlit secrets")
        _streamlit_secrets = {}
    return _streamlit_secrets


def _get_proxy_secret(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is not None:
        return value
    value = _get_streamlit_secrets().get(key, default)
    return str(value) if value is not None else default


def _get_vapid():
    """VAPID config for streamlit-free push (core.push), or None if unconfigured.
    Mirrors functions._get_vapid_config but reads the proxy's own secret source."""
    public_key = _get_proxy_secret("VAPID_PUBLIC_KEY")
    private_key = _get_proxy_secret("VAPID_PRIVATE_KEY")
    subject = _get_proxy_secret("VAPID_SUBJECT", "https://skhlmc-dbt-marksys.onrender.com")
    if not public_key or not private_key:
        return None
    return {"public_key": public_key, "private_key": private_key, "subject": subject}


def _get_db_engine():
    global _db_engine
    if _db_engine is None:
        db_url = _get_db_url()
        if not db_url:
            return None
        _db_engine = create_engine(db_url, pool_pre_ping=True)

        @event.listens_for(_db_engine, "connect")
        def _set_search_path(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET search_path TO public, extensions")
            finally:
                cursor.close()
    return _db_engine


class _ProxyDb:
    """DB executor over the proxy's own SQLAlchemy engine, matching the duck-typed
    contract consumed by ``core`` domain logic (query / execute / execute_count).

    The streamlit-free counterpart of ``db.StreamlitDb`` — lets the proxy reuse
    ``core.vote_logic`` without importing Streamlit. Search path (public,
    extensions) is already set by the engine's connect listener.
    """

    def __init__(self, engine):
        self._engine = engine

    def query(self, sql_str, params=None):
        import pandas as pd
        with self._engine.connect() as conn:
            result = conn.execute(text(sql_str), params or {})
            rows = result.fetchall()
            columns = list(result.keys())
        return pd.DataFrame(rows, columns=columns)

    def execute(self, sql_str, params=None):
        with self._engine.begin() as conn:
            conn.execute(text(sql_str), params or {})

    def execute_count(self, sql_str, params=None):
        with self._engine.begin() as conn:
            result = conn.execute(text(sql_str), params or {})
            return result.rowcount


def get_vote_db():
    """The DB executor passed to ``core.vote_logic`` from the API handlers."""
    engine = _get_db_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return _ProxyDb(engine)


def _verify_committee_token(token: str):
    """Verify a signed ``user_id:sig`` token against the shared cookie secret."""
    if not token or ":" not in token:
        return None

    engine = _get_db_engine()
    if engine is None:
        return None

    user_id, sig = token.rsplit(":", 1)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT value FROM system_config WHERE key = 'cookie_secret'")
        ).fetchone()

    if row is None:
        return None

    secret = row._mapping["value"]
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
        row = conn.execute(
            text("SELECT value FROM system_config WHERE key = 'cookie_secret'")
        ).fetchone()
    if row is None:
        return None
    _relay_cookie_secret = str(row._mapping["value"])
    return _relay_cookie_secret


def _verify_relay_signature(token: str, sig: str) -> bool:
    """Verify the HMAC signature the app attaches to a Gemini Live ephemeral
    token (see auth.sign_relay_token). Blocks anyone from using /gemini-live as
    an open relay with an arbitrary token."""
    if not token or not sig:
        return False
    secret = _get_relay_cookie_secret()
    if not secret:
        return False
    expected = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _require_committee_user(request: Request):
    # The cookie is the primary source, but requests originating from the
    # sandboxed Streamlit component iframe cannot carry a SameSite=Strict
    # cookie, so fall back to a signed bearer token in the Authorization header.
    user_id = _verify_committee_cookie(request)
    if not user_id:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            user_id = _verify_committee_token(auth[7:].strip())
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user_id


def _ensure_push_subscriptions_table(conn):
    conn.execute(text(CREATE_PUSH_SUBSCRIPTIONS))
    conn.execute(text(
        f"CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user_active "
        f"ON {TABLE_PUSH_SUBSCRIPTIONS}(user_id, is_active)"
    ))


def _ensure_video_tracking_tables(conn):
    conn.execute(text(CREATE_VIDEO_VIEWS))
    conn.execute(text(CREATE_VIDEO_PROGRESS))
    conn.execute(text(
        f"CREATE INDEX IF NOT EXISTS idx_video_views_user_updated "
        f"ON {TABLE_VIDEO_VIEWS}(user_id, viewed_at DESC)"
    ))
    conn.execute(text(
        f"CREATE INDEX IF NOT EXISTS idx_video_progress_user_updated "
        f"ON {TABLE_VIDEO_PROGRESS}(user_id, updated_at DESC)"
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
                        headers=_cache_headers(CACHE_STATIC))


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

    endpoint = str(subscription.get("endpoint", "")).strip() if isinstance(subscription, dict) else ""
    if not endpoint:
        raise HTTPException(status_code=400, detail="Missing endpoint")

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
    new_endpoint = (
        str(subscription.get("endpoint", "")).strip()
        if isinstance(subscription, dict) else ""
    )
    if not new_endpoint:
        raise HTTPException(status_code=400, detail="Missing new endpoint")

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        _ensure_push_subscriptions_table(conn)

        # Recover the owning user from the old row (if the old endpoint is known).
        user_id = None
        if old_endpoint:
            row = conn.execute(
                text(
                    f"SELECT user_id FROM {TABLE_PUSH_SUBSCRIPTIONS} "
                    "WHERE endpoint = :old_endpoint"
                ),
                {"old_endpoint": old_endpoint},
            ).fetchone()
            if row is not None:
                user_id = row[0]

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
        return bool(_get_proxy_secret("CUSTOM_TTS_URL").strip())
    return bool(
        _get_proxy_secret("AZURE_SPEECH_KEY").strip()
        and _get_proxy_secret("AZURE_SPEECH_REGION").strip()
    )


_LEXICON_TTL = 60.0  # seconds; dictionary edits in ai_training.py take effect within this
_lexicon_cache = {"rows": None, "at": 0.0}


def _load_lexicon_overrides():
    """Active (term, reading) pairs from tts_lexicon, longest term first so
    overlapping terms don't partially clobber. Cached with a short TTL; on DB
    error, keep the last good snapshot rather than dropping overrides."""
    now = time.monotonic()
    cached = _lexicon_cache["rows"]
    if cached is not None and (now - _lexicon_cache["at"]) < _LEXICON_TTL:
        return cached
    rows = []
    try:
        engine = _get_db_engine()
        with engine.begin() as conn:
            result = conn.execute(
                text("SELECT term, reading FROM tts_lexicon WHERE is_active = TRUE")
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


def _preprocess_tts_text(text_value: str) -> str:
    """讀音字典前處理 (tts_rd_plan.md 第二節「讀音層」). 合成前把 tts_lexicon 嘅
    term → reading 覆寫。單人 (/api/tts/azure) 同聯機 (_room_gemini_pump) 都經呢度,
    改字典一次兩邊生效。將來可喺呢度加 G2P (ToJyutping/PyCantonese)。"""
    processed = (text_value or "").strip()
    if not processed:
        return processed
    replacements = {}
    for term_value, reading_value in _load_lexicon_overrides():
        replacements.setdefault(term_value, reading_value)
    if not replacements:
        return processed
    pattern = re.compile("|".join(re.escape(term) for term in replacements))
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
        async with httpx.AsyncClient(timeout=30) as client:
            azure_response = await client.post(
                endpoint,
                content=ssml.encode("utf-8"),
                headers={
                    "Ocp-Apim-Subscription-Key": speech_key,
                    "Content-Type": "application/ssml+xml; charset=utf-8",
                    "X-Microsoft-OutputFormat": output_format,
                    "User-Agent": "skhlmc-dbt-marksys",
                },
            )
    except httpx.HTTPError as e:
        logger.warning("Azure TTS request failed: %s", e)
        raise TtsUnavailable("Azure TTS request failed", status=502)

    if azure_response.status_code != 200:
        logger.warning(
            "Azure TTS returned %s: %s",
            azure_response.status_code,
            azure_response.text[:300],
        )
        raise TtsUnavailable("Azure TTS request failed", status=502)

    return (
        azure_response.content,
        azure_response.headers.get("content-type") or "audio/mpeg",
    )


async def _synthesize_custom(text_value: str) -> tuple[bytes, str]:
    """自家粵語 TTS (tts_rd_plan.md 第三節). 待 v0 checkpoint 出咗先實作:
    call CUSTOM_TTS_URL、回傳音 bytes + mime。而家先 stub。"""
    custom_url = _get_proxy_secret("CUSTOM_TTS_URL").strip()
    if not custom_url:
        raise TtsUnavailable("Custom TTS is not configured", status=503)
    raise TtsUnavailable("Custom TTS is not implemented yet", status=503)


async def _synthesize_tts(text_value: str) -> tuple[bytes, str]:
    """統一 TTS 入口:單人 (/api/tts/azure route)、聯機 (_room_gemini_pump)、
    將來 custom model 全部行呢度。換 provider = 改 TTS_PROVIDER secret。"""
    processed = _preprocess_tts_text(text_value)
    if not processed:
        raise TtsUnavailable("Missing text", status=400)
    provider = (_get_proxy_secret("TTS_PROVIDER", "azure").strip() or "azure").lower()
    if provider == "custom":
        return await _synthesize_custom(processed)
    return await _synthesize_azure(processed)


@app.post("/api/tts/azure")
async def azure_tts(request: Request):
    _require_committee_user(request)

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    tts_text = str(payload.get("text") or "").strip()
    if not tts_text:
        raise HTTPException(status_code=400, detail="Missing text")
    if len(tts_text) > 1200:
        raise HTTPException(status_code=400, detail="Text is too long")

    try:
        audio_bytes, mime = await _synthesize_tts(tts_text)
    except TtsUnavailable as e:
        raise HTTPException(status_code=e.status, detail=str(e))

    return Response(
        content=audio_bytes,
        media_type=mime or "audio/mpeg",
        headers={"Cache-Control": "no-store"},
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
            text(
                f"INSERT INTO {TABLE_VIDEO_VIEWS} (video_id, user_id, viewed_at) "
                "VALUES (:video_id, :user_id, :viewed_at)"
            ),
            {"video_id": video_id, "user_id": user_id, "viewed_at": now},
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
# Additive and self-contained: new routes are registered before the catch-all
# proxy below, backed by a single lazily-created table (projector_state) plus
# read-only reads of the existing matches/debaters tables. No Streamlit page,
# existing endpoint, or schema migration is touched, so deploying this to
# Render does not change existing behaviour. The projector intentionally shows
# NO timer — timing stays on the chairperson's own device exactly as before.
# ---------------------------------------------------------------------------

PROJECTOR_DEFAULT_DISPLAY = "main"

CREATE_PROJECTOR_STATE = """
CREATE TABLE IF NOT EXISTS projector_state (
    display_key   TEXT PRIMARY KEY,
    match_id      TEXT,
    debate_format TEXT,
    seg_index     INTEGER DEFAULT 0,
    visible       BOOLEAN DEFAULT TRUE,
    updated_at    TIMESTAMP
);
"""

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


def _ensure_projector_table(conn):
    conn.execute(text(CREATE_PROJECTOR_STATE))


def _resolve_projector_state(engine, display_key):
    """Turn the stored row into ready-to-render display JSON (motion, team
    names, current speaking role/name). All resolution happens here so the
    display page can stay dumb and just poll."""
    with engine.begin() as conn:
        _ensure_projector_table(conn)
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
            "FROM matches ORDER BY match_date DESC NULLS LAST, match_time DESC NULLS LAST"
        )).fetchall()
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
        _ensure_projector_table(conn)
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
    # New HTML voting page (Phase 2). Served alongside the legacy Streamlit page
    # so committee members can use either while it stabilises.
    return FileResponse(BASE_DIR / "frontend" / "vote" / "index.html",
                        media_type="text/html",
                        headers=_cache_headers(CACHE_HTML))


@app.get("/api/practice/bell")
async def appliance_practice_bell():
    return FileResponse(BASE_DIR / "assets" / "bell.mp3", media_type="audio/mpeg",
                        headers=_cache_headers(CACHE_STATIC))


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

    config = get_debate_timer_config(
        debate_format,
        free_debate_minutes=_opt_float("free_minutes"),
        closing_prep_minutes=_opt_float("closing_prep_minutes"),
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
_PRACTICE_LIVE_MAX_PER_HOUR = 30
_PRACTICE_LIVE_MIN_GAP_SEC = 3


def _practice_live_rate_check(user_id: str):
    """Return an error message if this user is minting too fast, else None."""
    now = time.time()
    hits = [t for t in _practice_live_hits.get(user_id, []) if now - t < 3600]
    if hits and now - hits[-1] < _PRACTICE_LIVE_MIN_GAP_SEC:
        return "太快喇，請等幾秒再開始。"
    if len(hits) >= _PRACTICE_LIVE_MAX_PER_HOUR:
        return "練習次數已達每小時上限，請稍後再試。"
    hits.append(now)
    _practice_live_hits[user_id] = hits
    return None


def _sign_relay_token(token: str) -> str:
    """Mirror auth.sign_relay_token: HMAC the ephemeral token with the shared
    cookie_secret so the /gemini-live relay (_verify_relay_signature) accepts it."""
    secret = _get_relay_cookie_secret()
    if not secret:
        return ""
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def _practice_bell_src() -> str:
    try:
        data = (BASE_DIR / "assets" / "bell.mp3").read_bytes()
        return "data:audio/mpeg;base64," + base64.b64encode(data).decode()
    except FileNotFoundError:
        return ""


def _mint_gemini_live_token(duration_minutes: float):
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
    expire = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=token_minutes + 2)
    try:
        client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})
        token = client.auth_tokens.create(config={
            "uses": 1,
            "expire_time": expire,
            "new_session_expire_time": expire,
            "http_options": {"api_version": "v1alpha"},
        })
    except Exception as e:
        logger.warning("Gemini Live token mint failed: %s", e)
        return None, "Gemini 未能建立練習連線，請稍後再試。"
    token_name = getattr(token, "name", None)
    if not token_name:
        return None, "Gemini 未回傳 token。"
    return token_name, None


def _render_live_debate_html(token, prompt, live_minutes, bell_schedule, ai_starts):
    """Server-render templates/live_debate.html the same way ai_coach does, so the
    kiosk gets the identical Live engine. Free-debate only — mock fields empty."""
    html = (BASE_DIR / "templates" / "live_debate.html").read_text(encoding="utf-8")
    relay_ws_base = _get_proxy_secret("LIVE_RELAY_WS_BASE", "") or ""
    token_sigs = {}
    if relay_ws_base:
        sig = _sign_relay_token(token)
        if sig:
            token_sigs[token] = sig
    replacements = {
        "__RELAY_WS_BASE__": json.dumps(relay_ws_base),
        "__TOKEN_SIGS__": json.dumps(token_sigs),
        "__LIVE_TOKEN__": json.dumps(token),
        "__LIVE_MODEL__": json.dumps(FREE_DEBATE_LIVE_MODEL),
        "__LIVE_PROMPT__": json.dumps(prompt, ensure_ascii=False),
        "__LIVE_MINUTES__": json.dumps(float(live_minutes or 2.5)),
        "__BELL_SRC__": json.dumps(_practice_bell_src()),
        "__BELL_SCHEDULE__": json.dumps(bell_schedule or [], ensure_ascii=False),
        "__MOCK_SEGMENTS__": json.dumps([]),
        "__MOCK_TOKENS__": json.dumps([]),
        "__MOCK_SESSION_LABELS__": json.dumps([]),
        "__AI_STARTS__": json.dumps(bool(ai_starts)),
        "__LIVE_PROMPTS__": json.dumps(LIVE_RUNTIME_PROMPTS, ensure_ascii=False),
    }
    for key, value in replacements.items():
        html = html.replace(key, value)
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
    if not topic:
        return _practice_error_page("未有辯題", "請先輸入辯題再開始。")
    if side not in ("正方", "反方"):
        side = "正方"
    if debate_format not in _PRACTICE_LIVE_FORMATS:
        debate_format = _PRACTICE_LIVE_FORMATS[0]

    if debate_format == "聯中":
        try:
            live_minutes = float(q.get("minutes") or 5)
        except (TypeError, ValueError):
            live_minutes = 5.0
        live_minutes = min(10.0, max(0.5, live_minutes))
    else:
        live_minutes = 2.5

    bell_schedule = get_debate_timer_config(
        debate_format, free_debate_minutes=live_minutes,
    )["bell_schedules"].get("free", [])
    token_minutes = max(3, math.ceil(live_minutes * 2 + 2))

    token, mint_error = _mint_gemini_live_token(token_minutes)
    if mint_error:
        return _practice_error_page("未能開始", mint_error)

    prompt = build_free_debate_live_prompt(topic, side, "")
    html = _render_live_debate_html(token, prompt, live_minutes, bell_schedule, side == "反方")
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
    if not _verify_relay_signature(token, sig):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    backend_url = f"{GEMINI_LIVE_WS_URL}?access_token={quote_plus(token)}"

    try:
        # max_size=None：AI 回覆嘅音訊 frame 可以大過 websockets 預設 1MB 上限。
        # ping_interval=None：避免 keepalive pong timeout 喺長時間等待時誤斷線。
        backend = await websockets.connect(backend_url, max_size=None, ping_interval=None)
    except Exception as e:
        logger.exception("Gemini Live relay backend connect failed: %s", e)
        await websocket.close(code=1011)
        return

    async with backend:
        async def client_to_backend():
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    await backend.close()
                    break
                elif "text" in message and message["text"] is not None:
                    await backend.send(message["text"])
                elif "bytes" in message and message["bytes"] is not None:
                    await backend.send(message["bytes"])

        async def backend_to_client():
            async for message in backend:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)

        tasks = [
            asyncio.create_task(client_to_backend()),
            asyncio.create_task(backend_to_client()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        except WebSocketDisconnect:
            return
        except Exception as e:
            logger.exception("Gemini Live relay failed: %s", e)

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
MAX_ROOMS = int(os.getenv("MAX_ROOMS", "8"))
ROOM_EMPTY_GRACE_MS = 60 * 1000       # keep an empty room this long for reconnects
ROOM_MAX_AGE_MS = 90 * 60 * 1000      # hard TTL
ROOM_JUDGEMENT_MODELS = model_slugs_for_labels(ROOM_JUDGEMENT_MODEL_LABELS)

ROOMS = {}  # code -> Room


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
        self.gemini_ws = None
        self.gemini_task = None
        self.tick_task = None
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


def _gc_rooms():
    now = _now_ms()
    for code in list(ROOMS.keys()):
        room = ROOMS.get(code)
        if room is None:
            continue
        if now - room.created_at > ROOM_MAX_AGE_MS:
            ROOMS.pop(code, None)
            continue
        if any(m.connected for m in room.members.values()):
            room.empty_since = None
        else:
            if room.empty_since is None:
                room.empty_since = now
            elif now - room.empty_since > ROOM_EMPTY_GRACE_MS:
                ROOMS.pop(code, None)


def _active_room_count():
    return len([r for r in ROOMS.values() if r.phase != "ended"])


async def _room_broadcast(room, msg, exclude=None):
    text = json.dumps(msg, ensure_ascii=False)
    for m in list(room.members.values()):
        if not m.connected or (exclude and m.user_id == exclude):
            continue
        try:
            await m.ws.send_text(text)
        except Exception:
            m.connected = False


async def _room_tick(room):
    try:
        while room.phase == "active" and ROOMS.get(room.code) is room:
            await asyncio.sleep(1)
            if room.phase != "active" or ROOMS.get(room.code) is not room:
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


async def _room_start_active(room):
    room.phase = "active"
    room.seg_index = 0
    room.seg_started_ms = _now_ms()
    room.side_elapsed_ms = {"正方": 0, "反方": 0}
    room.active_turn_user = None
    room.active_turn_side = None
    room.active_turn_started_ms = None
    room.free_first_done = False
    room.precheck_id = None
    room.precheck_results = {}
    await _room_start_gemini_if_needed(room)
    _room_ensure_tick(room)
    await _room_broadcast(room, room.state_msg())


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
        await _room_start_active(room)
    else:
        await _room_broadcast(room, _room_precheck_msg(room, "precheck_failed"))


def _room_start_blocker(room):
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


# --- Gemini leg (mode B): one server-owned Gemini Live session per room ------
#
# The server (not any browser) owns the single upstream Gemini Live socket, so
# the ephemeral token never leaves the server, the whole team shares one AI
# context, and a member dropping does not kill the AI. Same connect/pump shape
# as gemini_live_relay above; here the proxy is the client. Render's Singapore
# egress means no geo-block, so no relay hop is needed for this leg.

async def _room_start_gemini_if_needed(room):
    if room.mode != "B" or room.gemini is None or room.gemini_ws is not None:
        return
    tokens = room.gemini.get("tokens") or []
    token = tokens[0] if tokens else ""
    model = room.gemini.get("model") or ""
    prompt = room.gemini.get("prompt") or ""
    if not token or not model:
        await _room_broadcast(room, {"type": "error",
                                     "message": "未有 AI 連線資料，AI 對手未能啟動。"})
        return

    backend_url = f"{GEMINI_LIVE_WS_URL}?access_token={quote_plus(token)}"
    try:
        gws = await websockets.connect(backend_url, max_size=None, ping_interval=None)
    except Exception as e:
        logger.exception("room Gemini connect failed (%s): %s", room.code, e)
        await _room_broadcast(room, {"type": "error",
                                     "message": "AI 對手連線失敗，請重新建立房間。"})
        return

    room.gemini_ws = gws
    # Decide once per session whether to re-voice the AI via server-side TTS.
    # Off by default when the provider is unconfigured or ROOM_TTS_ENABLED=0, so
    # the room degrades to Gemini native "Kore" audio (existing behaviour).
    room.tts_enabled = (
        tts_provider_configured()
        and _get_proxy_secret("ROOM_TTS_ENABLED", "1").strip() != "0"
    )
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
        await gws.send(json.dumps(setup))
    except Exception as e:
        logger.exception("room Gemini setup failed (%s): %s", room.code, e)
    room.gemini_task = asyncio.create_task(_room_gemini_pump(room, gws))


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
    return {"pending": "", "native": [], "fallback": False}


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
        await _room_broadcast(room, {"type": "serverContent", "serverContent": _strip_audio_parts(sc)})
    else:
        parts = ((sc.get("modelTurn") or {}).get("parts")) or []
        if any((p.get("inlineData") or {}).get("data") for p in parts):
            state["native"].append(sc)
        await _room_broadcast(room, {"type": "serverContent", "serverContent": _strip_audio_parts(sc)})
        ot = sc.get("outputTranscription") or {}
        if ot.get("text"):
            state["pending"] += ot["text"]
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
                ai_buffer["text"] += ot["text"]
            if turn_complete:
                if room.tts_enabled:
                    tts_state = _tts_new_turn_state()
                text_value = ai_buffer["text"].strip()
                ai_buffer["text"] = ""
                if text_value:
                    item = {
                        "speaker": "AI", "side": ai_side, "seg": room.seg_index,
                        "label": (room.current_segment() or {}).get("label", ""),
                        "text": text_value[:2000], "created_ms": _now_ms(),
                    }
                    room.transcript.append(item)
                    room.transcript = room.transcript[-80:]
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
        await gws.send(json.dumps({
            "realtimeInput": {"audio": {"data": data,
                                        "mimeType": mime or "audio/pcm;rate=16000"}}
        }))
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
        await gws.send(json.dumps({"clientContent": {
            "turns": [{"role": "user", "parts": [{"text": cue}]}],
            "turnComplete": True,
        }}))
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
    ai_side = "反方" if room.human_side == "正方" else "正方"
    if seg.get("side") == ai_side:
        await _room_cue_ai_segment(room, seg)


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
    if room.structure == "free" and member.role in room.side_elapsed_ms:
        used = room.side_elapsed_ms.get(member.role, 0)
        if room.active_turn_side == member.role and room.active_turn_started_ms is not None:
            used += max(0, _now_ms() - room.active_turn_started_ms)
        if used >= int(room.free_minutes * 60 * 1000):
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
    if room.mode == "B" and room.structure == "free":
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
        if room.structure == "free" and member.role in room.side_elapsed_ms:
            if room.side_elapsed_ms.get(member.role, 0) >= int(room.free_minutes * 60 * 1000):
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
    if room.mode == "B" and room.structure == "free" and room.gemini_ws is not None:
        try:
            key = "activityStart" if speaking else "activityEnd"
            await room.gemini_ws.send(json.dumps({"realtimeInput": {key: {}}}))
        except Exception:
            pass
    # Mock structure: after a human finishes a 雙方 (free-debate) turn, cue the AI
    # to give a short rebuttal from the transcript.
    if (room.mode == "B" and room.structure == "mock" and not speaking
            and (room.current_segment() or {}).get("side") == "雙方"):
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
        "text": text_value[:2000],
        "created_ms": _now_ms(),
    }
    room.transcript.append(item)
    room.transcript = room.transcript[-80:]
    await _room_broadcast(room, {"type": "transcript", "item": item})


async def _room_request_judgement(room):
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
        async with httpx.AsyncClient(timeout=45) as client:
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
        idx = max(0, min(idx, len(room.segments) - 1))
        room.seg_index = idx
        room.seg_started_ms = _now_ms()
        room.active_turn_user = None
        room.active_turn_side = None
        room.active_turn_started_ms = None
        room.free_first_done = False
        await _room_broadcast(room, room.state_msg())
        await _room_on_segment_enter(room)
        return

    if mtype == "end" and is_host:
        room.phase = "ended"
        if room.tick_task is not None and not room.tick_task.done():
            room.tick_task.cancel()
        await _room_close_gemini(room)
        await _room_broadcast(room, {"type": "ended"})
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
        asyncio.create_task(_room_request_judgement(room))
        return

    if mtype == "test_ping":
        await member.ws.send_text(json.dumps({
            "type": "test_pong",
            "client_ts": msg.get("client_ts"),
            "server_now_ms": _now_ms(),
        }, ensure_ascii=False))
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
    if structure == "free" and debate_format not in FREE_DEBATE_FORMATS:
        raise HTTPException(status_code=400, detail=f"{debate_format}不設自由辯論，請改用完整 Mock。")
    topic = str(payload.get("topic") or "").strip()
    try:
        free_minutes = float(payload.get("free_minutes") or 2.5)
    except Exception:
        free_minutes = 2.5
    if mode == "A":
        capacity = 2
    elif structure == "mock":
        capacity = 3 if debate_format == "星島" else 4
    else:
        try:
            capacity = max(1, min(4, int(payload.get("capacity") or 4)))
        except Exception:
            capacity = 4

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
        gem = payload.get("gemini")
        if isinstance(gem, dict):
            room.gemini = {
                "tokens": gem.get("tokens") or [],
                "sigs": gem.get("sigs") or {},
                "prompt": gem.get("prompt") or "",
                "model": gem.get("model") or "",
                "session_labels": gem.get("session_labels") or [],
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
    # Auth before accept(): only committee-signed tokens (user_id:sig) get in,
    # mirroring the Gemini relay's reject-before-handshake discipline.
    token = websocket.query_params.get("u", "")
    user_id = _verify_committee_token(token)
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
        member.connected = False
        await _room_broadcast(room, {"type": "peer_left", "user_id": user_id})
        await _room_broadcast(room, {
            "type": "roster", "roster": room.roster(),
            "position_labels": room.position_labels(),
            "required_positions": room.required_positions(),
        })
        _gc_rooms()


@app.websocket("/{path:path}")
async def websocket_proxy(websocket: WebSocket, path: str):
    # Streamlit repurposes Sec-WebSocket-Protocol to carry tokens. The first
    # entry ("streamlit") is the actual subprotocol and MUST be echoed back to
    # the browser on accept — otherwise the browser rejects the handshake and
    # reconnects endlessly, leaving a blank page. The full list is forwarded to
    # the backend so it can read the xsrf token / session id entries.
    raw_subprotocols = websocket.headers.get("sec-websocket-protocol", "")
    requested_subprotocols = [p.strip() for p in raw_subprotocols.split(",") if p.strip()]
    selected_subprotocol = requested_subprotocols[0] if requested_subprotocols else None
    await websocket.accept(subprotocol=selected_subprotocol)

    query = f"?{websocket.url.query}" if websocket.url.query else ""
    backend_url = f"{STREAMLIT_WS_URL}/{path}{query}"
    headers = _websocket_headers(websocket.headers)
    headers["Host"] = "127.0.0.1:8501"
    headers["Origin"] = STREAMLIT_HTTP_URL

    connect_kwargs = {"subprotocols": requested_subprotocols} if requested_subprotocols else {}

    try:
        try:
            backend = await websockets.connect(backend_url, additional_headers=headers, **connect_kwargs)
        except TypeError:
            backend = await websockets.connect(backend_url, extra_headers=headers, **connect_kwargs)
    except Exception as e:
        logger.exception("WebSocket backend connection failed for %s: %s", backend_url, e)
        await websocket.close(code=1011)
        return

    async with backend:
        async def client_to_backend():
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    await backend.close()
                    break
                elif "text" in message:
                    await backend.send(message["text"])
                elif "bytes" in message:
                    await backend.send(message["bytes"])

        async def backend_to_client():
            async for message in backend:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)

        tasks = [
            asyncio.create_task(client_to_backend()),
            asyncio.create_task(backend_to_client()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        except WebSocketDisconnect:
            return
        except Exception as e:
            logger.exception("WebSocket proxy failed for %s: %s", backend_url, e)
            try:
                await websocket.close(code=1011)
            except RuntimeError:
                pass


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def http_proxy(request: Request, path: str):
    query = f"?{request.url.query}" if request.url.query else ""
    backend_url = f"{STREAMLIT_HTTP_URL}/{path}{query}"

    async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
        backend_response = await client.request(
            request.method,
            backend_url,
            content=await request.body(),
            headers=_proxy_headers(request.headers),
        )

    content = backend_response.content
    content_type = backend_response.headers.get("content-type", "")
    if "text/html" in content_type.lower():
        content = _inject_pwa_head(content)

    headers = _response_headers(backend_response.headers)
    cache_control = _streamlit_cache_control(path, content_type)
    if cache_control:
        headers["Cache-Control"] = cache_control

    return Response(
        content=content,
        status_code=backend_response.status_code,
        headers=headers,
        media_type=None,
    )
