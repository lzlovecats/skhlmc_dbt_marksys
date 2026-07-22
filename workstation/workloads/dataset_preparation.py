"""Allowlisted wrapper around the audited GPT-SoVITS dataset preparer."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import threading
import time

from system_limits import WORKSTATION_DATASET_PREPARATION_MAX_SECONDS
from workstation.workloads.errors import WorkloadError


_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_SPEAKER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,99}")


class DatasetPreparationAdapter:
    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.script = Path(__file__).resolve().parents[2] / "tools" / "prepare_gpt_sovits_dataset.py"

    def inventory(self) -> dict:
        datasets_root = self.data_root / "datasets"
        incoming = datasets_root / "incoming"
        prepared = datasets_root / "prepared"
        return {
            "incoming": sorted(
                path.stem for path in incoming.iterdir()
                if path.is_file() and path.suffix.lower() in {".json", ".zip"}
                and _ID_RE.fullmatch(path.stem)
            ) if incoming.is_dir() else [],
            "prepared": sorted(
                path.name for path in prepared.iterdir()
                if path.is_dir() and _ID_RE.fullmatch(path.name)
                and (path / "preparation_result.json").is_file()
            ) if prepared.is_dir() else [],
        }

    def prepare(
        self,
        *,
        dataset_id: str,
        speaker: str,
        cancel_event: threading.Event,
        resource_gate=None,
    ) -> Path:
        if not _ID_RE.fullmatch(str(dataset_id or "")):
            raise WorkloadError("invalid_dataset", "Dataset identifier is invalid.")
        if speaker and not _SPEAKER_RE.fullmatch(str(speaker)):
            raise WorkloadError("invalid_speaker", "Dataset speaker identifier is invalid.")
        incoming = self.data_root / "datasets" / "incoming"
        candidates = [
            path for path in (incoming / f"{dataset_id}.json", incoming / f"{dataset_id}.zip")
            if path.is_file()
        ]
        output = self.data_root / "datasets" / "prepared" / dataset_id
        progress = self.data_root / "datasets" / "progress" / f"{dataset_id}.json"
        if len(candidates) != 1 or output.exists() or not self.script.is_file():
            raise WorkloadError(
                "dataset_input_invalid",
                "Dataset requires exactly one new allowlisted manifest or archive.",
            )
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        progress.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        command = [
            "/usr/bin/python3",
            str(self.script),
            str(candidates[0]),
            "--output-dir",
            str(output),
            "--progress-file",
            str(progress),
        ]
        if speaker:
            command.extend(["--speaker", speaker])
        deadline = time.monotonic() + WORKSTATION_DATASET_PREPARATION_MAX_SECONDS
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            while process.poll() is None:
                if cancel_event.is_set() or time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    code = "cancelled" if cancel_event.is_set() else "dataset_timeout"
                    raise WorkloadError(code, "Dataset preparation did not complete.")
                if resource_gate is not None:
                    try:
                        resource_gate()
                    except WorkloadError:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=10)
                        raise
                time.sleep(0.5)
        except WorkloadError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkloadError(
                "dataset_preparation_failed", "Dataset preparation could not start."
            ) from exc
        if process.returncode or not (output / "preparation_result.json").is_file():
            raise WorkloadError(
                "dataset_preparation_failed", "Dataset preparation did not pass its gates."
            )
        return output
