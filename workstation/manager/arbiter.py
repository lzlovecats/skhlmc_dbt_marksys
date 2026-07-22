"""Single-GPU mode arbitration for Workstation v1."""

from __future__ import annotations

from collections import deque
import re
import threading
import time

from system_limits import LIVE_FREE_SESSION_MAX_SECONDS
from system_limits import (
    WORKSTATION_OPERATION_TIMING_MAX_MS,
    WORKSTATION_OPERATION_TIMING_MAX_STAGES,
)

from workstation.manager.models import (
    ManagedOperation,
    ManagerMode,
    ManagerState,
    OperationState,
    TERMINAL_OPERATION_STATES,
)
from workstation.manager.state_store import StateStore


_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_OPERATION_HISTORY_MAX = 200


class ArbitrationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ModeArbiter:
    """Own every workload transition; adapters never mutate mode directly."""

    def __init__(self, store: StateStore, *, clock=time.time):
        self.store = store
        self.clock = clock
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self.state = store.load()
        with self._condition:
            self._reconcile_after_restart()

    def _now(self) -> int:
        return int(self.clock())

    @staticmethod
    def _identifier(value: object, field_name: str, *, required: bool = True) -> str:
        clean = str(value or "")
        if required and not _ID_RE.fullmatch(clean):
            raise ArbitrationError("invalid_request", f"invalid {field_name}")
        if clean and not _ID_RE.fullmatch(clean):
            raise ArbitrationError("invalid_request", f"invalid {field_name}")
        return clean

    def _persist(self) -> None:
        self.state.revision += 1
        self.state.updated_epoch = self._now()
        self.state.sleep_inhibited = self._managed_work_active()
        self._trim_history()
        self.store.save(self.state)
        self._condition.notify_all()

    def _managed_work_active(self) -> bool:
        return bool(
            self.state.active_operation_id
            or self.state.voice_session_id
            or self.state.pending_voice_session
            or self.state.mode in {ManagerMode.TTS_TRAINING, ManagerMode.MAINTENANCE}
        )

    def _expire_voice_if_needed(self) -> bool:
        expires = self.state.voice_expires_epoch
        if not expires or self._now() < expires:
            return False
        if self.state.active_operation_id:
            active = self.state.operations.get(self.state.active_operation_id)
            if active and active.kind in {"asr", "rag", "voice_text", "tts"}:
                if not self.state.draining:
                    self.state.draining = True
                    self._persist()
                return False
        self.state.pending_voice_session = ""
        self.state.voice_session_id = ""
        self.state.voice_expires_epoch = 0
        self.state.draining = False
        if not self.state.active_operation_id:
            self.state.mode = ManagerMode.IDLE
        self._persist()
        return True

    def _trim_history(self) -> None:
        terminal = deque(
            key for key, item in self.state.operations.items()
            if OperationState(item.state) in TERMINAL_OPERATION_STATES
        )
        while len(self.state.operations) > _OPERATION_HISTORY_MAX and terminal:
            self.state.operations.pop(terminal.popleft(), None)

    def _reconcile_after_restart(self) -> None:
        interrupted = False
        for operation in self.state.operations.values():
            if OperationState(operation.state) not in TERMINAL_OPERATION_STATES:
                operation.state = OperationState.INTERRUPTED
                operation.error_code = "manager_restarted"
                operation.updated_epoch = self._now()
                interrupted = True
        if self.state.active_operation_id or self.state.voice_session_id or self.state.pending_voice_session:
            interrupted = True
        self.state.active_operation_id = ""
        self.state.voice_session_id = ""
        self.state.pending_voice_session = ""
        self.state.voice_expires_epoch = 0
        self.state.sleep_inhibited = False
        if interrupted:
            self.state.mode = ManagerMode.FAULTED
            self.state.draining = True
            self.state.reconcile_required = True
            self.state.last_error_code = "manager_restarted"
            self._persist()
        elif self.state.mode not in {ManagerMode.IDLE, ManagerMode.FAULTED}:
            self.state.mode = ManagerMode.IDLE
            self._persist()

    def acknowledge_reconcile(self) -> None:
        with self._condition:
            if not self.state.reconcile_required:
                return
            self.state.reconcile_required = False
            self.state.last_error_code = ""
            self.state.mode = ManagerMode.IDLE
            self.state.draining = False
            self._persist()

    def set_draining(self, draining: bool) -> None:
        with self._condition:
            self._expire_voice_if_needed()
            if self.state.mode == ManagerMode.FAULTED and not draining:
                raise ArbitrationError("faulted", "reconcile must be acknowledged first")
            self.state.draining = bool(draining)
            self._persist()

    def reserve_voice(self, session_id: str, *, expires_epoch: int = 0) -> str:
        session_id = self._identifier(session_id, "session_id")
        with self._condition:
            self._expire_voice_if_needed()
            if self.state.mode == ManagerMode.FAULTED:
                raise ArbitrationError("faulted", "manager reconciliation is required")
            if self.state.draining or self.state.mode in {ManagerMode.MAINTENANCE, ManagerMode.TTS_TRAINING}:
                raise ArbitrationError("busy", "workstation is not accepting voice sessions")
            if self.state.voice_session_id:
                if self.state.voice_session_id == session_id:
                    return "reserved"
                raise ArbitrationError("voice_busy", "another voice session is active")
            if self.state.pending_voice_session and self.state.pending_voice_session != session_id:
                raise ArbitrationError("voice_busy", "another voice session is pending")
            now = self._now()
            requested_expiry = int(expires_epoch or (now + LIVE_FREE_SESSION_MAX_SECONDS))
            if requested_expiry <= now:
                raise ArbitrationError("deadline_expired", "voice session deadline has expired")
            requested_expiry = min(requested_expiry, now + LIVE_FREE_SESSION_MAX_SECONDS)
            self.state.voice_expires_epoch = requested_expiry
            if self.state.active_operation_id:
                self.state.pending_voice_session = session_id
                self.state.draining = True
                self._persist()
                return "waiting_for_text"
            self.state.voice_session_id = session_id
            self.state.pending_voice_session = ""
            self.state.mode = ManagerMode.VOICE_COACH
            self._persist()
            return "reserved"

    def wait_for_voice(
        self,
        session_id: str,
        timeout: float,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        session_id = self._identifier(session_id, "session_id")
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self.state.voice_session_id != session_id:
                if cancel_event is not None and cancel_event.is_set():
                    return False
                self._expire_voice_if_needed()
                if self.state.pending_voice_session != session_id:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 1.0))
            return True

    def cancel_pending_voice(self, session_id: str) -> None:
        session_id = self._identifier(session_id, "session_id")
        with self._condition:
            if self.state.pending_voice_session != session_id:
                return
            self.state.pending_voice_session = ""
            self.state.voice_expires_epoch = 0
            self.state.draining = False
            self._persist()

    def release_voice(self, session_id: str) -> None:
        session_id = self._identifier(session_id, "session_id")
        with self._condition:
            self._expire_voice_if_needed()
            if self.state.voice_session_id != session_id and self.state.pending_voice_session != session_id:
                return
            if self.state.active_operation_id:
                raise ArbitrationError("active_job", "voice workload is still active")
            self.state.voice_session_id = ""
            self.state.pending_voice_session = ""
            self.state.voice_expires_epoch = 0
            self.state.draining = False
            self.state.mode = ManagerMode.IDLE
            self._persist()

    def start_operation(
        self,
        operation_id: str,
        kind: str,
        *,
        session_id: str = "",
        turn_id: str = "",
        stage: str = "",
        deadline_epoch: int = 0,
    ) -> ManagedOperation:
        operation_id = self._identifier(operation_id, "operation_id")
        kind = self._identifier(kind, "kind")
        session_id = self._identifier(session_id, "session_id", required=False)
        turn_id = self._identifier(turn_id, "turn_id", required=False)
        stage = self._identifier(stage, "stage", required=False)
        with self._condition:
            self._expire_voice_if_needed()
            existing = self.state.operations.get(operation_id)
            if existing:
                if existing.kind != kind or existing.session_id != session_id or existing.turn_id != turn_id:
                    raise ArbitrationError("idempotency_conflict", "operation identifier was reused")
                if OperationState(existing.state) in TERMINAL_OPERATION_STATES:
                    raise ArbitrationError("operation_terminal", "operation already reached a terminal state")
                raise ArbitrationError("duplicate_active", "operation is already active")
            if self.state.mode == ManagerMode.FAULTED:
                raise ArbitrationError("faulted", "manager reconciliation is required")
            if self.state.active_operation_id:
                raise ArbitrationError("busy", "GPU workload is active")
            if kind == "text":
                if self.state.draining or self.state.pending_voice_session or self.state.voice_session_id or self.state.mode != ManagerMode.IDLE:
                    raise ArbitrationError("busy", "text work is blocked by the current manager mode")
                self.state.mode = ManagerMode.TEXT_SERVE
            elif kind in {"asr", "rag", "voice_text", "tts"}:
                if not session_id or self.state.voice_session_id != session_id or self.state.mode != ManagerMode.VOICE_COACH:
                    raise ArbitrationError("invalid_session", "voice session is not reserved")
            elif kind == "tts_training":
                if self.state.draining or self.state.mode != ManagerMode.IDLE:
                    raise ArbitrationError("busy", "training requires an idle workstation")
                self.state.mode = ManagerMode.TTS_TRAINING
                self.state.draining = True
            elif kind == "maintenance":
                if self.state.mode != ManagerMode.IDLE:
                    raise ArbitrationError("busy", "maintenance requires an idle workstation")
                self.state.mode = ManagerMode.MAINTENANCE
                self.state.draining = True
            else:
                raise ArbitrationError("unsupported_kind", "unsupported workload kind")
            operation = ManagedOperation(
                operation_id=operation_id,
                kind=kind,
                state=OperationState.RUNNING,
                session_id=session_id,
                turn_id=turn_id,
                stage=stage,
                deadline_epoch=max(0, int(deadline_epoch or 0)),
                created_epoch=self._now(),
                updated_epoch=self._now(),
            )
            self.state.operations[operation_id] = operation
            self.state.active_operation_id = operation_id
            self._persist()
            return operation

    def finish_operation(self, operation_id: str, *, success: bool, error_code: str = "") -> ManagedOperation:
        operation_id = self._identifier(operation_id, "operation_id")
        with self._condition:
            operation = self.state.operations.get(operation_id)
            if operation is None:
                raise ArbitrationError("unknown_operation", "operation does not exist")
            if OperationState(operation.state) in TERMINAL_OPERATION_STATES:
                return operation
            operation.state = OperationState.SUCCEEDED if success else OperationState.FAILED
            operation.error_code = str(error_code or "")[:100]
            operation.updated_epoch = self._now()
            if self.state.active_operation_id == operation_id:
                self.state.active_operation_id = ""
            self._expire_voice_if_needed()
            if operation.kind == "text":
                if self.state.pending_voice_session:
                    self.state.voice_session_id = self.state.pending_voice_session
                    self.state.pending_voice_session = ""
                    self.state.mode = ManagerMode.VOICE_COACH
                    self.state.draining = False
                else:
                    self.state.mode = ManagerMode.IDLE
            elif operation.kind in {"tts_training", "maintenance"}:
                self.state.mode = ManagerMode.IDLE
                self.state.draining = False
            self._persist()
            return operation

    def record_operation_timings(
        self, operation_id: str, timings_ms: object,
    ) -> ManagedOperation:
        operation_id = self._identifier(operation_id, "operation_id")
        if not isinstance(timings_ms, dict):
            raise ArbitrationError("invalid_request", "operation timings are invalid")
        clean: dict[str, int] = {}
        for stage, duration in timings_ms.items():
            name = self._identifier(stage, "timing_stage")
            try:
                measured = int(duration)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ArbitrationError(
                    "invalid_request", "operation timing is invalid"
                ) from exc
            if not 0 <= measured <= WORKSTATION_OPERATION_TIMING_MAX_MS:
                raise ArbitrationError("invalid_request", "operation timing is invalid")
            clean[name] = measured
        with self._condition:
            operation = self.state.operations.get(operation_id)
            if operation is None:
                raise ArbitrationError("unknown_operation", "operation does not exist")
            merged = {**operation.timings_ms, **clean}
            if len(merged) > WORKSTATION_OPERATION_TIMING_MAX_STAGES:
                raise ArbitrationError(
                    "invalid_request", "operation has too many timing stages"
                )
            operation.timings_ms = merged
            operation.updated_epoch = self._now()
            self._persist()
            return operation

    def update_operation_stage(
        self, operation_id: str, stage: str,
    ) -> ManagedOperation:
        operation_id = self._identifier(operation_id, "operation_id")
        stage = self._identifier(stage, "stage")
        with self._condition:
            operation = self.state.operations.get(operation_id)
            if operation is None:
                raise ArbitrationError("unknown_operation", "operation does not exist")
            if OperationState(operation.state) in TERMINAL_OPERATION_STATES:
                raise ArbitrationError("operation_terminal", "operation is already terminal")
            operation.stage = stage
            operation.updated_epoch = self._now()
            self._persist()
            return operation

    def mark_tts_output_upload(self, operation_id: str) -> ManagedOperation:
        operation_id = self._identifier(operation_id, "operation_id")
        with self._condition:
            operation = self.state.operations.get(operation_id)
            if (
                operation is None
                or operation.kind != "tts"
                or self.state.active_operation_id != operation_id
                or OperationState(operation.state) in TERMINAL_OPERATION_STATES
            ):
                raise ArbitrationError(
                    "invalid_external_operation",
                    "external output stage does not match an active TTS operation",
                )
            operation.stage = "r2_upload"
            operation.updated_epoch = self._now()
            self._persist()
            return operation

    def finish_tts_output_upload(
        self,
        operation_id: str,
        *,
        success: bool,
        error_code: str = "",
    ) -> ManagedOperation:
        operation_id = self._identifier(operation_id, "operation_id")
        with self._condition:
            operation = self.state.operations.get(operation_id)
            if operation is None or operation.kind != "tts":
                raise ArbitrationError(
                    "invalid_external_operation",
                    "external output result does not match a TTS operation",
                )
            state = OperationState(operation.state)
            expected = (
                OperationState.SUCCEEDED if success else OperationState.FAILED
            )
            if state in TERMINAL_OPERATION_STATES:
                if state == expected:
                    return operation
                raise ArbitrationError(
                    "external_result_conflict",
                    "external output result conflicts with the terminal operation",
                )
            if (
                self.state.active_operation_id != operation_id
                or operation.stage != "r2_upload"
            ):
                raise ArbitrationError(
                    "invalid_external_operation",
                    "TTS operation is not awaiting output upload",
                )
            return self.finish_operation(
                operation_id,
                success=bool(success),
                error_code=str(error_code or "")[:100],
            )

    def cancel_operation(self, operation_id: str) -> ManagedOperation:
        operation_id = self._identifier(operation_id, "operation_id")
        with self._condition:
            operation = self.state.operations.get(operation_id)
            if operation is None:
                raise ArbitrationError("unknown_operation", "operation does not exist")
            if OperationState(operation.state) in TERMINAL_OPERATION_STATES:
                return operation
            operation.state = OperationState.CANCELLED
            operation.updated_epoch = self._now()
            if self.state.active_operation_id == operation_id:
                self.state.active_operation_id = ""
            self._expire_voice_if_needed()
            if operation.kind == "text":
                if self.state.pending_voice_session:
                    self.state.voice_session_id = self.state.pending_voice_session
                    self.state.pending_voice_session = ""
                    self.state.mode = ManagerMode.VOICE_COACH
                    self.state.draining = False
                else:
                    self.state.mode = ManagerMode.IDLE
            elif operation.kind in {"tts_training", "maintenance"}:
                self.state.mode = ManagerMode.IDLE
                self.state.draining = False
            self._persist()
            return operation

    def snapshot(self) -> dict:
        with self._condition:
            self._expire_voice_if_needed()
            active = self.state.operations.get(self.state.active_operation_id)
            recent = sorted(
                self.state.operations.values(),
                key=lambda item: (item.updated_epoch, item.created_epoch),
                reverse=True,
            )[:10]
            return {
                "revision": self.state.revision,
                "mode": self.state.mode,
                "draining": self.state.draining,
                "voice_session_active": bool(self.state.voice_session_id),
                "voice_session_pending": bool(self.state.pending_voice_session),
                "voice_expires_epoch": self.state.voice_expires_epoch,
                "active_operation": active.public_dict() if active else None,
                "recent_operations": [item.public_dict() for item in recent],
                "sleep_inhibited": self.state.sleep_inhibited,
                "reconcile_required": self.state.reconcile_required,
                "last_error_code": self.state.last_error_code,
                "updated_epoch": self.state.updated_epoch,
            }
