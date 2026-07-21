"""Shared bounded text client for the selected outbound local-AI node."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from typing import Callable

from ai_model_config import (
    LMC_AI_DEFAULT_MODE,
    LMC_AI_MODEL_SETS,
    lmc_ai_available_model_sets,
    resolve_lmc_ai_mode_options,
)
from core.lmc_ai_runtime import (
    BackendChangedError,
    NodeUnavailableError,
    QueueFullError,
    RUNTIME,
)
from core.lmc_ai_store import get_active_node_id, get_model_set, require_lmc_ai_schema
from system_limits import LMC_AI_QUEUE_MAX


class LocalAIError(RuntimeError):
    pass


LOCAL_AI_UNSELECTED_MESSAGE = (
    "尚未選用自家 AI 電腦，請聯絡 Developer 設定。"
)
LOCAL_AI_OFFLINE_MESSAGE = "目前選用的自家 AI 電腦離線。"
LOCAL_AI_NOT_READY_MESSAGE = "目前選用的自家 AI 電腦尚未完成準備。"
LOCAL_AI_DRAINING_MESSAGE = "目前選用的自家 AI 電腦正在暫停接單。"


def resolve_mode(mode: str | None, model_set: str | None = None) -> tuple[str, dict]:
    options = resolve_lmc_ai_mode_options(model_set)
    selected = str(mode or LMC_AI_DEFAULT_MODE).strip()
    selected = {"complex": "daily", "thinking": "deep"}.get(selected, selected)
    if selected not in options:
        raise LocalAIError("不支援的自家 AI 回答模式。")
    return selected, dict(options[selected])


async def _noop_finish(_job, _success: bool, _usage: dict, _error: str) -> None:
    return None


async def _generate_on_runtime_loop(
    *,
    node_id: str,
    actor_id: str,
    system_prompt: str,
    user_prompt: str,
    mode: str,
    model_set: str,
    operation_stage: str,
    on_provider_attempt: Callable[[], None] | None,
) -> tuple[str, dict]:
    _selected, mode_config = resolve_mode(mode, model_set)
    try:
        job, _position = await RUNTIME.submit(
            node_id=node_id,
            expected_fingerprint="",
            actor_id=str(actor_id or "local-feature"),
            usage_user_id=None,
            operation_stage=str(operation_stage or "local_feature")[:80],
            messages=[
                {"role": "system", "content": str(system_prompt or "")},
                {"role": "user", "content": str(user_prompt or "")},
            ],
            finish_callback=_noop_finish,
            has_history=False,
            model=str(mode_config["model"]),
            thinking_enabled=bool(mode_config["thinking"]),
        )
    except (QueueFullError, BackendChangedError, NodeUnavailableError) as exc:
        raise LocalAIError(str(exc)) from exc

    chunks: list[str] = []
    attempted = False
    terminal = False
    try:
        while True:
            event, payload = await job.events.get()
            if event == "status" and payload.get("state") == "generating":
                if not attempted and on_provider_attempt is not None:
                    on_provider_attempt()
                attempted = True
            elif event == "delta":
                chunks.append(str(payload.get("text") or ""))
            elif event == "complete":
                terminal = True
                result = "".join(chunks).strip()
                if not result:
                    raise LocalAIError("自家 AI 未有產生有效回覆。")
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                return result, {
                    **usage,
                    "search_calls": 0,
                    "audio_tokens": 0,
                    "cost_source": "local_zero_cost",
                }
            elif event == "error":
                terminal = True
                raise LocalAIError(str(payload.get("message") or "自家 AI 未能完成今次回覆。"))
    finally:
        if not terminal:
            await RUNTIME.cancel(node_id, job)


async def _await_owner_loop(coroutine):
    owner = RUNTIME.owner_loop
    current = asyncio.get_running_loop()
    if owner is None:
        coroutine.close()
        raise LocalAIError("自家 AI 暫時未有電腦連線。")
    if owner is current:
        return await coroutine
    if owner.is_closed():
        coroutine.close()
        raise LocalAIError("自家 AI runtime 暫時未能提供服務。")
    future: Future = asyncio.run_coroutine_threadsafe(coroutine, owner)
    return await asyncio.wrap_future(future)


def _selected_service(db) -> tuple[str, str]:
    require_lmc_ai_schema(db)
    return str(get_active_node_id(db) or ""), get_model_set(db)


async def _availability(db) -> tuple[dict, str]:
    """Return a public-safe selected-node status and its private node id."""

    try:
        node_id, model_set = await asyncio.to_thread(_selected_service, db)
    except RuntimeError as exc:
        return {
            "available": False,
            "selected": False,
            "state": "unavailable",
            "busy": False,
            "queue_length": 0,
            "message": str(exc),
            "modes": [],
        }, ""
    mode_options = resolve_lmc_ai_mode_options(model_set)
    model_set_label = str(LMC_AI_MODEL_SETS[model_set]["label"])
    if not node_id:
        return {
            "available": False,
            "selected": False,
            "state": "unconfigured",
            "busy": False,
            "queue_length": 0,
            "message": LOCAL_AI_UNSELECTED_MESSAGE,
            "model_set": model_set,
            "model_set_label": model_set_label,
            "modes": [
                {
                    "id": mode,
                    "label": config["label"],
                    "model": config["model"],
                    "thinking": bool(config["thinking"]),
                    "available": False,
                    "message": LOCAL_AI_UNSELECTED_MESSAGE,
                }
                for mode, config in mode_options.items()
            ],
        }, ""

    snapshot = None
    owner = RUNTIME.owner_loop
    if owner is not None and not owner.is_closed():
        try:
            snapshot = await _await_owner_loop(RUNTIME.snapshot(node_id))
        except LocalAIError:
            snapshot = None
    if not snapshot or not snapshot.get("online"):
        state = "offline"
        message = LOCAL_AI_OFFLINE_MESSAGE
        service_available = False
    elif not snapshot.get("ready"):
        state = "unavailable"
        message = LOCAL_AI_NOT_READY_MESSAGE
        service_available = False
    elif model_set not in lmc_ai_available_model_sets(snapshot.get("models")):
        state = "unavailable"
        message = "目前選用的自家 AI 電腦未完成所選模型組合 preflight。"
        service_available = False
    elif snapshot.get("draining"):
        state = "draining"
        message = LOCAL_AI_DRAINING_MESSAGE
        service_available = False
    elif (
        snapshot.get("busy")
        and int(snapshot.get("queue_length") or 0) >= LMC_AI_QUEUE_MAX
    ):
        state = "full"
        message = "自家 AI 而家排隊已滿，請稍後再試。"
        service_available = False
    elif snapshot.get("busy"):
        state = "busy"
        message = "自家 AI 正在處理工作，可以排隊等候。"
        service_available = True
    else:
        state = "online"
        message = "自家 AI 已選用並在線。"
        service_available = True

    available_models = set(
        (snapshot or {}).get("models") or [(snapshot or {}).get("model")]
    )
    modes = []
    for mode, config in mode_options.items():
        mode_available = service_available and config["model"] in available_models
        if not service_available:
            mode_message = message
        elif not mode_available:
            mode_message = (
                f"目前選用的自家 AI 電腦未提供「{config['label']}」模式。"
            )
        else:
            mode_message = message
        modes.append({
            "id": mode,
            "label": config["label"],
            "model": config["model"],
            "thinking": bool(config["thinking"]),
            "available": mode_available,
            "message": mode_message,
        })
    return {
        "available": service_available,
        "selected": True,
        "state": state,
        "busy": bool((snapshot or {}).get("busy")),
        "queue_length": int((snapshot or {}).get("queue_length") or 0),
        "queue_capacity": LMC_AI_QUEUE_MAX,
        "message": message,
        "model_set": model_set,
        "model_set_label": model_set_label,
        "modes": modes,
    }, node_id


async def local_ai_availability(db) -> dict:
    """Inspect the manually selected node without exposing its identifier."""

    status, _node_id = await _availability(db)
    return status


async def generate_local_text(
    db,
    *,
    actor_id: str,
    system_prompt: str,
    user_prompt: str,
    mode: str = LMC_AI_DEFAULT_MODE,
    operation_stage: str = "local_feature",
    on_provider_attempt: Callable[[], None] | None = None,
) -> tuple[str, dict]:
    """Generate on the manually selected node without any cloud fallback."""

    status, node_id = await _availability(db)
    model_set = str(status.get("model_set") or "")
    selected_mode, _mode_config = resolve_mode(mode, model_set)
    mode_status = next(
        (item for item in status["modes"] if item["id"] == selected_mode),
        None,
    )
    if not status["available"]:
        raise LocalAIError(status["message"])
    if not mode_status or not mode_status["available"]:
        raise LocalAIError(
            (mode_status or {}).get("message")
            or "所選回答模式未能喺目前 AI 電腦使用。"
        )
    return await _await_owner_loop(_generate_on_runtime_loop(
        node_id=node_id,
        actor_id=actor_id,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        mode=selected_mode,
        model_set=model_set,
        operation_stage=operation_stage,
        on_provider_attempt=on_provider_attempt,
    ))
