"""Local Unix-socket RPC; Manager remains the only workload/mode owner."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import threading
import time
import re
import secrets

from system_limits import (
    LMC_AI_NODE_WS_FRAME_MAX_BYTES,
    WORKSTATION_TTS_TRAINING_MAX_SECONDS,
    WORKSTATION_DATASET_PREPARATION_MAX_SECONDS,
    WORKSTATION_MODEL_PULL_MAX_SECONDS,
)
from workstation.config import WorkstationConfig
from workstation.manager.arbiter import ArbitrationError, ModeArbiter
from workstation.manager.executor import JobExecutor
from workstation.manager.health import HealthRunner
from workstation.manager.inhibitor import SleepInhibitor
from workstation.workloads.errors import WorkloadError
from workstation.manager.artifacts import SignedArtifactManager
from workstation.manager.update import UpdateStager


DEFAULT_MANAGER_SOCKET = Path("/run/lmc-ai-workstation/manager.sock")


class ManagerApplication:
    def __init__(self, config: WorkstationConfig, arbiter: ModeArbiter):
        self.config = config
        self.arbiter = arbiter
        self.inhibitor = SleepInhibitor()
        self.executor = JobExecutor(config, arbiter, self.inhibitor)
        self.health_runner = HealthRunner(config)
        self.artifacts = SignedArtifactManager(config, self.executor.ollama)
        self._health_lock = threading.Lock()
        self._health_report: dict = {}
        self._health_checked_monotonic = 0.0
        self._cancel_lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._training_lock = threading.Lock()
        self._training_thread: threading.Thread | None = None

    def health(self, *, force: bool = False, full: bool = False) -> dict:
        with self._health_lock:
            if not full and not force and self._health_report and time.monotonic() - self._health_checked_monotonic < 60:
                return self._health_report
            operation_id = ""
            preserve_drain = False
            if full:
                preserve_drain = bool(self.arbiter.snapshot().get("draining"))
                operation_id = f"health.{secrets.token_hex(8)}"
                self.arbiter.start_operation(
                    operation_id,
                    "maintenance",
                    stage="full_health",
                    deadline_epoch=int(time.time()) + 10 * 60,
                )
                self.executor._sync_inhibitor()
            try:
                report = self.health_runner.run(
                    full=full,
                    set_gpt_service=self.executor._set_gpt_sovits_service,
                    prepare_non_ollama=self.executor._prepare_non_ollama_gpu,
                    probe_r2=(
                        UpdateStager(self.config).r2_health_probe
                        if self.config.update.enabled else None
                    ),
                )
                if operation_id:
                    self.arbiter.finish_operation(
                        operation_id,
                        success=bool(report.get("healthy")),
                        error_code="" if report.get("healthy") else "health_gate",
                    )
            except Exception:
                if operation_id:
                    self.arbiter.finish_operation(
                        operation_id, success=False, error_code="health_exception",
                    )
                report = {
                    "healthy": False,
                    "required": ["health_runner"],
                    "checks": {
                        "health_runner": {
                            "ok": False,
                            "code": "health_exception",
                        },
                    },
                    "checked_epoch": int(time.time()),
                    "inventory": {},
                }
            finally:
                if operation_id:
                    self.executor._sync_inhibitor()
                    if preserve_drain:
                        self.arbiter.set_draining(True)
            self.health_runner.write_report(report)
            self._health_report = report
            self._health_checked_monotonic = time.monotonic()
            return report

    def _cancel_event(self, operation_id: str) -> threading.Event:
        with self._cancel_lock:
            event = threading.Event()
            self._cancel_events[operation_id] = event
            return event

    def _remove_cancel_event(self, operation_id: str) -> None:
        with self._cancel_lock:
            self._cancel_events.pop(operation_id, None)

    def cancel(self, operation_id: str) -> bool:
        with self._cancel_lock:
            event = self._cancel_events.get(str(operation_id))
        if event is None:
            return False
        event.set()
        return True

    def handle(self, request: dict, emit) -> None:
        action = str(request.get("action") or "")
        if action == "snapshot":
            emit("result", {"manager": self.arbiter.snapshot(), "health": self.health()})
            return
        if action == "health":
            emit("result", self.health(
                force=bool(request.get("force")),
                full=bool(request.get("full")),
            ))
            return
        if action == "drain":
            self.arbiter.set_draining(True)
            emit("result", self.arbiter.snapshot())
            return
        if action == "resume":
            self.arbiter.set_draining(False)
            emit("result", self.arbiter.snapshot())
            return
        if action == "ack_reconcile":
            self.arbiter.acknowledge_reconcile()
            emit("result", self.arbiter.snapshot())
            return
        if action == "cancel":
            emit("result", {"cancel_requested": self.cancel(str(request.get("operation_id") or ""))})
            return
        if action == "operation.timings":
            operation = self.arbiter.record_operation_timings(
                str(request.get("operation_id") or ""),
                request.get("timings_ms"),
            )
            emit("result", {"operation": operation.public_dict()})
            return
        if action == "operation.external_stage":
            if set(request) != {"action", "operation_id", "stage"} or request.get("stage") != "r2_upload":
                raise WorkloadError(
                    "invalid_request", "External operation stage is invalid."
                )
            operation = self.arbiter.mark_tts_output_upload(
                str(request.get("operation_id") or "")
            )
            self.executor._sync_inhibitor()
            emit("result", {"operation": operation.public_dict()})
            return
        if action == "operation.external_finish":
            if (
                set(request) != {
                    "action", "operation_id", "success", "error_code",
                    "timings_ms",
                }
                or not isinstance(request.get("success"), bool)
                or not isinstance(request.get("timings_ms"), dict)
            ):
                raise WorkloadError(
                    "invalid_request", "External operation result is invalid."
                )
            operation_id = str(request.get("operation_id") or "")
            self.arbiter.record_operation_timings(
                operation_id, request.get("timings_ms")
            )
            operation = self.arbiter.finish_tts_output_upload(
                operation_id,
                success=request["success"],
                error_code=str(request.get("error_code") or ""),
            )
            self.executor._sync_inhibitor()
            emit("result", {"operation": operation.public_dict()})
            return
        if action == "chat.run":
            self._chat(request, emit)
            return
        if action == "job.run":
            self._job(request, emit)
            return
        if action == "training.start":
            self._start_training(request, emit)
            return
        if action == "dataset.prepare":
            self._start_dataset_preparation(request, emit)
            return
        if action == "artifacts.inspect":
            emit("result", {"components": self.artifacts.inspect()})
            return
        if action in {"model.approve", "rag.install", "rag.rollback"}:
            self._start_artifact_action(action, emit)
            return
        raise WorkloadError("unsupported_manager_action", "Manager action is not supported.")

    def _chat(self, request: dict, emit) -> None:
        operation_id = str(request.get("operation_id") or "")
        messages = request.get("messages")
        if not isinstance(messages, list):
            raise WorkloadError("invalid_messages", "Chat messages are invalid.")
        cancel = self._cancel_event(operation_id)
        try:
            text, usage = self.executor.run_chat(
                operation_id=operation_id,
                model=str(request.get("model") or ""),
                messages=messages,
                think=request.get("think") is True,
                deadline_epoch=int(request.get("deadline_epoch") or 0),
                cancel_event=cancel,
                on_started=lambda: emit("started", {}),
                on_delta=lambda text: emit("delta", {"text": text}),
            )
            emit("result", {"text": text, "usage": usage})
        finally:
            self._remove_cancel_event(operation_id)

    def _job(self, request: dict, emit) -> None:
        job = request.get("job")
        if not isinstance(job, dict):
            raise WorkloadError("invalid_job", "Workstation job is invalid.")
        operation_id = str(job.get("operation_id") or "")
        cancel = self._cancel_event(operation_id)
        try:
            result = self.executor.run_workstation_job(
                job,
                on_stage=lambda stage: emit("stage", {"stage": stage}),
                on_provider_started=lambda: emit("provider_started", {}),
                cancel_event=cancel,
            )
            emit("result", result)
        finally:
            self._remove_cancel_event(operation_id)

    def _start_training(self, request: dict, emit) -> None:
        dataset_id = str(request.get("dataset_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", dataset_id):
            raise WorkloadError("invalid_dataset", "Training dataset identifier is invalid.")
        with self._training_lock:
            if self._training_thread is not None and self._training_thread.is_alive():
                raise WorkloadError("busy", "GPT-SoVITS training is already active.")
            operation_id = f"training.{dataset_id}.{secrets.token_hex(6)}"
            cancel = self._cancel_event(operation_id)
            self.arbiter.start_operation(
                operation_id,
                "tts_training",
                stage="training_preflight",
                deadline_epoch=int(time.time()) + WORKSTATION_TTS_TRAINING_MAX_SECONDS,
            )
            self.executor._sync_inhibitor()

            def run() -> None:
                try:
                    self.executor.run_tts_training(
                        operation_id=operation_id,
                        dataset_id=dataset_id,
                        cancel_event=cancel,
                        operation_started=True,
                    )
                except Exception:
                    # Arbiter keeps the bounded public error state; never log dataset data.
                    if (self.arbiter.snapshot().get("active_operation") or {}).get("operation_id") == operation_id:
                        self.arbiter.finish_operation(
                            operation_id, success=False, error_code="training_internal",
                        )
                finally:
                    self._remove_cancel_event(operation_id)
                    self.executor._sync_inhibitor()

            self._training_thread = threading.Thread(
                target=run,
                name="lmc-gpt-sovits-training",
                daemon=True,
            )
            try:
                self._training_thread.start()
            except Exception:
                self._remove_cancel_event(operation_id)
                self.arbiter.finish_operation(
                    operation_id, success=False, error_code="thread_start_failed",
                )
                self.executor._sync_inhibitor()
                raise
        emit("result", {"started": True, "operation_id": operation_id})

    def _start_dataset_preparation(self, request: dict, emit) -> None:
        dataset_id = str(request.get("dataset_id") or "")
        speaker = str(request.get("speaker") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", dataset_id):
            raise WorkloadError("invalid_dataset", "Dataset identifier is invalid.")
        if speaker and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,99}", speaker):
            raise WorkloadError("invalid_speaker", "Dataset speaker identifier is invalid.")
        with self._training_lock:
            if self._training_thread is not None and self._training_thread.is_alive():
                raise WorkloadError("busy", "Another long Workstation job is active.")
            operation_id = f"dataset.{dataset_id}.{secrets.token_hex(6)}"
            cancel = self._cancel_event(operation_id)
            self.arbiter.start_operation(
                operation_id,
                "maintenance",
                stage="dataset_preparation",
                deadline_epoch=(
                    int(time.time()) + WORKSTATION_DATASET_PREPARATION_MAX_SECONDS
                ),
            )
            self.executor._sync_inhibitor()

            def run() -> None:
                try:
                    self.executor.run_dataset_preparation(
                        operation_id=operation_id,
                        dataset_id=dataset_id,
                        speaker=speaker,
                        cancel_event=cancel,
                        operation_started=True,
                    )
                except Exception:
                    if (self.arbiter.snapshot().get("active_operation") or {}).get("operation_id") == operation_id:
                        self.arbiter.finish_operation(
                            operation_id, success=False, error_code="dataset_internal",
                        )
                finally:
                    self._remove_cancel_event(operation_id)
                    self.executor._sync_inhibitor()

            self._training_thread = threading.Thread(
                target=run,
                name="lmc-gpt-sovits-dataset",
                daemon=True,
            )
            try:
                self._training_thread.start()
            except Exception:
                self._remove_cancel_event(operation_id)
                self.arbiter.finish_operation(
                    operation_id, success=False, error_code="thread_start_failed",
                )
                self.executor._sync_inhibitor()
                raise
        emit("result", {"started": True, "operation_id": operation_id})

    def _start_artifact_action(self, action: str, emit) -> None:
        with self._training_lock:
            if self._training_thread is not None and self._training_thread.is_alive():
                raise WorkloadError("busy", "Another long Workstation job is active.")
            operation_id = f"artifact.{action.replace('.', '-')}.{secrets.token_hex(6)}"
            cancel = self._cancel_event(operation_id)
            self.arbiter.start_operation(
                operation_id,
                "maintenance",
                stage=action.replace(".", "_"),
                deadline_epoch=(
                    int(time.time()) + (
                        WORKSTATION_MODEL_PULL_MAX_SECONDS
                        if action == "model.approve"
                        else WORKSTATION_DATASET_PREPARATION_MAX_SECONDS
                    )
                ),
            )
            self.executor._sync_inhibitor()

            def run() -> None:
                try:
                    if action == "model.approve":
                        self.artifacts.approve_models(cancel_event=cancel)
                    elif action == "rag.install":
                        self.executor._prepare_ollama_gpu()
                        self.artifacts.install_rag(cancel_event=cancel)
                    else:
                        self.artifacts.rollback_rag()
                    self.arbiter.finish_operation(operation_id, success=True)
                except WorkloadError as exc:
                    if exc.code == "cancelled":
                        self.arbiter.cancel_operation(operation_id)
                    else:
                        self.arbiter.finish_operation(
                            operation_id, success=False, error_code=exc.code,
                        )
                except Exception:
                    self.arbiter.finish_operation(
                        operation_id, success=False, error_code="artifact_internal",
                    )
                finally:
                    self._remove_cancel_event(operation_id)
                    self.executor._sync_inhibitor()

            self._training_thread = threading.Thread(
                target=run,
                name="lmc-signed-artifact",
                daemon=True,
            )
            try:
                self._training_thread.start()
            except Exception:
                self._remove_cancel_event(operation_id)
                self.arbiter.finish_operation(
                    operation_id, success=False, error_code="thread_start_failed",
                )
                self.executor._sync_inhibitor()
                raise
        emit("result", {"started": True, "operation_id": operation_id})


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, handler, application: ManagerApplication):
        self.application = application
        super().__init__(path, handler)


class _Handler(socketserver.StreamRequestHandler):
    def _peer_allowed(self) -> bool:
        if not hasattr(socket, "SO_PEERCRED"):
            return False
        raw = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid in {0, os.getuid()}

    def _send(self, event: str, payload: dict) -> None:
        frame = json.dumps({"event": event, **payload}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(frame) > LMC_AI_NODE_WS_FRAME_MAX_BYTES:
            frame = b'{"event":"error","code":"manager_frame_too_large","message":"Manager response exceeds the safe limit."}\n'
        self.wfile.write(frame)
        self.wfile.flush()

    def handle(self) -> None:
        if not self._peer_allowed():
            self._send("error", {"code": "forbidden", "message": "Manager peer is not allowed."})
            return
        raw = self.rfile.readline(LMC_AI_NODE_WS_FRAME_MAX_BYTES + 1)
        if not raw or len(raw) > LMC_AI_NODE_WS_FRAME_MAX_BYTES or not raw.endswith(b"\n"):
            self._send("error", {"code": "invalid_frame", "message": "Manager request frame is invalid."})
            return
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request is not an object")
            self.server.application.handle(request, self._send)
        except (ArbitrationError, WorkloadError) as exc:
            self._send("error", {"code": exc.code, "message": str(exc), "retryable": bool(getattr(exc, "retryable", False))})
        except (TypeError, ValueError, json.JSONDecodeError):
            self._send("error", {"code": "invalid_request", "message": "Manager request is invalid."})
        except Exception:
            self._send("error", {"code": "manager_failure", "message": "Manager could not complete the request."})


def serve_manager(application: ManagerApplication, socket_path: Path = DEFAULT_MANAGER_SOCKET) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    socket_path.unlink(missing_ok=True)
    server = _ThreadingUnixServer(str(socket_path), _Handler, application)
    os.chmod(socket_path, 0o660)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)


class ManagerClient:
    def __init__(self, socket_path: Path = DEFAULT_MANAGER_SOCKET):
        self.socket_path = socket_path

    async def stream(self, request: dict):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            if len(raw) > LMC_AI_NODE_WS_FRAME_MAX_BYTES:
                raise WorkloadError("manager_frame_too_large", "Manager request exceeds the safe limit.")
            writer.write(raw)
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    break
                if len(line) > LMC_AI_NODE_WS_FRAME_MAX_BYTES:
                    raise WorkloadError("manager_frame_too_large", "Manager response exceeds the safe limit.")
                payload = json.loads(line)
                if payload.get("event") == "error":
                    raise WorkloadError(str(payload.get("code") or "manager_failure"), str(payload.get("message") or "Manager request failed."), retryable=bool(payload.get("retryable")))
                yield payload
                if payload.get("event") == "result":
                    break
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def request(self, request: dict) -> dict:
        result = None
        async for event in self.stream(request):
            if event.get("event") == "result":
                result = {key: value for key, value in event.items() if key != "event"}
        if result is None:
            raise WorkloadError("manager_disconnected", "Manager disconnected before returning a result.", retryable=True)
        return result
