"""Member SSE chat, Developer node controls and outbound node WebSocket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from typing import Literal
import uuid

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from ai_model_config import (
    LMC_AI_DEFAULT_MODE,
    LMC_AI_MODEL_SETS,
    get_lmc_ai_feature_mode,
    lmc_ai_available_model_sets,
    lmc_ai_required_models,
    resolve_lmc_ai_mode_options,
)
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
    backend_fingerprint,
)
from core.lmc_ai_store import (
    authenticate_node,
    create_node,
    get_active_node_id,
    get_model_set,
    get_thinking_enabled,
    list_node_rows,
    mark_node_disconnected,
    require_lmc_ai_schema,
    revoke_node,
    rotate_node_token,
    set_active_node_id,
    set_model_set,
    set_thinking_enabled,
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
    WORKSTATION_R2_HEALTH_PROBE_BYTES,
    WORKSTATION_UPDATE_MANIFEST_MAX_BYTES,
)
from version import APP_VERSION, REQUIRED_SCHEMA_MIGRATION


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
    # None identifies a cached pre-mode browser and follows the temporary
    # legacy global setting. New browsers always send an explicit mode.
    mode: Literal["daily", "complex", "deep", "fast", "thinking"] | None = None


class NodeCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=LMC_AI_NODE_NAME_MAX_CHARS)


class NodeSelection(BaseModel):
    node_id: str = Field(max_length=64)


class ModelSetSelection(BaseModel):
    model_set: str = Field(min_length=1, max_length=40)


class ThinkingSetting(BaseModel):
    enabled: bool


class WorkstationR2ProbeStart(BaseModel):
    sha256: str = Field(min_length=64, max_length=64)
    byte_size: int = Field(
        ge=WORKSTATION_R2_HEALTH_PROBE_BYTES,
        le=WORKSTATION_R2_HEALTH_PROBE_BYTES,
    )


class WorkstationR2ProbeFinish(BaseModel):
    claim: str = Field(min_length=40, max_length=4_096)


def _authenticated_node_request(request: Request) -> dict:
    authorization = str(request.headers.get("authorization") or "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Node bearer token is required.")
    raw_token = authorization[7:].strip()
    if not raw_token or len(raw_token) > 512:
        raise HTTPException(401, "Node bearer token is invalid.")
    try:
        auth = authenticate_node(_db(), raw_token)
    except RuntimeError as exc:
        raise HTTPException(503, "Node authentication is unavailable.") from exc
    if not auth:
        raise HTTPException(401, "Node bearer token is invalid.")
    return auth


@router.get("/api/lmc-ai/workstation/releases/{channel}")
def workstation_release_manifest(channel: str, request: Request):
    _authenticated_node_request(request)
    if channel not in {"stable", "candidate"}:
        raise HTTPException(404, "Release channel does not exist.")
    from core import r2_storage
    from workstation.manager.release_manifest import validate_manifest

    path = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / f"workstation_release_{channel}.json"
    )
    try:
        raw = path.read_bytes()
        if len(raw) > WORKSTATION_UPDATE_MANIFEST_MAX_BYTES:
            raise ValueError("manifest too large")
        signed = json.loads(raw)
        if not isinstance(signed, dict) or set(signed) != {"manifest", "signature"}:
            raise ValueError("invalid signed manifest")
        manifest = validate_manifest(signed["manifest"])
        if manifest["channel"] != channel:
            raise ValueError("channel mismatch")
        release = manifest["components"]["release_archive"]
        url = r2_storage.presign_get(
            release["r2_key"], mime_type="application/gzip",
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "No signed Workstation release is published.") from exc
    except Exception as exc:
        raise HTTPException(503, "Published Workstation release is invalid.") from exc
    return JSONResponse(
        {**signed, "downloads": {"release_archive": url}},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/lmc-ai/workstation/artifacts/{channel}/{component_name}")
def workstation_signed_artifact(
    channel: str, component_name: str, request: Request,
):
    _authenticated_node_request(request)
    if channel not in {"stable", "candidate"} or component_name not in {
        "model_bundle", "rag_bundle",
    }:
        raise HTTPException(404, "Signed artifact does not exist.")
    from core import r2_storage
    from workstation.manager.release_manifest import validate_manifest

    path = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / f"workstation_release_{channel}.json"
    )
    try:
        raw = path.read_bytes()
        if len(raw) > WORKSTATION_UPDATE_MANIFEST_MAX_BYTES:
            raise ValueError("manifest too large")
        signed = json.loads(raw)
        if not isinstance(signed, dict) or set(signed) != {"manifest", "signature"}:
            raise ValueError("signed manifest invalid")
        manifest = validate_manifest(signed["manifest"])
        if manifest["channel"] != channel:
            raise ValueError("channel mismatch")
        component = manifest["components"][component_name]
        url = r2_storage.presign_get(
            component["r2_key"],
            mime_type=(
                "application/json"
                if component_name == "model_bundle"
                else "application/gzip"
            ),
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "No signed artifact is published.") from exc
    except Exception as exc:
        raise HTTPException(503, "Published signed artifact is invalid.") from exc
    return JSONResponse({
        **signed,
        "component": component_name,
        "download_url": url,
    }, headers={"Cache-Control": "no-store"})


@router.post("/api/lmc-ai/workstation/health/r2/start")
def workstation_r2_health_start(body: WorkstationR2ProbeStart, request: Request):
    auth = _authenticated_node_request(request)
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret

    digest = str(body.sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HTTPException(400, "Health probe hash is invalid.")
    if not r2_storage.configured():
        raise HTTPException(503, "R2 is unavailable.")
    secret = _get_relay_cookie_secret()
    if not secret:
        raise HTTPException(503, "Health probe signing is unavailable.")
    nonce = uuid.uuid4().hex
    key = f"pending/workstation-health/{auth['node_id']}/{nonce}.bin"
    db = _db()
    try:
        reserved = r2_storage.reserve_workstation_r2_health_probe(
            db,
            intent_id=nonce,
            node_id=auth["node_id"],
            object_key=key,
            sha256=digest,
            byte_size=body.byte_size,
        )
    except Exception as exc:
        raise HTTPException(503, "R2 health probe reservation is unavailable.") from exc
    if not reserved:
        raise HTTPException(409, "A Workstation R2 health probe is already pending.")
    try:
        claim = r2_storage.sign_upload_claim({
            "kind": "workstation_r2_health",
            "intent_id": nonce,
            "node_id": auth["node_id"],
            "key": key,
            "sha256": digest,
            "byte_size": body.byte_size,
        }, secret)
        upload = r2_storage.presign_put(
            key, "application/octet-stream", digest, body.byte_size,
        )
        download = r2_storage.presign_get(
            key, mime_type="application/octet-stream",
        )
    except Exception as exc:
        r2_storage.delete_workstation_r2_health_probe(
            db,
            intent_id=nonce,
            node_id=auth["node_id"],
            object_key=key,
        )
        raise HTTPException(503, "R2 health probe URLs are unavailable.") from exc
    return JSONResponse({
        "claim": claim,
        "upload": {
            "url": upload,
            "headers": {
                "Content-Type": "application/octet-stream",
                "x-amz-meta-sha256": digest,
            },
        },
        "download_url": download,
    }, headers={"Cache-Control": "no-store"})


@router.post("/api/lmc-ai/workstation/health/r2/finish")
def workstation_r2_health_finish(body: WorkstationR2ProbeFinish, request: Request):
    auth = _authenticated_node_request(request)
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret

    claim = r2_storage.verify_upload_claim(body.claim, _get_relay_cookie_secret())
    intent_id = str((claim or {}).get("intent_id") or "")
    expected_key = (
        f"pending/workstation-health/{auth['node_id']}/{intent_id}.bin"
    )
    if (
        not claim
        or claim.get("kind") != "workstation_r2_health"
        or claim.get("node_id") != auth["node_id"]
        or not re.fullmatch(r"[0-9a-f]{32}", intent_id)
        or str(claim.get("key") or "") != expected_key
        or int(claim.get("byte_size") or 0) != WORKSTATION_R2_HEALTH_PROBE_BYTES
        or not re.fullmatch(r"[0-9a-f]{64}", str(claim.get("sha256") or ""))
    ):
        raise HTTPException(400, "R2 health probe claim is invalid.")
    key = str(claim["key"])
    db = _db()
    try:
        intent = r2_storage.get_workstation_r2_health_probe(
            db, intent_id=intent_id, node_id=auth["node_id"],
        )
    except Exception as exc:
        raise HTTPException(503, "R2 health probe reservation is unavailable.") from exc
    if (
        not intent
        or str(intent.get("object_key") or "") != key
        or str(intent.get("sha256") or "").lower()
        != str(claim["sha256"]).lower()
        or int(intent.get("byte_size") or 0) != int(claim["byte_size"])
    ):
        raise HTTPException(409, "R2 health probe reservation does not match.")
    verified = False
    try:
        remote = r2_storage.head(key)
        verified = (
            int(remote.get("ContentLength") or 0) == int(claim["byte_size"])
            and str((remote.get("Metadata") or {}).get("sha256") or "").lower()
            == str(claim["sha256"]).lower()
        )
    except Exception:
        verified = False
    deleted = r2_storage.delete_workstation_r2_health_probe(
        db,
        intent_id=intent_id,
        node_id=auth["node_id"],
        object_key=key,
    )
    if not verified or not deleted:
        raise HTTPException(409, "R2 health probe verification or deletion failed.")
    return {"ok": True, "deleted": True}


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


def _resolve_thinking_enabled(
    mode: str | None, legacy_enabled: bool, model_set: str | None = None
) -> bool:
    return _resolve_chat_mode(mode, legacy_enabled, model_set)[1]["thinking"]


def _resolve_chat_mode(
    mode: str | None, legacy_enabled: bool, model_set: str | None = None
) -> tuple[str, dict]:
    options = resolve_lmc_ai_mode_options(model_set)
    if mode is None:
        matching = [
            mode_id
            for mode_id, config in options.items()
            if bool(config["thinking"]) is bool(legacy_enabled)
        ]
        selected = (
            LMC_AI_DEFAULT_MODE
            if LMC_AI_DEFAULT_MODE in matching
            else matching[0]
            if matching
            else LMC_AI_DEFAULT_MODE
        )
    else:
        selected = {"complex": "daily", "thinking": "deep"}.get(mode, mode)
    if selected not in options:
        selected = LMC_AI_DEFAULT_MODE
    return selected, dict(options[selected])


async def _active_service(
    db,
) -> tuple[str, dict | None, bool, dict[str, str], str]:
    active_node_id = get_active_node_id(db)
    model_set = get_model_set(db)
    mode_options = resolve_lmc_ai_mode_options(model_set)
    legacy_thinking_enabled = get_thinking_enabled(db)
    snapshot = await RUNTIME.snapshot(active_node_id) if active_node_id else None
    fingerprints = {}
    if snapshot:
        available_models = set(snapshot.get("models") or [snapshot.get("model")])
        for mode, config in mode_options.items():
            if config["model"] in available_models:
                fingerprints[mode] = backend_fingerprint(
                    active_node_id, config["model"], config["thinking"],
                    model_digest=(snapshot.get("model_digests") or {}).get(config["model"], ""),
                )
        # Cached pre-mode clients continue to work during the node rollout.
        if "daily" in fingerprints:
            fingerprints["complex"] = fingerprints["daily"]
        if "deep" in fingerprints:
            fingerprints["thinking"] = fingerprints["deep"]
    return active_node_id, snapshot, legacy_thinking_enabled, fingerprints, model_set


def _public_status(
    active_node_id: str, snapshot: dict | None, model_set: str
) -> dict:
    if not active_node_id:
        state = "unconfigured"
    elif not snapshot or not snapshot.get("online") or not snapshot.get("ready"):
        state = "unavailable"
    elif model_set not in lmc_ai_available_model_sets(snapshot.get("models")):
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
        "model_set": model_set,
        "model_set_label": LMC_AI_MODEL_SETS[model_set]["label"],
        "modes": [
            {
                "id": mode,
                "label": config["label"],
                "model": config["model"],
                "thinking": bool(config["thinking"]),
                "available": config["model"] in set(
                    (snapshot or {}).get("models") or [(snapshot or {}).get("model")]
                ),
            }
            for mode, config in resolve_lmc_ai_mode_options(model_set).items()
        ],
    }


def _model_set_options(
    snapshot: dict | None = None, *, available_sets: object = None
) -> list[dict]:
    available = (
        set(lmc_ai_available_model_sets((snapshot or {}).get("models") or []))
        if available_sets is None
        else {str(item) for item in (available_sets or ())}
    )
    return [
        {
            "id": model_set,
            "label": config["label"],
            "available": model_set in available,
            "models": list(lmc_ai_required_models(model_set)),
        }
        for model_set, config in LMC_AI_MODEL_SETS.items()
    ]


async def _public_nodes(db, active_node_id: str) -> list[dict]:
    rows = list_node_rows(db)
    live = await RUNTIME.all_snapshots()
    nodes = []
    for source in rows:
        row = dict(source)
        node_id = str(row.get("node_id") or "")
        runtime = live.get(node_id) or {}
        if runtime.get("draining"):
            state = "draining"
        elif runtime.get("busy"):
            state = "busy"
        elif runtime.get("ready"):
            state = "online"
        else:
            state = "offline"
        nodes.append({
            "name": str(runtime.get("name") or row.get("display_name") or "AI 電腦"),
            "state": state,
            "selected": node_id == active_node_id,
            "queue_length": int(runtime.get("queue_length") or 0),
            "models": list(runtime.get("models") or []),
            "last_connected_at": row.get("last_connected_at"),
            "last_disconnected_at": row.get("last_disconnected_at"),
        })
    return nodes


@router.get("/api/lmc-ai/bootstrap")
async def lmc_ai_bootstrap(request: Request):
    actor_id = require_page_user_or_developer(request, "lmc_ai")
    db = _db()
    try:
        require_lmc_ai_schema(db)
        (
            active_node_id,
            snapshot,
            legacy_thinking_enabled,
            fingerprints,
            model_set,
        ) = (
            await _active_service(db)
        )
        nodes = await _public_nodes(db, active_node_id)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    status = _public_status(active_node_id, snapshot, model_set)
    suspension = interactive_features_suspension(request)
    if suspension.get("active"):
        status = {**status, "state": "suspended"}
    return {
        "name": LMC_AI_NAME,
        "emoji": LMC_AI_EMOJI,
        "identity": {"id": actor_id, "developer": actor_id == "developer"},
        "service": status,
        "default_mode": get_lmc_ai_feature_mode("lmc_ai"),
        "nodes": nodes,
        "suspension": suspension,
        "history_limits": {
            "messages": LMC_AI_BROWSER_HISTORY_MAX_MESSAGES,
            "characters": LMC_AI_BROWSER_HISTORY_MAX_CHARS,
            "user_message_characters": LMC_AI_MESSAGE_MAX_CHARS,
            "context_characters": LMC_AI_CONTEXT_MAX_CHARS,
            "request_messages": LMC_AI_REQUEST_MESSAGES_MAX,
        },
        # Cached pre-mode browsers use the singular fingerprint and continue
        # following the legacy global setting during this release transition.
        "backend_fingerprint": fingerprints.get(
            _resolve_chat_mode(
                None, legacy_thinking_enabled, model_set
            )[0]
        ),
        "backend_fingerprints": fingerprints,
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
        (
            active_node_id,
            _snapshot,
            legacy_thinking_enabled,
            _fingerprints,
            model_set,
        ) = (
            await _active_service(db)
        )
        _mode, mode_config = _resolve_chat_mode(
            body.mode, legacy_thinking_enabled, model_set
        )
        job, _position = await RUNTIME.submit(
            node_id=active_node_id,
            expected_fingerprint=body.expected_fingerprint,
            actor_id=actor_id,
            usage_user_id=None if actor_id == "developer" else actor_id,
            operation_stage="developer_chat" if actor_id == "developer" else "member_chat",
            messages=messages,
            has_history=body.has_history,
            model=mode_config["model"],
            thinking_enabled=mode_config["thinking"],
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
        model_set = get_model_set(db)
        thinking_enabled = get_thinking_enabled(db)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    live = await RUNTIME.all_snapshots()
    nodes = []
    for row in rows:
        node_id = str(row.pop("node_id"))
        runtime = live.get(node_id) or {}
        compatible_model_sets = list(lmc_ai_available_model_sets(
            runtime.get("models") or []
        ))
        nodes.append(
            {
                **row,
                **runtime,
                "node_id": node_id,
                "short_id": node_id[:8],
                "selected": node_id == active_node_id,
                "online": bool(runtime),
                "compatible_model_sets": compatible_model_sets,
            }
        )
    active_snapshot = live.get(active_node_id) or None
    available_sets = None
    if not active_node_id:
        available_sets = {
            model_set_id
            for runtime in live.values()
            if runtime.get("ready") and not runtime.get("draining")
            for model_set_id in lmc_ai_available_model_sets(runtime.get("models"))
        }
    return {
        "nodes": nodes,
        "active_node_id": active_node_id,
        "model_set": model_set,
        "model_sets": _model_set_options(
            active_snapshot, available_sets=available_sets
        ),
        # Temporary response compatibility for cached 4.9.3 Developer pages.
        "thinking_enabled": thinking_enabled,
    }


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
    previous_node_id = ""
    selected_node_id = str(body.node_id or "").strip()
    blocked_previous = False
    try:
        require_lmc_ai_schema(db)
        previous_node_id = get_active_node_id(db)
        model_set = get_model_set(db)
        if selected_node_id:
            snapshot = await RUNTIME.snapshot(selected_node_id)
            if not snapshot or not snapshot.get("online") or not snapshot.get("ready"):
                raise HTTPException(409, "只可選擇已連線並完成 preflight 嘅 AI 電腦。")
            if snapshot.get("draining"):
                raise HTTPException(409, "AI 電腦正在 drain，暫時唔可以選用。")
            if model_set not in lmc_ai_available_model_sets(snapshot.get("models")):
                raise HTTPException(
                    409,
                    "呢部 AI 電腦未完成目前模型組合嘅全部 preflight。",
                )
        if previous_node_id and previous_node_id != selected_node_id:
            await RUNTIME.block_new_jobs(previous_node_id)
            blocked_previous = True
        set_active_node_id(db, selected_node_id)
    except LookupError as exc:
        if blocked_previous:
            await RUNTIME.allow_new_jobs(previous_node_id)
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        if blocked_previous:
            await RUNTIME.allow_new_jobs(previous_node_id)
        raise HTTPException(503, str(exc)) from exc
    except Exception:
        if blocked_previous:
            await RUNTIME.allow_new_jobs(previous_node_id)
        raise
    if selected_node_id:
        await RUNTIME.allow_new_jobs(selected_node_id)
    if previous_node_id and previous_node_id != selected_node_id:
        await RUNTIME.fail_queued(
            previous_node_id,
            "Developer 已取消或切換 AI 電腦，請用新對話再試。",
        )
    return {"ok": True, "active_node_id": selected_node_id}


@router.post("/api/developer/lmc-ai/model-set")
async def developer_set_lmc_ai_model_set(
    body: ModelSetSelection, request: Request
):
    _developer_required(request)
    db = _db()
    try:
        require_lmc_ai_schema(db)
        if body.model_set not in LMC_AI_MODEL_SETS:
            raise HTTPException(400, "不支援的自家 AI 模型組合。")
        active_node_id = get_active_node_id(db)
        if active_node_id:
            candidates = [await RUNTIME.snapshot(active_node_id)]
        else:
            candidates = list((await RUNTIME.all_snapshots()).values())
        compatible = any(
            snapshot
            and snapshot.get("online")
            and snapshot.get("ready")
            and not snapshot.get("draining")
            and body.model_set in lmc_ai_available_model_sets(snapshot.get("models"))
            for snapshot in candidates
        )
        if not compatible:
            raise HTTPException(
                409,
                "目前未有可用電腦完成所選模型組合嘅全部 preflight。",
            )
        set_model_set(db, body.model_set)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "model_set": body.model_set}


@router.post("/api/developer/lmc-ai/thinking")
async def developer_set_lmc_ai_thinking(body: ThinkingSetting, request: Request):
    """Compatibility endpoint for cached 4.9.3 Developer pages."""
    _developer_required(request)
    try:
        set_thinking_enabled(_db(), body.enabled)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "thinking_enabled": body.enabled}


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
        hello_payload = json.loads(raw_hello)
        hello = RUNTIME.validate_hello(hello_payload)
        node = await RUNTIME.register(
            node_id,
            websocket,
            hello_payload,
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
                {
                    "type": "hello.accepted",
                    "protocol": hello["protocol"],
                    "node_id": node_id,
                    "website_version": APP_VERSION,
                    "database_migration_requirement": REQUIRED_SCHEMA_MIGRATION,
                },
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
