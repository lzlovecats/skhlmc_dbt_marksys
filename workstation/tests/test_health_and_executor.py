from __future__ import annotations

import json
import threading
import time

import pytest

from ai_model_config import LMC_AI_FAST_MODEL_TAG, lmc_ai_required_models
from system_limits import WORKSTATION_R2_HEALTH_RECEIPT_MAX_AGE_SECONDS
from workstation.config import parse_config
from workstation.manager import health as health_module
from workstation.manager.arbiter import ModeArbiter
from workstation.manager import executor as executor_module
from workstation.manager.executor import JobExecutor
from workstation.manager.health import HealthRunner
from workstation.manager.ipc import ManagerApplication
from workstation.manager.state_store import StateStore
from workstation.workloads.errors import WorkloadError


class _Inhibitor:
    def __init__(self):
        self.active = False

    def acquire(self):
        self.active = True

    def release(self):
        self.active = False


def _config(tmp_path):
    return parse_config({
        "schema_version": 1,
        "node": {"name": "AI Workstation", "server_url": "https://example.com", "token_file": str(tmp_path / "token")},
        "paths": {"state": str(tmp_path / "state"), "cache": str(tmp_path / "cache"), "data": str(tmp_path / "data"), "releases": str(tmp_path / "releases")},
        "power": {}, "workloads": {}, "gui": {},
    })


def test_executor_holds_inhibitor_for_entire_voice_reservation(tmp_path):
    config = _config(tmp_path)
    inhibitor = _Inhibitor()
    executor = JobExecutor(config, ModeArbiter(StateStore(tmp_path / "manager.json")), inhibitor)
    executor.reserve_voice("session-1")
    assert inhibitor.active is True
    executor.release_voice("session-1")
    assert inhibitor.active is False


def test_text_attempt_starts_only_when_ollama_stream_is_open(tmp_path, monkeypatch):
    config = _config(tmp_path)
    arbiter = ModeArbiter(StateStore(tmp_path / "manager.json"))
    executor = JobExecutor(config, arbiter, _Inhibitor())
    events = []

    def chat(**kwargs):
        kwargs["on_started"]()
        kwargs["on_delta"]("答案")
        return "答案", {
            "model": kwargs["model"],
            "load_duration_ms": 11,
            "prompt_eval_duration_ms": 22,
            "generation_duration_ms": 33,
            "wall_duration_ms": 99,
        }

    monkeypatch.setattr(executor.ollama, "chat", chat)
    text, _usage = executor.run_chat(
        operation_id="text-1",
        model=LMC_AI_FAST_MODEL_TAG,
        messages=[{"role": "user", "content": "測試"}],
        think=False,
        deadline_epoch=2_000_000_000,
        cancel_event=threading.Event(),
        on_started=lambda: events.append("started"),
        on_delta=lambda value: events.append(value),
    )
    assert text == "答案"
    assert events == ["started", "答案"]
    operation = next(
        item for item in arbiter.snapshot()["recent_operations"]
        if item["operation_id"] == "text-1"
    )
    assert operation["timings_ms"] == {
        "model_load": 11,
        "prompt_eval": 22,
        "generation": 33,
    }


def test_voice_reservation_timeout_clears_pending_drain(tmp_path, monkeypatch):
    config = _config(tmp_path)
    arbiter = ModeArbiter(StateStore(tmp_path / "manager.json"))
    executor = JobExecutor(config, arbiter, _Inhibitor())
    arbiter.start_operation("text-1", "text")
    monkeypatch.setattr(arbiter, "wait_for_voice", lambda *_args, **_kwargs: False)
    with pytest.raises(WorkloadError, match="could not reserve"):
        executor.reserve_voice("session-1")
    snapshot = arbiter.snapshot()
    assert snapshot["voice_session_pending"] is False
    assert snapshot["draining"] is False
    assert snapshot["mode"] == "text_serve"


