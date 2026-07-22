"""Benchmark-gated local Cantonese ASR adapter."""

from __future__ import annotations

from pathlib import Path
import json
import os
import hashlib
import re
import subprocess
import tempfile
import threading
import time

from system_limits import (
    LOCAL_PRACTICE_CONTEXT_MAX_CHARS,
    WORKSTATION_ASR_BENCHMARK_REPORT_MAX_BYTES,
)
from workstation.config import AsrConfig
from workstation.workloads.asr_integrity import AsrIntegrityError, verify_approval
from workstation.workloads.errors import WorkloadError


class FasterWhisperAdapter:
    def __init__(self, config: AsrConfig):
        self.config = config
        self._lock = threading.Lock()

    def _benchmark_approval(self) -> dict:
        try:
            path = self.config.benchmark_report
            if (
                not path.is_file()
                or path.stat().st_size <= 0
                or path.stat().st_size > WORKSTATION_ASR_BENCHMARK_REPORT_MAX_BYTES
            ):
                raise ValueError("benchmark report size is invalid")
            raw = path.read_bytes()
            report = json.loads(raw)
            required = {
                "schema_version", "generated_at_unix", "corpus_sha256",
                "required_categories", "sample_count", "results",
                "approval_written",
            }
            if (
                not isinstance(report, dict)
                or set(report) != required
                or report.get("schema_version") != 1
                or report.get("approval_written") is not False
                or not re.fullmatch(
                    r"[0-9a-f]{64}", str(report.get("corpus_sha256") or "")
                )
                or int(report.get("sample_count") or 0) <= 0
                or not isinstance(report.get("results"), list)
            ):
                raise ValueError("benchmark report schema is invalid")
            from workstation.scripts.benchmark_asr import REQUIRED_CATEGORIES

            if set(report.get("required_categories") or ()) != set(REQUIRED_CATEGORIES):
                raise ValueError("benchmark categories are incomplete")
            model = Path(self.config.model).resolve(strict=True)
            matched = next((
                result for result in report["results"]
                if isinstance(result, dict)
                and Path(str(result.get("model_path") or "")).resolve(strict=True) == model
                and result.get("device") == self.config.device
                and result.get("compute_type") == self.config.compute_type
            ), None)
            if matched is None:
                raise ValueError("approved ASR profile is absent from benchmark")
            return {"sha256": hashlib.sha256(raw).hexdigest()}
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def health(self) -> dict:
        if not self.config.enabled:
            return {"ok": False, "code": "disabled"}
        if not self.config.benchmark_approved:
            return {"ok": False, "code": "benchmark_required"}
        approval = self._benchmark_approval()
        if not approval:
            return {"ok": False, "code": "benchmark_report_mismatch"}
        if not self.config.runtime_python.is_file():
            return {"ok": False, "code": "runtime_missing"}
        model_path = Path(self.config.model)
        if not model_path.is_absolute() or not model_path.is_dir():
            return {"ok": False, "code": "model_missing"}
        try:
            verify_approval(self.config, full=False)
        except AsrIntegrityError:
            return {"ok": False, "code": "model_approval_mismatch"}
        try:
            probe = subprocess.run(
                [str(self.config.runtime_python), "-c", "import faster_whisper"],
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
        return {
            "ok": True,
            "model": self.config.model,
            "benchmark_report_sha256": approval["sha256"],
        }

    def verify_artifacts(self) -> dict:
        if not self.health().get("ok"):
            raise WorkloadError(
                "model_approval_mismatch", "Approved ASR profile is unavailable."
            )
        try:
            verify_approval(self.config, full=True)
        except AsrIntegrityError as exc:
            raise WorkloadError(
                "asr_digest_mismatch", "Approved ASR artifact digest changed."
            ) from exc
        return {"ok": True, "model": self.config.model}

    def transcribe(
        self,
        path: Path,
        *,
        timeout_seconds: int = 180,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        with self._lock:
            if not self.health().get("ok"):
                raise WorkloadError(
                    "asr_not_ready", "Local speech recognition is not ready."
                )
            worker = Path(__file__).with_name("asr_worker.py")
            descriptor, output_name = tempfile.mkstemp(
                prefix="asr-result-", suffix=".json", dir=path.parent,
            )
            os.close(descriptor)
            output = Path(output_name)
            output.unlink(missing_ok=True)
            command = [
                str(self.config.runtime_python), "-s", str(worker),
                "--model", self.config.model,
                "--audio", str(path),
                "--output", str(output),
                "--device", self.config.device,
                "--compute-type", self.config.compute_type,
            ]
            deadline = time.monotonic() + max(1, int(timeout_seconds))
            process = None
            result: dict = {}

            def stop_process(process) -> None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

            try:
                process = subprocess.Popen(
                    command,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        stop_process(process)
                        raise WorkloadError("cancelled", "Speech recognition was cancelled.")
                    if time.monotonic() >= deadline:
                        stop_process(process)
                        raise WorkloadError("asr_timeout", "Speech recognition timed out.")
                    time.sleep(0.1)
                if not output.is_file() or output.stat().st_size > 64 * 1024:
                    raise WorkloadError("asr_failed", "Local speech recognition failed.", retryable=True)
                result = json.loads(output.read_text(encoding="utf-8"))
            except WorkloadError:
                raise
            except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
                raise WorkloadError("asr_failed", "Local speech recognition failed.", retryable=True) from exc
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
                code, "Local speech recognition failed.", retryable=code == "asr_failed",
            )
        transcript = str(result.get("text") or "").strip()
        if not transcript or len(transcript) > LOCAL_PRACTICE_CONTEXT_MAX_CHARS:
            raise WorkloadError("empty_transcript", "No speech could be recognised.")
        return {**result, "model": self.config.model}

    def unload(self) -> None:
        # The pinned worker exits after each request, releasing its CUDA context.
        return None
