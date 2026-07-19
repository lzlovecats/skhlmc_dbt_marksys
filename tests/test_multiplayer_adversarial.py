"""Adversarial regressions for final STUN-only Mode A room behaviour."""

import asyncio
import json
import re
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from api import ai_training_api
from core import rag
import deploy.proxy as proxy
import system_limits


class _WebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_text(self, value):
        self.sent.append(value)

    async def close(self, **_kwargs):
        self.closed = True


@pytest.fixture(autouse=True)
def _isolate_rooms():
    original = dict(proxy.ROOMS)
    proxy.ROOMS.clear()
    yield
    proxy.ROOMS.clear()
    proxy.ROOMS.update(original)


def _free_room(code="ROOM1"):
    room = proxy.Room(
        code, "A", "host", proxy.DEBATE_FORMATS[0], "辯題", "free", 10, 2,
    )
    pro = proxy.RoomMember("host", _WebSocket())
    con = proxy.RoomMember("guest", _WebSocket())
    pro.role, con.role = "正方", "反方"
    pro.rtc_status = con.rtc_status = "connected"
    room.members = {"host": pro, "guest": con}
    room.roster_generation = 2
    return room


def _control_task(room, prefix):
    return next(
        task for key, task in room.control_tasks.items()
        if key.startswith(prefix)
    )


def _json_request(payload):
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "method": "POST", "path": "/api/room/create",
        "headers": [(b"content-type", b"application/json")],
    }, receive)


def test_mode_b_is_a_breaking_400_and_never_creates_room(monkeypatch):
    monkeypatch.setattr(proxy, "require_page_user", lambda *_args: "host")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(proxy.room_create(_json_request({"mode": "B"})))
    assert exc.value.status_code == 400
    assert "已移除" in str(exc.value.detail)
    assert proxy.ROOMS == {}


def test_room_is_exactly_two_people_and_gives_both_free_side_banks_plus_grace(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "ready"
        room.precheck_results = {
            "host": {"ok": True}, "guest": {"ok": True},
        }
        signature = proxy._room_connected_roster_signature(room)
        monkeypatch.setattr(proxy, "bandwidth_budget_status", lambda **_k: {
            "total_bytes": 0, "essential_only_bytes": 4 * 1024**3,
        })
        monkeypatch.setattr(proxy, "_room_ensure_tick", lambda _room: None)

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        assert await proxy._room_start_active(
            room, expected_precheck_id="ready",
            expected_roster_signature=signature,
        ) is None
        assert room.capacity == system_limits.ROOM_MAX_CAPACITY == 2
        assert room.mode == "A"
        assert room.hard_deadline_ms - room.started_ms == 35 * 60 * 1000

    asyncio.run(scenario())


def test_media_preflight_failure_blocks_start_and_is_retryable(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "check"
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        await proxy._room_handle_precheck_result(room, room.members["host"], {
            "type": "precheck_result", "check_id": "check",
            "media_ok": True,
        })
        await proxy._room_handle_precheck_result(room, room.members["guest"], {
            "type": "precheck_result", "check_id": "check",
            "media_ok": False, "message": "ICE failed",
        })
        assert room.phase == "lobby"
        assert room.precheck_id is None and room.precheck_results == {}
        assert any(item["type"] == "precheck_failed" for item in broadcasts)

    asyncio.run(scenario())


def test_precheck_requires_server_observed_rtc_connected(monkeypatch):
    async def scenario():
        room = _free_room()
        room.members["guest"].rtc_status = "new"
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        await proxy._room_begin_precheck(room)
        assert room.precheck_id is None
        assert broadcasts[-1]["type"] == "error"
        assert "RTC" in broadcasts[-1]["message"]

    asyncio.run(scenario())


def test_activation_budget_exception_rolls_back_to_retryable_lobby(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "ready"
        room.precheck_results = {
            "host": {"ok": True}, "guest": {"ok": True},
        }
        signature = proxy._room_connected_roster_signature(room)

        def fail_budget(**_kwargs):
            raise RuntimeError("budget backend unavailable")

        monkeypatch.setattr(proxy, "bandwidth_budget_status", fail_budget)
        error = await proxy._room_start_active(
            room, expected_precheck_id="ready",
            expected_roster_signature=signature,
        )
        assert "資源檢查" in error
        assert room.phase == "lobby"
        assert room.started_ms is None and room.hard_deadline_ms is None
        assert room.activation_ready is False

    asyncio.run(scenario())


def test_four_gb_room_keeps_p2p_but_disables_judgement_and_web_speech(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "ready"
        room.precheck_results = {
            "host": {"ok": True}, "guest": {"ok": True},
        }
        signature = proxy._room_connected_roster_signature(room)
        broadcasts = []

        monkeypatch.setattr(proxy, "bandwidth_budget_status", lambda **_kwargs: {
            "total_bytes": 4 * 1024**3,
            "essential_only_bytes": 4 * 1024**3,
        })
        monkeypatch.setattr(proxy, "_room_ensure_tick", lambda _room: None)

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        assert await proxy._room_start_active(
            room, expected_precheck_id="ready",
            expected_roster_signature=signature,
        ) is None
        assert room.phase == "active"
        assert room.judge_enabled is False
        assert "AI 評判及 Web Speech 逐字稿已停用" in room.judge_disabled_reason
        assert any(item.get("type") == "judge_disabled" for item in broadcasts)

    asyncio.run(scenario())


def test_rtc_drop_during_starting_rolls_activation_back_to_lobby(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "ready"
        room.precheck_results = {
            "host": {"ok": True}, "guest": {"ok": True},
        }
        signature = proxy._room_connected_roster_signature(room)
        entered = threading.Event()
        release = threading.Event()

        def blocked_budget(**_kwargs):
            entered.set()
            assert release.wait(1)
            return {"total_bytes": 0, "essential_only_bytes": 4 * 1024**3}

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "bandwidth_budget_status", blocked_budget)
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        activation = asyncio.create_task(proxy._room_start_active(
            room, expected_precheck_id="ready",
            expected_roster_signature=signature,
        ))
        assert await asyncio.to_thread(entered.wait, 1)
        assert room.phase == "starting"
        await proxy._room_handle_message(room, room.members["guest"], {
            "type": "rtc_status", "status": "disconnected",
            "roster_generation": room.roster_generation,
        })
        release.set()
        error = await activation
        assert "名單" in error or "連線" in error
        assert room.phase == "lobby"
        assert room.activation_ready is False
        assert room.started_ms is None and room.hard_deadline_ms is None

    asyncio.run(scenario())


def test_mock_deadline_is_planned_duration_plus_fifteen_minutes(monkeypatch):
    async def scenario():
        room = proxy.Room(
            "MOCK1", "A", "host", "聯中", "辯題", "mock", 5, 2,
        )
        host = proxy.RoomMember("host", _WebSocket())
        guest = proxy.RoomMember("guest", _WebSocket())
        host.role, guest.role = "正方", "反方"
        host.rtc_status = guest.rtc_status = "connected"
        room.members = {"host": host, "guest": guest}
        room.roster_generation = 2
        room.precheck_id = "ready"
        room.precheck_results = {
            "host": {"ok": True}, "guest": {"ok": True},
        }
        signature = proxy._room_connected_roster_signature(room)
        monkeypatch.setattr(proxy, "bandwidth_budget_status", lambda **_k: {
            "total_bytes": 0, "essential_only_bytes": 4 * 1024**3,
        })
        monkeypatch.setattr(proxy, "_room_ensure_tick", lambda _room: None)

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        assert await proxy._room_start_active(
            room, expected_precheck_id="ready",
            expected_roster_signature=signature,
        ) is None
        planned = proxy.full_mock_total_seconds(room.segments)
        assert room.hard_deadline_ms - room.started_ms == (
            planned + system_limits.ROOM_MOCK_HARD_GRACE_SECONDS
        ) * 1000
        assert proxy._room_expiry_deadline_ms(room) == room.hard_deadline_ms

    asyncio.run(scenario())


def test_lobby_expiry_is_ten_minutes_from_creation():
    room = _free_room()
    assert proxy._room_expiry_deadline_ms(room) == (
        room.created_at + system_limits.ROOM_LOBBY_TTL_SECONDS * 1000
    )


def test_preflight_ignores_legacy_transcript_result(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "check"
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        monkeypatch.setattr(proxy, "bandwidth_budget_status", lambda **_k: {
            "total_bytes": 0, "essential_only_bytes": 4 * 1024**3,
        })
        monkeypatch.setattr(proxy, "_room_ensure_tick", lambda _room: None)
        await proxy._room_handle_precheck_result(room, room.members["host"], {
            "check_id": "check", "media_ok": True, "transcript_ok": True,
        })
        await proxy._room_handle_precheck_result(room, room.members["guest"], {
            "check_id": "check", "media_ok": True, "transcript_ok": False,
        })
        assert room.phase == "active" and room.activation_ready is True
        assert room.judge_enabled is True
        reported_results = [
            result
            for item in broadcasts if item.get("type") == "precheck_status"
            for result in item.get("results", {}).values() if result
        ]
        assert reported_results
        assert all("transcript_ok" not in result for result in reported_results)
        assert all(item["type"] != "judge_disabled" for item in broadcasts)

    asyncio.run(scenario())


def test_signalling_forwards_to_only_peer_and_counts_no_audio(monkeypatch):
    async def scenario():
        room = _free_room()
        host, guest = room.members["host"], room.members["guest"]
        offer = {"type": "offer", "sdp": "v=0"}
        await proxy._room_handle_message(room, host, {
            "type": "rtc_offer", "description": offer,
            "roster_generation": room.roster_generation,
        })
        assert host.ws.sent == [] and len(guest.ws.sent) == 1
        forwarded = json.loads(guest.ws.sent[0])
        assert forwarded == {
            "type": "rtc_offer", "from": "host", "description": offer,
            "roster_generation": room.roster_generation,
        }
        assert room.bandwidth_bytes == len(guest.ws.sent[0].encode())

        await proxy._room_handle_message(room, host, {
            "type": "rtc_ice", "candidate": {"candidate": "stale"},
            "roster_generation": room.roster_generation - 1,
        })
        await proxy._room_handle_message(room, host, {
            "type": "audio", "data": "must-not-reach-render",
        })
        assert len(guest.ws.sent) == 1

    asyncio.run(scenario())


def test_preflight_ready_is_roster_bound_and_forwarded_only_to_peer():
    async def scenario():
        room = _free_room()
        host, guest = room.members["host"], room.members["guest"]
        await proxy._room_handle_message(room, host, {
            "type": "rtc_status", "status": "preflight_ready",
            "roster_generation": room.roster_generation - 1,
        })
        assert guest.ws.sent == []
        await proxy._room_handle_message(room, host, {
            "type": "rtc_status", "status": "preflight_ready",
            "roster_generation": room.roster_generation,
        })
        assert host.ws.sent == []
        assert json.loads(guest.ws.sent[0]) == {
            "type": "rtc_status", "status": "preflight_ready",
            "from": "host", "roster_generation": room.roster_generation,
        }

    asyncio.run(scenario())


def test_oversized_or_stale_signalling_is_dropped_before_fanout():
    async def scenario():
        room = _free_room()
        host, guest = room.members["host"], room.members["guest"]
        await proxy._room_handle_message(room, host, {
            "type": "rtc_ice", "candidate": {"candidate": "x" * 5_000},
            "roster_generation": room.roster_generation,
        })
        await proxy._room_handle_message(room, host, {
            "type": "rtc_offer", "description": {"sdp": "x" * 49_000},
            "roster_generation": room.roster_generation,
        })
        assert guest.ws.sent == []

    asyncio.run(scenario())


def test_authoritative_turn_accepts_ordered_final_transcript(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        monkeypatch.setattr(proxy, "ROOM_TURN_FINALIZE_TIMEOUT_SECONDS", 0)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 0, "text": "我方主張",
        })
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 1, "text": "應予支持",
        })
        await proxy._room_commit_transcript(room, host, {
            "turn_id": turn_id, "final_sequence": 2,
        })
        await proxy._room_handle_turn(room, host, False)
        assert room.judge_enabled is True
        assert room.transcript[0]["text"] == "我方主張 應予支持"
        assert room.transcript_revision == 1

    asyncio.run(scenario())


def test_manual_transcript_commit_is_idempotent(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 0, "text": "只可寫入一次",
        })
        commit = {"turn_id": turn_id, "final_sequence": 1}
        await proxy._room_commit_transcript(room, host, commit)
        await proxy._room_commit_transcript(room, host, commit)
        assert len(room.transcript) == 1
        assert room.transcript_revision == 1
        assert room.transcript[0]["partial"] is False

    asyncio.run(scenario())


def test_speaker_may_conservatively_mark_local_commit_partial(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 0, "text": "本地 stop timeout",
        })
        await proxy._room_commit_transcript(room, host, {
            "turn_id": turn_id, "final_sequence": 1, "partial": True,
        })
        assert room.transcript[0]["partial"] is True

    asyncio.run(scenario())


