from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstation.config import ConfigError, load_config, parse_config, read_secret
from workstation.manager.arbiter import ArbitrationError, ModeArbiter
from workstation.manager.models import ManagerMode
from workstation.manager.state_store import StateStore


def _config(tmp_path: Path) -> dict:
    return {
        "schema_version": 1,
        "node": {
            "name": "AI Workstation 1",
            "server_url": "https://example.com",
            "token_file": str(tmp_path / "token"),
        },
        "paths": {
            "state": str(tmp_path / "state"),
            "cache": str(tmp_path / "cache"),
            "data": str(tmp_path / "data"),
            "releases": str(tmp_path / "releases"),
        },
        "power": {"enabled": True, "timezone": "Asia/Hong_Kong", "suspend_at": "00:00", "wake_at": "08:00"},
        "workloads": {},
    }


def test_typed_config_keeps_secrets_as_references(tmp_path):
    config = parse_config(_config(tmp_path))
    assert config.node.server_url == "wss://example.com/api/lmc-ai/nodes/connect"
    assert "token_file" not in config.public_dict()["node"]


def test_config_state_and_secrets_reject_symlinks_or_oversize_files(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config(tmp_path)))
    assert load_config(config_path).node.name == "AI Workstation 1"
    config_path.write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(ConfigError):
        load_config(config_path)

    secret = tmp_path / "secret"
    secret.write_text("private-token")
    secret.chmod(0o600)
    link = tmp_path / "secret-link"
    link.symlink_to(secret)
    with pytest.raises(ConfigError):
        read_secret(link)

    state = tmp_path / "oversize-state.json"
    state.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(RuntimeError, match="unreadable"):
        StateStore(state).load()


def test_asr_requires_an_absolute_local_model_and_supported_qwen_dtype(tmp_path):
    value = _config(tmp_path)
    value["workloads"] = {"asr": {"enabled": True, "model": "candidate"}}
    with pytest.raises(ConfigError, match="absolute local path"):
        parse_config(value)
    value["workloads"]["asr"].update({
        "model": str(tmp_path / "Qwen3-ASR-1.7B"),
        "compute_type": "int8",
        "runtime_python": str(tmp_path / "asr-runtime/bin/python"),
    })
    with pytest.raises(ConfigError, match="compute_type"):
        parse_config(value)
    value["workloads"]["asr"]["compute_type"] = "bfloat16"
    assert parse_config(value).workloads.asr.compute_type == "bfloat16"


def test_voice_waits_for_started_text_then_blocks_new_text(tmp_path):
    arbiter = ModeArbiter(StateStore(tmp_path / "state.json"))
    arbiter.start_operation("text-1", "text")
    assert arbiter.reserve_voice("session-1") == "waiting_for_text"
    with pytest.raises(ArbitrationError) as raised:
        arbiter.start_operation("text-2", "text")
    assert raised.value.code == "busy"
    arbiter.finish_operation("text-1", success=True)
    assert arbiter.snapshot()["mode"] == ManagerMode.VOICE_COACH
    assert arbiter.snapshot()["voice_session_active"] is True
    arbiter.start_operation("asr-1", "asr", session_id="session-1", turn_id="turn-1")
    arbiter.finish_operation("asr-1", success=True)
    arbiter.release_voice("session-1")
    assert arbiter.snapshot()["mode"] == ManagerMode.IDLE


def test_training_and_voice_are_mutually_exclusive(tmp_path):
    arbiter = ModeArbiter(StateStore(tmp_path / "state.json"))
    arbiter.start_operation("train-1", "tts_training")
    with pytest.raises(ArbitrationError, match="not accepting voice"):
        arbiter.reserve_voice("session-1")
    arbiter.finish_operation("train-1", success=True)
    assert arbiter.reserve_voice("session-1") == "reserved"


def test_restart_marks_unknown_active_job_interrupted_and_faulted(tmp_path):
    store = StateStore(tmp_path / "state.json")
    first = ModeArbiter(store)
    first.start_operation("text-1", "text")
    second = ModeArbiter(store)
    snapshot = second.snapshot()
    assert snapshot["mode"] == ManagerMode.FAULTED
    assert snapshot["reconcile_required"] is True
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["operations"]["text-1"]["state"] == "interrupted"
    second.acknowledge_reconcile()
    assert second.snapshot()["mode"] == ManagerMode.IDLE


def test_voice_reservation_expires_without_leaving_manager_draining(tmp_path):
    now = [100]
    arbiter = ModeArbiter(StateStore(tmp_path / "state.json"), clock=lambda: now[0])
    assert arbiter.reserve_voice("session-1", expires_epoch=160) == "reserved"
    assert arbiter.snapshot()["voice_expires_epoch"] == 160
    now[0] = 161
    snapshot = arbiter.snapshot()
    assert snapshot["mode"] == ManagerMode.IDLE
    assert snapshot["voice_session_active"] is False
    assert snapshot["voice_expires_epoch"] == 0
    assert snapshot["sleep_inhibited"] is False


def test_terminal_operation_identifier_cannot_rerun_work(tmp_path):
    arbiter = ModeArbiter(StateStore(tmp_path / "state.json"))
    arbiter.start_operation("text-1", "text")
    arbiter.finish_operation("text-1", success=True)
    with pytest.raises(ArbitrationError) as raised:
        arbiter.start_operation("text-1", "text")
    assert raised.value.code == "operation_terminal"


def test_numeric_stage_timings_are_bounded_merged_and_durable(tmp_path):
    store = StateStore(tmp_path / "state.json")
    arbiter = ModeArbiter(store)
    arbiter.start_operation("text-1", "text")
    arbiter.record_operation_timings("text-1", {"generation": 123})
    arbiter.finish_operation("text-1", success=True)
    arbiter.record_operation_timings("text-1", {"r2_upload": 45})
    recent = arbiter.snapshot()["recent_operations"][0]
    assert recent["timings_ms"] == {"generation": 123, "r2_upload": 45}
    reloaded = ModeArbiter(store).snapshot()["recent_operations"][0]
    assert reloaded["timings_ms"] == recent["timings_ms"]
    with pytest.raises(ArbitrationError, match="timing"):
        arbiter.record_operation_timings("text-1", {"bad stage": 1})
    with pytest.raises(ArbitrationError, match="timing"):
        arbiter.record_operation_timings("text-1", {"generation": -1})


def test_operation_stage_updates_are_allowlisted_and_durable(tmp_path):
    store = StateStore(tmp_path / "state.json")
    arbiter = ModeArbiter(store)
    arbiter.reserve_voice("session-1")
    arbiter.start_operation("voice-1", "asr", session_id="session-1", turn_id="turn-1")
    arbiter.update_operation_stage("voice-1", "asr_transcribing")
    assert arbiter.snapshot()["active_operation"]["stage"] == "asr_transcribing"
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["operations"]["voice-1"]["stage"] == "asr_transcribing"
    with pytest.raises(ArbitrationError, match="stage"):
        arbiter.update_operation_stage("voice-1", "bad stage")
