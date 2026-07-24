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

from ai_model_config import LMC_AI_CONTEXT_LENGTH
from system_limits import (
    LMC_AI_NODE_WS_FRAME_MAX_BYTES,
    WORKSTATION_TTS_TRAINING_MAX_SECONDS,
    WORKSTATION_DATASET_PREPARATION_MAX_SECONDS,
    WORKSTATION_MODEL_PULL_MAX_SECONDS,
    WORKSTATION_REMOTE_AUDIT_MAX_ENTRIES,
)
from workstation.config import DEFAULT_CONFIG_PATH, WorkstationConfig, load_config
from workstation.manager.arbiter import ArbitrationError, ModeArbiter
from workstation.manager.executor import JobExecutor
from workstation.manager.health import HealthRunner
from workstation.manager.inhibitor import SleepInhibitor
from workstation.workloads.errors import WorkloadError
from workstation.manager.artifacts import SignedArtifactManager
from workstation.manager.update import UpdateStager
from workstation.remote_control import installed_profiles, validate_remote_command
from workstation.privileged_helper.client import PrivilegedActionError
from workstation.node.protocol import public_health_summary


DEFAULT_MANAGER_SOCKET = Path("/run/lmc-ai-workstation/manager.sock")


class ManagerApplication:
    def __init__(
        self, config: WorkstationConfig, arbiter: ModeArbiter,
        *, config_path: Path = DEFAULT_CONFIG_PATH,
    ):
        self.config = config
        self.config_path = config_path
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

    def _audit(self, action: str, outcome: str, *, code: str = "") -> None:
        path = self.config.paths.state / "remote-control-audit.json"
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= 256 * 1024:
                value = json.loads(path.read_bytes())
            else:
                value = []
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            value = []
        rows = value if isinstance(value, list) else []
        rows.append({
            "epoch": int(time.time()),
            "action": str(action)[:80],
            "outcome": str(outcome)[:40],
            "code": str(code)[:100],
        })
        rows = rows[-WORKSTATION_REMOTE_AUDIT_MAX_ENTRIES:]
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(rows, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)

    def _recent_audit(self) -> list[dict]:
        try:
            path = self.config.paths.state / "remote-control-audit.json"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
                return []
            value = json.loads(path.read_bytes())
            return list(value[-20:]) if isinstance(value, list) else []
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []

    def _control_status(self) -> dict:
        profiles = installed_profiles(self.config)
        power = self.config.public_dict()["power"]
        with suppress(OSError, ValueError):
            power = load_config(self.config_path).public_dict()["power"]
        return {
            "workloads": {
                "llm_enabled": self.config.workloads.ollama.enabled,
                "asr_enabled": self.config.workloads.asr.enabled,
                "rag_enabled": self.config.workloads.rag.enabled,
                "tts_enabled": self.config.workloads.gpt_sovits.enabled,
                **profiles["selected"],
            },
            "power": power,
            "update": {"enabled": self.config.update.enabled, "channel": self.config.update.channel},
            "installed_profiles": {
                "asr": profiles["asr"], "tts": profiles["tts"],
            },
            "audit": self._recent_audit(),
        }

    def _reload_config(self) -> None:
        with suppress(Exception):
            self.executor.asr.unload()
        with suppress(Exception):
            self.executor.ollama.unload_all()
        if self.config.workloads.gpt_sovits.enabled:
            with suppress(Exception):
                self.executor._set_gpt_sovits_service("stop")
        config = load_config(self.config_path)
        self.config = config
        self.executor = JobExecutor(config, self.arbiter, self.inhibitor)
        self.health_runner = HealthRunner(config)
        self.artifacts = SignedArtifactManager(config, self.executor.ollama)
        self._health_report = {}
        self._health_checked_monotonic = 0.0

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
                        if (
                            self.config.update.enabled
                            or self.config.workloads.gpt_sovits.enabled
                        ) else None
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

    def _resume_after_full_health(self) -> dict:
        if self.config.workloads.rag.enabled:
            health = self.health(force=True, full=True)
            if not health.get("healthy"):
                self.arbiter.set_draining(True)
                raise WorkloadError(
                    "health_gate",
                    "Resume requires a healthy active RAG bundle and full health check.",
                )
        self.arbiter.set_draining(False)
        return self.arbiter.snapshot()

    def handle(self, request: dict, emit) -> None:
        action = str(request.get("action") or "")
        if action == "snapshot":
            emit("result", {
                "manager": self.arbiter.snapshot(),
                "health": self.health(),
                "control": self._control_status(),
            })
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
            emit("result", self._resume_after_full_health())
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
        if action == "remote.control":
            self._remote_control(request, emit)
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

    def _remote_control(self, request: dict, emit) -> None:
        if set(request) != {"action", "command"}:
            raise WorkloadError("invalid_request", "Remote control request is invalid.")
        try:
            command = validate_remote_command(request.get("command"))
        except ValueError as exc:
            raise WorkloadError("invalid_request", str(exc)) from exc
        action = command["action"]
        self._audit(action, "started")
        try:
            if action == "status":
                result = {
                    "manager": self.arbiter.snapshot(),
                    "health": public_health_summary(self.health(force=True)),
                    "control": self._control_status(),
                }
            elif action == "drain":
                self.arbiter.set_draining(True)
                result = {"manager": self.arbiter.snapshot()}
            elif action == "resume":
                result = {"manager": self._resume_after_full_health()}
            elif action == "ack_reconcile":
                self.arbiter.acknowledge_reconcile()
                result = {"manager": self.arbiter.snapshot()}
            elif action == "cancel":
                result = {"cancel_requested": self.cancel(command["operation_id"])}
            elif action == "full_health":
                result = {
                    "health": public_health_summary(
                        self.health(force=True, full=True)
                    )
                }
            elif action == "artifact_inspect":
                result = {"components": self.artifacts.inspect()}
            elif action in {
                "artifact_models_install", "artifact_rag_install",
                "artifact_rag_rollback",
            }:
                mapped = {
                    "artifact_models_install": "model.approve",
                    "artifact_rag_install": "rag.install",
                    "artifact_rag_rollback": "rag.rollback",
                }[action]
                self._start_artifact_action(mapped, emit)
                self._audit(action, "accepted")
                return
            elif action == "power_schedule":
                result = self.executor._privileged_request({
                    "action": "set_power_schedule",
                    "enabled": command["enabled"],
                    "timezone": "Asia/Hong_Kong",
                    "suspend_at": command["suspend_at"],
                    "wake_at": command["wake_at"],
                })
            elif action == "restart_service":
                result = self.executor._privileged_request({
                    "action": "restart_service", "service": command["service"],
                })
            elif action in {"reboot", "update", "rollback"}:
                privileged_action = {
                    "reboot": "reboot",
                    "update": "trigger_update",
                    "rollback": "trigger_rollback",
                }[action]
                result = self.executor._privileged_request({"action": privileged_action})
            elif action in {"workloads_apply", "workloads_rollback"}:
                before = self.arbiter.snapshot()
                self.arbiter.set_draining(True)
                if before.get("active_operation") or before.get("voice_session_active") or before.get("voice_session_pending"):
                    raise WorkloadError("busy", "Workstation is draining active work.", retryable=True)
                privileged = (
                    {"action": "rollback_workloads"}
                    if action == "workloads_rollback"
                    else {**command, "action": "set_workloads"}
                )
                self.executor._privileged_request(privileged)
                self._reload_config()
                health = self.health(force=True, full=True)
                if not health.get("healthy"):
                    self.executor._privileged_request({"action": "rollback_workloads"})
                    self._reload_config()
                    self.health(force=True, full=True)
                    raise WorkloadError(
                        "health_gate", "Workload settings failed full health and were rolled back."
                    )
                if not before.get("draining"):
                    self.arbiter.set_draining(False)
                result = {
                    "health": public_health_summary(health),
                    "manager": self.arbiter.snapshot(),
                    "control": self._control_status(),
                }
            else:
                raise WorkloadError("invalid_request", "Remote control action is invalid.")
            self._audit(action, "succeeded")
            emit("result", result)
        except (ArbitrationError, WorkloadError) as exc:
            self._audit(action, "failed", code=exc.code)
            raise
        except (OSError, PrivilegedActionError, ValueError) as exc:
            self._audit(action, "failed", code="control_failed")
            raise WorkloadError(
                "control_failed", "Remote control action could not be completed."
            ) from exc

    def _chat(self, request: dict, emit) -> None:
        operation_id = str(request.get("operation_id") or "")
        messages = request.get("messages")
        if not isinstance(messages, list):
            raise WorkloadError("invalid_messages", "Chat messages are invalid.")
        context_length = request.get("context_length")
        if (
            isinstance(context_length, bool)
            or not isinstance(context_length, int)
            or context_length != LMC_AI_CONTEXT_LENGTH
        ):
            raise WorkloadError(
                "context_length_mismatch",
                "Chat context length does not match the approved model profile.",
            )
        cancel = self._cancel_event(operation_id)
        try:
            text, usage = self.executor.run_chat(
                operation_id=operation_id,
                model=str(request.get("model") or ""),
                messages=messages,
                think=request.get("think") is True,
                context_length=context_length,
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
                        self.executor._prepare_ollama_gpu(
                            self.config.workloads.rag.embedding_model
                        )
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