def test_cancelled_pending_voice_reservation_cannot_become_active(
    tmp_path, monkeypatch,
):
    config = _config(tmp_path)
    arbiter = ModeArbiter(StateStore(tmp_path / "manager.json"))
    executor = JobExecutor(config, arbiter, _Inhibitor())
    arbiter.start_operation("text-1", "text")
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(
        executor_module, "WORKSTATION_VOICE_RESERVE_TIMEOUT_SECONDS", 0.01,
    )
    with pytest.raises(WorkloadError) as raised:
        executor.run_workstation_job({
            "operation_id": "reserve-1",
            "job_kind": "voice.reserve",
            "session_id": "session-1",
            "turn_id": "",
            "stage": "reserve",
            "deadline_epoch": 2_000_000_000,
            "payload": {"session_expires_epoch": 1_999_999_999},
        }, cancel_event=cancel)
    assert raised.value.code == "cancelled"
    snapshot = arbiter.snapshot()
    assert snapshot["voice_session_pending"] is False
    assert snapshot["voice_session_active"] is False
    assert snapshot["draining"] is False
    assert snapshot["mode"] == "text_serve"


def test_tts_owns_gpu_service_only_for_inference(tmp_path, monkeypatch):
    raw = {
        "schema_version": 1,
        "node": {"name": "AI Workstation", "server_url": "https://example.com", "token_file": str(tmp_path / "token")},
        "paths": {"state": str(tmp_path / "state"), "cache": str(tmp_path / "cache"), "data": str(tmp_path / "data"), "releases": str(tmp_path / "releases")},
        "power": {},
        "workloads": {"gpt_sovits": {"enabled": True, "model_version": "voice-v1"}},
        "gui": {},
    }
    calls = []
    arbiter = ModeArbiter(StateStore(tmp_path / "manager.json"))
    executor = JobExecutor(
        parse_config(raw),
        arbiter,
        _Inhibitor(),
        privileged_request=lambda request: calls.append(request) or {"ok": True},
    )
    monkeypatch.setattr(executor.ollama, "unload_all", lambda: calls.append("ollama.unload"))
    monkeypatch.setattr(executor.gpt_sovits, "wait_until_ready", lambda: calls.append("tts.ready"))
    monkeypatch.setattr(
        "workstation.manager.executor.probe_audio",
        lambda *_args, **_kwargs: {
            "duration_seconds": 1.2, "sample_rate": 32000, "channels": 1,
        },
    )
    monkeypatch.setattr(
        executor.gpt_sovits,
        "synthesize",
        lambda _text, **_kwargs: {
            "path": tmp_path / "voice.wav",
            "mime_type": "audio/wav",
            "byte_size": 10,
            "sha256": "a" * 64,
            "model_version": "voice-v1",
        },
    )
    executor.reserve_voice("session-1")
    result = executor.run_workstation_job({
        "operation_id": "tts-1",
        "job_kind": "tts",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "stage": "synthesis",
        "deadline_epoch": 2_000_000_000,
        "payload": {"text": "測試"},
    })
    assert result["prepared_output"]["model_version"] == "voice-v1"
    active = arbiter.snapshot()["active_operation"]
    assert active["operation_id"] == "tts-1"
    assert active["state"] == "running"
    assert active["stage"] == "r2_upload"
    assert calls == [
        {"action": "set_service_state", "service": "lmc-ai-gpt-sovits.service", "state": "stop"},
        "ollama.unload",
        {"action": "set_service_state", "service": "lmc-ai-gpt-sovits.service", "state": "start"},
        "tts.ready",
        {"action": "set_service_state", "service": "lmc-ai-gpt-sovits.service", "state": "stop"},
    ]


def test_full_health_executes_direct_r2_probe_instead_of_trusting_old_receipt(
    tmp_path, monkeypatch,
):
    runner = HealthRunner(_config(tmp_path))
    calls = []

    def full_probes(**kwargs):
        probe = kwargs["probe_r2"]()
        calls.append(probe)
        return {
            "r2_probe": probe,
            "asr_probe": {"ok": True},
            "rag_probe": {"ok": True},
            "gpt_sovits_probe": {"ok": True},
        }

    monkeypatch.setattr(runner, "_full_probes", full_probes)
    report = runner.run(
        full=True,
        set_gpt_service=lambda _state: None,
        prepare_non_ollama=lambda: None,
        probe_r2=lambda: {"ok": True, "checked_epoch": 123},
    )
    assert calls == [{"ok": True, "checked_epoch": 123}]
    assert "r2_probe" in report["required"]
    assert report["checks"]["r2_probe"]["ok"] is True
    assert report["checks"]["r2"] == {
        "ok": True, "checked_epoch": 123,
    }


