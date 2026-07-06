import asyncio
import logging
import os
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, Response
from starlette.websockets import WebSocketDisconnect


STREAMLIT_HTTP_URL = os.getenv("STREAMLIT_HTTP_URL", "http://127.0.0.1:8501")
STREAMLIT_WS_URL = os.getenv("STREAMLIT_WS_URL", "ws://127.0.0.1:8501")
BASE_DIR = Path(__file__).resolve().parents[1]

PWA_HEAD = """
<!-- skh-pwa-head -->
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/app-icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="聖呂電子系統">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#111827">
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
