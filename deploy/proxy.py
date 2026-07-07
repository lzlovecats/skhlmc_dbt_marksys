import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote_plus
from xml.sax.saxutils import escape as xml_escape

import tomllib

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, Response
from sqlalchemy import create_engine, text
from starlette.websockets import WebSocketDisconnect

from schema import (
    CREATE_PUSH_SUBSCRIPTIONS,
    CREATE_VIDEO_PROGRESS,
    CREATE_VIDEO_VIEWS,
    TABLE_PUSH_SUBSCRIPTIONS,
    TABLE_VIDEO_PROGRESS,
    TABLE_VIDEO_VIEWS,
)
from debate_timing import get_full_mock_sequence  # pure helper, no side effects


STREAMLIT_HTTP_URL = os.getenv("STREAMLIT_HTTP_URL", "http://127.0.0.1:8501")
STREAMLIT_WS_URL = os.getenv("STREAMLIT_WS_URL", "ws://127.0.0.1:8501")
BASE_DIR = Path(__file__).resolve().parents[1]

PWA_HEAD = """
<!-- skh-pwa-head -->
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/app-icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="聖呂中辯">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#111827">
<style>
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


def _get_db_engine():
    global _db_engine
    if _db_engine is None:
        db_url = _get_db_url()
        if not db_url:
            return None
        _db_engine = create_engine(db_url, pool_pre_ping=True)
    return _db_engine


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
    return FileResponse(BASE_DIR / "static" / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(BASE_DIR / "deploy" / "sw.js", media_type="application/javascript")


@app.get("/app-icon-{size}.png")
async def app_icon(size: str):
    icon_path = BASE_DIR / "static" / f"app-icon-{size}.png"
    if size not in {"180", "192", "512"} or not icon_path.exists():
        return Response(status_code=404)
    return FileResponse(icon_path, media_type="image/png")


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

    speech_key = _get_proxy_secret("AZURE_SPEECH_KEY").strip()
    speech_region = _get_proxy_secret("AZURE_SPEECH_REGION").strip()
    if not speech_key or not speech_region:
        raise HTTPException(status_code=503, detail="Azure TTS is not configured")

    voice = _get_proxy_secret("AZURE_TTS_VOICE", "zh-HK-HiuMaanNeural").strip() or "zh-HK-HiuMaanNeural"
    rate = _get_proxy_secret("AZURE_TTS_RATE", "0%").strip() or "0%"
    output_format = (
        _get_proxy_secret("AZURE_TTS_OUTPUT_FORMAT", "audio-24khz-48kbitrate-mono-mp3").strip()
        or "audio-24khz-48kbitrate-mono-mp3"
    )
    ssml = _build_azure_tts_ssml(tts_text, voice, rate)
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
        raise HTTPException(status_code=502, detail="Azure TTS request failed")

    if azure_response.status_code != 200:
        logger.warning(
            "Azure TTS returned %s: %s",
            azure_response.status_code,
            azure_response.text[:300],
        )
        raise HTTPException(status_code=502, detail="Azure TTS request failed")

    return Response(
        content=azure_response.content,
        media_type=azure_response.headers.get("content-type") or "audio/mpeg",
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
                        media_type="text/html")


@app.get("/projector/control")
async def projector_control_page():
    return FileResponse(BASE_DIR / "templates" / "projector_control.html",
                        media_type="text/html")


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

    return Response(
        content=content,
        status_code=backend_response.status_code,
        headers=_response_headers(backend_response.headers),
        media_type=None,
    )
