"""Adversarial offline regressions for multiplayer Live room state."""

import asyncio
import base64
import json
import threading
from contextlib import contextmanager

import pytest
from fastapi import HTTPException, Request

from api import admin_console_api, ai_training_api
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


def _free_room(code="ROOM1", *, mode="A"):
    room = proxy.Room(
        code, mode, "host", proxy.DEBATE_FORMATS[0], "辯題",
        "free", 10, 4,
    )
    if mode == "A":
        pro = proxy.RoomMember("host", _WebSocket())
        con = proxy.RoomMember("guest", _WebSocket())
        pro.role, con.role = "正方", "反方"
        room.members = {"host": pro, "guest": con}
    return room


def test_multiplayer_free_preserves_original_ten_minute_room_cap(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "ready"
        room.precheck_results = {"host": {"ok": True}, "guest": {"ok": True}}
        roster_signature = proxy._room_connected_roster_signature(room)
        monkeypatch.setattr(proxy, "_bandwidth_live_gate_error", lambda: None)
        monkeypatch.setattr(proxy, "_reserve_room_practice_slots", lambda *_a: True)
        monkeypatch.setattr(proxy, "_room_mint_gemini_tokens", lambda _room: None)
        monkeypatch.setattr(proxy, "_room_ensure_tick", lambda _room: None)

        async def broadcast(*_args, **_kwargs):
            return None

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        assert await proxy._room_start_active(
            room, expected_precheck_id="ready",
            expected_roster_signature=roster_signature,
        ) is None
        assert room.hard_deadline_ms - room.started_ms == 10 * 60 * 1000
        assert room.activation_ready is True

        plan = proxy._build_room_plan(
            "ROOM2", "B", "host", proxy.DEBATE_FORMATS[0], "辯題",
            "free", 10, 4, {"human_side": "正方"},
        )
        assert plan.gemini["session_minutes"] == [12.0]

    asyncio.run(scenario())


def test_stale_parallel_precheck_cannot_reserve_after_first_failure(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "same-check"
        room.precheck_results = {"host": {"ok": True}, "guest": {"ok": True}}
        calls = {"reserve": 0, "mint": 0, "release": 0}

        monkeypatch.setattr(proxy, "_bandwidth_live_gate_error", lambda: None)

        def reserve(*_args):
            calls["reserve"] += 1
            return True

        def mint(_room):
            calls["mint"] += 1
            return "token mint failed"

        def release(*_args):
            calls["release"] += 1

        monkeypatch.setattr(proxy, "_reserve_room_practice_slots", reserve)
        monkeypatch.setattr(proxy, "_room_mint_gemini_tokens", mint)
        monkeypatch.setattr(proxy, "_release_room_practice_slots", release)

        results = await asyncio.gather(
            proxy._room_start_active(
                room, expected_precheck_id="same-check",
                expected_roster_signature=proxy._room_connected_roster_signature(room),
            ),
            proxy._room_start_active(
                room, expected_precheck_id="same-check",
                expected_roster_signature=proxy._room_connected_roster_signature(room),
            ),
        )
        assert calls == {"reserve": 1, "mint": 1, "release": 1}
        assert room.phase == "lobby" and room.precheck_id is None
        assert any("失效" in result for result in results if result)

    asyncio.run(scenario())


def test_failed_precheck_resets_atomically_and_host_can_retry(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "failed-check"
        room.precheck_results = {"host": {"ok": True}}
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        await proxy._room_handle_precheck_result(
            room, room.members["guest"], {
                "type": "precheck_result", "check_id": "failed-check",
                "ok": False, "message": "mic denied",
            },
        )
        assert room.phase == "lobby"
        assert room.precheck_id is None and room.precheck_results == {}
        failed = [item for item in broadcasts if item["type"] == "precheck_failed"]
        assert len(failed) == 1
        assert failed[0]["check_id"] == "failed-check"
        assert failed[0]["results"]["guest"]["ok"] is False

        monkeypatch.setattr(proxy.secrets, "token_hex", lambda _n: "retry-check")
        await proxy._room_begin_precheck(room)
        assert room.precheck_id == "retry-check"
        assert broadcasts[-2]["type"] == "precheck_request"
        assert broadcasts[-2]["check_id"] == "retry-check"

    asyncio.run(scenario())


def test_reconnect_during_bandwidth_gate_invalidates_precheck_before_reserve(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "ready"
        room.precheck_results = {"host": {"ok": True}, "guest": {"ok": True}}
        roster_signature = proxy._room_connected_roster_signature(room)
        gate_entered = threading.Event()
        gate_release = threading.Event()
        calls = {"reserve": 0, "mint": 0}

        def gate():
            gate_entered.set()
            gate_release.wait(timeout=2)
            return None

        def reserve(*_args):
            calls["reserve"] += 1
            return True

        def mint(_room):
            calls["mint"] += 1
            return None

        monkeypatch.setattr(proxy, "_bandwidth_live_gate_error", gate)
        monkeypatch.setattr(proxy, "_reserve_room_practice_slots", reserve)
        monkeypatch.setattr(proxy, "_room_mint_gemini_tokens", mint)
        task = asyncio.create_task(proxy._room_start_active(
            room, expected_precheck_id="ready",
            expected_roster_signature=roster_signature,
        ))
        assert await asyncio.to_thread(gate_entered.wait, 1)
        async with room.lock:
            room.members["host"].connection_generation += 1
            room.precheck_results.pop("host", None)
        gate_release.set()
        result = await task
        assert "有變" in result
        assert calls == {"reserve": 0, "mint": 0}
        assert room.phase == "lobby" and room.precheck_id is None

    asyncio.run(scenario())


def test_reconnect_during_reservation_rolls_back_before_token_mint(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "ready"
        room.precheck_results = {"host": {"ok": True}, "guest": {"ok": True}}
        roster_signature = proxy._room_connected_roster_signature(room)
        reserve_entered = threading.Event()
        reserve_release = threading.Event()
        calls = {"reserve": 0, "release": 0, "mint": 0}

        monkeypatch.setattr(proxy, "_bandwidth_live_gate_error", lambda: None)

        def reserve(*_args):
            calls["reserve"] += 1
            reserve_entered.set()
            reserve_release.wait(timeout=2)
            return True

        def release(*_args):
            calls["release"] += 1

        def mint(_room):
            calls["mint"] += 1
            return None

        monkeypatch.setattr(proxy, "_reserve_room_practice_slots", reserve)
        monkeypatch.setattr(proxy, "_release_room_practice_slots", release)
        monkeypatch.setattr(proxy, "_room_mint_gemini_tokens", mint)
        task = asyncio.create_task(proxy._room_start_active(
            room, expected_precheck_id="ready",
            expected_roster_signature=roster_signature,
        ))
        assert await asyncio.to_thread(reserve_entered.wait, 1)
        async with room.lock:
            room.members["host"].connection_generation += 1
            room.precheck_results.pop("host", None)
        reserve_release.set()
        result = await task
        assert "已撤銷扣除" in result
        assert calls == {"reserve": 1, "release": 1, "mint": 0}
        assert room.quota_users == []
        assert room.phase == "lobby" and room.precheck_id is None

    asyncio.run(scenario())


def test_activation_release_exception_resets_starting_without_false_refund(monkeypatch):
    async def scenario():
        room = _free_room()
        room.precheck_id = "ready"
        room.precheck_results = {"host": {"ok": True}, "guest": {"ok": True}}
        roster_signature = proxy._room_connected_roster_signature(room)
        monkeypatch.setattr(proxy, "_bandwidth_live_gate_error", lambda: None)
        monkeypatch.setattr(proxy, "_reserve_room_practice_slots", lambda *_a: True)
        monkeypatch.setattr(proxy, "_room_mint_gemini_tokens", lambda _room: "mint failed")

        def broken_release(*_args):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(proxy, "_release_room_practice_slots", broken_release)
        result = await proxy._room_start_active(
            room, expected_precheck_id="ready",
            expected_roster_signature=roster_signature,
        )
        assert "回滾暫時未能確認" in result
        assert "可能仍被佔用" in result
        assert "未有扣除" not in result
        assert room.phase == "lobby"
        assert room.precheck_id is None and room.precheck_results == {}
        assert room.started_ms is None and room.activation_ready is False
        assert room.quota_users == ["host", "guest"]

    asyncio.run(scenario())


def test_room_audio_and_lobby_test_audio_reject_fanout_amplification(monkeypatch):
    async def scenario():
        now = [10_000]
        broadcasts = []
        forwards = []
        room = _free_room()
        member = room.members["host"]
        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        monkeypatch.setattr(proxy, "_checkpoint_room_bandwidth", lambda *_a: None)
        monkeypatch.setattr(proxy, "_bandwidth_live_gate_error", lambda: None)

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        async def forward(*args):
            forwards.append(args)

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        monkeypatch.setattr(proxy, "_room_forward_audio_to_gemini", forward)

        valid_test = base64.b64encode(b"x" * 22_400).decode("ascii")
        await proxy._room_handle_message(room, member, {
            "type": "test_audio", "data": valid_test,
            "mimeType": "audio/pcm;rate=16000",
        })
        await proxy._room_handle_message(room, member, {
            "type": "test_audio",
            "data": base64.b64encode(
                b"x" * (system_limits.ROOM_TEST_AUDIO_MAX_BYTES + 1),
            ).decode("ascii"),
            "mimeType": "audio/pcm;rate=16000",
        })
        await proxy._room_handle_message(room, member, {
            "type": "test_audio", "data": "!!!!",
            "mimeType": "audio/pcm;rate=16000",
        })
        await proxy._room_handle_message(room, member, {
            "type": "test_audio", "data": valid_test,
            "mimeType": "audio/webm",
        })
        # Repeated valid fan-out inside the cooldown is also dropped.
        await proxy._room_handle_message(room, member, {
            "type": "test_audio", "data": valid_test,
            "mimeType": "audio/pcm;rate=16000",
        })
        assert [item["type"] for item in broadcasts] == ["test_audio"]

        room.phase = "active"
        room.activation_ready = True
        room.active_turn_user = "host"
        room.mode = "B"
        room.human_side = "正方"
        room.gemini_ws = object()
        valid_frame = base64.b64encode(b"y" * 4096).decode("ascii")
        for data, mime in (
            (valid_frame, "audio/pcm;rate=16000"),
            ("!!!!", "audio/pcm;rate=16000"),
            (valid_frame, "audio/webm"),
            (base64.b64encode(
                b"z" * (system_limits.ROOM_AUDIO_FRAME_MAX_BYTES + 1),
            ).decode("ascii"), "audio/pcm;rate=16000"),
        ):
            await proxy._room_handle_audio(
                room, member,
                {"type": "audio", "data": data, "mimeType": mime},
            )
        peer_audio = [item for item in broadcasts if item["type"] == "peer_audio"]
        assert len(peer_audio) == 1 and peer_audio[0]["data"] == valid_frame
        assert len(forwards) == 1 and forwards[0][2] == valid_frame

    asyncio.run(scenario())


def test_audio_requires_accepted_turn_and_member_bucket_survives_reconnect(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        member = room.members["host"]
        now = [member.joined_at]
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        frame = base64.b64encode(
            b"a" * system_limits.ROOM_AUDIO_FRAME_MAX_BYTES,
        ).decode("ascii")

        # Valid PCM is still rejected until turn_begin was accepted by server.
        await proxy._room_handle_audio(
            room, member,
            {"type": "audio", "data": frame, "mimeType": "audio/pcm;rate=16000"},
        )
        assert broadcasts == []

        room.active_turn_user = member.user_id
        room.active_turn_side = member.role
        room.active_turn_started_ms = now[0]
        await proxy._room_handle_audio(room, member, {
            "type": "audio", "data": frame, "mimeType": "audio/pcm;rate=16000",
        })
        await proxy._room_handle_audio(room, member, {
            "type": "audio", "data": frame, "mimeType": "audio/pcm;rate=16000",
        })
        await proxy._room_handle_audio(room, member, {
            "type": "audio", "data": frame, "mimeType": "audio/pcm;rate=16000",
        })
        assert len([x for x in broadcasts if x["type"] == "peer_audio"]) == 2

        replacement = _WebSocket()
        replaced, error, _code = await proxy._room_register_socket(
            room, member.user_id, replacement,
        )
        assert replaced is member and not error
        await proxy._room_handle_audio(room, member, {
            "type": "audio", "data": frame, "mimeType": "audio/pcm;rate=16000",
        })
        assert len([x for x in broadcasts if x["type"] == "peer_audio"]) == 2

        now[0] += 1_000
        await proxy._room_handle_audio(room, member, {
            "type": "audio", "data": frame, "mimeType": "audio/pcm;rate=16000",
        })
        assert len([x for x in broadcasts if x["type"] == "peer_audio"]) == 3

    asyncio.run(scenario())


def test_audio_message_bucket_charges_invalid_frames_and_survives_reconnect(monkeypatch):
    async def scenario():
        room = _free_room()
        member = room.members["host"]
        now = [member.joined_at]
        handled = []
        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])

        async def handle_audio(_room, _member, message):
            handled.append(message)

        monkeypatch.setattr(proxy, "_room_handle_audio", handle_audio)
        invalid = {
            "type": "audio", "data": "!!!!",
            "mimeType": "audio/pcm;rate=16000",
        }
        for _ in range(system_limits.ROOM_AUDIO_RATE_BURST_MESSAGES + 5):
            await proxy._room_handle_message(room, member, invalid)
        assert len(handled) == system_limits.ROOM_AUDIO_RATE_BURST_MESSAGES

        replacement = _WebSocket()
        replaced, error, _code = await proxy._room_register_socket(
            room, member.user_id, replacement,
        )
        assert replaced is member and not error
        await proxy._room_handle_message(room, member, invalid)
        assert len(handled) == system_limits.ROOM_AUDIO_RATE_BURST_MESSAGES

        now[0] += 1_000
        for _ in range(system_limits.ROOM_AUDIO_RATE_MESSAGES_PER_SECOND + 5):
            await proxy._room_handle_message(room, member, invalid)
        assert len(handled) == (
            system_limits.ROOM_AUDIO_RATE_BURST_MESSAGES
            + system_limits.ROOM_AUDIO_RATE_MESSAGES_PER_SECOND
        )

        # Normal 50 fps audio remains below the configured sustained floor.
        fresh = proxy.RoomMember("fresh", _WebSocket())
        fresh.audio_message_updated_ms = now[0]
        for _ in range(50):
            await proxy._room_handle_message(room, fresh, invalid)
        assert len(handled) == (
            system_limits.ROOM_AUDIO_RATE_BURST_MESSAGES
            + system_limits.ROOM_AUDIO_RATE_MESSAGES_PER_SECOND + 50
        )

    asyncio.run(scenario())


def test_room_text_parser_rejects_oversize_before_json_decode(monkeypatch):
    assert proxy._parse_room_client_text('{"type":"chat"}') == {"type": "chat"}
    assert proxy._parse_room_client_text("[]") is None
    oversized = "x" * (system_limits.ROOM_WS_TEXT_MAX_BYTES + 1)
    unicode_oversized = "長" * (system_limits.ROOM_WS_TEXT_MAX_BYTES // 2)

    monkeypatch.setattr(
        proxy.json, "loads",
        lambda _value: (_ for _ in ()).throw(
            AssertionError("oversized frame reached json.loads"),
        ),
    )
    assert proxy._parse_room_client_text(oversized) is None
    assert proxy._parse_room_client_text(unicode_oversized) is None


@pytest.mark.parametrize(
    ("raw", "expected_code", "expected_reason"),
    [
        (
            {"type": "websocket.receive", "bytes": b"binary"},
            1009,
            "text JSON required",
        ),
        (
            {
                "type": "websocket.receive",
                "text": "x" * (system_limits.ROOM_WS_TEXT_MAX_BYTES + 1),
            },
            1009,
            "room message too large",
        ),
        (
            {"type": "websocket.receive", "text": "{not-json"},
            1007,
            "invalid room JSON",
        ),
    ],
)
def test_room_ws_closes_invalid_raw_frame_before_dispatch(
    monkeypatch, raw, expected_code, expected_reason,
):
    class _InboundWebSocket(_WebSocket):
        def __init__(self, message):
            super().__init__()
            self.cookies = {"committee_user": "signed"}
            self.message = message
            self.accepted = False
            self.close_calls = []
            self.receive_calls = 0

        async def accept(self):
            self.accepted = True

        async def receive(self):
            self.receive_calls += 1
            if self.receive_calls > 1:
                raise AssertionError("invalid frame did not terminate receive loop")
            return self.message

        async def close(self, **kwargs):
            self.close_calls.append(kwargs)
            self.closed = True

    async def scenario():
        room = _free_room("RAW01")
        proxy.ROOMS[room.code] = room
        client = _InboundWebSocket(raw)
        monkeypatch.setattr(proxy, "_verify_committee_token", lambda _value: "host")

        async def broadcast(*_args, **_kwargs):
            return None

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("invalid raw frame reached message dispatch")

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        monkeypatch.setattr(proxy, "_room_handle_message", forbidden)
        await proxy.room_ws(client, room.code)
        assert client.accepted is True
        assert client.receive_calls == 1
        assert client.close_calls[-1] == {
            "code": expected_code, "reason": expected_reason,
        }

    asyncio.run(scenario())


def test_control_message_bucket_bounds_fanout_and_survives_reconnect(monkeypatch):
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
                room, member, {"type": "chat", "text": f"message-{index}"},
            )
        assert len(broadcasts) == system_limits.ROOM_CONTROL_RATE_BURST_MESSAGES

        replacement = _WebSocket()
        replaced, error, _code = await proxy._room_register_socket(
            room, member.user_id, replacement,
        )
        assert replaced is member and not error
        await proxy._room_handle_message(
            room, member, {"type": "chat", "text": "reconnect-bypass"},
        )
        assert len(broadcasts) == system_limits.ROOM_CONTROL_RATE_BURST_MESSAGES

        now[0] += 1_000
        for index in range(system_limits.ROOM_CONTROL_RATE_MESSAGES_PER_SECOND + 3):
            await proxy._room_handle_message(
                room, member, {"type": "chat", "text": f"refill-{index}"},
            )
        assert len(broadcasts) == (
            system_limits.ROOM_CONTROL_RATE_BURST_MESSAGES
            + system_limits.ROOM_CONTROL_RATE_MESSAGES_PER_SECOND
        )

    asyncio.run(scenario())


def test_lobby_test_ack_requires_server_nonce_connected_source_and_is_bounded(monkeypatch):
    async def scenario():
        room = _free_room()
        host = room.members["host"]
        guest = room.members["guest"]
        third = proxy.RoomMember("third", _WebSocket())
        third.role = "正方"
        room.members[third.user_id] = third
        now = [max(m.joined_at for m in room.members.values()) + 10_000]
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        monkeypatch.setattr(proxy, "_checkpoint_room_bandwidth", lambda *_a: None)
        monkeypatch.setattr(proxy, "_bandwidth_live_gate_error", lambda: None)
        monkeypatch.setattr(proxy, "ROOM_TEST_AUDIO_PENDING_MAX", 3)
        monkeypatch.setattr(proxy, "ROOM_TEST_AUDIO_ACK_TTL_MS", 1_000_000)
        monkeypatch.setattr(proxy, "ROOM_TEST_AUDIO_COOLDOWN_MS", 0)
        tone = base64.b64encode(b"x" * 22_400).decode("ascii")

        await proxy._room_handle_message(room, host, {
            "type": "test_audio", "data": tone,
            "mimeType": "audio/pcm;rate=16000",
        })
        test_audio = broadcasts[-1]
        test_id = test_audio["test_id"]
        assert test_audio["from"] == "host" and len(test_id) == 16

        # A client cannot invent either the source or the server-issued nonce.
        await proxy._room_handle_message(room, guest, {
            "type": "test_received", "source": "not-a-member",
            "test_id": test_id,
        })
        await proxy._room_handle_message(room, guest, {
            "type": "test_received", "source": "host",
            "test_id": "forged",
        })
        assert not [x for x in broadcasts if x["type"] == "test_received"]
        await proxy._room_handle_message(room, guest, {
            "type": "test_received", "source": "host", "test_id": test_id,
        })
        assert [x for x in broadcasts if x["type"] == "test_received"] == [{
            "type": "test_received", "from": "guest", "source": "host",
        }]
        await proxy._room_handle_message(room, guest, {
            "type": "test_received", "source": "host", "test_id": test_id,
        })
        assert len([x for x in broadcasts if x["type"] == "test_received"]) == 1

        # A second real source is still cooldown-limited, then accepted later.
        await proxy._room_handle_message(room, third, {
            "type": "test_audio", "data": tone,
            "mimeType": "audio/pcm;rate=16000",
        })
        third_test_id = broadcasts[-1]["test_id"]
        await proxy._room_handle_message(room, guest, {
            "type": "test_received", "source": "third",
            "test_id": third_test_id,
        })
        assert len([x for x in broadcasts if x["type"] == "test_received"]) == 1
        now[0] += system_limits.ROOM_TEST_RECEIVED_COOLDOWN_MS
        await proxy._room_handle_message(room, guest, {
            "type": "test_received", "source": "third",
            "test_id": third_test_id,
        })
        assert len([x for x in broadcasts if x["type"] == "test_received"]) == 2

        for _ in range(6):
            now[0] += 1
            await proxy._room_handle_message(room, host, {
                "type": "test_audio", "data": tone,
                "mimeType": "audio/pcm;rate=16000",
            })
        assert len(guest.pending_test_audio) <= 3
        assert system_limits.ROOM_TEST_AUDIO_COOLDOWN_MS >= 5_000

        sent_before = len(host.ws.sent)
        for invalid in ({"large": "x"}, "1", float("nan"), -1, 10**17):
            await proxy._room_handle_message(room, host, {
                "type": "test_ping", "client_ts": invalid,
            })
        assert len(host.ws.sent) == sent_before
        await proxy._room_handle_message(room, host, {
            "type": "test_ping", "client_ts": 12345,
        })
        assert json.loads(host.ws.sent[-1])["client_ts"] == 12345

    asyncio.run(scenario())


def test_creation_lifecycle_enforces_ttl_and_bandwidth_outside_active_tick(monkeypatch):
    async def ttl_scenario():
        room = _free_room("TTL01")
        proxy.ROOMS[room.code] = room
        now = [room.created_at]
        ended = []

        async def advance(_seconds):
            now[0] = room.created_at + proxy.ROOM_MAX_AGE_MS

        async def end(target, reason):
            ended.append(reason)
            target.phase = "ended"

        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        monkeypatch.setattr(proxy.asyncio, "sleep", advance)
        monkeypatch.setattr(proxy, "_room_end", end)
        await proxy._room_lifecycle(room)
        assert ended == ["ttl"]

    asyncio.run(ttl_scenario())

    async def bandwidth_scenario():
        room = _free_room("BW001")
        proxy.ROOMS[room.code] = room
        checkpoints = []
        ended = []

        async def one_cycle(_seconds):
            return None

        async def end(target, reason):
            ended.append(reason)
            target.phase = "ended"

        monkeypatch.setattr(proxy.asyncio, "sleep", one_cycle)
        monkeypatch.setattr(
            proxy, "_checkpoint_room_bandwidth",
            lambda target, final=False: checkpoints.append((target.code, final)),
        )
        monkeypatch.setattr(proxy, "_bandwidth_live_gate_error", lambda: "stop")
        monkeypatch.setattr(proxy, "_room_end", end)
        await proxy._room_lifecycle(room)
        assert checkpoints == [(room.code, False)]
        assert ended == ["monthly_bandwidth_limit"]

    asyncio.run(bandwidth_scenario())


def test_final_judgement_timeout_retains_late_member_result(monkeypatch):
    async def scenario():
        now = [1_000]
        release = asyncio.Event()
        broadcasts = []
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.transcript = [{"side": "正方", "text": "論點"}]
        room.transcript_revision = 1
        proxy.ROOMS[room.code] = room

        monkeypatch.setattr(proxy, "_now_ms", lambda: now[0])
        monkeypatch.setattr(proxy, "ROOM_FINAL_JUDGEMENT_TIMEOUT_SECONDS", 0.001)

        async def close_gemini(_room):
            return None

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        async def delayed_judgement(target):
            await release.wait()
            target.judgement = "遲到但完整的評判"
            target.judgement_revision = target.transcript_revision

        monkeypatch.setattr(proxy, "_room_close_gemini", close_gemini)
        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        monkeypatch.setattr(proxy, "_room_request_judgement_unlocked", delayed_judgement)
        monkeypatch.setattr(proxy, "_record_room_bandwidth_once", lambda _room: None)

        await proxy._room_end(room, "host")
        assert room.phase == "ended"
        assert room.judgement_task is not None and not room.judgement_task.done()
        assert any(item["type"] == "judgement_pending" for item in broadcasts)
        first_expiry = room.ended_retain_until_ms

        now[0] = 5_000
        release.set()
        await room.judgement_task
        assert room.judgement == "遲到但完整的評判"
        assert room.ended_retain_until_ms > first_expiry

        monkeypatch.setattr(proxy, "_require_committee_user", lambda _request: "host")
        request = Request({
            "type": "http", "method": "GET",
            "path": f"/api/room/{room.code}/transcript", "headers": [],
        })
        response = await proxy.room_transcript(room.code, request)
        payload = json.loads(response.body)
        assert payload["phase"] == "ended"
        assert payload["judgement"] == "遲到但完整的評判"
        assert payload["judgement_pending"] is False
        assert payload["transcript_revision"] == payload["judgement_revision"] == 1
        page = await proxy.ai_coach_room_page(room.code, request)
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "loadTranscriptSnapshot" in page.body.decode("utf-8")

        monkeypatch.setattr(proxy, "_require_committee_user", lambda _request: "outsider")
        with pytest.raises(HTTPException) as denied:
            await proxy.room_transcript(room.code, request)
        assert denied.value.status_code == 403

        now[0] = room.ended_retain_until_ms - 1
        proxy._gc_rooms()
        assert proxy.ROOMS.get(room.code) is room
        now[0] = room.ended_retain_until_ms + 1
        proxy._gc_rooms()
        assert room.code not in proxy.ROOMS

    asyncio.run(scenario())


def test_stale_gemini_resume_cannot_cross_mock_session_epoch(monkeypatch):
    async def scenario():
        room = type("RoomState", (), {})()
        room.phase = "active"
        room.gemini_generation = 4
        room.gemini_session_index = 2
        room.gemini_connect_epoch = 9
        room.gemini_resume_handle = "handle"
        room.gemini_resume_attempts = 0

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("stale resume reached a new Mock section")

        monkeypatch.setattr(proxy, "_room_start_gemini_if_needed", forbidden)
        monkeypatch.setattr(proxy, "_room_broadcast", forbidden)
        monkeypatch.setattr(proxy, "_room_end", forbidden)
        await proxy._room_resume_gemini(
            room, 4, "old socket", session_index=1, connect_epoch=8,
        )

    asyncio.run(scenario())


def test_tick_unexpected_failure_safe_ends_instead_of_losing_deadline(monkeypatch):
    async def scenario():
        room = _free_room()
        room.phase = "active"
        room.activation_ready = True
        room.hard_deadline_ms = None
        room.last_bandwidth_checkpoint_ms = proxy._now_ms()
        proxy.ROOMS[room.code] = room
        ended = []

        async def no_wait(_seconds):
            return None

        async def broken_broadcast(*_args, **_kwargs):
            raise RuntimeError("broadcast failed")

        async def end(target, reason):
            ended.append(reason)
            target.phase = "ended"

        monkeypatch.setattr(proxy.asyncio, "sleep", no_wait)
        monkeypatch.setattr(proxy, "_room_broadcast", broken_broadcast)
        monkeypatch.setattr(proxy, "_room_end", end)
        await proxy._room_tick(room)
        assert ended == ["server_timer_failure"]
        assert room.phase == "ended"

    asyncio.run(scenario())


def test_training_and_coach_share_one_rag_embedding_semaphore():
    assert ai_training_api.RAG_EMBED_SEMAPHORE is rag.RAG_EMBED_SEMAPHORE
    assert system_limits.RAG_EMBED_CONCURRENCY == 3


def test_disabling_account_reads_and_cleans_exemption_under_lock(monkeypatch):
    events = []
    written = {}

    class _Frame:
        empty = False

    class _Connection:
        def execute(self, statement, _params=None):
            events.append(str(statement))

    class _Db:
        def query(self, *_args, **_kwargs):
            return _Frame()

        @contextmanager
        def transaction(self):
            yield _Connection()

    def configs(_conn, _keys):
        assert any("pg_advisory_xact_lock" in event for event in events)
        return {
            "login_disabled_accounts": [],
            "tts_recording_reviewers": ["alice", "backup"],
            "lateness_fund_managers": ["alice", "backup"],
            "tts_recording_allowed_users": ["alice"],
            "ai_fund_treasurers": ["alice"],
            "bypass_active_check_until": {"alice": "future"},
            "solo_quota_exemptions": {
                "alice": {"mode": "all", "expires_at": "2099-01-01T00:00:00Z"},
            },
        }

    def set_configs(_conn, values):
        written.update(values)

    monkeypatch.setattr(admin_console_api, "_require", lambda *_args: None)
    monkeypatch.setattr(admin_console_api, "_db", lambda: _Db())
    monkeypatch.setattr(admin_console_api, "get_configs_from_connection", configs)
    monkeypatch.setattr(admin_console_api, "set_configs_on_connection", set_configs)
    request = Request({"type": "http", "method": "PATCH", "path": "/", "headers": []})
    result = admin_console_api.set_account_access(
        "alice", admin_console_api.AccountAccessBody(disabled=True), request,
    )
    assert result == {"ok": True, "disabled": True}
    assert written["login_disabled_accounts"] == ["alice"]
    assert "alice" not in written["solo_quota_exemptions"]
    assert written["tts_recording_reviewers"] == ["backup"]


def test_mock_jit_token_rate_floor_and_room_result_ui_contract():
    specs = system_limits.effective_limits()
    assert specs["PRACTICE_LIVE_MAX_PER_HOUR"]["minimum"] >= 8
    assert system_limits.ROOM_TEST_AUDIO_MAX_BYTES <= system_limits.ROOM_AUDIO_FRAME_MAX_BYTES
    html = (proxy.BASE_DIR / "templates" / "room_debate.html").read_text("utf-8")
    assert "loadTranscriptSnapshot" in html
    assert 'data.phase === "ended"' in html
