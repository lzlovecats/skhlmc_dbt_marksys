"""Official Qwen3-ASR adapter for local Cantonese transcription."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import select
import tempfile
import threading
import time

from system_limits import (
    LOCAL_PRACTICE_CONTEXT_MAX_CHARS,
    WORKSTATION_ASR_PREWARM_TTL_SECONDS,
)
from workstation.config import AsrConfig
from workstation.workloads.errors import WorkloadError


class Qwen3AsrAdapter:
    def __init__(self, config: AsrConfig):
        self.config = config
        self._lock = threading.Lock()
        self._prepared_process = None
        self._prepared_deadline = 0.0

    @staticmethod
    def _environment() -> dict:
        return {
            **os.environ,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    @staticmethod
    def _stop_process(process) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def _worker_command(self, *, serve: bool = False) -> list[str]:
        command = [
            str(self.config.runtime_python), "-s",
            str(Path(__file__).with_name("asr_worker.py")),
            "--model", self.config.model,
            "--device", self.config.device,
            "--compute-type", self.config.compute_type,
        ]
        if serve:
            command.append("--serve")
        return command

    def prepare(
        self, *, timeout_seconds: int = 180,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        with self._lock:
            if not self.health().get("ok"):
                raise WorkloadError("asr_not_ready", "Local speech recognition is not ready.")
            current = self._prepared_process
            if current is not None and current.poll() is None:
                self._prepared_deadline = time.monotonic() + WORKSTATION_ASR_PREWARM_TTL_SECONDS
                return {"prepared": True, "reused": True}
            self._stop_process(current)
            process = subprocess.Popen(
                self._worker_command(serve=True),
                env=self._environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + max(1, int(timeout_seconds))
            ready = False
            try:
                while process.poll() is None and time.monotonic() < deadline:
                    if cancel_event is not None and cancel_event.is_set():
                        self._stop_process(process)
                        raise WorkloadError(
                            "cancelled", "ASR prewarm was cancelled."
                        )
                    readable, _writeable, _errors = select.select(
                        [process.stdout], [], [], 0.1,
                    )
                    if readable and process.stdout.readline(64) == b"READY\n":
                        ready = True
                        break
                if not ready:
                    self._stop_process(process)
                    raise WorkloadError(
                        "asr_model_load", "ASR model could not be prewarmed.",
                        retryable=True,
                    )
                self._prepared_process = process
                self._prepared_deadline = (
                    time.monotonic() + WORKSTATION_ASR_PREWARM_TTL_SECONDS
                )
                threading.Thread(
                    target=self._expire_prepared,
                    args=(process,),
                    name="lmc-asr-prewarm-ttl",
                    daemon=True,
                ).start()
                return {"prepared": True, "reused": False}
            except Exception:
                if not ready:
                    self._stop_process(process)
                raise

    def _expire_prepared(self, process) -> None:
        while True:
            with self._lock:
                if self._prepared_process is not process or process.poll() is not None:
                    return
                remaining = self._prepared_deadline - time.monotonic()
                if remaining <= 0:
                    self._prepared_process = None
                    self._prepared_deadline = 0.0
                    self._stop_process(process)
                    return
            time.sleep(min(1.0, max(0.1, remaining)))

    def health(self) -> dict:
        if not self.config.enabled:
            return {"ok": False, "code": "disabled"}
        if not self.config.runtime_python.is_file():
            return {"ok": False, "code": "runtime_missing"}
        model_path = Path(self.config.model)
        if (
            not model_path.is_absolute()
            or model_path.is_symlink()
            or not model_path.is_dir()
            or not (model_path / "config.json").is_file()
        ):
            return {"ok": False, "code": "model_missing"}
        try:
            probe = subprocess.run(
                [
                    str(self.config.runtime_python),
                    "-c",
                    "from qwen_asr import Qwen3ASRModel",
                ],
                env={
                    **os.environ,
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "code": "runtime_missing"}
        if probe.returncode:
            return {"ok": False, "code": "runtime_missing"}
        return {"ok": True, "model": self.config.model, "backend": "qwen3-asr"}

    def verify_artifacts(self) -> dict:
        status = self.health()
        if not status.get("ok"):
            raise WorkloadError(
                "asr_not_ready", "Local speech recognition is not ready."
            )
        return status

    def transcribe(
        self,
        path: Path,
        *,
        timeout_seconds: int = 180,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        with self._lock:
            prepared = self._prepared_process
            prepared_alive = prepared is not None and prepared.poll() is None
            if not prepared_alive and not self.health().get("ok"):
                raise WorkloadError(
                    "asr_not_ready", "Local speech recognition is not ready."
                )
            descriptor, output_name = tempfile.mkstemp(
                prefix="asr-result-", suffix=".json", dir=path.parent,
            )
            os.close(descriptor)
            output = Path(output_name)
            output.unlink(missing_ok=True)
            process = None
            use_prepared = prepared_alive
            if use_prepared:
                process = prepared
                self._prepared_process = None
                self._prepared_deadline = 0.0
                command = None
            else:
                command = [
                    *self._worker_command(), "--audio", str(path),
                    "--output", str(output),
                ]
            deadline = time.monotonic() + max(1, int(timeout_seconds))
            result: dict = {}

            try:
                if use_prepared:
                    process.stdin.write(json.dumps({
                        "audio": str(path), "output": str(output),
                    }, separators=(",", ":")).encode("utf-8") + b"\n")
                    process.stdin.flush()
                    process.stdin.close()
                else:
                    process = subprocess.Popen(
                        command,
                        env=self._environment(),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        self._stop_process(process)
                        raise WorkloadError(
                            "cancelled", "Speech recognition was cancelled."
                        )
                    if time.monotonic() >= deadline:
                        self._stop_process(process)
                        raise WorkloadError(
                            "asr_timeout", "Speech recognition timed out."
                        )
                    time.sleep(0.1)
                if not output.is_file() or output.stat().st_size > 64 * 1024:
                    raise WorkloadError(
                        "asr_failed",
                        "Local speech recognition failed.",
                        retryable=True,
                    )
                result = json.loads(output.read_text(encoding="utf-8"))
            except WorkloadError:
                raise
            except (
                OSError,
                ValueError,
                TypeError,
                json.JSONDecodeError,
                subprocess.SubprocessError,
            ) as exc:
                raise WorkloadError(
                    "asr_failed",
                    "Local speech recognition failed.",
                    retryable=True,
                ) from exc
            finally:
                output.unlink(missing_ok=True)
        if (
            process is None
            or process.returncode != 0
            or not isinstance(result, dict)
            or result.get("ok") is not True
        ):
            code = str((result or {}).get("code") or "asr_failed")
            if code not in {"empty_transcript", "out_of_memory", "asr_failed"}:
                code = "asr_failed"
            raise WorkloadError(
                code,
                "Local speech recognition failed.",
                retryable=code == "asr_failed",
            )
        transcript = str(result.get("text") or "").strip()
        if not transcript or len(transcript) > LOCAL_PRACTICE_CONTEXT_MAX_CHARS:
            raise WorkloadError("empty_transcript", "No speech could be recognised.")
        return {**result, "model": self.config.model}

    def unload(self) -> None:
        with self._lock:
            process = self._prepared_process
            self._prepared_process = None
            self._prepared_deadline = 0.0
            self._stop_process(process)
