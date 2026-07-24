from __future__ import annotations

import json
import threading
import time

import pytest

from ai_model_config import (
    LMC_AI_CONTEXT_LENGTH,
    LMC_AI_FAST_MODEL_TAG,
    lmc_ai_workstation_required_models,
)
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


def _config(tmp_path, *, update_enabled=False):
    return parse_config({
        "schema_version": 1,
        "node": {"name": "AI Workstation", "server_url": "https://example.com", "token_file": str(tmp_path / "token")},
        "paths": {"state": str(tmp_path / "state"), "cache": str(tmp_path / "cache"), "data": str(tmp_path / "data"), "releases": str(tmp_path / "releases")},
        "power": {}, "workloads": {},
        "update": {"enabled": update_enabled},
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
    prepared_models = []

    def chat(**kwargs):
        assert kwargs["context_length"] == LMC_AI_CONTEXT_LENGTH
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
    monkeypatch.setattr(executor.ollama, "unload_except", prepared_models.append)
    text, _usage = executor.run_chat(
        operation_id="text-1",
        model=LMC_AI_FAST_MODEL_TAG,
        messages=[{"role": "user", "content": "測試"}],
        think=False,
        context_length=LMC_AI_CONTEXT_LENGTH,
        deadline_epoch=2_000_000_000,
        cancel_event=threading.Event(),
        on_started=lambda: events.append("started"),
        on_delta=lambda value: events.append(value),
    )
    assert text == "答案"
    assert events == ["started", "答案"]
    assert prepared_models == [LMC_AI_FAST_MODEL_TAG]
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


def test_full_health_never_requires_the_on_demand_r2_probe(
    tmp_path, monkeypatch,
):
    runner = HealthRunner(_config(tmp_path, update_enabled=True))
    monkeypatch.setattr(runner, "_full_probes", lambda **_kwargs: {})
    report = runner.run(
        full=True,
        set_gpt_service=lambda _state: None,
        prepare_non_ollama=lambda: None,
    )
    assert "r2" not in report["checks"]
    assert "r2_probe" not in report["checks"]
    assert not {"r2", "r2_probe"}.intersection(report["required"])


def test_full_health_skips_disabled_future_capability_probes(tmp_path):
    runner = HealthRunner(_config(tmp_path))

    def unexpected(*_args, **_kwargs):
        raise AssertionError("disabled future capability must not be probed")

    probes = runner._full_probes(
        set_gpt_service=unexpected,
        prepare_non_ollama=unexpected,
    )

    assert probes == {}
    assert runner.rag.health() == {"ok": False, "code": "disabled"}


def test_full_health_requires_only_enabled_capabilities(tmp_path, monkeypatch):
    runner = HealthRunner(_config(tmp_path))
    monkeypatch.setattr(runner, "_inventory", lambda: {"quota_status": "ok"})
    for name in ("_os", "_gpu", "_memory", "_disk", "_power_tools", "_ollama_health"):
        monkeypatch.setattr(runner, name, lambda: {"ok": True})
    monkeypatch.setattr(
        runner, "_connection_receipt",
        lambda *_args, **_kwargs: {"ok": True},
    )

    report = runner.run(
        full=True,
        set_gpt_service=lambda _state: None,
        prepare_non_ollama=lambda: None,
    )

    assert report["healthy"] is True
    assert report["required"] == [
        "os", "gpu", "memory", "disk", "power", "ollama", "quota", "wss",
    ]
    assert not {
        "asr", "rag", "gpt_sovits", "r2",
        "asr_probe", "rag_probe", "gpt_sovits_probe", "r2_probe",
    }.intersection(report["required"])


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


def test_health_rejects_ollama_digest_drift_from_signed_receipt(
    tmp_path, monkeypatch,
):
    runner = HealthRunner(_config(tmp_path))
    required = list(lmc_ai_workstation_required_models())
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


def test_resume_stays_drained_when_required_rag_full_health_fails(
    tmp_path, monkeypatch,
):
    config = parse_config({
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
        "workloads": {
            "rag": {
                "enabled": True,
                "embedding_model": "embeddinggemma:300m",
            },
        },
    })
    arbiter = ModeArbiter(StateStore(tmp_path / "manager.json"))
    arbiter.set_draining(True)
    application = ManagerApplication(config, arbiter)
    monkeypatch.setattr(application, "health", lambda **_kwargs: {
        "healthy": False,
        "required": ["rag", "rag_probe"],
        "checks": {
            "rag": {"ok": False, "code": "index_unavailable"},
            "rag_probe": {"ok": False, "code": "rag_probe_empty"},
        },
    })

    with pytest.raises(WorkloadError) as raised:
        application.handle({"action": "resume"}, lambda *_args: None)

    assert raised.value.code == "health_gate"
    assert arbiter.snapshot()["draining"] is True


def test_enabled_rag_is_required_even_by_shallow_readiness(
    tmp_path, monkeypatch,
):
    config = parse_config({
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
        "workloads": {
            "ollama": {"enabled": False},
            "rag": {
                "enabled": True,
                "embedding_model": "embeddinggemma:300m",
            },
        },
    })
    runner = HealthRunner(config)
    for name in ("_os", "_gpu", "_memory", "_disk", "_power_tools"):
        monkeypatch.setattr(runner, name, lambda: {"ok": True})
    monkeypatch.setattr(
        runner.rag,
        "health",
        lambda: {"ok": False, "code": "index_unavailable"},
    )

    report = runner.run()

    assert "rag" in report["required"]
    assert report["healthy"] is False


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
