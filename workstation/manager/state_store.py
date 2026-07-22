"""Atomic durable manager-state persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from workstation.manager.models import ManagerState


MANAGER_STATE_MAX_BYTES = 2 * 1024 * 1024


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> ManagerState:
        if not self.path.exists() and not self.path.is_symlink():
            return ManagerState()
        try:
            if (
                self.path.is_symlink()
                or not self.path.is_file()
                or not 0 < self.path.stat().st_size <= MANAGER_STATE_MAX_BYTES
            ):
                raise ValueError("manager state file is invalid")
            value = json.loads(self.path.read_bytes())
        except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("manager state is unreadable") from exc
        try:
            return ManagerState.from_dict(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("manager state is invalid") from exc

    def save(self, state: ManagerState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        descriptor, temporary = tempfile.mkstemp(prefix="manager-state-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state.to_dict(), stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