def test_multiple_final_transcript_chunks_keep_a_word_boundary(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 0, "text": "hello",
        })
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 1, "text": "world",
        })
        await proxy._room_commit_transcript(room, host, {
            "turn_id": turn_id, "final_sequence": 2,
        })
        assert room.transcript[0]["text"] == "hello world"
        assert room.transcript[0]["partial"] is False

    asyncio.run(scenario())


def test_manual_stop_intent_freezes_server_time_while_stt_drains(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        now = [100_000]
        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_message(room, host, {
            "type": "turn_begin", "request_id": "begin-1",
        })
        turn_id = room.active_turn_id
        assert room.state_msg()["active_turn_request_id"] == "begin-1"
        now[0] += 500
        await proxy._room_handle_message(room, host, {
            "type": "turn_stop_intent", "turn_id": turn_id,
            "request_id": "begin-1", "reason": "manual",
        })
        assert room.turn_stop_pending is True
        assert room.side_elapsed_snapshot()["正方"] == 500
        now[0] += 2_500
        assert room.side_elapsed_snapshot()["正方"] == 500
        await proxy._room_handle_message(room, host, {
            "type": "transcript_chunk", "turn_id": turn_id,
            "sequence": 0, "text": "最後一句",
        })
        await proxy._room_handle_message(room, host, {
            "type": "transcript_commit", "turn_id": turn_id,
            "final_sequence": 1,
        })
        await proxy._room_handle_message(room, host, {
            "type": "turn_end", "turn_id": turn_id,
            "request_id": "begin-1",
        })
        assert room.active_turn_id is None
        assert room.side_elapsed_ms["正方"] == 500
        assert room.transcript[0]["text"] == "最後一句"
        assert room.transcript[0]["partial"] is False
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_slow_stop_notification_does_not_block_queued_commit(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        first_emit_entered = asyncio.Event()
        first_emit_release = asyncio.Event()
        emit_calls = 0

        async def emit(*_args, **_kwargs):
            nonlocal emit_calls
            emit_calls += 1
            if emit_calls == 1:
                first_emit_entered.set()
                await first_emit_release.wait()

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True, request_id="slow-stop")
        turn_id = room.active_turn_id
        monkeypatch.setattr(proxy, "_room_emit_due_bells", emit)
        await asyncio.wait_for(proxy._room_handle_message(room, host, {
            "type": "turn_stop_intent", "turn_id": turn_id,
            "request_id": "slow-stop",
        }), timeout=0.1)
        notification = _control_task(room, "stop_intent:")
        await first_emit_entered.wait()
        await proxy._room_handle_message(room, host, {
            "type": "transcript_chunk", "turn_id": turn_id,
            "sequence": 0, "text": "notification 仲發緊都收到",
        })
        await proxy._room_handle_message(room, host, {
            "type": "transcript_commit", "turn_id": turn_id,
            "final_sequence": 1,
        })
        await proxy._room_handle_message(room, host, {
            "type": "turn_end", "turn_id": turn_id,
            "request_id": "slow-stop",
        })
        assert room.active_turn_id is None
        assert room.transcript[0]["partial"] is False
        first_emit_release.set()
        await notification

    asyncio.run(scenario())


def test_manual_watchdog_keeps_already_committed_item_complete(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True, request_id="committed")
        turn_id = room.active_turn_id
        await proxy._room_handle_turn_stop_intent(room, host, {
            "turn_id": turn_id, "request_id": "committed",
        })
        scheduled_watchdog = room.manual_stop_task
        scheduled_watchdog.cancel()
        await asyncio.gather(scheduled_watchdog, return_exceptions=True)
        room.manual_stop_task = None
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 0, "text": "已完成 commit",
        })
        await proxy._room_commit_transcript(room, host, {
            "turn_id": turn_id, "final_sequence": 1,
        })
        monkeypatch.setattr(
            proxy, "ROOM_MANUAL_TURN_FINALIZE_TIMEOUT_SECONDS", 0,
        )
        await proxy._room_manual_stop_watchdog(room, turn_id)
        assert room.active_turn_id is None
        assert room.transcript[0]["partial"] is False

    asyncio.run(scenario())


