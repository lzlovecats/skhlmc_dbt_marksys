"""Member SSE chat, Developer node controls and outbound node WebSocket."""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from ai_name import LMC_AI_EMOJI, LMC_AI_NAME
from api.access import (
    interactive_features_suspension,
    require_interactive_features_available,
    require_page_user_or_developer,
)
from core.funds_logic import log_ai_usage
from core.lmc_ai_runtime import (
    BackendChangedError,
    ChatJob,
    NodeUnavailableError,
    QueueFullError,
    RUNTIME,
)
from core.lmc_ai_store import (
    authenticate_node,
    create_node,
    get_active_node_id,
    list_node_rows,
    mark_node_disconnected,
    require_lmc_ai_schema,
    revoke_node,
    rotate_node_token,
    set_active_node_id,
    update_node_hello,
)
from system_limits import (
    LMC_AI_BROWSER_HISTORY_MAX_CHARS,
    LMC_AI_BROWSER_HISTORY_MAX_MESSAGES,
    LMC_AI_CONTEXT_MAX_CHARS,
    LMC_AI_MESSAGE_MAX_CHARS,
    LMC_AI_NODE_NAME_MAX_CHARS,
    LMC_AI_NODE_WS_FRAME_MAX_BYTES,
    LMC_AI_QUEUE_MAX,
    LMC_AI_REQUEST_MESSAGES_MAX,
)


router = APIRouter(tags=["lmc-ai"])
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


def _db():
    from deploy.proxy import get_vote_db

    return get_vote_db()


def _developer_required(request: Request) -> None:
    from api.admin_console_api import developer_session_active

    if not developer_session_active(request):
        raise HTTPException(401, "未登入開發者設定。")


class ChatMessage(BaseModel):
    role: str = Field(max_length=20)
    content: str = Field(max_length=LMC_AI_MESSAGE_MAX_CHARS)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(
        min_length=1, max_length=LMC_AI_REQUEST_MESSAGES_MAX
    )
    expected_fingerprint: str = Field(min_length=64, max_length=64)
    has_history: bool


class NodeCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=LMC_AI_NODE_NAME_MAX_CHARS)


class NodeSelection(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)


def _validated_messages(body: ChatRequest) -> list[dict]:
    messages = []
    total_chars = 0
    expected_role = "user"
    for item in body.messages:
        role = str(item.role or "").strip()
        if role == "system":
            raise HTTPException(400, "Browser 不可以提供 system prompt。")
        if role not in {"user", "assistant"}:
            raise HTTPException(400, "對話角色無效。")
        if role != expected_role:
            raise HTTPException(400, "只接受完整並依次排列嘅對話回合。")
        content = str(item.content or "")
        if not content.strip():
            raise HTTPException(400, "對話訊息不可留空。")
        total_chars += len(content)
        if total_chars > LMC_AI_CONTEXT_MAX_CHARS:
            raise HTTPException(413, "送往 AI 嘅對話內容超過上限。")
        messages.append({"role": role, "content": content})
        expected_role = "assistant" if role == "user" else "user"
    if messages[-1]["role"] != "user":
        raise HTTPException(400, "最後一則訊息必須係使用者訊息。")
    return messages


async def _active_service(db) -> tuple[str, dict | None]:
    active_node_id = get_active_node_id(db)
    snapshot = await RUNTIME.snapshot(active_node_id) if active_node_id else None
    return active_node_id, snapshot


def _public_status(active_node_id: str, snapshot: dict | None) -> dict:
    if not active_node_id:
        state = "unconfigured"
    elif not snapshot or not snapshot.get("online") or not snapshot.get("ready"):
        state = "unavailable"
    elif snapshot.get("draining"):
        state = "draining"
    elif snapshot.get("busy"):
        state = "busy"
    else:
        state = "online"
    return {
        "state": state,
        "queue_length": int((snapshot or {}).get("queue_length") or 0),
        "queue_capacity": LMC_AI_QUEUE_MAX,
        "rag_enabled": False,
        "fine_tuned": False,
    }


