"""Process-local WebSocket, fingerprint and FIFO queue runtime for local AI."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import secrets
import re
import time
from typing import Awaitable, Callable

from ai_model_config import (
    LMC_AI_CONTEXT_LENGTH,
    LMC_AI_DEFAULT_MODEL,
    LMC_AI_MODE_OPTIONS,
    LMC_AI_MODEL_PROFILE_VERSION,
)
from ai_name import LMC_AI_EMOJI, LMC_AI_NAME
from system_limits import (
    LMC_AI_CONTEXT_MAX_CHARS,
    LMC_AI_HEARTBEAT_TIMEOUT_SECONDS,
    LMC_AI_OUTPUT_MAX_BYTES,
    LMC_AI_QUEUE_MAX,
    LMC_AI_REQUEST_TIMEOUT_SECONDS,
)


PERSONA_DIR = Path(__file__).resolve().parents[1] / "local_ai" / "persona"
PERSONA_FILES = ("AGENTS.md", "SOUL.md", "IDENTITY.md")
PROTOCOL_VERSION = 1
FinishCallback = Callable[["ChatJob", bool, dict, str], Awaitable[None]]
StartCallback = Callable[["ChatJob"], Awaitable[None]]


def compile_persona() -> tuple[str, str]:
    sections = []
    for filename in PERSONA_FILES:
        sections.append((PERSONA_DIR / filename).read_text(encoding="utf-8").strip())
    prompt = "\n\n".join(sections)
    prompt = prompt.replace("{{LMC_AI_NAME}}", LMC_AI_NAME)
    prompt = prompt.replace("{{LMC_AI_EMOJI}}", LMC_AI_EMOJI)
    if "{{" in prompt or "}}" in prompt:
        raise RuntimeError("local AI persona contains an unresolved placeholder")
    prompt += (
        "\n\n# Runtime capabilities\n"
        "- 對話：已啟用\n"
        "- RAG：未啟用\n"
        "- Fine-tune：未啟用\n"
        "- 圖片、工具及上網：未啟用\n"
    )
    return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()


SYSTEM_PROMPT, PERSONA_VERSION = compile_persona()


def backend_fingerprint(
    node_id: str, model: str, thinking_enabled: bool = False, *, model_digest: str = ""
) -> str:
    thinking_mode = "thinking" if thinking_enabled else "non-thinking"
    material = f"{node_id}\n{model}\n{model_digest}\n{PERSONA_VERSION}\n{thinking_mode}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


@dataclass
class ChatJob:
    operation_id: str
    actor_id: str
    usage_user_id: str | None
    operation_stage: str
    messages: list[dict]
    fingerprint: str
    model: str
    has_history: bool
    thinking_enabled: bool
    output_max_bytes: int
    finish_callback: FinishCallback
    start_callback: StartCallback | None
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    created_monotonic: float = field(default_factory=time.monotonic)
    provider_attempted: bool = False
    output_bytes: int = 0
    collected_text: str = ""
    cancelled: bool = False
    forced_error: str = ""
    finished: bool = False
    terminal_published: bool = False
    timeout_task: asyncio.Task | None = None


@dataclass
class ConnectedNode:
    node_id: str
    websocket: object
    generation: str
    name: str
    runtime: str
    runtime_version: str
    model: str
    models: tuple[str, ...]
    model_digests: dict[str, str]
    capabilities: dict
    ready: bool
    draining: bool
    connected_monotonic: float = field(default_factory=time.monotonic)
    last_heartbeat_monotonic: float = field(default_factory=time.monotonic)
    active: ChatJob | None = None
    queue: deque[ChatJob] = field(default_factory=deque)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    monitor_task: asyncio.Task | None = None

    @property
    def fingerprint(self) -> str:
        return backend_fingerprint(
            self.node_id, self.model, model_digest=self.model_digests.get(self.model, "")
        )


class QueueFullError(RuntimeError):
    pass


class BackendChangedError(RuntimeError):
    pass


class NodeUnavailableError(RuntimeError):
    pass


class LocalAIRuntime:
    def __init__(self):
        self._nodes: dict[str, ConnectedNode] = {}
        self._blocked_new_jobs: set[str] = set()
        self._lock = asyncio.Lock()
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    @property
    def owner_loop(self) -> asyncio.AbstractEventLoop | None:
        return self._owner_loop

    @staticmethod
    def validate_hello(payload: object) -> dict:
        if not isinstance(payload, dict) or payload.get("type") != "hello":
            raise ValueError("first node message must be hello")
        if payload.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("unsupported node protocol")
        if payload.get("model_profile_version") != LMC_AI_MODEL_PROFILE_VERSION:
            raise ValueError("unsupported node model profile")
        capabilities = payload.get("capabilities")
        required = {
            "chat": True,
            "rag": False,
            "fine_tuned": False,
            "thinking_control": True,
        }
        if not isinstance(capabilities, dict) or any(
            capabilities.get(key) is not value for key, value in required.items()
        ):
            raise ValueError("unsupported node capabilities")
        name = str(payload.get("name") or "").strip()
        model = str(payload.get("model") or "").strip()
        if not name or not model:
            raise ValueError("node name and effective model are required")
        allowed_models = {
            str(config["model"]) for config in LMC_AI_MODE_OPTIONS.values()
        }
        raw_models = payload.get("models")
        if raw_models is None:
            models = (model,)
        elif isinstance(raw_models, (list, tuple)):
            models = tuple(dict.fromkeys(
                str(item or "").strip() for item in raw_models
                if str(item or "").strip()
            ))
        else:
            raise ValueError("node models must be a list")
        if (
            not models
            or model not in models
            or len(models) > len(allowed_models)
            or any(item not in allowed_models for item in models)
        ):
            raise ValueError("node advertised an unsupported model set")
        if model != LMC_AI_DEFAULT_MODEL or LMC_AI_DEFAULT_MODEL not in models:
            raise ValueError("node model profile is missing required default model")
        raw_digests = payload.get("model_digests")
        if raw_digests is None:
            model_digests = {}
        elif isinstance(raw_digests, dict):
            model_digests = {
                str(key): str(value).lower()
                for key, value in raw_digests.items()
                if str(key) in models
                and re.fullmatch(r"[0-9a-fA-F]{64}", str(value or ""))
            }
            if set(model_digests) != set(raw_digests) or set(model_digests) != set(models):
                raise ValueError("node model digests do not match advertised models")
        else:
            raise ValueError("node model digests must be an object")
        return {
            "name": name,
            "runtime": str(payload.get("runtime") or "")[:80],
            "runtime_version": str(payload.get("runtime_version") or "")[:80],
            "model": model[:200],
            "models": tuple(item[:200] for item in models),
            "model_digests": model_digests,
            "capabilities": required,
            "ready": bool(payload.get("ready")),
            "draining": bool(payload.get("draining")),
        }

    async def register(
        self, node_id: str, websocket, hello: dict, *, pending: bool = False
    ) -> ConnectedNode:
        clean = self.validate_hello(hello)
        self._owner_loop = asyncio.get_running_loop()
        if pending:
            clean["ready"] = False
        node = ConnectedNode(
            node_id=node_id,
            websocket=websocket,
            generation=secrets.token_hex(8),
            **clean,
        )
        old = None
        async with self._lock:
            old = self._nodes.get(node_id)
            self._nodes[node_id] = node
        if old is not None:
            await self._fail_node_jobs(old, "AI 電腦連線已被新連線取代。")
            try:
                await old.websocket.close(code=1012, reason="connection replaced")
            except Exception:
                pass
        node.monitor_task = asyncio.create_task(self._heartbeat_monitor(node))
        return node

    async def activate(
        self, node: ConnectedNode, *, ready: bool, draining: bool
    ) -> bool:
        """Publish a pending connection only if it survived token revalidation."""
        async with self._lock:
            if self._nodes.get(node.node_id) is not node:
                return False
            node.ready = bool(ready)
            node.draining = bool(draining)
            node.last_heartbeat_monotonic = time.monotonic()
            return True

    async def unregister(self, node: ConnectedNode, reason: str) -> bool:
        removed = False
        async with self._lock:
            if self._nodes.get(node.node_id) is node:
                self._nodes.pop(node.node_id, None)
                removed = True
        if node.monitor_task and node.monitor_task is not asyncio.current_task():
            node.monitor_task.cancel()
        if removed:
            await self._fail_node_jobs(node, reason)
        return removed

    async def disconnect_node(self, node_id: str, reason: str) -> None:
        async with self._lock:
            node = self._nodes.pop(node_id, None)
        if node is None:
            return
        await self._fail_node_jobs(node, reason)
        try:
            await node.websocket.close(code=1008, reason="node revoked")
        except Exception:
            pass

    async def fail_queued(self, node_id: str, reason: str) -> None:
        """Keep an in-flight answer but stop queued work after active-node switch."""
        async with self._lock:
            node = self._nodes.get(node_id)
        if node is not None:
            await self._fail_queued(node, reason)

    async def block_new_jobs(self, node_id: str) -> None:
        async with self._lock:
            self._blocked_new_jobs.add(str(node_id))

    async def allow_new_jobs(self, node_id: str) -> None:
        async with self._lock:
            self._blocked_new_jobs.discard(str(node_id))

    async def _heartbeat_monitor(self, node: ConnectedNode) -> None:
        try:
            while True:
                await asyncio.sleep(min(5, LMC_AI_HEARTBEAT_TIMEOUT_SECONDS / 3))
                if time.monotonic() - node.last_heartbeat_monotonic > LMC_AI_HEARTBEAT_TIMEOUT_SECONDS:
                    try:
                        await node.websocket.close(code=1011, reason="heartbeat timeout")
                    finally:
                        await self.unregister(node, "AI 電腦心跳逾時。")
                    return
        except asyncio.CancelledError:
            return

    async def snapshot(self, node_id: str) -> dict | None:
        async with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                return None
            return {
                "online": True,
                "ready": node.ready,
                "draining": node.draining,
                "busy": node.active is not None,
                "queue_length": len(node.queue),
                "name": node.name,
                "runtime": node.runtime,
                "runtime_version": node.runtime_version,
                "model": node.model,
                "models": list(node.models),
                "model_digests": dict(node.model_digests),
                "capabilities": dict(node.capabilities),
                "fingerprint": node.fingerprint,
                "last_heartbeat_seconds": max(
                    0, int(time.monotonic() - node.last_heartbeat_monotonic)
                ),
            }

    async def all_snapshots(self) -> dict[str, dict]:
        async with self._lock:
            identifiers = list(self._nodes)
        snapshots = await asyncio.gather(*(self.snapshot(key) for key in identifiers))
        return {key: value for key, value in zip(identifiers, snapshots) if value}

    async def submit(
        self,
        *,
        node_id: str,
        expected_fingerprint: str,
        actor_id: str,
        usage_user_id: str | None,
        operation_stage: str,
        messages: list[dict],
        finish_callback: FinishCallback,
        has_history: bool = False,
        model: str = "",
        thinking_enabled: bool = False,
        operation_id: str = "",
        require_idle: bool = False,
        output_max_bytes: int = LMC_AI_OUTPUT_MAX_BYTES,
        start_callback: StartCallback | None = None,
    ) -> tuple[ChatJob, int]:
        async with self._lock:
            node = self._nodes.get(node_id)
            if (
                node is None
                or node_id in self._blocked_new_jobs
                or not node.ready
                or node.draining
            ):
                raise NodeUnavailableError("自家 AI 暫時未能提供服務。")
            selected_model = str(model or node.model)
            if selected_model not in node.models:
                raise NodeUnavailableError("所選回答模式未能喺目前 AI 電腦使用。")
            if require_idle and (node.active is not None or node.queue):
                raise NodeUnavailableError("AI 電腦必須完全空閒先可以開始固定評估。")
            fingerprint = backend_fingerprint(
                node.node_id, selected_model, thinking_enabled,
                model_digest=node.model_digests.get(selected_model, ""),
            )
            if expected_fingerprint and expected_fingerprint != fingerprint:
                raise BackendChangedError("AI 設定已更新，請開始新對話。")
            if node.active is not None and len(node.queue) >= LMC_AI_QUEUE_MAX:
                raise QueueFullError("自家 AI 而家排隊已滿，請稍後再試。")
            job = ChatJob(
                operation_id=str(operation_id or secrets.token_hex(16))[:200],
                actor_id=actor_id,
                usage_user_id=usage_user_id,
                operation_stage=operation_stage,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
                fingerprint=fingerprint,
                model=selected_model,
                has_history=bool(has_history),
                thinking_enabled=bool(thinking_enabled),
                output_max_bytes=max(1, min(int(output_max_bytes), LMC_AI_OUTPUT_MAX_BYTES)),
                finish_callback=finish_callback,
                start_callback=start_callback,
            )
            if node.active is None:
                node.active = job
                position = 0
            else:
                node.queue.append(job)
                position = len(node.queue)
            job.timeout_task = asyncio.create_task(self._job_timeout(node, job))
        if position == 0:
            await job.events.put(("status", {"state": "starting"}))
            await self._send_start(node, job)
        else:
            await job.events.put(("queued", {"position": position}))
        return job, position

    async def _send(self, node: ConnectedNode, payload: dict) -> None:
        async with node.send_lock:
            await node.websocket.send_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )

    async def _send_start(self, node: ConnectedNode, job: ChatJob) -> None:
        try:
            await self._send(
                node,
                {
                    "type": "chat.start",
                    "operation_id": job.operation_id,
                    "messages": job.messages,
                    "model": job.model,
                    "think": job.thinking_enabled,
                    "context_length": LMC_AI_CONTEXT_LENGTH,
                    "allow_model_fallback": False,
                },
            )
        except Exception:
            await self._finish(node, job, False, {}, "未能將工作送到 AI 電腦。")

    async def cancel(self, node_id: str, job: ChatJob) -> None:
        job.cancelled = True
        node = None
        active = False
        async with self._lock:
            node = self._nodes.get(node_id)
            if node is None or job.finished:
                return
            if node.active is job:
                active = True
            else:
                try:
                    node.queue.remove(job)
                except ValueError:
                    return
                job.finished = True
        if active:
            try:
                await self._send(
                    node, {"type": "chat.cancel", "operation_id": job.operation_id}
                )
            except Exception:
                await self._finish(node, job, False, {}, "停止生成時連線中斷。")
        else:
            if job.timeout_task:
                job.timeout_task.cancel()
            job.terminal_published = True
            await job.events.put(("error", {"message": "已停止排隊。"}))
            await self._publish_queue_positions(node)

    async def handle_node_message(self, node: ConnectedNode, payload: object) -> None:
        if not isinstance(payload, dict):
            raise ValueError("node message must be a JSON object")
        message_type = payload.get("type")
        if message_type == "heartbeat":
            node.last_heartbeat_monotonic = time.monotonic()
            return
        if message_type == "status":
            node.last_heartbeat_monotonic = time.monotonic()
            node.ready = bool(payload.get("ready"))
            node.draining = bool(payload.get("draining"))
            reported_model = str(payload.get("model") or "").strip()
            reported_models = payload.get("models")
            reported_digests = payload.get("model_digests")
            clean_models = node.models
            allowed_models = {
                str(config["model"]) for config in LMC_AI_MODE_OPTIONS.values()
            }
            if reported_model and reported_model not in allowed_models:
                raise ValueError("node reported an unsupported model")
            if reported_models is not None and not isinstance(reported_models, list):
                raise ValueError("node reported an invalid model set")
            if isinstance(reported_models, list):
                candidate = tuple(dict.fromkeys(
                    str(item or "").strip()[:200] for item in reported_models
                    if str(item or "").strip() in allowed_models
                ))
                if not candidate or (reported_model and reported_model not in candidate):
                    raise ValueError("node reported an invalid model set")
                clean_models = candidate
            clean_digests = node.model_digests
            if reported_digests is not None:
                if not isinstance(reported_digests, dict):
                    raise ValueError("node reported invalid model digests")
                clean_digests = {
                    str(key): str(value).lower()
                    for key, value in reported_digests.items()
                    if str(key) in clean_models
                    and re.fullmatch(r"[0-9a-fA-F]{64}", str(value or ""))
                }
                if set(clean_digests) != set(clean_models):
                    raise ValueError("node reported incomplete model digests")
            backend_changed = bool(
                (reported_model and reported_model[:200] != node.model)
                or clean_models != node.models
                or clean_digests != node.model_digests
            )
            if reported_model:
                node.model = reported_model[:200]
            node.models = clean_models
            node.model_digests = clean_digests
            if backend_changed or node.draining or not node.ready:
                await self._fail_queued(
                    node,
                    "AI 設定已更新，請開始新對話。"
                    if backend_changed
                    else "AI 電腦暫停接收新工作。",
                )
            return
        operation_id = str(payload.get("operation_id") or "")
        job = node.active
        if job is None or operation_id != job.operation_id:
            return  # stale completion from a replaced/cancelled request
        if message_type == "chat.started":
            if not job.provider_attempted:
                job.provider_attempted = True
                if job.start_callback is not None:
                    try:
                        await job.start_callback(job)
                    except Exception:
                        await self._force_cancel_active(
                            node, job, "未能保存 AI 評估 attempt，已停止生成。"
                        )
                        return
            await job.events.put(("status", {"state": "generating"}))
            return
        if message_type == "chat.delta":
            if not job.provider_attempted or job.cancelled:
                return
            text = str(payload.get("text") or "")
            if not text:
                return
            encoded = text.encode("utf-8")
            if job.output_bytes + len(encoded) > job.output_max_bytes:
                await self._force_cancel_active(
                    node, job, "AI 回覆超過安全輸出上限。"
                )
                return
            job.output_bytes += len(encoded)
            job.collected_text += text
            await job.events.put(("delta", {"text": text}))
            return
        if message_type == "chat.complete":
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            usage = {
                "input_tokens": max(0, int(usage.get("input_tokens") or 0)),
                "output_tokens": max(0, int(usage.get("output_tokens") or 0)),
                "duration_ms": max(0, int(usage.get("duration_ms") or 0)),
                "model": str(payload.get("model") or node.model)[:200],
            }
            await self._finish(node, job, True, usage, "")
            return
        if message_type == "chat.error":
            code = str(payload.get("code") or "runtime_error")[:80]
            message = {
                "cancelled": "已停止生成。",
                "model_load": "AI 模型載入失敗，請開始新對話再試。",
                "out_of_memory": "AI 電腦記憶體不足，請開始新對話再試。",
                "model_unavailable": "所選回答模式未能喺目前 AI 電腦使用。",
            }.get(code, "AI 電腦未能完成今次回覆。")
            await self._finish(node, job, False, {}, message)
            return
        raise ValueError("unsupported node message type")

    async def _job_timeout(self, node: ConnectedNode, job: ChatJob) -> None:
        try:
            await asyncio.sleep(LMC_AI_REQUEST_TIMEOUT_SECONDS)
            if not job.finished:
                if node.active is job:
                    await self._force_cancel_active(
                        node, job, "AI 回覆逾時，請再試。"
                    )
                else:
                    await self._finish(
                        node, job, False, {}, "AI 回覆逾時，請再試。"
                    )
        except asyncio.CancelledError:
            return

    async def _force_cancel_active(
        self, node: ConnectedNode, job: ChatJob, reason: str
    ) -> None:
        """End the browser response but retain the node slot until its ACK."""
        job.cancelled = True
        job.forced_error = reason
        try:
            await self._send(
                node, {"type": "chat.cancel", "operation_id": job.operation_id}
            )
        except Exception:
            await self._finish(node, job, False, {}, reason)
            return
        await self._publish_terminal(job, False, {}, reason)

    async def _publish_terminal(
        self, job: ChatJob, success: bool, usage: dict, error: str
    ) -> None:
        if job.terminal_published:
            return
        job.terminal_published = True
        if job.provider_attempted:
            await job.finish_callback(job, success, usage, error)
        event = "complete" if success else "error"
        payload = (
            {
                "fingerprint": job.fingerprint,
                "model_changed": usage.get("model") != job.model,
                "usage": dict(usage),
            }
            if success
            else {"message": error or "AI 未能完成今次回覆。"}
        )
        await job.events.put((event, payload))

    async def _finish(
        self, node: ConnectedNode, job: ChatJob, success: bool, usage: dict, error: str
    ) -> None:
        if job.forced_error:
            success = False
            error = job.forced_error
        next_job = None
        queue_changed = False
        async with self._lock:
            if job.finished:
                return
            job.finished = True
            if node.active is job:
                node.active = None
                if node.queue and node.ready and not node.draining:
                    next_job = node.queue.popleft()
                    node.active = next_job
                queue_changed = True
            else:
                try:
                    node.queue.remove(job)
                    queue_changed = True
                except ValueError:
                    pass
        if job.timeout_task and job.timeout_task is not asyncio.current_task():
            job.timeout_task.cancel()
        await self._publish_terminal(job, success, usage, error)
        if queue_changed:
            await self._publish_queue_positions(node)
        if next_job is not None:
            await next_job.events.put(("status", {"state": "starting"}))
            await self._send_start(node, next_job)

    async def _publish_queue_positions(self, node: ConnectedNode) -> None:
        for position, queued_job in enumerate(tuple(node.queue), start=1):
            await queued_job.events.put(("queued", {"position": position}))

    async def _fail_node_jobs(self, node: ConnectedNode, reason: str) -> None:
        jobs = []
        async with self._lock:
            if node.active is not None:
                jobs.append(node.active)
                node.active = None
            jobs.extend(node.queue)
            node.queue.clear()
        for job in jobs:
            if job.finished:
                continue
            job.finished = True
            if job.timeout_task:
                job.timeout_task.cancel()
            await self._publish_terminal(job, False, {}, reason)

    async def _fail_queued(self, node: ConnectedNode, reason: str) -> None:
        async with self._lock:
            jobs = list(node.queue)
            node.queue.clear()
        for job in jobs:
            if job.finished:
                continue
            job.finished = True
            if job.timeout_task:
                job.timeout_task.cancel()
            await self._publish_terminal(job, False, {}, reason)


RUNTIME = LocalAIRuntime()