def test_start_timeout_can_cancel_the_matching_request_without_turn_id(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        monkeypatch.setattr(
            proxy, "ROOM_MANUAL_TURN_FINALIZE_TIMEOUT_SECONDS", 0,
        )

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_message(room, host, {
            "type": "turn_begin", "request_id": "late-ack",
        })
        await proxy._room_handle_message(room, host, {
            "type": "turn_stop_intent", "request_id": "late-ack",
            "reason": "start_timeout",
        })
        host.connected = False
        watchdog = room.manual_stop_task
        await watchdog
        assert room.active_turn_id is None
        assert room.active_turn_request_id is None

    asyncio.run(scenario())


def test_stale_turn_end_cannot_stop_a_newer_turn(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        guest = room.members["guest"]
        await proxy._room_handle_turn(room, host, True, request_id="first")
        stale_turn_id = room.active_turn_id
        await proxy._room_handle_turn(room, host, False)
        await proxy._room_handle_turn(room, guest, True, request_id="second")
        current_turn_id = room.active_turn_id
        await proxy._room_handle_message(room, host, {
            "type": "turn_end", "turn_id": stale_turn_id,
            "request_id": "first",
        })
        assert room.active_turn_id == current_turn_id
        assert room.active_turn_request_id == "second"

    asyncio.run(scenario())


def test_free_debate_strictly_alternates_and_skips_an_exhausted_side(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        host = room.members["host"]
        guest = room.members["guest"]

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        assert room.expected_turn_side() == "正方"
        await proxy._room_handle_turn(room, host, True, request_id="pro-1")
        await proxy._room_handle_turn(room, host, False)
        assert room.expected_turn_side() == "反方"

        await proxy._room_handle_turn(room, host, True, request_id="pro-again")
        assert room.active_turn_user is None
        assert "輪到反方" in json.loads(host.ws.sent[-1])["message"]

        await proxy._room_handle_turn(room, guest, True, request_id="con-1")
        await proxy._room_handle_turn(room, guest, False)
        assert room.expected_turn_side() == "正方"

        limit_ms = int(room.current_segment()["seconds"]) * 1000
        room.side_elapsed_ms["正方"] = limit_ms
        assert room.expected_turn_side() == "反方"
        await proxy._room_handle_turn(room, guest, True, request_id="con-after-skip")
        assert room.active_turn_user == guest.user_id

    asyncio.run(scenario())


def test_forced_stop_fallback_preserves_received_chunks_as_partial(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 0,
            "text": "server 已收到但 browser 未 commit",
        })
        host.connected = False
        await proxy._room_request_turn_finalization(room, "side_time_limit")
        assert room.active_turn_user is None
        assert room.transcript == [{
            "revision": 1, "turn_id": turn_id,
            "speaker": "host", "side": "正方", "seg": 0,
            "label": "自由辯論", "text": "server 已收到但 browser 未 commit",
            "partial": True,
            "created_ms": room.transcript[0]["created_ms"],
        }]

    asyncio.run(scenario())


def test_forced_stop_successful_commit_is_still_marked_partial(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 0, "text": "最後一句",
        })

        async def deliver(_room, _member, message, **_kwargs):
            assert message["type"] == "turn_stop_requested"
            assert message["turn_id"] == turn_id
            await proxy._room_commit_transcript(room, host, {
                "turn_id": turn_id, "final_sequence": 1,
            })
            return True

        monkeypatch.setattr(proxy, "_room_send_member", deliver)
        await proxy._room_request_turn_finalization(room, "room_end")
        assert len(room.transcript) == 1
        assert room.transcript[0]["text"] == "最後一句"
        assert room.transcript[0]["partial"] is True

    asyncio.run(scenario())


def test_forced_transition_upserts_a_precommitted_item_as_partial(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        events = []

        async def broadcast(_room, payload, **_kwargs):
            if payload.get("type") == "transcript":
                events.append(dict(payload["item"]))

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 0, "text": "已 commit",
        })
        await proxy._room_commit_transcript(room, host, {
            "turn_id": turn_id, "final_sequence": 1,
        })
        host.connected = False
        await proxy._room_request_turn_finalization(room, "room_end")
        assert [item["revision"] for item in events] == [1, 2]
        assert events[0]["partial"] is False
        assert events[1]["partial"] is True
        assert events[0]["turn_id"] == events[1]["turn_id"] == turn_id
        assert room.transcript[0]["revision"] == 2

    asyncio.run(scenario())


def test_concurrent_finishers_finalize_one_turn_only_once(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        bell_entered = asyncio.Event()
        bell_release = asyncio.Event()
        original_emit = proxy._room_emit_due_bells
        calls = 0

        async def blocked_emit(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                bell_entered.set()
                await bell_release.wait()
            return await original_emit(*args, **kwargs)

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_emit_due_bells", blocked_emit)
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        first = asyncio.create_task(proxy._room_finish_turn(
            room, turn_id, partial_fallback=True,
        ))
        await bell_entered.wait()
        second = asyncio.create_task(proxy._room_finish_turn(
            room, turn_id, partial_fallback=True,
        ))
        await asyncio.sleep(0)
        bell_release.set()
        results = await asyncio.gather(first, second)
        assert sorted(results) == [False, True]
        assert room.active_turn_id is None

    asyncio.run(scenario())


def test_old_finalizer_cannot_clear_new_turn_stop_pending(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True, request_id="old")
        old_turn = room.active_turn_id
        old_state = room.turn_transcript_chunks[old_turn]
        finish_entered = asyncio.Event()
        finish_release = asyncio.Event()
        original_finish = proxy._room_finish_turn

        async def delayed_finish(_room, turn_id, **_kwargs):
            assert turn_id == old_turn
            finish_entered.set()
            await finish_release.wait()
            return False

        monkeypatch.setattr(proxy, "_room_finish_turn", delayed_finish)
        finalizer = asyncio.create_task(
            proxy._room_request_turn_finalization(room, "side_time_limit"),
        )
        await asyncio.sleep(0)
        room.active_turn_user = None
        room.active_turn_side = None
        room.active_turn_started_ms = None
        room.active_turn_id = None
        room.active_turn_request_id = None
        room.turn_stop_pending = False
        old_state["finalized_event"].set()
        await finish_entered.wait()

        # Restore the real finisher only after turn B has claimed its stop gate.
        monkeypatch.setattr(proxy, "_room_finish_turn", original_finish)

        async def no_broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", no_broadcast)
        await proxy._room_handle_turn(room, host, True, request_id="new")
        new_turn = room.active_turn_id
        await proxy._room_handle_turn_stop_intent(room, host, {
            "turn_id": new_turn, "request_id": "new",
        })
        assert room.turn_stop_pending is True
        finish_release.set()
        await finalizer
        assert room.active_turn_id == new_turn
        assert room.turn_stop_pending is True
        await proxy._room_handle_turn(room, host, False)

    asyncio.run(scenario())


def test_forced_stop_does_not_deadlock_when_final_broadcast_drops_peer():
    class FailingSocket(_WebSocket):
        async def send_text(self, _value):
            raise RuntimeError("closed transport")

    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.started_ms = room.seg_started_ms = proxy._now_ms()
        room.hard_deadline_ms = room.started_ms + 25 * 60 * 1000
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 0, "text": "斷線前內容",
        })
        room.members["guest"].ws = FailingSocket()
        # Skip the client-finalization wait so the regression isolates the
        # nested disconnect triggered by a failed final-state broadcast.
        host.connected = False
        await asyncio.wait_for(
            proxy._room_request_turn_finalization(room, "server_transition"),
            timeout=0.5,
        )
        assert room.active_turn_id is None
        assert room.transcript[0]["partial"] is True
        assert room.members["guest"].connected is False
        for task in (room.rtc_restart_task, room.empty_cleanup_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_room_transcript_total_cap_is_enforced(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        monkeypatch.setattr(proxy, "ROOM_TRANSCRIPT_TOTAL_MAX_CHARS", 5)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 0, "text": "123456789",
        })
        await proxy._room_commit_transcript(room, host, {
            "turn_id": turn_id, "final_sequence": 1,
        })
        assert room.transcript_chars == 5
        assert room.transcript[0]["text"] == "12345"
        assert room.transcript[0]["partial"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["gap", "empty", "uncommitted"])
