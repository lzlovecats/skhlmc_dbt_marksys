import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote_plus

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