def test_connection_receipt_rejects_non_object_and_future_timestamp(tmp_path):
    runner = HealthRunner(_config(tmp_path))
    state = tmp_path / "state"
    state.mkdir()
    (state / "website.json").write_text("[]")
    assert runner._connection_receipt(
        "website.json", maximum_age_seconds=120,
    )["code"] == "receipt_unavailable"
    (state / "website.json").write_text(json.dumps({
        "checked_epoch": int(time.time()) + 600,
    }))
    assert runner._connection_receipt(
        "website.json", maximum_age_seconds=120,
    )["code"] == "receipt_unavailable"


def test_r2_receipt_spans_full_health_interval_and_failure_invalidates_it(
    tmp_path, monkeypatch,
):
    runner = HealthRunner(_config(tmp_path))
    state = tmp_path / "state"
    state.mkdir()
    now = 2_000_000_000
    receipt = state / "r2-health.json"
    receipt.write_text(json.dumps({
        "checked_epoch": now - (6 * 60 * 60 + 30 * 60),
    }))
    monkeypatch.setattr(health_module.time, "time", lambda: now)
    assert runner._connection_receipt(
        "r2-health.json",
        maximum_age_seconds=WORKSTATION_R2_HEALTH_RECEIPT_MAX_AGE_SECONDS,
    )["ok"] is True

    def failed_probe():
        raise RuntimeError("provider unavailable")

    probes = runner._full_probes(
        set_gpt_service=lambda _state: None,
        prepare_non_ollama=lambda: None,
        probe_r2=failed_probe,
    )
    assert probes["r2_probe"] == {
        "ok": False, "code": "r2_probe_failed",
    }
    assert not receipt.exists()


def test_health_rejects_ollama_digest_drift_from_signed_receipt(
    tmp_path, monkeypatch,
):
    runner = HealthRunner(_config(tmp_path))
    required = list(lmc_ai_required_models())
    expected = {
        name: {"digest": f"{index + 1:064x}", "bytes": 123}
        for index, name in enumerate(required)
    }
    receipt = tmp_path / "data/models/active-receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({
        "id": "models-v1",
        "sha256": "a" * 64,
        "approved_epoch": 123,
        "model_bytes": 123 * len(required),
        "models": expected,
    }))
    actual = {name: details["digest"] for name, details in expected.items()}
    actual[required[0]] = "f" * 64
    monkeypatch.setattr(runner.ollama, "health", lambda _required: {
        "ok": True,
        "models": sorted(actual),
        "model_digests": actual,
        "missing_models": [],
    })
    health = runner._ollama_health()
    assert health["ok"] is False
    assert health["code"] == "model_digest_mismatch"
    assert health["mismatched_models"] == [required[0]]


def test_manager_health_unexpected_exception_replaces_stale_success(
    tmp_path, monkeypatch,
):
    config = _config(tmp_path)
    application = ManagerApplication(
        config, ModeArbiter(StateStore(tmp_path / "manager.json")),
    )
    application._health_report = {"healthy": True, "checks": {}}
    application._health_checked_monotonic = 0
    monkeypatch.setattr(
        application.health_runner,
        "run",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("secret detail")),
    )
    report = application.health(force=True)
    assert report["healthy"] is False
    assert report["checks"] == {
        "health_runner": {"ok": False, "code": "health_exception"},
    }
    assert "secret detail" not in json.dumps(report)


def test_sixteen_gb_physical_profile_allows_normal_firmware_ram_reservation(
    monkeypatch,
):
    from workstation.manager import health as health_module

    class MemoryFile:
        def __init__(self, total_kib):
            self.total_kib = total_kib

        def read_text(self, **_kwargs):
            return (
                f"MemTotal: {self.total_kib} kB\n"
                "MemAvailable: 4194304 kB\n"
            )

    visible_15_5_gib = int(15.5 * 1024 * 1024)
    monkeypatch.setattr(
        health_module, "Path", lambda _value: MemoryFile(visible_15_5_gib),
    )
    assert HealthRunner._memory()["ok"] is True

    visible_below_15_gib = health_module.WORKSTATION_MIN_RAM_BYTES // 1024 - 1
    monkeypatch.setattr(
        health_module, "Path", lambda _value: MemoryFile(visible_below_15_gib),
    )
    assert HealthRunner._memory()["ok"] is False