def test_incomplete_transcript_is_skipped_without_disabling_judge(
    monkeypatch, failure,
):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        if failure == "gap":
            await proxy._room_handle_transcript(room, host, {
                "turn_id": turn_id, "sequence": 1, "text": "缺漏",
            })
        elif failure == "empty":
            await proxy._room_commit_transcript(room, host, {
                "turn_id": turn_id, "final_sequence": 0,
            })
        if room.active_turn_user:
            await proxy._room_handle_turn(room, host, False)
        assert room.phase == "active"
        assert room.judge_enabled is True
        assert room.transcript == []

    asyncio.run(scenario())


def test_segment_advance_cannot_silently_drop_uncommitted_active_turn(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        monkeypatch.setattr(proxy, "ROOM_TURN_FINALIZE_TIMEOUT_SECONDS", 0)
        host = room.members["host"]
        room.segments.append({
            "id": "next", "label": "下一節", "side": "反方",
            "seconds": 60, "bells": [],
        })
        await proxy._room_handle_turn(room, host, True)
        await proxy._room_advance_segment(room, 1)
        assert room.phase == "active"
        assert room.active_turn_user is None
        assert room.judge_enabled is True
        assert room.transcript == []

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["next", "end", "rtc_disconnect"])
def test_forced_control_transition_leaves_ws_loop_free_for_queued_commit(
    monkeypatch, transition,
):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.started_ms = room.seg_started_ms = proxy._now_ms()
        room.hard_deadline_ms = room.started_ms + 60_000
        room.segments = [
            {"id": "one", "label": "一", "side": "雙方", "seconds": 30,
             "bells": []},
            {"id": "two", "label": "二", "side": "雙方", "seconds": 30,
             "bells": []},
        ]
        monkeypatch.setattr(
            proxy, "_record_room_bandwidth_once", lambda _room: None,
        )
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True, request_id="queued")
        turn_id = room.active_turn_id

        if transition == "next":
            message = {"type": "next_segment"}
            task_prefix = "segment"
        elif transition == "end":
            message = {"type": "end"}
            task_prefix = "end"
        else:
            message = {
                "type": "rtc_status", "status": "disconnected",
                "roster_generation": room.roster_generation,
            }
            task_prefix = "rtc_pause:"
        await asyncio.wait_for(
            proxy._room_handle_message(room, host, message), timeout=0.1,
        )
        task = _control_task(room, task_prefix)
        await asyncio.sleep(0)
        assert room.active_turn_id == turn_id
        assert any(
            json.loads(value).get("type") == "turn_stop_requested"
            for value in host.ws.sent
        )
        await proxy._room_handle_message(room, host, {
            "type": "transcript_chunk", "turn_id": turn_id,
            "sequence": 0, "text": "同一 receive loop 排隊嘅 commit",
        })
        await proxy._room_handle_message(room, host, {
            "type": "transcript_commit", "turn_id": turn_id,
            "final_sequence": 1,
        })
        await asyncio.wait_for(task, timeout=2)
        assert room.transcript[0]["text"] == "同一 receive loop 排隊嘅 commit"
        assert room.transcript[0]["partial"] is True
        if transition == "next":
            assert room.phase == "active" and room.seg_index == 1
        elif transition == "end":
            assert room.phase == "ended"
        else:
            assert room.rtc_restart_task is not None
            room.rtc_restart_task.cancel()
            await asyncio.gather(room.rtc_restart_task, return_exceptions=True)

    asyncio.run(scenario())