@router.get("/api/lmc-ai/bootstrap")
async def lmc_ai_bootstrap(request: Request):
    actor_id = require_page_user_or_developer(request, "lmc_ai")
    db = _db()
    try:
        require_lmc_ai_schema(db)
        active_node_id, snapshot = await _active_service(db)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    status = _public_status(active_node_id, snapshot)
    suspension = interactive_features_suspension(request)
    if suspension.get("active"):
        status = {**status, "state": "suspended"}
    return {
        "name": LMC_AI_NAME,
        "emoji": LMC_AI_EMOJI,
        "identity": {"id": actor_id, "developer": actor_id == "developer"},
        "service": status,
        "suspension": suspension,
        "history_limits": {
            "messages": LMC_AI_BROWSER_HISTORY_MAX_MESSAGES,
            "characters": LMC_AI_BROWSER_HISTORY_MAX_CHARS,
            "user_message_characters": LMC_AI_MESSAGE_MAX_CHARS,
            "context_characters": LMC_AI_CONTEXT_MAX_CHARS,
            "request_messages": LMC_AI_REQUEST_MESSAGES_MAX,
        },
        "backend_fingerprint": (snapshot or {}).get("fingerprint"),
    }


async def _record_usage(job: ChatJob, success: bool, usage: dict, error: str) -> None:
    metadata = {
        "provider": "custom",
        "model_label": str(usage.get("model") or job.model),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "estimated_cost_usd": 0,
        "estimated_cost_hkd": 0,
        "operation_id": job.operation_id,
        "operation_stage": job.operation_stage,
        "cost_source": "local_zero_cost",
        "duration_ms": usage.get("duration_ms", 0),
    }
    try:
        await asyncio.to_thread(
            log_ai_usage,
            job.usage_user_id,
            "lmc_ai_chat",
            success,
            metadata,
            error,
            _db(),
        )
    except Exception:
        # Ledger availability must not leak DB details into or overwrite a live answer.
        return


def _sse(event: str, payload: dict) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


@router.post("/api/lmc-ai/chat")
async def lmc_ai_chat(body: ChatRequest, request: Request):
    actor_id = require_page_user_or_developer(request, "lmc_ai")
    require_interactive_features_available(request)
    if not _FINGERPRINT_RE.fullmatch(body.expected_fingerprint):
        raise HTTPException(400, "Backend fingerprint 無效。")
    messages = _validated_messages(body)
    db = _db()
    try:
        require_lmc_ai_schema(db)
        active_node_id, _snapshot = await _active_service(db)
        job, _position = await RUNTIME.submit(
            node_id=active_node_id,
            expected_fingerprint=body.expected_fingerprint,
            actor_id=actor_id,
            usage_user_id=None if actor_id == "developer" else actor_id,
            operation_stage="developer_chat" if actor_id == "developer" else "member_chat",
            messages=messages,
            has_history=body.has_history,
            finish_callback=_record_usage,
        )
    except QueueFullError as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "15"}) from exc
    except BackendChangedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (NodeUnavailableError, RuntimeError) as exc:
        raise HTTPException(503, str(exc)) from exc

    async def event_stream():
        terminal = False
        try:
            while not terminal:
                event, payload = await job.events.get()
                terminal = event in {"complete", "error"}
                yield _sse(event, payload)
        except asyncio.CancelledError:
            raise
        finally:
            if not terminal:
                await RUNTIME.cancel(active_node_id, job)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/developer/lmc-ai/nodes")
async def developer_lmc_ai_nodes(request: Request):
    _developer_required(request)
    db = _db()
    try:
        rows = list_node_rows(db)
        active_node_id = get_active_node_id(db)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    live = await RUNTIME.all_snapshots()
    nodes = []
    for row in rows:
        node_id = str(row.pop("node_id"))
        runtime = live.get(node_id) or {}
        nodes.append(
            {
                **row,
                **runtime,
                "node_id": node_id,
                "short_id": node_id[:8],
                "selected": node_id == active_node_id,
                "online": bool(runtime),
            }
        )
    return {"nodes": nodes, "active_node_id": active_node_id}


@router.post("/api/developer/lmc-ai/nodes")
async def developer_create_lmc_ai_node(body: NodeCreate, request: Request):
    _developer_required(request)
    try:
        node, raw_token = create_node(_db(), body.display_name)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400 if isinstance(exc, ValueError) else 503, str(exc)) from exc
    return {"node": {**node, "short_id": node["node_id"][:8]}, "token": raw_token}


