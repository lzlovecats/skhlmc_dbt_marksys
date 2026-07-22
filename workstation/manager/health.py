"""Bounded preflight and health inventory for the Workstation manager."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from ai_model_config import lmc_ai_required_models
from system_limits import (
    WORKSTATION_CACHE_QUOTA_BYTES,
    WORKSTATION_CHECKPOINT_QUOTA_BYTES,
    WORKSTATION_DATASET_QUOTA_BYTES,
    WORKSTATION_MIN_FREE_DISK_BYTES,
    WORKSTATION_MAX_GPU_TEMPERATURE_C,
    WORKSTATION_MIN_AVAILABLE_RAM_BYTES,
    WORKSTATION_MIN_GPU_VRAM_MIB,
    WORKSTATION_MIN_RAM_BYTES,
    WORKSTATION_R2_HEALTH_RECEIPT_MAX_AGE_SECONDS,
)
from workstation.config import WorkstationConfig
from workstation.workloads.asr import FasterWhisperAdapter
from workstation.workloads.dataset_preparation import DatasetPreparationAdapter
from workstation.workloads.gpt_sovits import GptSoVitsAdapter
from workstation.workloads.ollama import OllamaAdapter
from workstation.workloads.rag import LocalRagIndex
from workstation.workloads.storage import directory_bytes, quota_entry
from workstation.workloads.media import probe_audio
from workstation.workloads.errors import WorkloadError


def _command(command: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


class HealthRunner:
    def __init__(self, config: WorkstationConfig):
        self.config = config
        self.ollama = OllamaAdapter(config.workloads.ollama)
        self.asr = FasterWhisperAdapter(config.workloads.asr)
        self.rag = LocalRagIndex(config.workloads.rag, self.ollama)
        self.gpt_sovits = GptSoVitsAdapter(config.workloads.gpt_sovits)
        self.dataset_preparation = DatasetPreparationAdapter(config.paths.data)

    def _state_json(self, name: str, *, maximum_bytes: int = 64 * 1024) -> dict:
        path = self.config.paths.state / name
        if (
            path.is_symlink()
            or not path.is_file()
            or not 0 < path.stat().st_size <= maximum_bytes
        ):
            raise ValueError("Workstation state receipt is invalid")
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("Workstation state receipt is invalid")
        return value

    def _receipt(self, name: str, identity: str, *, maximum_age_seconds: int) -> dict:
        try:
            value = self._state_json(name)
            current = int(time.time())
            checked = int(value.get("checked_epoch") or 0)
            if (
                value.get("identity") != identity
                or checked <= current - maximum_age_seconds
                or checked > current + 300
            ):
                raise ValueError("receipt is stale")
            return {"ok": True, "checked_epoch": checked, "identity": identity}
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            return {"ok": False, "code": "full_probe_required"}

    def _write_receipt(self, name: str, identity: str, **details) -> dict:
        self.config.paths.state.mkdir(parents=True, exist_ok=True, mode=0o750)
        value = {
            "identity": identity,
            "checked_epoch": int(time.time()),
            **details,
        }
        destination = self.config.paths.state / name
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o640)
        os.replace(temporary, destination)
        return {"ok": True, **value}

    def _asr_identity(self) -> str:
        digest = hashlib.sha256()
        digest.update(
            (
                f"{self.config.workloads.asr.model}\0"
                f"{self.config.workloads.asr.runtime_python}\0"
                f"{self.config.workloads.asr.device}\0"
                f"{self.config.workloads.asr.compute_type}"
            ).encode()
        )
        for path in (
            self.config.workloads.asr.benchmark_report,
            self.config.workloads.asr.runtime_provenance,
            self.config.workloads.asr.approval_receipt,
        ):
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"missing")
        return digest.hexdigest()

    def _ollama_health(self) -> dict:
        required = tuple(lmc_ai_required_models())
        status = self.ollama.health(required)
        if not status.get("ok"):
            return status
        try:
            receipt = json.loads(
                (self.config.paths.data / "models" / "active-receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            approved = receipt.get("models") if isinstance(receipt, dict) else None
            if (
                not isinstance(approved, dict)
                or set(approved) != set(required)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", str(receipt.get("id") or ""))
                or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("sha256") or "").lower())
            ):
                raise ValueError("model approval receipt is invalid")
            expected = {}
            for model, details in approved.items():
                if not isinstance(details, dict) or set(details) != {"digest", "bytes"}:
                    raise ValueError("model approval entry is invalid")
                digest = str(details.get("digest") or "").lower()
                if not re.fullmatch(r"[0-9a-f]{64}", digest) or int(details.get("bytes") or 0) <= 0:
                    raise ValueError("model approval entry is invalid")
                expected[model] = digest
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            return {
                **status,
                "ok": False,
                "code": "model_approval_receipt_invalid",
            }
        actual = status.get("model_digests") or {}
        mismatched = sorted(
            model for model, digest in expected.items()
            if actual.get(model) != digest
        )
        return {
            **status,
            "ok": not mismatched,
            "code": "model_digest_mismatch" if mismatched else "",
            "mismatched_models": mismatched,
        }

    def _tts_identity(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.config.workloads.gpt_sovits.model_version.encode())
        digest.update(str(self.config.workloads.gpt_sovits.runtime_root).encode())
        try:
            digest.update(
                (self.config.workloads.gpt_sovits.runtime_root / "APPROVED_COMMIT").read_bytes()
            )
        except OSError:
            digest.update(b"missing")
        for path in (
            self.config.workloads.gpt_sovits.reference_audio,
            self.config.workloads.gpt_sovits.reference_text_file,
            self.config.workloads.gpt_sovits.inference_config,
            self.config.workloads.gpt_sovits.approval_receipt,
        ):
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"missing")
        return digest.hexdigest()

    def _asr_shallow(self) -> dict:
        static = self.asr.health()
        if not static.get("ok"):
            return static
        return {
            **static,
            **self._receipt(
                "asr-preflight.json", self._asr_identity(),
                maximum_age_seconds=24 * 60 * 60,
            ),
        }

    def _tts_shallow(self) -> dict:
        static = self.gpt_sovits.health()
        if not static.get("ok"):
            return static
        return {
            **static,
            **self._receipt(
                "gpt-sovits-preflight.json", self._tts_identity(),
                maximum_age_seconds=24 * 60 * 60,
            ),
        }

    def _connection_receipt(self, name: str, *, maximum_age_seconds: int) -> dict:
        try:
            value = self._state_json(name)
            current = int(time.time())
            checked = int(value.get("checked_epoch") or 0)
            if checked <= current - maximum_age_seconds or checked > current + 300:
                raise ValueError("stale")
            return {"ok": True, "checked_epoch": checked}
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            return {"ok": False, "code": "receipt_unavailable"}

    def _full_probes(
        self, *, set_gpt_service, prepare_non_ollama, probe_r2=None,
    ) -> dict:
        results = {}
        try:
            if probe_r2 is None:
                raise WorkloadError(
                    "r2_probe_unavailable", "Direct-R2 probe is unavailable."
                )
            probe = probe_r2()
            if not isinstance(probe, dict) or probe.get("ok") is not True:
                raise WorkloadError("r2_probe_failed", "Direct-R2 probe failed.")
            results["r2_probe"] = probe
        except Exception as exc:
            try:
                (self.config.paths.state / "r2-health.json").unlink(
                    missing_ok=True
                )
            except OSError:
                pass
            results["r2_probe"] = {
                "ok": False,
                "code": (
                    exc.code if isinstance(exc, WorkloadError)
                    else "r2_probe_failed"
                ),
            }
        sample = self.config.paths.data / "health" / "asr-cantonese.wav"
        expected_file = self.config.paths.data / "health" / "asr-cantonese.txt"
        try:
            prepare_non_ollama()
            self.asr.verify_artifacts()
            expected = expected_file.read_text(encoding="utf-8").strip()
            if not sample.is_file() or not expected:
                raise OSError("sample unavailable")
            transcript = self.asr.transcribe(sample)
            normalized_expected = "".join(expected.split()).casefold()
            normalized_actual = "".join(str(transcript.get("text") or "").split()).casefold()
            if normalized_expected not in normalized_actual:
                raise WorkloadError("asr_probe_mismatch", "Cantonese ASR probe text did not match.")
            results["asr_probe"] = self._write_receipt(
                "asr-preflight.json", self._asr_identity(),
                transcript_sha256=hashlib.sha256(normalized_actual.encode()).hexdigest(),
            )
        except (OSError, WorkloadError):
            results["asr_probe"] = {"ok": False, "code": "asr_probe_failed"}
        finally:
            self.asr.unload()
        try:
            retrieval = self.rag.retrieve("香港辯論", top_k=1)
            if not retrieval.get("results"):
                raise WorkloadError("rag_probe_empty", "RAG probe returned no result.")
            results["rag_probe"] = {
                "ok": True,
                "bundle_version": retrieval.get("bundle_version"),
            }
        except WorkloadError as exc:
            results["rag_probe"] = {"ok": False, "code": exc.code}
        output = None
        started = False
        try:
            prepare_non_ollama()
            self.gpt_sovits.verify_artifacts()
            set_gpt_service("start")
            started = True
            self.gpt_sovits.wait_until_ready()
            output = self.gpt_sovits.synthesize(
                "更新後語音健康檢查。", output_dir=self.config.paths.cache,
            )
            media = probe_audio(
                Path(output["path"]), maximum_seconds=30, declared_mime="audio/wav",
            )
            results["gpt_sovits_probe"] = self._write_receipt(
                "gpt-sovits-preflight.json", self._tts_identity(),
                duration_seconds=media["duration_seconds"],
            )
        except (OSError, WorkloadError):
            results["gpt_sovits_probe"] = {"ok": False, "code": "gpt_sovits_probe_failed"}
        finally:
            if output:
                Path(output["path"]).unlink(missing_ok=True)
            if started:
                try:
                    set_gpt_service("stop")
                except WorkloadError:
                    results["gpt_sovits_probe"] = {"ok": False, "code": "gpt_sovits_stop_failed"}
        return results

    @staticmethod
    def _directory_bytes(path: Path) -> int | None:
        return directory_bytes(path)

    def _inventory(self) -> dict:
        data = self.config.paths.data
        datasets = self.dataset_preparation.inventory()
        checkpoints = data / "checkpoints"
        model_root = data / "models"
        usage = {
            "datasets": self._directory_bytes(data / "datasets"),
            "checkpoints": self._directory_bytes(checkpoints),
            "cache": self._directory_bytes(self.config.paths.cache),
            "models": self._directory_bytes(model_root),
            "rag": self._directory_bytes(data / "rag"),
        }
        quotas = {
            "datasets": quota_entry(usage["datasets"], WORKSTATION_DATASET_QUOTA_BYTES),
            "checkpoints": quota_entry(usage["checkpoints"], WORKSTATION_CHECKPOINT_QUOTA_BYTES),
            "cache": quota_entry(usage["cache"], WORKSTATION_CACHE_QUOTA_BYTES),
        }
        return {
            "datasets": datasets,
            "checkpoints": sorted(
                path.name for path in checkpoints.iterdir()
                if path.is_dir() and len(path.name) <= 200
            ) if checkpoints.is_dir() else [],
            "model_directories": sorted(
                path.name for path in model_root.iterdir()
                if path.is_dir() and len(path.name) <= 200
            ) if model_root.is_dir() else [],
            "usage_bytes": usage,
            "quotas": quotas,
            "quota_status": "ok" if all(item["ok"] for item in quotas.values()) else "blocked",
        }

    def _os(self) -> dict:
        try:
            values = {}
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value.strip().strip('"')
            ok = values.get("ID") == "ubuntu" and values.get("VERSION_ID") == "24.04"
            return {"ok": ok, "id": values.get("ID", ""), "version": values.get("VERSION_ID", "")}
        except OSError:
            return {"ok": False, "code": "os_release_unavailable"}

    def _gpu(self) -> dict:
        try:
            result = _command([
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total,memory.used,temperature.gpu",
                "--format=csv,noheader,nounits",
            ], timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "code": "nvidia_smi_unavailable"}
        if result.returncode or not result.stdout.strip():
            return {"ok": False, "code": "nvidia_smi_failed"}
        fields = [item.strip() for item in result.stdout.splitlines()[0].split(",")]
        if len(fields) != 5:
            return {"ok": False, "code": "nvidia_smi_invalid"}
        try:
            vram_total = int(fields[2])
            temperature = int(fields[4])
            return {
                "ok": bool(
                    vram_total >= WORKSTATION_MIN_GPU_VRAM_MIB
                    and temperature <= WORKSTATION_MAX_GPU_TEMPERATURE_C
                ),
                "name": fields[0][:100],
                "driver": fields[1][:40],
                "vram_total_mib": vram_total,
                "vram_used_mib": int(fields[3]),
                "temperature_c": temperature,
                "minimum_vram_mib": WORKSTATION_MIN_GPU_VRAM_MIB,
                "maximum_temperature_c": WORKSTATION_MAX_GPU_TEMPERATURE_C,
            }
        except ValueError:
            return {"ok": False, "code": "nvidia_smi_invalid"}

    def _disk(self) -> dict:
        try:
            usage = shutil.disk_usage(self.config.paths.data)
            return {
                "ok": usage.free >= WORKSTATION_MIN_FREE_DISK_BYTES,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "minimum_free_bytes": WORKSTATION_MIN_FREE_DISK_BYTES,
            }
        except OSError:
            return {"ok": False, "code": "disk_unavailable"}

    @staticmethod
    def _memory() -> dict:
        try:
            values = {}
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0]) * 1_024
            total = values.get("MemTotal", 0)
            available = values.get("MemAvailable", 0)
            return {
                "ok": bool(
                    total >= WORKSTATION_MIN_RAM_BYTES
                    and available >= WORKSTATION_MIN_AVAILABLE_RAM_BYTES
                ),
                "total_bytes": total,
                "available_bytes": available,
                "minimum_total_bytes": WORKSTATION_MIN_RAM_BYTES,
                "minimum_available_bytes": WORKSTATION_MIN_AVAILABLE_RAM_BYTES,
            }
        except (OSError, ValueError, KeyError):
            return {"ok": False, "code": "memory_unavailable"}

    @staticmethod
    def _power_tools() -> dict:
        commands_present = bool(
            shutil.which("systemd-inhibit")
            and shutil.which("rtcwake")
            and shutil.which("systemctl")
        )
        rtc_probe = None
        inhibitor_probe = None
        suspend_supported = False
        if commands_present:
            try:
                rtc_probe = _command(["rtcwake", "--mode", "show"], timeout=10)
                inhibitor_probe = _command([
                    "systemd-inhibit", "--list", "--no-legend", "--no-pager",
                ], timeout=10)
                states = Path("/sys/power/state").read_text(
                    encoding="ascii"
                ).split()
                suspend_supported = "mem" in states
            except (OSError, subprocess.TimeoutExpired):
                pass
        return {
            "ok": bool(
                commands_present
                and rtc_probe is not None
                and rtc_probe.returncode == 0
                and inhibitor_probe is not None
                and inhibitor_probe.returncode == 0
                and suspend_supported
            ),
            "systemd_inhibit": bool(
                inhibitor_probe is not None and inhibitor_probe.returncode == 0
            ),
            "rtcwake": bool(rtc_probe is not None and rtc_probe.returncode == 0),
            "suspend_mem": suspend_supported,
        }

    def run(
        self,
        *,
        required_capabilities: tuple[str, ...] = (),
        full: bool = False,
        set_gpt_service=None,
        prepare_non_ollama=None,
        probe_r2=None,
    ) -> dict:
        inventory = self._inventory()
        checks = {
            "os": self._os(),
            "gpu": self._gpu(),
            "memory": self._memory(),
            "disk": self._disk(),
            "power": self._power_tools(),
            "ollama": self._ollama_health(),
            "asr": self._asr_shallow(),
            "rag": self.rag.health(),
            "gpt_sovits": self._tts_shallow(),
            "gpt_sovits_training": self.gpt_sovits.training_health(),
            "quota": {"ok": inventory["quota_status"] == "ok"},
            "wss": self._connection_receipt("website.json", maximum_age_seconds=120),
            "r2": self._connection_receipt(
                "r2-health.json",
                maximum_age_seconds=(
                    WORKSTATION_R2_HEALTH_RECEIPT_MAX_AGE_SECONDS
                ),
            ),
        }
        base = ("os", "gpu", "memory", "disk", "power", "ollama")
        if full:
            probes = self._full_probes(
                set_gpt_service=set_gpt_service,
                prepare_non_ollama=prepare_non_ollama,
                probe_r2=probe_r2,
            )
            checks.update(probes)
            checks["asr"] = self._asr_shallow()
            checks["gpt_sovits"] = self._tts_shallow()
            checks["r2"] = dict(probes.get("r2_probe") or {
                "ok": False, "code": "r2_probe_failed",
            })
            required_capabilities = tuple(dict.fromkeys((
                *required_capabilities,
                "asr", "rag", "gpt_sovits", "gpt_sovits_training",
                "quota", "wss", "r2",
                "asr_probe", "rag_probe", "gpt_sovits_probe", "r2_probe",
            )))
        required = tuple(dict.fromkeys((*base, *required_capabilities)))
        healthy = all(bool((checks.get(name) or {}).get("ok")) for name in required)
        return {
            "healthy": healthy,
            "required": list(required),
            "checks": checks,
            "checked_epoch": int(time.time()),
            "inventory": inventory,
        }

    def write_report(self, report: dict) -> Path:
        destination = self.config.paths.state / "health.json"
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o640)
        os.replace(temporary, destination)
        return destination
