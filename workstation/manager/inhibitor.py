"""Explicit systemd sleep inhibitor for every managed workload."""

from __future__ import annotations

import subprocess
import threading


class SleepInhibitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._process and self._process.poll() is None)

    def acquire(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                return
            self._process = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=sleep",
                    "--who=lmc-ai-workstation",
                    "--why=managed AI workload is active",
                    "--mode=block",
                    "sleep",
                    "infinity",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            if self._process.poll() is not None:
                self._process = None
                raise RuntimeError("failed to acquire system sleep inhibitor")

    def release(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