@router.post("/api/developer/lmc-ai/nodes/{node_id}/rotate-token")
async def developer_rotate_lmc_ai_node(node_id: str, request: Request):
    _developer_required(request)
    try:
        raw_token = rotate_node_token(_db(), node_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    await RUNTIME.disconnect_node(node_id, "AI 電腦憑證已更新。")
    return {"token": raw_token}


@router.post("/api/developer/lmc-ai/nodes/{node_id}/revoke")
async def developer_revoke_lmc_ai_node(node_id: str, request: Request):
    _developer_required(request)
    try:
        revoke_node(_db(), node_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    await RUNTIME.disconnect_node(node_id, "AI 電腦已被撤銷。")
    return {"ok": True}


@router.post("/api/developer/lmc-ai/active-node")
async def developer_select_lmc_ai_node(body: NodeSelection, request: Request):
    _developer_required(request)
    db = _db()
    try:
        require_lmc_ai_schema(db)
        previous_node_id = get_active_node_id(db)
        snapshot = await RUNTIME.snapshot(body.node_id)
        if not snapshot or not snapshot.get("online") or not snapshot.get("ready"):
            raise HTTPException(409, "只可選擇已連線並完成 preflight 嘅 AI 電腦。")
        if snapshot.get("draining"):
            raise HTTPException(409, "AI 電腦正在 drain，暫時唔可以選用。")
        set_active_node_id(db, body.node_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    if previous_node_id and previous_node_id != body.node_id:
        await RUNTIME.fail_queued(
            previous_node_id,
            "Developer 已切換 AI 電腦，請用新對話再試。",
        )
    return {"ok": True}


@router.websocket("/api/lmc-ai/nodes/connect")
async def lmc_ai_node_connect(websocket: WebSocket):
    node = None
    node_id = ""
    if websocket.query_params.get("token") is not None:
        await websocket.close(code=1008, reason="URL token forbidden")
        return
    if websocket.headers.get("origin"):
        await websocket.close(code=1008, reason="browser origin forbidden")
        return
    authorization = str(websocket.headers.get("authorization") or "")
    if not authorization.startswith("Bearer "):
        await websocket.close(code=1008, reason="bearer token required")
        return
    raw_token = authorization[7:].strip()
    try:
        auth = await asyncio.to_thread(authenticate_node, _db(), raw_token)
    except RuntimeError:
        await websocket.close(code=1013, reason="schema unavailable")
        return
    if not auth:
        await websocket.close(code=1008, reason="invalid node token")
        return
    node_id = auth["node_id"]
    await websocket.accept()
    try:
        raw_hello = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        if len(raw_hello.encode("utf-8")) > LMC_AI_NODE_WS_FRAME_MAX_BYTES:
            await websocket.close(code=1009, reason="node frame too large")
            return
        hello = RUNTIME.validate_hello(json.loads(raw_hello))
        node = await RUNTIME.register(
            node_id,
            websocket,
            {"type": "hello", "protocol": 1, **hello},
            pending=True,
        )
        try:
            await asyncio.to_thread(
                update_node_hello, _db(), node_id, raw_token, hello
            )
        except LookupError:
            await websocket.close(code=1008, reason="node token invalidated")
            return
        await websocket.send_text(
            json.dumps(
                {"type": "hello.accepted", "protocol": 1, "node_id": node_id},
                separators=(",", ":"),
            )
        )
        activated = await RUNTIME.activate(
            node, ready=hello["ready"], draining=hello["draining"]
        )
        if not activated:
            await websocket.close(code=1008, reason="node connection invalidated")
            return
        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > LMC_AI_NODE_WS_FRAME_MAX_BYTES:
                await websocket.close(code=1009, reason="node frame too large")
                break
            try:
                payload = json.loads(raw)
                await RUNTIME.handle_node_message(node, payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                await websocket.close(code=1007, reason="invalid node message")
                break
    except (asyncio.TimeoutError, WebSocketDisconnect):
        pass
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        try:
            await websocket.close(code=1007, reason="invalid node message")
        except Exception:
            pass
    except Exception:
        try:
            await websocket.close(code=1011, reason="node connection failed")
        except Exception:
            pass
    finally:
        disconnected_current = False
        if node is not None:
            disconnected_current = await RUNTIME.unregister(node, "AI 電腦已離線。")
        if node_id and (node is None or disconnected_current):
            await asyncio.to_thread(mark_node_disconnected, _db(), node_id)
