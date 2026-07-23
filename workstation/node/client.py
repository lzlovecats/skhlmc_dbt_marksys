"""Outbound authenticated WSS adapter for protocol-v2 Workstation jobs."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import inspect
import os
from pathlib import Path
import re
import threading
import time

import websockets

from ai_model_config import (
    LMC_AI_MODEL_PROFILE_VERSION,
    lmc_ai_all_models,
    resolve_lmc_ai_mode_options,
)
from system_limits import (
    LMC_AI_HEARTBEAT_INTERVAL_SECONDS,
    LMC_AI_NODE_WS_FRAME_MAX_BYTES,
    WORKSTATION_JOB_TIMEOUT_SECONDS,
    WORKSTATION_TTS_OUTPUT_MAX_BYTES,
)
from workstation.config import WorkstationConfig, read_secret
from workstation.manager.ipc import ManagerClient
from workstation.node.protocol import hello_frame, validate_server_job
from workstation.workloads.errors import WorkloadError
from workstation.workloads.r2_transfer import upload_path


def _websocket_authorization_argument(token: str, connector=None) -> dict:
    """Support Ubuntu 24.04 websockets 10.4 and reviewed newer runtimes."""
    target = connector or websockets.connect
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError) as exc:
        raise RuntimeError("unsupported websockets client runtime") from exc
    headers = {"Authorization": f"Bearer {token}"}
    if "additional_headers" in parameters:
        return {"additional_headers": headers}
    if "extra_headers" in parameters:
        return {"extra_headers": headers}
    raise RuntimeError("unsupported websockets client runtime")


def _generation_inventory(health: dict) -> tuple[list[str], dict[str, str]]:
    """Keep the dedicated embedding model out of the chat protocol inventory."""
    checks = health.get("checks") if isinstance(health, dict) else {}
    ollama = checks.get("ollama") if isinstance(checks, dict) else {}
    ollama = ollama if isinstance(ollama, dict) else {}
    installed = {
        str(item) for item in (ollama.get("models") or [])
        if str(item)
    }
    raw_digests = ollama.get("model_digests")
    raw_digests = raw_digests if isinstance(raw_digests, dict) else {}
    models = [
        model for model in lmc_ai_all_models()
        if model in installed
    ]
    return models, {
        model: str(raw_digests.get(model) or "")
        for model in models
    }


class WorkstationNodeClient:
    def __init__(self, config: WorkstationConfig, manager: ManagerClient):
        self.config = config
        self.manager = manager
        self.websocket = None
        self.active_task: asyncio.Task | None = None
        self.control_task: asyncio.Task | None = None
        self.active_operation_id = ""
        self.control_operation_id = ""
        self.upload_waiters: dict[str, asyncio.Future] = {}
        self.upload_verification_waiters: dict[str, asyncio.Future] = {}
        self.website_context: dict = {}

    def _write_website_receipt(self) -> None:
        if not self.website_context:
            return
        destination = self.config.paths.state / "website.json"
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({
                **self.website_context,
                "checked_epoch": int(time.time()),
            }, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o640)
        os.replace(temporary, destination)

    async def send(self, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > LMC_AI_NODE_WS_FRAME_MAX_BYTES:
            raise RuntimeError("outbound Workstation frame exceeds limit")
        await self.websocket.send(raw)

    async def _snapshot(self) -> dict:
        return await self.manager.request({"action": "snapshot"})

    async def _reconcile_external_upload(self, snapshot: dict) -> dict:
        active = (snapshot.get("manager") or {}).get("active_operation") or {}
        if active.get("kind") != "tts" or active.get("stage") != "r2_upload":
            return snapshot
        with suppress(OSError, WorkloadError):
            await self.manager.request({
                "action": "operation.external_finish",
                "operation_id": str(active.get("operation_id") or ""),
                "success": False,
                "error_code": "node_restarted_during_upload",
                "timings_ms": dict(active.get("timings_ms") or {}),
            })
        return await self._snapshot()

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(LMC_AI_HEARTBEAT_INTERVAL_SECONDS)
            self._write_website_receipt()
            snapshot = await self._snapshot()
            health = snapshot.get("health") or {}
            manager = snapshot.get("manager") or {}
            models, model_digests = _generation_inventory(health)
            capabilities = hello_frame(
                name=self.config.node.name,
                model_profile_version=LMC_AI_MODEL_PROFILE_VERSION,
                model=resolve_lmc_ai_mode_options()["fast"]["model"],
                models=models,
                model_digests=model_digests,
                health=health,
                manager=manager,
                control=snapshot.get("control") or {},
            )["capabilities"]
            await self.send({
                "type": "status",
                "ready": bool(health.get("healthy")),
                "draining": bool(manager.get("draining")),
                "model": resolve_lmc_ai_mode_options()["fast"]["model"],
                "models": models,
                "model_digests": model_digests,
                "capabilities": capabilities,
                "manager": manager,
                "health": hello_frame(
                    name=self.config.node.name,
                    model_profile_version=LMC_AI_MODEL_PROFILE_VERSION,
                    model=resolve_lmc_ai_mode_options()["fast"]["model"],
                    models=models,
                    model_digests=model_digests,
                    health=health,
                    manager=manager,
                    control=snapshot.get("control") or {},
                )["health"],
                "control": snapshot.get("control") or {},
            })
            await self.send({"type": "heartbeat"})

    async def run_chat(self, payload: dict) -> None:
        operation_id = str(payload.get("operation_id") or "")
        self.active_operation_id = operation_id
        terminal = False
        try:
            request = {
                "action": "chat.run",
                "operation_id": operation_id,
                "model": str(payload.get("model") or ""),
                "messages": payload.get("messages") or [],
                "think": payload.get("think") is True,
                "deadline_epoch": int(time.time()) + WORKSTATION_JOB_TIMEOUT_SECONDS,
            }
            async for event in self.manager.stream(request):
                event_type = event.get("event")
                if event_type == "started":
                    await self.send({"type": "chat.started", "operation_id": operation_id, "model": request["model"]})
                elif event_type == "delta":
                    await self.send({"type": "chat.delta", "operation_id": operation_id, "text": str(event.get("text") or "")})
                elif event_type == "result":
                    terminal = True
                    await self.send({"type": "chat.complete", "operation_id": operation_id, "model": request["model"], "usage": event.get("usage") or {}})
        except WorkloadError as exc:
            terminal = True
            await self.send({"type": "chat.error", "operation_id": operation_id, "code": exc.code})
        finally:
            if not terminal:
                with suppress(Exception):
                    await self.send({"type": "chat.error", "operation_id": operation_id, "code": "runtime_error"})
            self.active_task = None
            self.active_operation_id = ""

    async def run_workstation_job(self, payload: dict, *, control: bool = False) -> None:
        operation_id = str(payload.get("operation_id") or "")
        try:
            job = validate_server_job(payload)
        except (TypeError, ValueError):
            await self.send({
                "type": "workstation.job.error",
                "operation_id": operation_id,
                "code": "invalid_request",
            })
            if control:
                self.control_task = None
                self.control_operation_id = ""
            else:
                self.active_task = None
                self.active_operation_id = ""
            return
        operation_id = job["operation_id"]
        if control:
            self.control_operation_id = operation_id
        else:
            self.active_operation_id = operation_id
        output_path = None
        external_tts_pending = False
        external_tts_timings: dict[str, int] = {}
        terminal_code = "node_output_upload_interrupted"
        try:
            await self.send({"type": "workstation.job.started", "operation_id": operation_id, "job_kind": job["job_kind"]})
            result = None
            manager_request = (
                {"action": "remote.control", "command": job["payload"]["command"]}
                if job["job_kind"] == "control"
                else {"action": "job.run", "job": job}
            )
            async for event in self.manager.stream(manager_request):
                if event.get("event") == "stage":
                    await self.send({"type": "workstation.job.stage", "operation_id": operation_id, "stage": str(event.get("stage") or "")})
                elif event.get("event") == "provider_started":
                    await self.send({"type": "workstation.job.stage", "operation_id": operation_id, "stage": "provider_started"})
                elif event.get("event") == "result":
                    result = {key: value for key, value in event.items() if key != "event"}
            if result is None:
                raise WorkloadError("manager_disconnected", "Manager returned no Workstation result.", retryable=True)
            prepared = result.pop("prepared_output", None)
            if isinstance(prepared, dict):
                output_path = Path(str(prepared.pop("path") or ""))
                timings = prepared.pop("timings_ms", {})
                timings = dict(timings) if isinstance(timings, dict) else {}
                external_tts_timings = timings
                external_tts_pending = True
                await self.manager.request({
                    "action": "operation.external_stage",
                    "operation_id": operation_id,
                    "stage": "r2_upload",
                })
                waiter = asyncio.get_running_loop().create_future()
                self.upload_waiters[operation_id] = waiter
                await self.send({
                    "type": "workstation.job.stage",
                    "operation_id": operation_id,
                    "stage": "r2_upload",
                })
                await self.send({
                    "type": "workstation.upload.request",
                    "operation_id": operation_id,
                    "media": prepared,
                })
                authorization = await asyncio.wait_for(waiter, timeout=30)
                upload = authorization.get("upload") if isinstance(authorization.get("upload"), dict) else {}
                upload_started = time.monotonic()
                transfer = await asyncio.to_thread(
                    upload_path,
                    str(upload.get("url") or ""),
                    output_path,
                    headers=upload.get("headers") or {},
                    expected_sha256=str(prepared.get("sha256") or ""),
                    max_bytes=WORKSTATION_TTS_OUTPUT_MAX_BYTES,
                    timeout_seconds=60,
                )
                timings["r2_upload"] = int(
                    (time.monotonic() - upload_started) * 1_000
                )
                result["output"] = {
                    **prepared,
                    **transfer,
                    "intent_id": str(authorization.get("intent_id") or ""),
                }
                result["timings_ms"] = timings
                verification = asyncio.get_running_loop().create_future()
                self.upload_verification_waiters[operation_id] = verification
                await self.send({
                    "type": "workstation.upload.complete",
                    "operation_id": operation_id,
                    "result": result,
                })
                try:
                    await asyncio.wait_for(verification, timeout=30)
                except asyncio.TimeoutError as exc:
                    raise WorkloadError(
                        "output_verification_timeout",
                        "Server did not verify the generated media in time.",
                        retryable=True,
                    ) from exc
                await self.manager.request({
                    "action": "operation.external_finish",
                    "operation_id": operation_id,
                    "success": True,
                    "error_code": "",
                    "timings_ms": timings,
                })
                external_tts_pending = False
            await self.send({"type": "workstation.job.complete", "operation_id": operation_id, "result": result})
        except (WorkloadError, asyncio.TimeoutError) as exc:
            code = exc.code if isinstance(exc, WorkloadError) else "upload_authorization_timeout"
            terminal_code = code
            await self.send({"type": "workstation.job.error", "operation_id": operation_id, "code": code})
        except Exception:
            terminal_code = "runtime_error"
            with suppress(Exception):
                await self.send({
                    "type": "workstation.job.error",
                    "operation_id": operation_id,
                    "code": "runtime_error",
                })
        finally:
            if external_tts_pending:
                with suppress(OSError, WorkloadError):
                    await self.manager.request({
                        "action": "operation.external_finish",
                        "operation_id": operation_id,
                        "success": False,
                        "error_code": terminal_code,
                        "timings_ms": external_tts_timings,
                    })
            self.upload_waiters.pop(operation_id, None)
            self.upload_verification_waiters.pop(operation_id, None)
            if output_path is not None:
                with suppress(OSError):
                    output_path.unlink(missing_ok=True)
            if control:
                self.control_task = None
                self.control_operation_id = ""
            else:
                self.active_task = None
                self.active_operation_id = ""

    async def cancel_active(self, operation_id: str) -> None:
        task = None
        if self.active_task and self.active_operation_id == operation_id:
            task = self.active_task
        elif self.control_task and self.control_operation_id == operation_id:
            task = self.control_task
        if task is None:
            return
        with suppress(OSError, WorkloadError):
            await self.manager.request({"action": "cancel", "operation_id": operation_id})
        with suppress(asyncio.CancelledError, WorkloadError):
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def session(self) -> None:
        snapshot = await self._reconcile_external_upload(
            await self._snapshot()
        )
        health = snapshot.get("health") or {}
        manager = snapshot.get("manager") or {}
        models, model_digests = _generation_inventory(health)
        fast_model = resolve_lmc_ai_mode_options()["fast"]["model"]
        token = read_secret(self.config.node.token_file)
        async with websockets.connect(
            self.config.node.server_url,
            **_websocket_authorization_argument(token),
            max_size=LMC_AI_NODE_WS_FRAME_MAX_BYTES,
            ping_interval=None,
            open_timeout=20,
        ) as websocket:
            self.websocket = websocket
            await self.send(hello_frame(
                name=self.config.node.name,
                model_profile_version=LMC_AI_MODEL_PROFILE_VERSION,
                model=fast_model,
                models=models,
                model_digests=model_digests,
                health=health,
                manager=manager,
                control=snapshot.get("control") or {},
            ))
            accepted = json.loads(await asyncio.wait_for(websocket.recv(), timeout=20))
            if (
                accepted.get("type") != "hello.accepted"
                or int(accepted.get("protocol") or 0) != 2
                or not re.fullmatch(
                    r"[0-9]+\.[0-9]+\.[0-9]+",
                    str(accepted.get("website_version") or ""),
                )
                or not re.fullmatch(
                    r"[0-9]{8}_[0-9]{4}",
                    str(accepted.get("database_migration_requirement") or ""),
                )
            ):
                raise RuntimeError("website rejected Workstation protocol")
            self.website_context = {
                "website_version": str(accepted["website_version"]),
                "database_migration_requirement": str(
                    accepted["database_migration_requirement"]
                ),
            }
            self._write_website_receipt()
            heartbeat = asyncio.create_task(self.heartbeat())
            try:
                async for raw in websocket:
                    if not isinstance(raw, str) or len(raw.encode("utf-8")) > LMC_AI_NODE_WS_FRAME_MAX_BYTES:
                        await websocket.close(code=1009, reason="text frame required")
                        break
                    payload = json.loads(raw)
                    message_type = payload.get("type")
                    if message_type in {"chat.start", "workstation.job.start"}:
                        is_remote_control = (
                            message_type == "workstation.job.start"
                            and payload.get("job_kind") == "control"
                        )
                        is_reservation = (
                            message_type == "workstation.job.start"
                            and payload.get("job_kind") == "voice.reserve"
                        )
                        if (is_reservation or is_remote_control) and self.control_task is None:
                            self.control_operation_id = str(
                                payload.get("operation_id") or ""
                            )
                            self.control_task = asyncio.create_task(
                                self.run_workstation_job(payload, control=True)
                            )
                        elif self.active_task is not None:
                            error_type = "chat.error" if message_type == "chat.start" else "workstation.job.error"
                            await self.send({"type": error_type, "operation_id": payload.get("operation_id"), "code": "busy"})
                        else:
                            target = self.run_chat(payload) if message_type == "chat.start" else self.run_workstation_job(payload)
                            self.active_task = asyncio.create_task(target)
                    elif message_type in {"chat.cancel", "workstation.job.cancel"}:
                        await self.cancel_active(str(payload.get("operation_id") or ""))
                    elif message_type == "workstation.upload.authorized":
                        operation_id = str(payload.get("operation_id") or "")
                        waiter = self.upload_waiters.get(operation_id)
                        if waiter and not waiter.done():
                            waiter.set_result(payload)
                    elif message_type in {
                        "workstation.upload.verified",
                        "workstation.upload.rejected",
                    }:
                        operation_id = str(payload.get("operation_id") or "")
                        waiter = self.upload_verification_waiters.get(operation_id)
                        if waiter and not waiter.done():
                            if message_type == "workstation.upload.verified":
                                waiter.set_result(payload)
                            else:
                                waiter.set_exception(WorkloadError(
                                    "output_verification_failed",
                                    "Server rejected the generated media.",
                                ))
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
                if self.active_task:
                    await self.cancel_active(self.active_operation_id)
                if self.control_task:
                    await self.cancel_active(self.control_operation_id)


async def run_forever(config: WorkstationConfig, manager: ManagerClient) -> None:
    delay = 1
    while True:
        client = WorkstationNodeClient(config, manager)
        try:
            await client.session()
            delay = 1
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
