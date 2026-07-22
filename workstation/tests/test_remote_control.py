from __future__ import annotations

import json

import pytest

from workstation.config import parse_config
from workstation.manager.arbiter import ModeArbiter
from workstation.manager.ipc import ManagerApplication
from workstation.manager.state_store import StateStore
from workstation.privileged_helper.protocol import validate_request
from workstation.remote_control import (
    installed_profiles,
    resolve_workload_paths,
    validate_remote_command,
)


def _config(tmp_path):
    return parse_config({
        "schema_version": 1,
        "node": {
            "name": "AI Workstation",
            "server_url": "https://example.com",
            "token_file": str(tmp_path / "token"),
        },
        "paths": {
            "state": str(tmp_path / "state"),
            "cache": str(tmp_path / "cache"),
            "data": str(tmp_path / "data"),
            "releases": str(tmp_path / "releases"),
        },
        "power": {},
        "workloads": {},
        "gui": {},
    })


def test_remote_control_rejects_shell_paths_urls_and_unknown_fields():
    assert validate_remote_command({"action": "drain"}) == {"action": "drain"}
    with pytest.raises(ValueError, match="allowlisted"):
        validate_remote_command({"action": "shell", "command": "id"})
    with pytest.raises(ValueError, match="fields"):
        validate_remote_command({"action": "update", "url": "https://evil.test"})
    with pytest.raises(ValueError, match="model id"):
        validate_remote_command({
            "action": "workloads_apply",
            "llm_enabled": True,
            "asr_enabled": True,
            "rag_enabled": False,
            "tts_enabled": False,
            "asr_model_id": "../../escape",
            "tts_voice_id": "",
        })
    with pytest.raises(ValueError, match="allowlisted"):
        validate_remote_command({
            "action": "restart_service", "service": "ssh.service",
        })


def test_installed_profile_ids_resolve_only_under_managed_roots(tmp_path):
    config = _config(tmp_path)
    asr = config.paths.data / "models/asr/Qwen3-ASR-1.7B"
    asr.mkdir(parents=True)
    (asr / "config.json").write_text("{}")
    voice = config.paths.data / "models/gpt-sovits/voices/achoi-v1"
    voice.mkdir(parents=True)
    paths = {
        "reference_audio": tmp_path / "reference.wav",
        "reference_text": tmp_path / "reference.txt",
        "inference_config": voice / "tts_infer.json",
    }
    for path in paths.values():
        path.write_text("test")
    (voice / "active-receipt.json").write_text(json.dumps({
        "model_version": "achoi-v1",
        **{key: {"path": str(path)} for key, path in paths.items()},
    }))

    profiles = installed_profiles(config)
    assert profiles["asr"] == ["Qwen3-ASR-1.7B"]
    assert profiles["tts"] == ["achoi-v1"]
    resolved = resolve_workload_paths(
        config,
        asr_model_id="Qwen3-ASR-1.7B",
        tts_voice_id="achoi-v1",
    )
    assert resolved["asr_model"] == str(asr)
    assert resolved["tts"]["approval_receipt"] == str(
        voice / "active-receipt.json"
    )
    with pytest.raises(ValueError, match="not installed"):
        resolve_workload_paths(
            config, asr_model_id="missing", tts_voice_id="",
        )


def test_privileged_workload_contract_carries_ids_not_paths():
    clean = validate_request({
        "action": "set_workloads",
        "llm_enabled": True,
        "asr_enabled": True,
        "rag_enabled": False,
        "tts_enabled": False,
        "asr_model_id": "Qwen3-ASR-1.7B",
        "tts_voice_id": "",
    })
    assert clean["asr_model_id"] == "Qwen3-ASR-1.7B"
    assert not any("/" in str(value) for value in clean.values())


def test_manager_remote_drain_is_audited_without_exposing_content(tmp_path):
    config = _config(tmp_path)
    application = ManagerApplication(
        config, ModeArbiter(StateStore(tmp_path / "manager.json")),
    )
    emitted = []
    application.handle(
        {"action": "remote.control", "command": {"action": "drain"}},
        lambda event, payload: emitted.append((event, payload)),
    )
    assert emitted[-1][1]["manager"]["draining"] is True
    audit = json.loads(
        (config.paths.state / "remote-control-audit.json").read_text()
    )
    assert [row["outcome"] for row in audit] == ["started", "succeeded"]
    assert set(audit[0]) == {"epoch", "action", "outcome", "code"}
