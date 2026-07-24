"""Shared allowlist for website-to-Workstation control commands."""

from __future__ import annotations

import re
import json
from pathlib import Path

from workstation.config import WorkstationConfig


_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
REMOTE_RESTART_SERVICES = frozenset({
    "lmc-ai-node.service",
    "ollama.service",
    "lmc-ai-gpt-sovits.service",
})
REMOTE_CONTROL_MIN_WORKSTATION_VERSION = (1, 1, 0)
REMOTE_CONTROL_ACTIONS = frozenset({
    "status",
    "drain",
    "resume",
    "ack_reconcile",
    "cancel",
    "full_health",
    "restart_service",
    "reboot",
    "update",
    "rollback",
    "power_schedule",
    "workloads_apply",
    "workloads_rollback",
    "artifact_inspect",
    "artifact_models_install",
    "artifact_rag_install",
    "artifact_rag_rollback",
})


def validate_remote_command(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("remote control command must be an object")
    action = str(value.get("action") or "")
    if action not in REMOTE_CONTROL_ACTIONS:
        raise ValueError("remote control action is not allowlisted")
    if action in {
        "status", "drain", "resume", "ack_reconcile", "full_health",
        "reboot", "update", "rollback", "workloads_rollback",
        "artifact_inspect", "artifact_models_install",
        "artifact_rag_install", "artifact_rag_rollback",
    }:
        if set(value) != {"action"}:
            raise ValueError("remote control command fields are invalid")
        return {"action": action}
    if action == "cancel":
        if set(value) != {"action", "operation_id"}:
            raise ValueError("cancel command fields are invalid")
        operation_id = str(value.get("operation_id") or "")
        if not _ID_RE.fullmatch(operation_id):
            raise ValueError("cancel operation id is invalid")
        return {"action": action, "operation_id": operation_id}
    if action == "restart_service":
        if set(value) != {"action", "service"}:
            raise ValueError("restart command fields are invalid")
        service = str(value.get("service") or "")
        if service not in REMOTE_RESTART_SERVICES:
            raise ValueError("restart service is not allowlisted")
        return {"action": action, "service": service}
    if action == "power_schedule":
        required = {"action", "enabled", "suspend_at", "wake_at"}
        if set(value) != required or not isinstance(value.get("enabled"), bool):
            raise ValueError("power schedule fields are invalid")
        suspend_at = str(value.get("suspend_at") or "")
        wake_at = str(value.get("wake_at") or "")
        if (
            not _TIME_RE.fullmatch(suspend_at)
            or not _TIME_RE.fullmatch(wake_at)
            or suspend_at == wake_at
        ):
            raise ValueError("power schedule is invalid")
        return {
            "action": action,
            "enabled": value["enabled"],
            "suspend_at": suspend_at,
            "wake_at": wake_at,
        }
    required = {
        "action", "llm_enabled", "asr_enabled", "rag_enabled", "tts_enabled",
        "asr_model_id", "tts_voice_id",
    }
    if set(value) != required or any(
        not isinstance(value.get(key), bool)
        for key in ("llm_enabled", "asr_enabled", "rag_enabled", "tts_enabled")
    ):
        raise ValueError("workload settings fields are invalid")
    asr_model_id = str(value.get("asr_model_id") or "")
    tts_voice_id = str(value.get("tts_voice_id") or "")
    if asr_model_id and not _PROFILE_RE.fullmatch(asr_model_id):
        raise ValueError("ASR model id is invalid")
    if tts_voice_id and not _PROFILE_RE.fullmatch(tts_voice_id):
        raise ValueError("TTS voice id is invalid")
    if value["asr_enabled"] and not asr_model_id:
        raise ValueError("enabled ASR requires an installed model id")
    if value["tts_enabled"] and not tts_voice_id:
        raise ValueError("enabled TTS requires an installed voice id")
    return {
        "action": action,
        "llm_enabled": value["llm_enabled"],
        "asr_enabled": value["asr_enabled"],
        "rag_enabled": value["rag_enabled"],
        "tts_enabled": value["tts_enabled"],
        "asr_model_id": asr_model_id,
        "tts_voice_id": tts_voice_id,
    }


def installed_profiles(config: WorkstationConfig) -> dict:
    """Return IDs only; remote callers never receive or submit local paths."""
    asr_root = config.paths.data / "models" / "asr"
    asr = sorted(
        path.name for path in asr_root.iterdir()
        if (
            path.is_dir()
            and not path.is_symlink()
            and _PROFILE_RE.fullmatch(path.name)
            and (path / "config.json").is_file()
        )
    ) if asr_root.is_dir() else []
    voices: dict[str, dict] = {}
    candidates = [config.paths.data / "models" / "gpt-sovits"]
    voices_root = candidates[0] / "voices"
    if voices_root.is_dir():
        candidates.extend(
            path for path in voices_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        )
    for root in candidates:
        receipt_path = root / "active-receipt.json"
        try:
            if receipt_path.is_symlink() or not receipt_path.is_file():
                continue
            value = json.loads(receipt_path.read_bytes())
            identifier = str(value.get("model_version") or "")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if _PROFILE_RE.fullmatch(identifier):
            voices[identifier] = {"root": root, "receipt": value}
    selected_asr = Path(config.workloads.asr.model).name if config.workloads.asr.model else ""
    return {
        "asr": asr,
        "tts": sorted(voices),
        "selected": {
            "asr_model_id": selected_asr if selected_asr in asr else "",
            "tts_voice_id": (
                config.workloads.gpt_sovits.model_version
                if config.workloads.gpt_sovits.model_version in voices else ""
            ),
        },
    }


def resolve_workload_paths(
    config: WorkstationConfig, *, asr_model_id: str, tts_voice_id: str,
) -> dict:
    """Resolve already installed IDs inside fixed managed roots."""
    profiles = installed_profiles(config)
    result: dict = {}
    if asr_model_id:
        if asr_model_id not in profiles["asr"]:
            raise ValueError("ASR model id is not installed")
        result["asr_model"] = str(
            config.paths.data / "models" / "asr" / asr_model_id
        )
    if tts_voice_id:
        if tts_voice_id not in profiles["tts"]:
            raise ValueError("TTS voice id is not installed")
        roots = [config.paths.data / "models" / "gpt-sovits"]
        voices_root = roots[0] / "voices"
        if voices_root.is_dir():
            roots.extend(path for path in voices_root.iterdir() if path.is_dir())
        try:
            root = next(
                candidate for candidate in roots
                if (
                    (candidate / "active-receipt.json").is_file()
                    and json.loads(
                        (candidate / "active-receipt.json").read_bytes()
                    ).get("model_version") == tts_voice_id
                )
            )
            receipt = json.loads((root / "active-receipt.json").read_bytes())
            result["tts"] = {
                "model_version": tts_voice_id,
                "reference_audio": str(receipt["reference_audio"]["path"]),
                "reference_text_file": str(receipt["reference_text"]["path"]),
                "inference_config": str(receipt["inference_config"]["path"]),
                "approval_receipt": str(root / "active-receipt.json"),
            }
        except (
            OSError, StopIteration, KeyError, TypeError, ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ValueError("TTS voice receipt is invalid") from exc
    return result
