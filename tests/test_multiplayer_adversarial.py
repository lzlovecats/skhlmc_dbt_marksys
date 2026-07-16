"""Adversarial regressions for final STUN-only Mode A room behaviour."""

import asyncio
import json
import re

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


def test_room_is_exactly_two_people_and_preserves_free_debate_hard_stop(monkeypatch):
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
        assert room.hard_deadline_ms - room.started_ms == 10 * 60 * 1000

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
        assert room.transcript[0]["text"] == "我方主張應予支持"
        assert room.transcript_revision == 1

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
        host = room.members["host"]
        await proxy._room_handle_turn(room, host, True)
        await proxy._room_advance_segment(room, 0)
        assert room.phase == "active"
        assert room.active_turn_user is None
        assert room.judge_enabled is True
        assert room.transcript == []

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
            room, room.members["host"], {"type": "rtc_status", "status": "disconnected"},
        )
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
        room.started_ms = room.seg_started_ms = proxy._now_ms() - 1_000
        room.hard_deadline_ms = proxy._now_ms() + 60_000
        broadcasts = []

        async def broadcast(_room, payload, **_kwargs):
            broadcasts.append(payload)

        monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
        host, guest = room.members["host"], room.members["guest"]
        await proxy._room_handle_message(
            room, host, {"type": "rtc_status", "status": "disconnected"},
        )
        assert room.rtc_pause_started_ms is not None
        assert any(item.get("status") == "restart" for item in broadcasts)

        guest.rtc_status = "disconnected"
        await proxy._room_handle_message(
            room, host, {"type": "rtc_status", "status": "connected"},
        )
        assert room.rtc_pause_started_ms is not None
        await proxy._room_handle_message(
            room, guest, {"type": "rtc_status", "status": "connected"},
        )
        assert room.rtc_pause_started_ms is None
        await asyncio.sleep(0)

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


def test_room_frontend_is_stun_only_and_has_no_render_media_fallback():
    html = (proxy.BASE_DIR / "templates" / "room_debate.html").read_text("utf-8")
    js = (proxy.BASE_DIR / "frontend/shared/room-debate-p2p.js").read_text("utf-8")
    assert "stun.cloudflare.com:3478" in js
    assert "turn:" not in js.lower() and "sfu" not in js.lower()
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


def test_training_and_coach_share_one_rag_embedding_semaphore():
    assert ai_training_api.RAG_EMBED_SEMAPHORE is rag.RAG_EMBED_SEMAPHORE
    assert system_limits.RAG_EMBED_CONCURRENCY == 3
