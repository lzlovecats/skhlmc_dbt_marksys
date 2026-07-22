"""Durable manager state models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import time


class ManagerMode(StrEnum):
    IDLE = "idle"
    TEXT_SERVE = "text_serve"
    VOICE_COACH = "voice_coach"
    TTS_TRAINING = "tts_training"
    MAINTENANCE = "maintenance"
    FAULTED = "faulted"


class OperationState(StrEnum):
    RESERVED = "reserved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_OPERATION_STATES = {
    OperationState.SUCCEEDED,
    OperationState.FAILED,
    OperationState.CANCELLED,
    OperationState.INTERRUPTED,
}


@dataclass
class ManagedOperation:
    operation_id: str
    kind: str
    state: str = OperationState.RESERVED
    session_id: str = ""
    turn_id: str = ""
    stage: str = ""
    deadline_epoch: int = 0
    created_epoch: int = field(default_factory=lambda: int(time.time()))
    updated_epoch: int = field(default_factory=lambda: int(time.time()))
    error_code: str = ""
    timings_ms: dict[str, int] = field(default_factory=dict)

    def public_dict(self) -> dict:
        result = asdict(self)
        result.pop("error_code", None)
        return result


@dataclass
class ManagerState:
    revision: int = 0
    mode: str = ManagerMode.IDLE
    draining: bool = False
    pending_voice_session: str = ""
    voice_session_id: str = ""
    voice_expires_epoch: int = 0
    active_operation_id: str = ""
    last_error_code: str = ""
    reconcile_required: bool = False
    sleep_inhibited: bool = False
    operations: dict[str, ManagedOperation] = field(default_factory=dict)
    updated_epoch: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "operations": {key: asdict(value) for key, value in self.operations.items()},
        }

    @classmethod
    def from_dict(cls, value: object) -> "ManagerState":
        raw = value if isinstance(value, dict) else {}
        operations = {}
        for key, item in (raw.get("operations") or {}).items():
            if isinstance(item, dict):
                operation_id = str(item.get("operation_id") or key)
                kind = str(item.get("kind") or "")
                if not operation_id or not kind:
                    continue
                operations[str(key)] = ManagedOperation(
                    operation_id=operation_id,
                    kind=kind,
                    state=str(item.get("state") or OperationState.RESERVED),
                    session_id=str(item.get("session_id") or ""),
                    turn_id=str(item.get("turn_id") or ""),
                    stage=str(item.get("stage") or ""),
                    deadline_epoch=max(0, int(item.get("deadline_epoch") or 0)),
                    created_epoch=max(0, int(item.get("created_epoch") or 0)),
                    updated_epoch=max(0, int(item.get("updated_epoch") or 0)),
                    error_code=str(item.get("error_code") or "")[:100],
                    timings_ms={
                        str(stage)[:80]: max(0, int(duration or 0))
                        for stage, duration in (item.get("timings_ms") or {}).items()
                        if isinstance(stage, str)
                    } if isinstance(item.get("timings_ms"), dict) else {},
                )
        return cls(
            revision=max(0, int(raw.get("revision") or 0)),
            mode=str(raw.get("mode") or ManagerMode.IDLE),
            draining=bool(raw.get("draining")),
            pending_voice_session=str(raw.get("pending_voice_session") or ""),
            voice_session_id=str(raw.get("voice_session_id") or ""),
            voice_expires_epoch=max(0, int(raw.get("voice_expires_epoch") or 0)),
            active_operation_id=str(raw.get("active_operation_id") or ""),
            last_error_code=str(raw.get("last_error_code") or "")[:100],
            reconcile_required=bool(raw.get("reconcile_required")),
            sleep_inhibited=bool(raw.get("sleep_inhibited")),
            operations=operations,
            updated_epoch=max(0, int(raw.get("updated_epoch") or 0)),
        )
