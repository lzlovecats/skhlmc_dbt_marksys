"""Process-local WebSocket, fingerprint and FIFO queue runtime for local AI."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import secrets
import time
from typing import Awaitable, Callable

from ai_model_config import LMC_AI_CONTEXT_LENGTH
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
    node_id: str, model: str, thinking_enabled: bool = False
) -> str:
    thinking_mode = "thinking" if thinking_enabled else "non-thinking"
    material = f"{node_id}\n{model}\n{PERSONA_VERSION}\n{thinking_mode}".encode("utf-8")
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
    finish_callback: FinishCallback
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    created_monotonic: float = field(default_factory=time.monotonic)
    provider_attempted: bool = False
    output_bytes: int = 0
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
        return backend_fingerprint(self.node_id, self.model)


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

    @staticmethod
    def validate_hello(payload: object) -> dict:
        if not isinstance(payload, dict) or payload.get("type") != "hello":
            raise ValueError("first node message must be hello")
        if payload.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("unsupported node protocol")
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
        return {
            "name": name,
            "runtime": str(payload.get("runtime") or "")[:80],
            "runtime_version": str(payload.get("runtime_version") or "")[:80],
            "model": model[:200],
            "capabilities": required,
            "ready": bool(payload.get("ready")),
            "draining": bool(payload.get("draining")),
        }

    async def register(
        self, node_id: str, websocket, hello: dict, *, pending: bool = False
    ) -> ConnectedNode:
        clean = self.validate_hello(hello)
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
        thinking_enabled: bool = False,
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
            fingerprint = backend_fingerprint(
                node.node_id, node.model, thinking_enabled
            )
            if expected_fingerprint and expected_fingerprint != fingerprint:
                raise BackendChangedError("AI 設定已更新，請開始新對話。")
            if node.active is not None and len(node.queue) >= LMC_AI_QUEUE_MAX:
                raise QueueFullError("自家 AI 而家排隊已滿，請稍後再試。")
            job = ChatJob(
                operation_id=secrets.token_hex(16),
                actor_id=actor_id,
                usage_user_id=usage_user_id,
                operation_stage=operation_stage,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
                fingerprint=fingerprint,
                model=node.model,
                has_history=bool(has_history),
                thinking_enabled=bool(thinking_enabled),
                finish_callback=finish_callback,
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
                    "think": job.thinking_enabled,
                    "context_length": LMC_AI_CONTEXT_LENGTH,
                    "allow_model_fallback": not job.has_history,
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
            backend_changed = bool(reported_model and reported_model[:200] != node.model)
            if reported_model:
                node.model = reported_model[:200]
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
            job.provider_attempted = True
            await job.events.put(("status", {"state": "generating"}))
            return
        if message_type == "chat.delta":
            if not job.provider_attempted or job.cancelled:
                return
            text = str(payload.get("text") or "")
            if not text:
                return
            encoded = text.encode("utf-8")
            if job.output_bytes + len(encoded) > LMC_AI_OUTPUT_MAX_BYTES:
                await self._force_cancel_active(
                    node, job, "AI 回覆超過安全輸出上限。"
                )
                return
            job.output_bytes += len(encoded)
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
