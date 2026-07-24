from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading
import time
import uuid

import pytest

from system_limits import WORKSTATION_DATASET_PREPARATION_MAX_SECONDS
from workstation.config import parse_config
from workstation.manager.arbiter import ModeArbiter
from workstation.manager import ipc
from workstation.manager.ipc import ManagerApplication, ManagerClient, serve_manager
from workstation.manager.state_store import StateStore
from workstation.workloads.errors import WorkloadError


def _config(tmp_path):
    return parse_config({
        "schema_version": 1,
        "node": {"name": "AI Workstation", "server_url": "https://example.com", "token_file": str(tmp_path / "token")},
        "paths": {"state": str(tmp_path / "state"), "cache": str(tmp_path / "cache"), "data": str(tmp_path), "releases": str(tmp_path / "releases")},
        "power": {}, "workloads": {},
    })


def test_manager_ipc_is_closed_schema_and_returns_snapshot(tmp_path, monkeypatch):
    config = _config(tmp_path)
    socket_path = Path("/tmp") / f"lmc-ai-manager-{uuid.uuid4().hex[:12]}.sock"
    application = ManagerApplication(config, ModeArbiter(StateStore(tmp_path / "manager.json")))
    monkeypatch.setattr(application, "health", lambda **_kwargs: {"healthy": True, "checks": {}})
    monkeypatch.setattr(ipc._Handler, "_peer_allowed", lambda _self: True)
    thread = threading.Thread(target=serve_manager, args=(application, socket_path), daemon=True)
    thread.start()
    for _ in range(100):
        if socket_path.exists():
            break
        threading.Event().wait(0.01)

    async def request():
        return await ManagerClient(socket_path).request({"action": "snapshot"})

    result = asyncio.run(request())
    assert result["manager"]["mode"] == "idle"
    assert result["health"]["healthy"] is True
    assert os.stat(socket_path).st_mode & 0o777 == 0o660
    socket_path.unlink(missing_ok=True)


def test_dataset_preparation_uses_its_bounded_deadline(tmp_path, monkeypatch):
    application = ManagerApplication(
        _config(tmp_path), ModeArbiter(StateStore(tmp_path / "manager.json")),
    )
    monkeypatch.setattr(application.executor.inhibitor, "acquire", lambda: None)
    monkeypatch.setattr(application.executor.inhibitor, "release", lambda: None)
    monkeypatch.setattr(threading.Thread, "start", lambda _self: None)
    emitted = []
    before = int(time.time())
    application.handle(
        {
            "action": "dataset.prepare",
            "dataset_id": "speaker-1",
            "speaker": "speaker",
        },
        lambda event, payload: emitted.append((event, payload)),
    )
    active = application.arbiter.snapshot()["active_operation"]
    assert active["stage"] == "dataset_preparation"
    assert (
        before + WORKSTATION_DATASET_PREPARATION_MAX_SECONDS
        <= active["deadline_epoch"]
        <= before + WORKSTATION_DATASET_PREPARATION_MAX_SECONDS + 1
    )
    assert emitted[0][0] == "result"


def test_external_tts_upload_is_closed_schema_and_controls_terminal_state(
    tmp_path, monkeypatch,
):
    application = ManagerApplication(
        _config(tmp_path), ModeArbiter(StateStore(tmp_path / "manager.json")),
    )
    monkeypatch.setattr(application.executor.inhibitor, "acquire", lambda: None)
    monkeypatch.setattr(application.executor.inhibitor, "release", lambda: None)
    application.arbiter.reserve_voice("session-1")
    application.arbiter.start_operation(
        "tts-1", "tts", session_id="session-1", turn_id="turn-1",
        stage="tts_synthesis",
    )
    emitted = []
    application.handle({
        "action": "operation.external_stage",
        "operation_id": "tts-1",
        "stage": "r2_upload",
    }, lambda event, payload: emitted.append((event, payload)))
    assert application.arbiter.snapshot()["active_operation"]["stage"] == "r2_upload"

    with pytest.raises(WorkloadError, match="invalid"):
        application.handle({
            "action": "operation.external_finish",
            "operation_id": "tts-1",
            "success": True,
            "error_code": "",
            "timings_ms": {"r2_upload": 5},
            "command": "id",
        }, lambda *_args: None)

    application.handle({
        "action": "operation.external_finish",
        "operation_id": "tts-1",
        "success": True,
        "error_code": "",
        "timings_ms": {"r2_upload": 5},
    }, lambda event, payload: emitted.append((event, payload)))
    operation = next(
        item for item in application.arbiter.snapshot()["recent_operations"]
        if item["operation_id"] == "tts-1"
    )
    assert operation["state"] == "succeeded"
    assert operation["timings_ms"]["r2_upload"] == 5