def test_non_active_speaker_cannot_inject_transcript(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        await proxy._room_handle_turn(room, room.members["host"], True)
        await proxy._room_handle_transcript(room, room.members["guest"], {
            "turn_id": room.active_turn_id, "sequence": 0, "text": "注入",
        })
        state = room.turn_transcript_chunks[room.active_turn_id]
        assert state["chunks"] == [] and room.judge_enabled is True

    asyncio.run(scenario())


def test_preparation_segment_rejects_speech(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.segments = [{
            "id": "prep", "label": "準備", "side": "準備",
            "seconds": 60, "bells": [],
        }]
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        assert room.active_turn_user is None
        assert json.loads(host.ws.sent[-1]) == {
            "type": "turn_rejected",
            "message": "準備環節不設發言，請等待下一個正式發言環節。",
        }

    asyncio.run(scenario())


def test_turn_begin_requires_both_server_observed_rtc_connections():
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.members["guest"].rtc_status = "new"
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        assert room.active_turn_user is None
        assert json.loads(host.ws.sent[-1]) == {
            "type": "turn_rejected",
            "message": "雙方 RTC 連線未準備好，請等待重新連線完成。",
        }

    asyncio.run(scenario())


def test_mock_free_segment_uses_independent_side_banks(monkeypatch):
    async def scenario():
        room = proxy.Room(
            "MOCK2", "A", "host", "聯中", "辯題", "mock", 2, 2,
        )
        host = proxy.RoomMember("host", _WebSocket())
        guest = proxy.RoomMember("guest", _WebSocket())
        host.role, guest.role = "正方", "反方"
        host.rtc_status = guest.rtc_status = "connected"
        room.members = {"host": host, "guest": guest}
        room.phase = "active"
        room.activation_ready = True
        room.seg_index = next(
            index for index, segment in enumerate(room.segments)
            if segment.get("side") == "雙方"
        )
        room.seg_started_ms = proxy._now_ms()

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        now = [room.seg_started_ms]
        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        await proxy._room_handle_turn(room, host, True)
        now[0] += 30_000
        await proxy._room_handle_turn(room, host, False)
        await proxy._room_handle_turn(room, guest, True)
        now[0] += 45_000
        await proxy._room_handle_turn(room, guest, False)
        assert room.side_elapsed_ms == {"正方": 30_000, "反方": 45_000}

    asyncio.run(scenario())


def test_server_emits_each_authoritative_bell_once(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.seg_started_ms = 1_000
        now = [1_000]
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        await proxy._room_handle_turn(room, room.members["host"], True)
        bells = [item for item in broadcasts if item.get("type") == "bell"]
        assert [(item["side"], item["bell_index"]) for item in bells] == [
            ("正方", 0),
        ]
        now[0] += 10 * 60 * 1000
        await proxy._room_emit_due_bells(room, now_ms=now[0])
        await proxy._room_emit_due_bells(room, now_ms=now[0])
        bells = [item for item in broadcasts if item.get("type") == "bell"]
        assert [(item["side"], item["bell_index"]) for item in bells] == [
            ("正方", 0), ("正方", 1), ("正方", 2),
        ]

    asyncio.run(scenario())


def test_manual_stop_at_bank_limit_cannot_skip_final_bell(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.segments[0]["seconds"] = 1
        room.segments[0]["bells"] = [
            {"t": 0, "rings": 1, "label": "開始"},
            {"t": 1, "rings": 2, "label": "完結"},
        ]
        now = [100_000]
        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        now[0] += 1_001
        await proxy._room_handle_turn(room, host, False)
        final_bells = [
            item for item in broadcasts
            if item.get("type") == "bell" and item.get("rings") == 2
        ]
        assert len(final_bells) == 1
        assert room.side_elapsed_ms["正方"] == 1_000

    asyncio.run(scenario())


def test_server_enforces_ten_second_ice_restart_timeout(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        ended = []

        async def immediate(_seconds):
            return None

        async def end(_room, reason):
            ended.append(reason)

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy.asyncio, "sleep", immediate)
        monkeypatch.setattr(proxy, "_room_end", end)
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        await proxy._room_handle_message(
            room, room.members["host"], {
                "type": "rtc_status", "status": "disconnected",
                "roster_generation": room.roster_generation,
            },
        )
        await _control_task(room, "rtc_pause:")
        task = room.rtc_restart_task
        if task is not None:
            await task
        assert ended == ["p2p_ice_restart_timeout"]

    asyncio.run(scenario())


def test_rtc_restart_pauses_and_resumes_only_after_both_peers_connected(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        now = [100_000]
        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        room.started_ms = room.seg_started_ms = now[0] - 1_000
        room.hard_deadline_ms = now[0] + 60_000
        original_started_ms = room.started_ms
        original_seg_started_ms = room.seg_started_ms
        original_deadline_ms = room.hard_deadline_ms
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host, guest = room.members["host"], room.members["guest"]
        await proxy._room_handle_message(
            room, host, {
                "type": "rtc_status", "status": "disconnected",
                "roster_generation": room.roster_generation,
            },
        )
        await _control_task(room, "rtc_pause:")
        assert room.rtc_pause_started_ms is not None
        assert any(item.get("status") == "restart" for item in broadcasts)

        guest.rtc_status = "disconnected"
        await proxy._room_handle_message(
            room, host, {
                "type": "rtc_status", "status": "connected",
                "roster_generation": room.roster_generation,
            },
        )
        assert room.rtc_pause_started_ms is not None
        now[0] += 5_000
        await proxy._room_handle_message(
            room, guest, {
                "type": "rtc_status", "status": "connected",
                "roster_generation": room.roster_generation,
            },
        )
        assert room.rtc_pause_started_ms is None
        assert room.started_ms == original_started_ms
        assert room.hard_deadline_ms == original_deadline_ms
        assert room.seg_started_ms == original_seg_started_ms + 5_000
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_rtc_disconnect_finalizes_active_chunks_before_pause(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.started_ms = room.seg_started_ms = proxy._now_ms()
        room.hard_deadline_ms = room.started_ms + 25 * 60 * 1000

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        turn_id = room.active_turn_id
        await proxy._room_handle_transcript(room, host, {
            "turn_id": turn_id, "sequence": 0, "text": "斷線前內容",
        })
        # A failed control send makes the bounded fallback immediate.
        host.connected = False
        await proxy._room_handle_message(
            room, host, {
                "type": "rtc_status", "status": "disconnected",
                "roster_generation": room.roster_generation,
            },
        )
        await _control_task(room, "rtc_pause:")
        assert room.active_turn_user is None
        assert room.transcript[0]["partial"] is True
        assert room.rtc_pause_started_ms is not None
        room.rtc_restart_task.cancel()
        await asyncio.gather(room.rtc_restart_task, return_exceptions=True)

    asyncio.run(scenario())


def test_simultaneous_rtc_disconnects_create_one_restart_timeout(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        entered = 0
        timeout_calls = []
        timeout_release = asyncio.Event()
        broadcasts = []

        async def finalize(*_args, **_kwargs):
            nonlocal entered
            entered += 1
            await asyncio.sleep(0)

        async def restart_timeout(_room, stamp):
            timeout_calls.append(stamp)
            await timeout_release.wait()

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_room_request_turn_finalization", finalize)
        monkeypatch.setattr(proxy, "_room_rtc_restart_timeout", restart_timeout)
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        await asyncio.gather(*(
            proxy._room_handle_message(
                room, member,
                {
                    "type": "rtc_status", "status": "disconnected",
                    "roster_generation": room.roster_generation,
                },
            )
            for member in room.members.values()
        ))
        await _control_task(room, "rtc_pause:")
        await asyncio.sleep(0)
        assert entered == 1
        assert len(timeout_calls) == 1
        assert sum(
            item.get("status") == "restart" for item in broadcasts
        ) == 1
        timeout_release.set()
        await room.rtc_restart_task

    asyncio.run(scenario())


def test_segment_controls_cannot_mutate_timer_during_or_into_rtc_pause(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.segments = [
            {"id": "one", "label": "一", "side": "雙方", "seconds": 30,
             "bells": []},
            {"id": "two", "label": "二", "side": "雙方", "seconds": 30,
             "bells": []},
        ]
        room.seg_started_ms = 90_000
        room.rtc_pause_started_ms = 100_000
        await proxy._room_advance_segment(room, 1)
        assert room.seg_index == 0 and room.seg_started_ms == 90_000

        room.rtc_pause_started_ms = None
        room.active_turn_user = "host"
        room.active_turn_id = "turn"

        async def pause_during_finalization(_room, _reason):
            room.rtc_pause_started_ms = 110_000
            room.active_turn_user = None
            room.active_turn_id = None

        monkeypatch.setattr(
            proxy, "_room_request_turn_finalization", pause_during_finalization,
        )
        await proxy._room_advance_segment(room, 1)
        assert room.seg_index == 0 and room.seg_started_ms == 90_000

    asyncio.run(scenario())


def test_rapid_disconnect_reconnect_disconnect_keeps_latest_restart(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        now = [100_000]
        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        finalize_calls = 0
        restart_release = asyncio.Event()

        async def finalize(*_args, **_kwargs):
            nonlocal finalize_calls
            finalize_calls += 1
            if finalize_calls == 1:
                first_entered.set()
                await release_first.wait()

        async def restart_timeout(_room, _stamp):
            await restart_release.wait()

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_request_turn_finalization", finalize)
        monkeypatch.setattr(proxy, "_room_rtc_restart_timeout", restart_timeout)
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_message(room, host, {
            "type": "rtc_status", "status": "disconnected",
            "roster_generation": room.roster_generation,
        })
        first_task = _control_task(room, "rtc_pause:")
        await first_entered.wait()
        await proxy._room_handle_message(room, host, {
            "type": "rtc_status", "status": "connected",
            "roster_generation": room.roster_generation,
        })
        assert room.rtc_pause_started_ms is None
        now[0] += 1_000
        await proxy._room_handle_message(room, host, {
            "type": "rtc_status", "status": "disconnected",
            "roster_generation": room.roster_generation,
        })
        second_stamp = room.rtc_pause_started_ms
        second_task = next(
            task for key, task in room.control_tasks.items()
            if key == f"rtc_pause:{second_stamp}"
        )
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert room.rtc_pause_started_ms == second_stamp
        assert room.rtc_restart_task is not None
        restart_release.set()
        await room.rtc_restart_task

    asyncio.run(scenario())


def test_stale_rtc_generation_cannot_resume_replacement_pause(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        old_generation = room.roster_generation
        replacement, error, _ = await proxy._room_register_socket(
            room, "guest", _WebSocket(),
        )
        assert not error and room.roster_generation > old_generation
        pause_stamp = replacement.restart_required

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        await proxy._room_complete_disconnect_pause(
            room, pause_stamp, "control_replaced",
        )
        restart_task = room.rtc_restart_task
        host = room.members["host"]
        await proxy._room_handle_message(room, host, {
            "type": "rtc_status", "status": "connected",
            "roster_generation": old_generation,
        })
        assert host.rtc_status == "new"
        await proxy._room_handle_message(room, replacement, {
            "type": "rtc_status", "status": "connected",
            "roster_generation": room.roster_generation,
        })
        assert room.rtc_pause_started_ms is not None
        await proxy._room_handle_message(room, host, {
            "type": "rtc_status", "status": "connected",
            "roster_generation": room.roster_generation,
        })
        assert room.rtc_pause_started_ms is None
        await asyncio.gather(restart_task, return_exceptions=True)

    asyncio.run(scenario())


def test_control_rate_bucket_survives_socket_replacement(monkeypatch):
    async def scenario():
        room = _free_room()
        member = room.members["host"]
        now = [member.joined_at]
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        for index in range(system_limits.ROOM_CONTROL_RATE_BURST_MESSAGES + 5):
            await proxy._room_handle_message(
                room, member, {"type": "chat", "text": str(index)},
            )
        assert len(broadcasts) == system_limits.ROOM_CONTROL_RATE_BURST_MESSAGES
        replaced, error, _ = await proxy._room_register_socket(
            room, member.user_id, _WebSocket(),
        )
        assert replaced is member and not error
        await proxy._room_handle_message(room, member, {"type": "chat", "text": "bypass"})
        assert len(broadcasts) == system_limits.ROOM_CONTROL_RATE_BURST_MESSAGES

    asyncio.run(scenario())


def test_ignored_media_and_malformed_types_still_consume_control_rate(monkeypatch):
    async def scenario():
        room = _free_room()
        member = room.members["host"]
        now = [member.joined_at]
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        await proxy._room_handle_message(room, member, {"type": []})
        assert member.control_rate_tokens == (
            system_limits.ROOM_CONTROL_RATE_BURST_MESSAGES - 1
        )
        for _index in range(system_limits.ROOM_CONTROL_RATE_BURST_MESSAGES - 1):
            await proxy._room_handle_message(room, member, {"type": "audio"})
        await proxy._room_handle_message(
            room, member, {"type": "chat", "text": "rate bypass"},
        )
        assert broadcasts == []

    asyncio.run(scenario())


def test_exhausted_normal_bucket_still_accepts_commit_and_safe_turn_end(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        fixed_now = host.control_rate_updated_ms
        monkeypatch.setattr(proxy, "_now_ms", lambda: fixed_now)
        await proxy._room_handle_turn(room, host, True, request_id="burst")
        turn_id = room.active_turn_id
        for sequence in range(system_limits.ROOM_CONTROL_RATE_BURST_MESSAGES):
            await proxy._room_handle_message(room, host, {
                "type": "transcript_chunk", "turn_id": turn_id,
                "sequence": sequence, "text": str(sequence),
            })
        assert host.control_rate_tokens == 0
        await proxy._room_handle_message(room, host, {
            "type": "transcript_commit", "turn_id": turn_id,
            "final_sequence": system_limits.ROOM_CONTROL_RATE_BURST_MESSAGES,
        })
        await proxy._room_handle_message(room, host, {
            "type": "turn_end", "turn_id": turn_id,
            "request_id": "burst",
        })
        assert room.active_turn_id is None
        assert len(room.transcript) == 1
        assert room.transcript[0]["partial"] is False
        assert host.critical_rate_tokens == (
            system_limits.ROOM_CRITICAL_RATE_BURST_MESSAGES - 2
        )

    asyncio.run(scenario())


def test_member_sends_are_serialized_and_state_sequence_is_monotonic():
    class BlockingSocket(_WebSocket):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def send_text(self, value):
            self.sent.append(value)
            if len(self.sent) == 1:
                self.entered.set()
                await self.release.wait()

    async def scenario():
        room = _free_room()
        host = room.members["host"]
        host.ws = BlockingSocket()
        generation = host.connection_generation
        first = asyncio.create_task(proxy._room_send_member(
            room, host, {"order": 1}, generation=generation,
        ))
        await host.ws.entered.wait()
        second = asyncio.create_task(proxy._room_send_member(
            room, host, {"order": 2}, generation=generation,
        ))
        await asyncio.sleep(0)
        assert [json.loads(value)["order"] for value in host.ws.sent] == [1]
        host.ws.release.set()
        assert await asyncio.gather(first, second) == [True, True]
        assert [json.loads(value)["order"] for value in host.ws.sent] == [1, 2]
        state_one = room.state_msg()
        state_two = room.state_msg()
        assert state_two["state_sequence"] == state_one["state_sequence"] + 1

    asyncio.run(scenario())


def test_socket_replacement_invalidates_whole_lobby_precheck():
    async def scenario():
        room = _free_room()
        room.precheck_id = "stale-check"
        room.precheck_results = {
            "host": {"ok": True}, "guest": {"ok": True},
        }
        old_generation = room.members["guest"].connection_generation
        replacement, error, _ = await proxy._room_register_socket(
            room, "guest", _WebSocket(),
        )
        assert not error
        assert replacement.connection_generation == old_generation + 1
        assert room.precheck_id is None and room.precheck_results == {}

    asyncio.run(scenario())


def test_guest_first_reserves_creator_side_and_creator_slot():
    async def scenario():
        room = proxy.Room(
            "ORDER", "A", "host", proxy.DEBATE_FORMATS[0],
            "辯題", "free", 2.5, 2,
        )
        room.creator_side = "正方"
        guest, error, _ = await proxy._room_register_socket(
            room, "guest", _WebSocket(),
        )
        assert not error and guest.role == "反方"
        blocked, error, close_code = await proxy._room_register_socket(
            room, "outsider", _WebSocket(),
        )
        assert blocked is None and "主持" in error and close_code == 1013
        assert proxy._room_nonmember_access_error(room, "outsider") == (
            409, "房間正為主持預留一個位置。",
        )
        host, error, _ = await proxy._room_register_socket(
            room, "host", _WebSocket(),
        )
        assert not error and host.role == "正方"
        host.rtc_status = guest.rtc_status = "connected"
        assert proxy._room_start_blocker(room) is None

    asyncio.run(scenario())


def test_active_replacement_send_failure_still_starts_restart_window():
    class FailingSocket(_WebSocket):
        async def send_text(self, _value):
            raise RuntimeError("closed transport")

    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.started_ms = room.seg_started_ms = proxy._now_ms()
        room.hard_deadline_ms = room.started_ms + 25 * 60 * 1000
        member = room.members["guest"]
        old_socket = member.ws
        replacement_socket = FailingSocket()
        replacement, error, _ = await proxy._room_register_socket(
            room, member.user_id, replacement_socket,
        )
        assert not error and replacement is member
        assert old_socket.closed is True
        assert room.rtc_pause_started_ms is not None
        assert member.restart_required == room.rtc_pause_started_ms
        assert member.rtc_status == "new"
        await proxy._room_handle_turn(room, room.members["host"], True)
        assert room.active_turn_user is None

        # If bootstrap egress fails before room_ws consumes the saved stamp,
        # the broadcast drop path must carry it forward into the same timeout.
        await proxy._room_broadcast(room, {"type": "state"})
        await _control_task(room, "rtc_pause:")
        assert member.restart_required is None
        assert member.connected is False
        assert room.rtc_restart_task is not None
        room.rtc_restart_task.cancel()
        await asyncio.gather(room.rtc_restart_task, return_exceptions=True)

    asyncio.run(scenario())


def test_replaced_turn_owner_can_commit_while_restart_finalizes(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True, request_id="replace")
        turn_id = room.active_turn_id
        replacement, error, _ = await proxy._room_register_socket(
            room, "host", _WebSocket(),
        )
        assert not error and replacement.restart_required is not None
        stamp = replacement.restart_required
        replacement.restart_required = None
        task = proxy._room_schedule_control_task(
            room, f"rtc_pause:{stamp}",
            lambda: proxy._room_complete_disconnect_pause(
                room, stamp, "control_replaced",
            ),
        )
        await asyncio.sleep(0)
        await proxy._room_handle_message(room, replacement, {
            "type": "transcript_chunk", "turn_id": turn_id,
            "sequence": 0, "text": "reconnect commit",
        })
        await proxy._room_handle_message(room, replacement, {
            "type": "transcript_commit", "turn_id": turn_id,
            "final_sequence": 1,
        })
        await asyncio.wait_for(task, timeout=0.5)
        assert room.transcript[0]["text"] == "reconnect commit"
        assert room.transcript[0]["partial"] is True
        room.rtc_restart_task.cancel()
        await asyncio.gather(room.rtc_restart_task, return_exceptions=True)

    asyncio.run(scenario())


def test_broadcast_failure_drops_lobby_ghost_and_publishes_roster():
    class FailingSocket(_WebSocket):
        async def send_text(self, _value):
            raise RuntimeError("closed transport")

    async def scenario():
        room = _free_room()
        room.members["guest"].ws = FailingSocket()
        host = room.members["host"]
        await proxy._room_broadcast(room, {"type": "chat", "text": "hello"})
        assert "guest" not in room.members
        assert room.roster_generation == 3
        host_messages = [json.loads(value) for value in host.ws.sent]
        assert any(item.get("type") == "peer_left" for item in host_messages)
        assert host_messages[-1]["type"] == "roster"
        assert [item["user_id"] for item in host_messages[-1]["roster"]] == [
            "host",
        ]

    asyncio.run(scenario())


def test_room_leave_is_once_only_against_socket_finally(monkeypatch):
    async def scenario():
        room = _free_room()
        proxy.ROOMS[room.code] = room
        initial_generation = room.roster_generation
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "require_page_user", lambda *_args: "guest")
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        request = _json_request({
            "leave_token": room.members["guest"].leave_token,
        })
        await proxy.room_leave(room.code, request)
        await proxy.room_leave(room.code, request)
        assert room.roster_generation == initial_generation + 1
        assert "guest" not in room.members
        assert [item["type"] for item in broadcasts] == ["peer_left", "roster"]

    asyncio.run(scenario())


def test_active_room_leave_freezes_clocks_and_starts_restart_window(monkeypatch):
    async def scenario():
        room = _free_room("LEAVE")
        room.phase = "active"
        room.activation_ready = True
        room.started_ms = room.seg_started_ms = proxy._now_ms()
        room.hard_deadline_ms = room.started_ms + 25 * 60 * 1000
        proxy.ROOMS[room.code] = room
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "require_page_user", lambda *_args: "guest")
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        replacement, error, _ = await proxy._room_register_socket(
            room, "guest", _WebSocket(),
        )
        assert not error and replacement.restart_required is not None
        await proxy.room_leave(room.code, _json_request({
            "leave_token": replacement.leave_token,
        }))
        assert room.rtc_pause_started_ms is not None
        assert room.rtc_restart_task is not None
        assert room.members["guest"].rtc_status == "disconnected"
        assert room.members["guest"].restart_required is None
        roster_index = next(
            index for index, item in enumerate(broadcasts)
            if item.get("type") == "roster"
        )
        state_index = next(
            index for index, item in enumerate(broadcasts)
            if item.get("type") == "state"
        )
        assert roster_index < state_index
        peer_left = next(
            item for item in broadcasts if item.get("type") == "peer_left"
        )
        assert peer_left["roster_generation"] == room.roster_generation
        room.rtc_restart_task.cancel()
        await asyncio.gather(room.rtc_restart_task, return_exceptions=True)

    asyncio.run(scenario())


def test_stale_leave_token_cannot_disconnect_replacement_socket(monkeypatch):
    async def scenario():
        room = _free_room("NONCE")
        room.phase = "active"
        room.activation_ready = True
        proxy.ROOMS[room.code] = room
        guest = room.members["guest"]
        stale_token = guest.leave_token
        replacement_socket = _WebSocket()
        replacement, error, _ = await proxy._room_register_socket(
            room, "guest", replacement_socket,
        )
        assert not error and replacement.leave_token != stale_token
        generation = replacement.connection_generation
        monkeypatch.setattr(proxy, "require_page_user", lambda *_args: "guest")
        response = await proxy.room_leave(room.code, _json_request({
            "leave_token": stale_token,
        }))
        assert response.status_code == 409
        assert replacement.connected is True
        assert replacement.ws is replacement_socket
        assert replacement.connection_generation == generation
        assert replacement_socket.closed is False

    asyncio.run(scenario())


def test_non_string_message_type_is_safely_ignored():
    async def scenario():
        room = _free_room()
        await proxy._room_handle_message(
            room, room.members["host"], {"type": []},
        )
        assert room.members["host"].connected is True

    asyncio.run(scenario())


def test_room_text_parser_rejects_oversize_before_json_decode(monkeypatch):
    assert proxy._parse_room_client_text('{"type":"chat"}') == {"type": "chat"}
    assert proxy._parse_room_client_text("[]") is None
    oversized = "長" * (system_limits.ROOM_WS_TEXT_MAX_BYTES // 2)
    monkeypatch.setattr(
        proxy.json, "loads",
        lambda _value: (_ for _ in ()).throw(
            AssertionError("oversized frame reached json.loads"),
        ),
    )
    assert proxy._parse_room_client_text(oversized) is None


@pytest.mark.parametrize(
    ("origin", "host", "expected"),
    [
        ("https://coach.example", "coach.example", True),
        ("https://coach.example:443", "coach.example", True),
        ("https://coach.example:444", "coach.example", False),
        ("https://evil.example", "coach.example", False),
        ("http://coach.example", "coach.example", False),
        ("https://coach.example/path", "coach.example", False),
        ("", "coach.example", False),
    ],
)
def test_room_websocket_origin_is_strict_same_host(origin, host, expected):
    socket = SimpleNamespace(
        headers={"origin": origin, "host": host},
        scope={"scheme": "wss"},
    )
    assert proxy._room_websocket_origin_allowed(socket) is expected


def test_room_websocket_origin_honours_tls_terminating_proxy_proto():
    socket = SimpleNamespace(
        headers={
            "origin": "https://coach.example",
            "host": "coach.example",
            "x-forwarded-proto": "https",
        },
        scope={"scheme": "ws"},
    )
    assert proxy._room_websocket_origin_allowed(socket) is True


def test_disabled_judge_never_starts_final_provider_call(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.judge_enabled = False
        room.transcript = [{"side": "正方", "text": "已有逐字稿"}]
        room.transcript_revision = 1

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("disabled judge reached provider")

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_request_judgement", forbidden)
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        monkeypatch.setattr(proxy, "_record_room_bandwidth_once", lambda _room: None)
        await proxy._room_end(room, "host")
        assert room.phase == "ended" and room.judgement_task is None

    asyncio.run(scenario())


def test_room_end_never_auto_starts_enabled_judgement(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.transcript = [
            {"side": "正方", "text": "正方稿"},
            {"side": "反方", "text": "反方稿"},
        ]
        room.transcript_revision = 2

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("room end auto-started AI judgement")

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_request_judgement", forbidden)
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        monkeypatch.setattr(proxy, "_record_room_bandwidth_once", lambda _room: None)
        await proxy._room_end(room, "host")
        assert room.phase == "ended"
        assert room.judgement_task is None
        assert room.judgement_request_started is False
        assert room.ended_retain_until_ms - room.ended_at_ms == (
            system_limits.ROOM_ENDED_RETENTION_SECONDS * 1000
        )

    asyncio.run(scenario())


def test_manual_ended_judgement_is_host_only_once_and_idempotent(monkeypatch):
    async def scenario():
        room = _free_room("JUDGE")
        room.phase = "ended"
        room.ended_at_ms = proxy._now_ms()
        room.ended_retain_until_ms = room.ended_at_ms + 1_000
        room.transcript = [
            {"side": "正方", "text": "正方稿"},
            {"side": "反方", "text": "反方稿"},
        ]
        room.transcript_revision = 2
        proxy.ROOMS[room.code] = room
        calls = 0
        current_user = ["guest"]

        async def fake_unlocked(target):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            target.judgement = "建議勝方：正方"
            target.judgement_revision = target.transcript_revision

        monkeypatch.setattr(
            proxy, "require_page_user", lambda *_args: current_user[0],
        )
        monkeypatch.setattr(proxy, "_room_request_judgement_unlocked", fake_unlocked)
        with pytest.raises(HTTPException) as forbidden:
            await proxy.room_request_judgement(room.code, object())
        assert forbidden.value.status_code == 403
        assert room.judgement_request_started is False
        current_user[0] = "host"
        first = await proxy.room_request_judgement(room.code, object())
        second = await proxy.room_request_judgement(room.code, object())
        assert first.status_code == second.status_code == 202
        assert room.judgement_request_started is True
        await room.judgement_task
        third = await proxy.room_request_judgement(room.code, object())
        assert third.status_code == 200
        payload = json.loads(third.body)
        assert payload["judgement"] == "建議勝方：正方"
        assert payload["judgement_pending"] is False
        assert calls == 1
        assert room.ended_retain_until_ms >= (
            proxy._now_ms() + system_limits.ROOM_ENDED_RETENTION_SECONDS * 1000 - 10
        )
        if room.ended_cleanup_task is not None:
            room.ended_cleanup_task.cancel()
            await asyncio.gather(room.ended_cleanup_task, return_exceptions=True)

    asyncio.run(scenario())


def test_missing_side_does_not_consume_manual_judgement(monkeypatch):
    async def scenario():
        room = _free_room("MISS1")
        room.phase = "ended"
        room.transcript = [{"side": "正方", "text": "正方稿"}]
        room.transcript_revision = 1
        proxy.ROOMS[room.code] = room
        monkeypatch.setattr(proxy, "require_page_user", lambda *_args: "host")
        with pytest.raises(HTTPException) as exc:
            await proxy.room_request_judgement(room.code, object())
        assert exc.value.status_code == 409
        assert "反方未有逐字稿" in str(exc.value.detail)
        assert room.judgement_request_started is False
        assert room.judgement_task is None

    asyncio.run(scenario())


def test_transcript_snapshot_reports_manual_judgement_capability(monkeypatch):
    async def scenario():
        room = _free_room("SNAP1")
        room.phase = "ended"
        room.transcript = [
            {"side": "正方", "text": "正方稿"},
            {"side": "反方", "text": "反方稿"},
        ]
        room.transcript_revision = 2
        proxy.ROOMS[room.code] = room
        monkeypatch.setattr(proxy, "require_page_user", lambda *_args: "host")
        response = await proxy.room_transcript(room.code, object())
        payload = json.loads(response.body)
        assert payload["host"] == "host" and payload["is_host"] is True
        assert payload["roster"] == room.roster()
        assert payload["judgement_requested"] is False
        assert payload["judgement_pending"] is False
        assert payload["can_request_judgement"] is True

    asyncio.run(scenario())


def test_judgement_requires_transcript_from_both_sides_before_provider(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.transcript = [{"side": "正方", "text": "已有逐字稿"}]
        room.transcript_revision = 1
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("missing-side transcript reached provider setup")

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        monkeypatch.setattr(proxy, "_get_proxy_secret", forbidden)
        await proxy._room_request_judgement(room)

        assert "反方未有逐字稿" in room.judgement
        assert broadcasts[-1] == {"type": "judgement", "text": room.judgement}

    asyncio.run(scenario())


def test_ws_judgement_request_is_rejected_without_starting_task(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        await proxy._room_handle_message(
            room, room.members["host"], {"type": "request_judgement"},
        )
        assert room.judgement_task is None
        assert "完場後" in json.loads(room.members["host"].ws.sent[-1])["message"]

    asyncio.run(scenario())


def test_room_info_and_page_enforce_full_and_started_member_gates(monkeypatch):
    async def scenario():
        room = _free_room("GATE1")
        proxy.ROOMS[room.code] = room
        current_user = ["outsider"]
        monkeypatch.setattr(
            proxy, "require_page_user", lambda *_args: current_user[0],
        )
        with pytest.raises(HTTPException) as full:
            await proxy.room_info(room.code, object())
        assert full.value.status_code == 409
        page = await proxy.ai_coach_room_page(room.code, object())
        assert "房間已滿" in page.body.decode("utf-8")

        room.phase = "active"
        with pytest.raises(HTTPException) as started:
            await proxy.room_info(room.code, object())
        assert started.value.status_code == 403
        current_user[0] = "host"
        member_response = await proxy.room_info(room.code, object())
        assert json.loads(member_response.body)["phase"] == "active"

    asyncio.run(scenario())


def test_room_create_requires_nonblank_topic(monkeypatch):
    monkeypatch.setattr(proxy, "require_page_user", lambda *_args: "host")
    payload = {
        "mode": "A", "debate_format": proxy.DEBATE_FORMATS[0],
        "structure": "free", "side": "正方", "topic": "   ",
        "free_minutes": 2.5,
    }
    with pytest.raises(HTTPException) as exc:
        asyncio.run(proxy.room_create(_json_request(payload)))
    assert exc.value.status_code == 400
    assert "辯題" in str(exc.value.detail)
    assert proxy.ROOMS == {}


def test_retained_ended_registry_cap_blocks_new_room(monkeypatch):
    async def scenario():
        now = proxy._now_ms()
        for index in range(system_limits.ROOM_RETAINED_ENDED_MAX):
            retained = _free_room(f"E{index:04d}"[-5:])
            retained.phase = "ended"
            retained.ended_retain_until_ms = (
                now + system_limits.ROOM_ENDED_RETENTION_SECONDS * 1000
            )
            proxy.ROOMS[retained.code] = retained
        monkeypatch.setattr(proxy, "require_page_user", lambda *_args: "host")
        payload = {
            "mode": "A", "debate_format": proxy.DEBATE_FORMATS[0],
            "structure": "free", "side": "正方", "topic": "新辯題",
            "free_minutes": 2.5,
        }
        with pytest.raises(HTTPException) as exc:
            await proxy.room_create(_json_request(payload))
        assert exc.value.status_code == 429
        assert "完場結果" in str(exc.value.detail)

    asyncio.run(scenario())


def test_room_frontend_is_stun_only_and_has_no_render_media_fallback():
    html = (proxy.BASE_DIR / "templates" / "room_debate.html").read_text("utf-8")
    js = (proxy.BASE_DIR / "frontend/shared/room-debate-p2p.js").read_text("utf-8")
    assert "stun.cloudflare.com:3478" in js
    lowered = js.lower()
    assert 'urls: "turn:' not in lowered and "urls: 'turn:" not in lowered
    assert "sfu" not in lowered
    assert "MediaRecorder" not in js and "audio_base64" not in js
    assert "preflight_ready" in js
    assert "ensurePeer().then" not in js
    assert "建議所有用戶使用電腦版 Chrome。" in html
    assert "Mode A · STUN-only P2P" not in html + js
    assert "8 秒粵語 final transcript" not in html
    assert "只會傳送 P2P Opus 音訊" not in html
    assert "要求 AI 評價" in html
    assert "transcriptSides" in js
    assert "cantoneseTranscriptTest" not in js
    referenced_ids = set(re.findall(r'\$\("([A-Za-z][A-Za-z0-9_-]*)"\)', js))
    html_ids = set(re.findall(r'\bid="([A-Za-z][A-Za-z0-9_-]*)"', html))
    assert referenced_ids <= html_ids


def test_changed_practice_shells_revalidate_room_contract(monkeypatch):
    monkeypatch.setattr(proxy, "_scheduled_feature_page_block", lambda _request: None)
    coach = asyncio.run(proxy.ai_coach_page(object()))
    assert coach.headers["cache-control"] == "no-cache"
    monkeypatch.setattr(proxy, "require_kiosk_user", lambda _request: "kiosk")
    kiosk = asyncio.run(proxy.appliance_ai_debate_page(object()))
    assert kiosk.headers["cache-control"] == "no-cache"


def test_training_and_coach_share_one_rag_embedding_semaphore():
    assert ai_training_api.RAG_EMBED_SEMAPHORE is rag.RAG_EMBED_SEMAPHORE
    assert system_limits.RAG_EMBED_CONCURRENCY == 3
