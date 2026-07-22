import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from workstation.config import PowerConfig
from workstation.manager.power import decide_power_action, read_power_override
from workstation.node.protocol import advertised_capabilities, validate_server_job
from workstation.node.client import WorkstationNodeClient, _websocket_authorization_argument
from workstation.privileged_helper.protocol import PrivilegedRequestError, validate_request


def test_power_never_suspends_a_managed_job_and_uses_rtc_window():
    config = PowerConfig(True, "Asia/Hong_Kong", "00:00", "08:00")
    now = datetime(2026, 7, 22, 1, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    active = decide_power_action(config, {"active_operation": {"operation_id": "x"}}, now=now)
    assert active.action == "delay"
    idle = decide_power_action(config, {}, now=now)
    assert idle.action == "suspend"
    assert datetime.fromtimestamp(idle.wake_epoch, ZoneInfo("Asia/Hong_Kong")).hour == 8


def test_power_overnight_window_covers_both_sides_of_midnight():
    zone = ZoneInfo("Asia/Hong_Kong")
    config = PowerConfig(True, "Asia/Hong_Kong", "23:00", "08:00")

    before_midnight = decide_power_action(
        config, {}, now=datetime(2026, 7, 22, 23, 30, tzinfo=zone),
    )
    assert before_midnight.action == "suspend"
    assert datetime.fromtimestamp(before_midnight.wake_epoch, zone) == datetime(
        2026, 7, 23, 8, 0, tzinfo=zone,
    )

    after_midnight = decide_power_action(
        config, {}, now=datetime(2026, 7, 23, 1, 0, tzinfo=zone),
    )
    assert after_midnight.action == "suspend"
    assert datetime.fromtimestamp(after_midnight.wake_epoch, zone) == datetime(
        2026, 7, 23, 8, 0, tzinfo=zone,
    )

    awake = decide_power_action(
        config, {}, now=datetime(2026, 7, 23, 12, 0, tzinfo=zone),
    )
    assert awake.action == "none"
    assert datetime.fromtimestamp(awake.next_check_epoch, zone) == datetime(
        2026, 7, 23, 23, 0, tzinfo=zone,
    )


def test_power_override_is_bounded_and_read_from_a_small_receipt(tmp_path):
    marker = tmp_path / "power-override.json"
    marker.write_text(
        '{"schema_version":1,"until_epoch":2000}', encoding="utf-8",
    )
    assert read_power_override(tmp_path, now_epoch=1000) == 2000
    decision = decide_power_action(
        PowerConfig(True, "Asia/Hong_Kong", "00:00", "08:00"),
        {},
        now=datetime.fromtimestamp(1000, ZoneInfo("Asia/Hong_Kong")),
        override_until_epoch=2000,
    )
    assert decision.reason == "temporary_override"
    marker.write_bytes(b"x" * 4097)
    assert read_power_override(tmp_path, now_epoch=1000) == 0


def test_privileged_protocol_has_no_command_or_arbitrary_target():
    assert validate_request({"action": "trigger_rollback"}) == {
        "action": "trigger_rollback"
    }
    with pytest.raises(PrivilegedRequestError):
        validate_request({"action": "trigger_rollback", "version": "1.2.3"})
    assert validate_request({"action": "restart_service", "service": "ollama.service"})["service"] == "ollama.service"
    with pytest.raises(PrivilegedRequestError):
        validate_request({"action": "restart_service", "service": "ssh.service"})
    assert validate_request({
        "action": "set_service_state",
        "service": "lmc-ai-gpt-sovits.service",
        "state": "stop",
    })["state"] == "stop"
    with pytest.raises(PrivilegedRequestError):
        validate_request({
            "action": "set_service_state", "service": "ollama.service", "state": "stop",
        })
    with pytest.raises(PrivilegedRequestError):
        validate_request({"action": "suspend", "wake_epoch": 2_000, "command": "id"}, now=1_000)
    schedule = validate_request({
        "action": "set_power_schedule", "enabled": True,
        "timezone": "Asia/Hong_Kong", "suspend_at": "00:00", "wake_at": "08:00",
    })
    assert schedule["wake_at"] == "08:00"
    assert validate_request({
        "action": "set_power_override", "until_epoch": 2_000,
    }, now=1_000)["until_epoch"] == 2_000
    assert validate_request({
        "action": "set_power_override", "until_epoch": 0,
    }, now=1_000)["until_epoch"] == 0
    with pytest.raises(PrivilegedRequestError):
        validate_request({
            "action": "set_power_override", "until_epoch": 1_000 + 8 * 86400,
        }, now=1_000)
    with pytest.raises(PrivilegedRequestError):
        validate_request({
            "action": "set_power_schedule", "enabled": True,
            "timezone": "UTC", "suspend_at": "00:00", "wake_at": "08:00",
        })


def test_capability_advertisement_is_health_derived_and_job_schema_is_closed():
    capabilities = advertised_capabilities({"checks": {"ollama": {"ok": True}, "asr": {"ok": False}}})
    assert capabilities["chat"] is True
    assert capabilities["asr"] is False
    assert capabilities["manager"] is True
    assert capabilities["direct_r2"] is False
    capabilities = advertised_capabilities({"checks": {"r2": {"ok": True}}})
    assert capabilities["direct_r2"] is True
    job = validate_server_job({
        "type": "workstation.job.start",
        "operation_id": "op-1",
        "job_kind": "asr",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "stage": "transcribing",
        "deadline_epoch": 2_000_000_000,
        "payload": {
            "download": {
                "url": "https://example.invalid/signed",
                "byte_size": 4096,
                "sha256": "a" * 64,
            },
            "mime_type": "audio/webm",
            "file_ext": "webm",
        },
    })
    assert job["job_kind"] == "asr"
    with pytest.raises(ValueError, match="unknown"):
        validate_server_job({**job, "type": "workstation.job.start", "shell": "id"})


def test_websocket_bearer_header_supports_ubuntu_and_new_runtime_signatures():
    def ubuntu_connect(uri, *, extra_headers=None):
        return uri, extra_headers

    def newer_connect(uri, *, additional_headers=None):
        return uri, additional_headers

    assert _websocket_authorization_argument(
        "secret", ubuntu_connect,
    ) == {"extra_headers": {"Authorization": "Bearer secret"}}
    assert _websocket_authorization_argument(
        "secret", newer_connect,
    ) == {"additional_headers": {"Authorization": "Bearer secret"}}

    def unsupported(uri):
        return uri

    with pytest.raises(RuntimeError, match="unsupported"):
        _websocket_authorization_argument("secret", unsupported)


def test_pending_voice_reservation_control_job_can_be_cancelled():
    async def scenario():
        released = asyncio.Event()
        calls = []

        class Manager:
            async def request(self, payload):
                calls.append(payload)
                released.set()
                return {"ok": True}

        client = WorkstationNodeClient(SimpleNamespace(), Manager())

        async def pending():
            await released.wait()

        client.control_operation_id = "reserve-1"
        client.control_task = asyncio.create_task(pending())
        await client.cancel_active("reserve-1")
        assert calls == [{"action": "cancel", "operation_id": "reserve-1"}]
        assert client.control_task.done()

    asyncio.run(scenario())


def test_node_restart_fails_an_orphaned_external_tts_upload():
    async def scenario():
        calls = []

        class Manager:
            async def request(self, payload):
                calls.append(payload)
                if payload["action"] == "snapshot":
                    return {"manager": {"active_operation": None}}
                return {"ok": True}

        client = WorkstationNodeClient(SimpleNamespace(), Manager())
        result = await client._reconcile_external_upload({
            "manager": {"active_operation": {
                "operation_id": "tts-1",
                "kind": "tts",
                "stage": "r2_upload",
                "timings_ms": {"tts_synthesis": 100},
            }},
        })
        assert calls[0] == {
            "action": "operation.external_finish",
            "operation_id": "tts-1",
            "success": False,
            "error_code": "node_restarted_during_upload",
            "timings_ms": {"tts_synthesis": 100},
        }
        assert calls[1] == {"action": "snapshot"}
        assert result["manager"]["active_operation"] is None

    asyncio.run(scenario())


def test_tts_reports_r2_upload_stage_before_requesting_authorization(
    tmp_path, monkeypatch,
):
    async def scenario():
        output = tmp_path / "speech.wav"
        output.write_bytes(b"audio")
        manager_requests = []

        class Manager:
            async def stream(self, _payload):
                yield {
                    "event": "result",
                    "prepared_output": {
                        "path": str(output),
                        "mime_type": "audio/wav",
                        "size_bytes": 5,
                        "sha256": "a" * 64,
                        "duration_ms": 100,
                        "timings_ms": {"synthesis": 10},
                    },
                }

            async def request(self, _payload):
                manager_requests.append(_payload)
                return {"ok": True}

        class Socket:
            def __init__(self, client):
                self.client = client
                self.sent = []

            async def send(self, raw):
                import json

                payload = json.loads(raw)
                self.sent.append(payload)
                if payload["type"] == "workstation.upload.request":
                    self.client.upload_waiters[payload["operation_id"]].set_result({
                        "intent_id": "intent-1",
                        "upload": {
                            "url": "https://r2.example.invalid/signed",
                            "headers": {},
                        },
                    })
                elif payload["type"] == "workstation.upload.complete":
                    self.client.upload_verification_waiters[
                        payload["operation_id"]
                    ].set_result({"type": "workstation.upload.verified"})

        monkeypatch.setattr(
            "workstation.node.client.upload_path",
            lambda *_args, **_kwargs: {"etag": "etag-1"},
        )
        client = WorkstationNodeClient(SimpleNamespace(), Manager())
        client.websocket = Socket(client)
        await client.run_workstation_job({
            "type": "workstation.job.start",
            "operation_id": "tts.session-1.turn-1",
            "job_kind": "tts",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "stage": "synthesis",
            "deadline_epoch": 2_000_000_000,
            "payload": {"text": "測試讀音"},
        })
        types = [item["type"] for item in client.websocket.sent]
        assert types.index("workstation.job.stage") < types.index(
            "workstation.upload.request"
        )
        stage = next(
            item for item in client.websocket.sent
            if item["type"] == "workstation.job.stage"
        )
        assert stage["stage"] == "r2_upload"
        assert types.index("workstation.upload.complete") < types.index(
            "workstation.job.complete"
        )
        assert client.websocket.sent[-1]["type"] == "workstation.job.complete"
        assert manager_requests[0] == {
            "action": "operation.external_stage",
            "operation_id": "tts.session-1.turn-1",
            "stage": "r2_upload",
        }
        assert manager_requests[1]["action"] == "operation.external_finish"
        assert manager_requests[1]["success"] is True
        assert manager_requests[1]["timings_ms"]["synthesis"] == 10
        assert manager_requests[1]["timings_ms"]["r2_upload"] >= 0

    asyncio.run(scenario())
