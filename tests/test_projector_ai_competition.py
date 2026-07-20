import datetime as dt
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import pandas as pd
from fastapi import HTTPException

from api import kiosk_api, projector_ai_api
from deploy import proxy


ROOT = Path(__file__).resolve().parents[1]


def test_free_debate_is_one_both_marker_and_prompt_infers_each_turn():
    match = {
        "match_id": "M1",
        "debate_format": "聯中",
        "free_debate_minutes": 5,
    }
    sequence = projector_ai_api._match_sequence(match)
    free_index = next(i for i, segment in enumerate(sequence) if segment["id"] == "free")
    side, label, index = projector_ai_api._marker_for_segment(match, free_index)
    assert (side, index) == ("both", free_index)
    assert "自由辯論" in label

    transcript_system, transcript_user = kiosk_api._transcript_prompts(
        {
            **match,
            "topic": "測試辯題",
            "pro_team": "甲隊",
            "con_team": "乙隊",
        },
        600,
        [{"offset_seconds": 0, "side": "both", "segment": label}],
    )
    assert "自由辯論只會標成「雙方」" in transcript_system
    assert "匿名講者站方錨點" in transcript_user
    assert "不可硬估" in transcript_system


def test_review_output_separates_private_detail_from_bounded_public_summary():
    raw = """PROJECTOR_SUMMARY_START
AI輔助第二意見，正式賽果以評判團為準。建議勝方：正方；信心：中。理由一、二、三。限制：部分疊聲。
PROJECTOR_SUMMARY_END
FULL_REVIEW_START
1. 聲明\n2. 建議勝方：正方\n3. 完整評語
FULL_REVIEW_END"""
    full, summary = kiosk_api._bounded_review_output(raw)
    assert full.startswith("1. 聲明")
    assert "完整評語" in full
    assert "建議勝方：正方" in summary
    assert len(summary) <= 1200
    assert "FULL_REVIEW" not in summary


def test_encrypted_result_round_trip_and_wrong_secret_fails_closed(monkeypatch):
    monkeypatch.setattr(proxy, "_get_relay_cookie_secret", lambda: "secret-one")
    ciphertext = projector_ai_api._seal_json(
        {"projector_summary": "只屬第二意見", "transcript": "私人逐字稿"}
    )
    assert "私人逐字稿".encode("utf-8") not in ciphertext
    assert projector_ai_api._open_json(ciphertext)["transcript"] == "私人逐字稿"

    monkeypatch.setattr(proxy, "_get_relay_cookie_secret", lambda: "secret-two")
    try:
        projector_ai_api._open_json(ciphertext)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 503
    else:  # pragma: no cover - authenticated encryption must reject this
        raise AssertionError("ciphertext unexpectedly decrypted with another key")


def test_projector_segment_change_persists_server_time_marker():
    now = dt.datetime(2026, 7, 14, 12, 0, 10)
    session_row = SimpleNamespace(
        _mapping={
            "session_id": "session-1",
            "recording_started_at": now - dt.timedelta(seconds=10),
        }
    )
    calls = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Conn:
        def execute(self, statement, params):
            sql = " ".join(str(statement).split())
            calls.append((sql, params))
            if sql.startswith("SELECT session_id"):
                return Result(session_row)
            return Result(None)

    match = {
        "match_id": "M1",
        "debate_format": "校園隨想",
        "free_debate_minutes": None,
    }
    assert projector_ai_api.record_projector_segment_change(
        Conn(), display="main", match=match, seg_index=0, now=now, force=True
    )
    insert = next(item for item in calls if item[0].startswith("INSERT INTO projector_ai_markers"))
    assert insert[1]["side"] in {"pro", "con"}
    assert insert[1]["offset"] == 10.0


def test_stale_operator_session_is_rejected_while_control_row_is_locked():
    statements = []

    class Result:
        def fetchone(self):
            return SimpleNamespace(_mapping={"current_session_id": "new-session"})

    class Conn:
        def execute(self, statement, params):
            statements.append((" ".join(str(statement).split()), params))
            return Result()

    with pytest.raises(HTTPException) as raised:
        projector_ai_api._lock_current_session(
            Conn(), "main", "stale-session"
        )
    assert raised.value.status_code == 409
    assert "目前投影場次" in str(raised.value.detail)
    assert "FOR UPDATE" in statements[0][0]


def test_projector_public_surface_never_contains_transcript_contract():
    source = (ROOT / "api" / "projector_ai_api.py").read_text(encoding="utf-8")
    public_block = source.split('@router.get("/api/projector/ai/public")', 1)[1].split(
        '@router.get("/api/kiosk/projector-ai/command")', 1
    )[0]
    assert '"projector_summary"' in public_block
    assert '"transcript"' not in public_block
    migration = (
        ROOT / "migrations" / "20260714_0007_projector_ai_sessions.up.sql"
    ).read_text(encoding="utf-8")
    assert "result_ciphertext" in migration
    assert "result_expires_at" in migration
    assert "projector_ai_markers" in migration


def test_projector_public_poll_fails_each_feed_closed_independently():
    display = (ROOT / "templates" / "projector_display.html").read_text(
        encoding="utf-8"
    )
    poll_block = display.split("async function pollPublic()", 1)[1].split(
        "function showLogin", 1
    )[0]

    assert "projectorStateFailures" in poll_block
    assert "publicAiFailures" in poll_block
    assert "projectorState = null" in poll_block
    assert "publicAiState = null" in poll_block
    assert "renderConnectionStatus()" in poll_block
    assert "results.some" not in poll_block


def test_no_tts_provider_means_text_only_and_no_synthesis(monkeypatch):
    monkeypatch.setattr(proxy, "tts_provider_configured", lambda: False)

    async def unexpected(*_args, **_kwargs):  # pragma: no cover - safety guard
        raise AssertionError("TTS synthesis must not run without a provider")

    monkeypatch.setattr(proxy, "synthesize_tts_accounted", unexpected)
    audio, mime, status, detail = asyncio.run(
        projector_ai_api._synthesize_projector_summary("粵語摘要", "session-1")
    )
    assert audio is None and mime == ""
    assert status == "unavailable"
    assert "只投影文字" in detail


def test_bandwidth_gate_degrades_tts_to_text_without_provider_spend(monkeypatch):
    monkeypatch.setattr(proxy, "tts_provider_configured", lambda: True)
    monkeypatch.setattr(proxy, "_bandwidth_live_gate_error", lambda: "blocked")

    async def unexpected(*_args, **_kwargs):  # pragma: no cover - safety guard
        raise AssertionError("bandwidth-blocked narration must not call the provider")

    monkeypatch.setattr(proxy, "synthesize_tts_accounted", unexpected)
    audio, mime, status, detail = asyncio.run(
        projector_ai_api._synthesize_projector_summary("粵語摘要", "session-1")
    )
    assert audio is None and mime == ""
    assert status == "unavailable"
    assert "只投影文字" in detail


def test_projector_result_is_server_authoritative_without_client_fallback():
    source = (ROOT / "api" / "projector_ai_api.py").read_text(encoding="utf-8")
    display = (ROOT / "templates" / "projector_display.html").read_text(
        encoding="utf-8"
    )
    kiosk_source = (ROOT / "api" / "kiosk_api.py").read_text(encoding="utf-8")

    assert "class KioskResultBody" not in source
    assert '/api/kiosk/projector-ai/result' not in source
    assert '/api/kiosk/projector-ai/result' not in display
    assert "postResult(" not in display
    assert "projector_persisted" in display
    assert "persist_completed_review_for_projector" in kiosk_source
    assert "authenticated browser still receives" not in kiosk_source


def test_heartbeat_preserves_recording_or_processing_status(monkeypatch):
    statements = []

    class Conn:
        def execute(self, statement, params):
            statements.append((" ".join(str(statement).split()), params))
            return SimpleNamespace(fetchone=lambda: None)

    class DB:
        @contextmanager
        def transaction(self):
            yield Conn()

    monkeypatch.setattr(projector_ai_api, "_require_kiosk", lambda _request: "kiosk")
    monkeypatch.setattr(projector_ai_api, "_db", lambda: DB())
    monkeypatch.setattr(
        projector_ai_api,
        "_locked_lease_control",
        lambda _conn, _display: SimpleNamespace(_mapping={}),
    )
    monkeypatch.setattr(
        projector_ai_api,
        "_assert_lease_mapping",
        lambda *_args, **_kwargs: {
            "device_id": "device-a",
            "lease_generation": 2,
        },
    )
    projector_ai_api.kiosk_heartbeat(
        projector_ai_api.HeartbeatBody(display="main", capabilities={"recording": True}),
        SimpleNamespace(cookies={}),
    )
    update_sql = next(sql for sql, _params in statements if sql.startswith("UPDATE"))
    assert "CASE WHEN COALESCE(kiosk_status,'') IN ('','offline')" in update_sql
    assert "kiosk_status='online'" not in update_sql


def test_competition_templates_use_separate_staff_control_and_kiosk_engine():
    control = (ROOT / "templates" / "projector_control.html").read_text(
        encoding="utf-8"
    )
    display = (ROOT / "templates" / "projector_display.html").read_text(
        encoding="utf-8"
    )
    assert "/api/projector/ai/status" in control
    assert "recording_notice_confirmed" in control
    assert "自由辯論正方" not in control and "自由辯論反方" not in control
    assert 'params.get("kiosk") === "1"' in display
    assert "/api/kiosk/projector-ai/command" in display
    assert "/api/kiosk/match-review/analyze" in display
    assert "operation_id" in display
    assert "/api/kiosk/match-review/preflight" in display
    assert "/api/tts/synthesize" in display
    assert "/api/tts/azure" not in display
    assert 'command.ack_revision < command.revision' in display
    assert "reload_recovery" in display
    assert "/api/projector/ai/cancel-start" in control
    assert 'case "cancel_start"' in display
    assert 'id="kioskPrimeButton" type="button" style="display: none"' in display
    assert "投影文字並準備語音" in control
    assert "讀出／重新讀出" in control
    assert "投影並讀出" not in control


def test_projector_control_polling_never_scrolls_the_whole_operator_page():
    control = (ROOT / "templates" / "projector_control.html").read_text(
        encoding="utf-8"
    )
    assert ".scrollIntoView(" not in control
    assert "var previousScrollTop = box.scrollTop" in control
    assert "box.scrollTop = previousScrollTop" in control
    assert "revealCurrentSegment(box, box.querySelector" in control
    assert "state.seg_index !== previousSegment" in control


def test_projector_public_match_binding_and_backend_only_schema_contract():
    source = (ROOT / "api" / "projector_ai_api.py").read_text(encoding="utf-8")
    public_block = source.split('@router.get("/api/projector/ai/public")', 1)[1].split(
        '@router.get("/api/kiosk/projector-ai/command")', 1
    )[0]
    assert '"match_id"' in public_block and '"match"' in public_block
    assert '"transcript"' not in public_block and '"markdown"' not in public_block

    migration = (
        ROOT / "migrations" / "20260714_0007_projector_ai_sessions.up.sql"
    ).read_text(encoding="utf-8")
    schema = (ROOT / "schema.py").read_text(encoding="utf-8")
    assert "idx_projector_ai_sessions_one_active_display" in migration
    assert "LOCK_PROJECTOR_AI_PRIVILEGES" in schema
    assert "tts_claim_token" in migration and "'generating'" in migration

    kiosk_source = (ROOT / "api" / "kiosk_api.py").read_text(encoding="utf-8")
    assert "persist_completed_review_for_projector" in kiosk_source
    assert '"projector_persisted"' in kiosk_source
    assert '"projector_revision"' in kiosk_source


def test_projector_start_expiry_and_cancelled_state_are_versioned_contracts():
    source = (ROOT / "api" / "projector_ai_api.py").read_text(encoding="utf-8")
    schema = (ROOT / "schema.py").read_text(encoding="utf-8")
    migration = (
        ROOT / "migrations" / "20260717_0001_projector_ai_cancelled_sessions.up.sql"
    ).read_text(encoding="utf-8")
    down = (
        ROOT / "migrations" / "20260717_0001_projector_ai_cancelled_sessions.down.sql"
    ).read_text(encoding="utf-8")

    assert "PROJECTOR_START_COMMAND_TTL_SECONDS" in source
    assert "cancel_start" in source
    assert "Kiosk 尚未確認上一個控制指令" in source
    assert "ack_revision" in source
    assert "'cancelled'" in schema
    assert "'cancelled'" in migration
    assert "status='error'" in down


def test_kiosk_lease_claim_decision_pins_an_active_session_to_its_owner():
    now = dt.datetime(2026, 7, 17, 12, 0, 0)

    assert projector_ai_api._lease_can_claim(
        owner_device_id="",
        owner_client_id="",
        requesting_device_id="device-a",
        requesting_client_id="tab-a",
        lease_expires_at=None,
        session_status="",
        now=now,
    )
    assert not projector_ai_api._lease_can_claim(
        owner_device_id="device-a",
        owner_client_id="tab-a",
        requesting_device_id="device-b",
        requesting_client_id="tab-b",
        lease_expires_at=now - dt.timedelta(seconds=1),
        session_status="recording",
        now=now,
    )
    assert projector_ai_api._lease_can_claim(
        owner_device_id="device-a",
        owner_client_id="tab-a",
        requesting_device_id="device-b",
        requesting_client_id="tab-b",
        lease_expires_at=now - dt.timedelta(seconds=1),
        session_status="ready",
        now=now,
    )
    assert projector_ai_api._lease_can_claim(
        owner_device_id="device-a",
        owner_client_id="tab-a",
        requesting_device_id="device-a",
        requesting_client_id="tab-a",
        lease_expires_at=now - dt.timedelta(seconds=1),
        session_status="processing",
        now=now,
    )


def test_stale_lease_generation_and_command_generation_fail_closed(monkeypatch):
    now = dt.datetime(2026, 7, 17, 12, 0, 0)
    token = "lease-token"
    request = SimpleNamespace(
        headers={
            projector_ai_api.LEASE_TOKEN_HEADER: token,
            projector_ai_api.LEASE_CLIENT_HEADER: "client-abcdefghijklmnop",
            projector_ai_api.LEASE_GENERATION_HEADER: "4",
        },
        cookies={projector_ai_api.DEVICE_COOKIE_NAME: "signed-device"},
    )
    monkeypatch.setattr(
        projector_ai_api,
        "_verify_device_cookie",
        lambda _token: {"device_id": "a" * 32, "generation": 1},
    )
    mapping = {
        "lease_device_id": "a" * 32,
        "lease_client_id": "client-abcdefghijklmnop",
        "lease_token_hash": projector_ai_api._lease_token_hash(token),
        "lease_generation": 4,
        "lease_expires_at": now + dt.timedelta(seconds=15),
        "lease_device_enabled": True,
        "lease_device_revoked_at": None,
        "lease_device_credential_generation": 1,
        "command_lease_generation": 4,
    }
    assert projector_ai_api._assert_lease_mapping(
        mapping, request, now=now, require_command_generation=True
    )["lease_generation"] == 4

    stale_request = SimpleNamespace(
        headers={**request.headers, projector_ai_api.LEASE_GENERATION_HEADER: "3"},
        cookies=request.cookies,
    )
    with pytest.raises(HTTPException) as stale:
        projector_ai_api._assert_lease_mapping(mapping, stale_request, now=now)
    assert stale.value.status_code == 423

    with pytest.raises(HTTPException) as wrong_command:
        projector_ai_api._assert_lease_mapping(
            {**mapping, "command_lease_generation": 3},
            request,
            now=now,
            require_command_generation=True,
        )
    assert wrong_command.value.status_code == 409


def test_first_kiosk_claim_is_active_and_second_same_display_is_standby(monkeypatch):
    now = dt.datetime(2026, 7, 17, 12, 0, 0)
    state = {
        "lease_device_id": "",
        "lease_client_id": "",
        "lease_generation": 0,
        "lease_expires_at": None,
        "current_session_status": "",
        "lease_device_label": "",
    }

    class Conn:
        def execute(self, statement, params):
            sql = " ".join(str(statement).split())
            if sql.startswith("UPDATE projector_ai_controls"):
                state.update(
                    lease_device_id=params["device"],
                    lease_client_id=params["client"],
                    lease_generation=params["generation"],
                    lease_expires_at=params["expires"],
                    lease_device_label="Kiosk A",
                )
            return SimpleNamespace(fetchone=lambda: None)

    class DB:
        @contextmanager
        def transaction(self):
            yield Conn()

    monkeypatch.setattr(projector_ai_api, "_require_kiosk", lambda _request: "kiosk")
    monkeypatch.setattr(projector_ai_api, "_db", lambda: DB())
    monkeypatch.setattr(projector_ai_api, "_now", lambda: now)
    monkeypatch.setattr(projector_ai_api, "_ensure_control", lambda *_args: None)
    monkeypatch.setattr(
        projector_ai_api,
        "_locked_lease_control",
        lambda _conn, _display: SimpleNamespace(_mapping=state.copy()),
    )
    monkeypatch.setattr(
        projector_ai_api,
        "_device_for_claim",
        lambda _conn, request, _response, _now: {
            "device_id": request.device_id,
            "label": request.device_label,
        },
    )
    first = projector_ai_api.claim_kiosk_lease(
        projector_ai_api.LeaseClaimBody(
            display="main",
            client_id="client-a-abcdefghijkl",
        ),
        SimpleNamespace(device_id="a" * 32, device_label="Kiosk A", headers={}, cookies={}),
        projector_ai_api.Response(),
    )
    second = projector_ai_api.claim_kiosk_lease(
        projector_ai_api.LeaseClaimBody(
            display="main",
            client_id="client-b-abcdefghijkl",
        ),
        SimpleNamespace(device_id="b" * 32, device_label="Kiosk B", headers={}, cookies={}),
        projector_ai_api.Response(),
    )
    assert first["role"] == "active"
    assert first["lease_generation"] == 1
    assert second["role"] == "standby"
    assert state["lease_device_id"] == "a" * 32


def test_same_kiosk_tab_reload_rotates_lease_only_without_an_active_session(
    monkeypatch,
):
    now = dt.datetime(2026, 7, 17, 12, 0, 0)
    client_id = "client-a-abcdefghijkl"
    state = {
        "lease_device_id": "a" * 32,
        "lease_client_id": client_id,
        "lease_token_hash": projector_ai_api._lease_token_hash("old-token"),
        "lease_generation": 4,
        "lease_expires_at": now + dt.timedelta(seconds=15),
        "current_session_status": "ready",
        "lease_device_label": "Kiosk A",
    }

    class Conn:
        def execute(self, statement, params):
            sql = " ".join(str(statement).split())
            if sql.startswith("UPDATE projector_ai_controls"):
                state.update(
                    lease_device_id=params["device"],
                    lease_client_id=params["client"],
                    lease_token_hash=params["token_hash"],
                    lease_generation=params["generation"],
                    lease_expires_at=params["expires"],
                )
            return SimpleNamespace(fetchone=lambda: None)

    class DB:
        @contextmanager
        def transaction(self):
            yield Conn()

    monkeypatch.setattr(projector_ai_api, "_require_kiosk", lambda _request: "kiosk")
    monkeypatch.setattr(projector_ai_api, "_db", lambda: DB())
    monkeypatch.setattr(projector_ai_api, "_now", lambda: now)
    monkeypatch.setattr(projector_ai_api, "_ensure_control", lambda *_args: None)
    monkeypatch.setattr(
        projector_ai_api,
        "_locked_lease_control",
        lambda _conn, _display: SimpleNamespace(_mapping=state.copy()),
    )
    monkeypatch.setattr(
        projector_ai_api,
        "_device_for_claim",
        lambda _conn, _request, _response, _now: {
            "device_id": "a" * 32,
            "label": "Kiosk A",
        },
    )
    request = SimpleNamespace(headers={}, cookies={})

    reclaimed = projector_ai_api.claim_kiosk_lease(
        projector_ai_api.LeaseClaimBody(display="main", client_id=client_id),
        request,
        projector_ai_api.Response(),
    )

    assert reclaimed["role"] == "active"
    assert reclaimed["lease_generation"] == 5
    assert reclaimed["lease_token"]
    assert state["lease_token_hash"] == projector_ai_api._lease_token_hash(
        reclaimed["lease_token"]
    )

    with pytest.raises(HTTPException) as malformed:
        projector_ai_api.claim_kiosk_lease(
            projector_ai_api.LeaseClaimBody(display="main", client_id=client_id),
            SimpleNamespace(
                headers={projector_ai_api.LEASE_GENERATION_HEADER: "5"},
                cookies={},
            ),
            projector_ai_api.Response(),
        )
    assert malformed.value.status_code == 423
    assert state["lease_generation"] == 5

    state["current_session_status"] = "recording"
    pinned = projector_ai_api.claim_kiosk_lease(
        projector_ai_api.LeaseClaimBody(display="main", client_id=client_id),
        request,
        projector_ai_api.Response(),
    )

    assert pinned["role"] == "standby"
    assert pinned["reason"] == "active_session_pinned"
    assert state["lease_generation"] == 5


def test_staff_takeover_cancels_processing_or_interrupts_recording_only_when_confirmed(
    monkeypatch,
):
    statements = []

    class Conn:
        def execute(self, statement, params):
            statements.append((" ".join(str(statement).split()), params))
            return SimpleNamespace(fetchone=lambda: None)

    class DB:
        @contextmanager
        def transaction(self):
            yield Conn()

    monkeypatch.setattr(
        projector_ai_api, "require_competition_staff", lambda _request: "staff"
    )
    monkeypatch.setattr(projector_ai_api, "_db", lambda: DB())

    def lease_row(status):
        return SimpleNamespace(
            _mapping={
                "lease_device_id": "a" * 32,
                "lease_generation": 7,
                "current_session_id": "session-1",
                "current_session_status": status,
            }
        )

    monkeypatch.setattr(
        projector_ai_api,
        "_locked_lease_control",
        lambda _conn, _display: lease_row("processing"),
    )
    with pytest.raises(HTTPException) as processing_unconfirmed:
        projector_ai_api.takeover_kiosk_lease(
            projector_ai_api.LeaseTakeoverBody(
                display="main",
                expected_generation=7,
                confirm_interrupt_active_session=False,
            ),
            SimpleNamespace(cookies={}),
        )
    assert processing_unconfirmed.value.status_code == 409

    processing_response = projector_ai_api.takeover_kiosk_lease(
        projector_ai_api.LeaseTakeoverBody(
            display="main",
            expected_generation=7,
            confirm_interrupt_active_session=True,
        ),
        SimpleNamespace(cookies={}),
    )
    assert processing_response["generation"] == 8
    assert any("SET status='cancelled'" in sql for sql, _params in statements)
    assert any("其後完成的 AI 結果不會寫入" in sql for sql, _params in statements)

    statements.clear()

    monkeypatch.setattr(
        projector_ai_api,
        "_locked_lease_control",
        lambda _conn, _display: lease_row("recording"),
    )
    with pytest.raises(HTTPException) as unconfirmed:
        projector_ai_api.takeover_kiosk_lease(
            projector_ai_api.LeaseTakeoverBody(
                display="main",
                expected_generation=7,
                confirm_interrupt_active_session=False,
            ),
            SimpleNamespace(cookies={}),
        )
    assert unconfirmed.value.status_code == 409

    response = projector_ai_api.takeover_kiosk_lease(
        projector_ai_api.LeaseTakeoverBody(
            display="main",
            expected_generation=7,
            confirm_interrupt_active_session=True,
        ),
        SimpleNamespace(cookies={}),
    )
    assert response["generation"] == 8
    assert any("SET status='interrupted'" in sql for sql, _params in statements)
    assert any("lease_token_hash=NULL" in sql for sql, _params in statements)


def test_expired_owner_command_poll_atomically_renews_its_lease(monkeypatch):
    now = dt.datetime(2026, 7, 17, 12, 0, 0)
    token = "lease-token"
    updates = []
    mapping = {
        "lease_device_id": "a" * 32,
        "lease_client_id": "client-abcdefghijklmnop",
        "lease_token_hash": projector_ai_api._lease_token_hash(token),
        "lease_generation": 4,
        "lease_expires_at": now - dt.timedelta(seconds=1),
        "lease_device_enabled": True,
        "lease_device_revoked_at": None,
        "lease_device_credential_generation": 1,
        "command_lease_generation": 4,
    }

    class Conn:
        def execute(self, statement, params):
            updates.append((" ".join(str(statement).split()), dict(params)))
            return SimpleNamespace(fetchone=lambda: None)

    class DB:
        @contextmanager
        def transaction(self):
            yield Conn()

    request = SimpleNamespace(
        headers={
            projector_ai_api.LEASE_TOKEN_HEADER: token,
            projector_ai_api.LEASE_CLIENT_HEADER: "client-abcdefghijklmnop",
            projector_ai_api.LEASE_GENERATION_HEADER: "4",
        },
        cookies={projector_ai_api.DEVICE_COOKIE_NAME: "signed-device"},
    )
    monkeypatch.setattr(projector_ai_api, "_require_kiosk", lambda _request: "kiosk")
    monkeypatch.setattr(projector_ai_api, "_db", lambda: DB())
    monkeypatch.setattr(projector_ai_api, "_now", lambda: now)
    monkeypatch.setattr(projector_ai_api, "_ensure_control", lambda *_args: None)
    monkeypatch.setattr(
        projector_ai_api,
        "_locked_lease_control",
        lambda _conn, _display: SimpleNamespace(_mapping=mapping),
    )
    monkeypatch.setattr(
        projector_ai_api,
        "_verify_device_cookie",
        lambda _token: {"device_id": "a" * 32, "generation": 1},
    )
    monkeypatch.setattr(
        projector_ai_api,
        "_control_status",
        lambda _db, _display, include_private=False: {"control": {}, "session": None},
    )
    monkeypatch.setattr(proxy, "tts_provider_configured", lambda: False)

    response = projector_ai_api.kiosk_command(request, display="main")

    assert response.status_code == 200
    renewal = next(
        (sql, params)
        for sql, params in updates
        if sql.startswith("UPDATE projector_ai_controls")
    )
    assert "lease_expires_at=:expires" in renewal[0]
    assert renewal[1]["expires"] == now + dt.timedelta(
        seconds=projector_ai_api.PROJECTOR_KIOSK_LEASE_TTL_SECONDS
    )

    mapping["lease_device_revoked_at"] = now
    with pytest.raises(HTTPException) as revoked:
        projector_ai_api.kiosk_command(request, display="main")
    assert revoked.value.status_code == 423


def test_recording_ack_loads_match_timing_on_the_existing_transaction(monkeypatch):
    now = dt.datetime(2026, 7, 17, 12, 0, 0)
    statements = []
    captured = {}

    class Conn:
        def execute(self, statement, params):
            sql = " ".join(str(statement).split())
            statements.append(sql)
            if sql.startswith("SELECT status FROM projector_ai_sessions"):
                row = SimpleNamespace(_mapping={"status": "start_requested"})
                return SimpleNamespace(fetchone=lambda: row)
            if sql.startswith("UPDATE projector_ai_sessions"):
                row = SimpleNamespace(_mapping={"session_id": "session-1"})
                return SimpleNamespace(fetchone=lambda: row)
            if sql.startswith("SELECT match_id,seg_index FROM projector_state"):
                row = SimpleNamespace(_mapping={"match_id": "M1", "seg_index": 0})
                return SimpleNamespace(fetchone=lambda: row)
            if "FROM matches" in sql:
                row = SimpleNamespace(
                    _mapping={
                        "match_id": "M1",
                        "debate_format": "聯中",
                        "free_debate_minutes": 5,
                    }
                )
                return SimpleNamespace(fetchone=lambda: row)
            return SimpleNamespace(fetchone=lambda: None)

    class DB:
        def query(self, _sql, _params):
            raise AssertionError("kiosk_ack must not checkout a second DB connection")

        @contextmanager
        def transaction(self):
            yield Conn()

    control = SimpleNamespace(
        _mapping={
            "command_revision": 3,
            "ack_revision": 2,
            "command": "start",
            "current_session_id": "session-1",
            "kiosk_status": "starting",
        }
    )
    monkeypatch.setattr(projector_ai_api, "_require_kiosk", lambda _request: "kiosk")
    monkeypatch.setattr(projector_ai_api, "_db", lambda: DB())
    monkeypatch.setattr(projector_ai_api, "_now", lambda: now)
    monkeypatch.setattr(projector_ai_api, "_ensure_control", lambda *_args: None)
    monkeypatch.setattr(projector_ai_api, "_locked_lease_control", lambda *_args: control)
    monkeypatch.setattr(
        projector_ai_api,
        "_assert_lease_mapping",
        lambda *_args, **_kwargs: {
            "device_id": "device-a",
            "lease_generation": 4,
        },
    )
    monkeypatch.setattr(
        projector_ai_api,
        "record_projector_segment_change",
        lambda _conn, **kwargs: captured.update(kwargs) or True,
    )

    result = projector_ai_api.kiosk_ack(
        projector_ai_api.KioskAckBody(
            display="main",
            client_id="client-abcdefghijklmnop",
            lease_generation=4,
            revision=3,
            session_id="session-1",
            state="recording",
            detail="正式錄音中",
        ),
        SimpleNamespace(headers={}, cookies={}),
    )

    assert result["ack_revision"] == 3
    assert any("FROM matches" in sql for sql in statements)
    assert captured["match"]["match_id"] == "M1"


def test_projector_kiosk_lease_is_a_versioned_backend_only_contract():
    migration = (
        ROOT / "migrations" / "20260717_0002_projector_kiosk_lease.up.sql"
    ).read_text(encoding="utf-8")
    down = (
        ROOT / "migrations" / "20260717_0002_projector_kiosk_lease.down.sql"
    ).read_text(encoding="utf-8")
    schema = (ROOT / "schema.py").read_text(encoding="utf-8")
    limits = (ROOT / "system_limits.py").read_text(encoding="utf-8")

    assert "CREATE TABLE public.projector_kiosk_devices" in migration
    assert "lease_token_hash" in migration
    assert "lease_generation" in migration
    assert "command_lease_generation" in migration
    assert "kiosk_device_id" in migration
    assert "kiosk_lease_generation" in migration
    assert "'interrupted'" in migration
    assert "REVOKE ALL PRIVILEGES" in migration
    assert "projector_kiosk_devices" in schema
    assert "'interrupted'" in schema
    assert "PROJECTOR_KIOSK_LEASE_TTL_SECONDS" in limits
    assert "status='error'" in down


def test_every_projector_kiosk_side_effect_is_bound_to_the_owner_lease():
    source = (ROOT / "api" / "projector_ai_api.py").read_text(encoding="utf-8")
    kiosk_source = (ROOT / "api" / "kiosk_api.py").read_text(encoding="utf-8")

    assert '@router.post("/api/kiosk/projector-ai/lease/claim")' in source
    assert '@router.post("/api/projector/ai/lease/takeover")' in source
    assert "X-Kiosk-Lease-Token" in source
    assert "command_lease_generation" in source
    assert "kiosk_lease_generation" in source
    assert "validate_projector_lease" in kiosk_source
    upload_block = kiosk_source.split(
        '@router.post("/match-review/upload-intent")', 1
    )[1].split("def _claim_review_intent", 1)[0]
    analyze_block = kiosk_source.split(
        '@router.post("/match-review/analyze")', 1
    )[1]
    assert "validate_projector_lease" in upload_block
    assert "lease_generation" in upload_block
    assert "validate_projector_lease" in analyze_block
    assert analyze_block.index("validate_projector_lease") < analyze_block.index(
        "_claim_review_intent"
    )


def test_projector_templates_expose_active_standby_and_explicit_takeover():
    control = (ROOT / "templates" / "projector_control.html").read_text(
        encoding="utf-8"
    )
    display = (ROOT / "templates" / "projector_display.html").read_text(
        encoding="utf-8"
    )

    assert "sessionStorage" in display
    assert "/api/kiosk/projector-ai/lease/claim" in display
    assert "X-Kiosk-Lease-Token" in display
    assert "lease_lost" in display
    assert "Standby" in display and "Active" in display
    assert "/api/projector/ai/lease/takeover" in control
    assert "kioskTakeover" in control
    assert "interrupted" in control
    assert "已開始的 provider call 可能仍會產生費用" in control
    assert '"processing",' in control


def test_hardware_confirmation_is_revision_bound_and_transactional():
    source = (ROOT / "api" / "projector_ai_api.py").read_text(encoding="utf-8")
    block = source.split(
        '@router.post("/api/projector/ai/hardware-confirm")', 1
    )[1].split('@router.post("/api/projector/ai/start")', 1)[0]
    assert "body.revision" in block
    assert "FOR UPDATE" in block
    assert "with db.transaction()" in block


def test_start_waits_for_previous_kiosk_command_ack(monkeypatch):
    now = dt.datetime(2026, 7, 17, 12, 0, 0)
    statements = []

    class Conn:
        def execute(self, statement, params):
            sql = " ".join(str(statement).split())
            statements.append(sql)
            if sql.startswith("SELECT match_id FROM projector_state"):
                row = SimpleNamespace(_mapping={"match_id": "M1"})
                return SimpleNamespace(fetchone=lambda: row)
            if sql.startswith("SELECT kiosk_last_seen_at"):
                row = SimpleNamespace(
                    _mapping={
                        "kiosk_last_seen_at": now,
                        "hardware_status": {},
                        "capabilities": {},
                        "command_revision": 9,
                        "ack_revision": 8,
                        "lease_device_id": "device-a",
                        "lease_generation": 2,
                        "lease_expires_at": now + dt.timedelta(seconds=15),
                    }
                )
                return SimpleNamespace(fetchone=lambda: row)
            return SimpleNamespace(fetchone=lambda: None)

    class DB:
        def query(self, _sql, _params):
            return pd.DataFrame([{"match_id": "M1"}])

        @contextmanager
        def transaction(self):
            yield Conn()

    monkeypatch.setattr(
        projector_ai_api, "require_competition_staff", lambda _request: "staff"
    )
    monkeypatch.setattr(projector_ai_api, "_db", lambda: DB())
    monkeypatch.setattr(projector_ai_api, "_now", lambda: now)
    monkeypatch.setattr(projector_ai_api, "_prune_expired", lambda _db, _now: None)
    monkeypatch.setattr(
        projector_ai_api,
        "_official_match",
        lambda _db, _match: {
            "match_id": "M1",
            "topic": "測試辯題",
            "pro_team": "甲隊",
            "con_team": "乙隊",
        },
    )

    with pytest.raises(HTTPException) as raised:
        projector_ai_api.start_session(
            projector_ai_api.StartBody(
                display="main",
                match_id="M1",
                recording_notice_confirmed=True,
            ),
            SimpleNamespace(cookies={}),
        )
    assert raised.value.status_code == 409
    assert "上一個控制指令" in raised.value.detail
    assert not any("INSERT INTO projector_ai_sessions" in sql for sql in statements)


def test_start_rechecks_locked_projector_match_before_creating_session(monkeypatch):
    now = dt.datetime(2026, 7, 17, 12, 0, 0)
    statements = []

    class Conn:
        def execute(self, statement, params):
            sql = " ".join(str(statement).split())
            statements.append(sql)
            if sql.startswith("SELECT match_id FROM projector_state"):
                row = SimpleNamespace(_mapping={"match_id": "M2"})
                return SimpleNamespace(fetchone=lambda: row)
            if sql.startswith("SELECT kiosk_last_seen_at"):
                row = SimpleNamespace(
                    _mapping={
                        "kiosk_last_seen_at": now,
                        "hardware_status": {"passed": True},
                        "capabilities": {"media_primed": True},
                        "command_revision": 1,
                        "ack_revision": 1,
                        "lease_device_id": "device-a",
                        "lease_generation": 2,
                        "lease_expires_at": now + dt.timedelta(seconds=15),
                    }
                )
                return SimpleNamespace(fetchone=lambda: row)
            return SimpleNamespace(fetchone=lambda: None)

    class DB:
        def query(self, _sql, _params):
            # The old implementation only checked this stale pre-transaction view.
            return pd.DataFrame([{"match_id": "M1"}])

        @contextmanager
        def transaction(self):
            yield Conn()

    monkeypatch.setattr(
        projector_ai_api, "require_competition_staff", lambda _request: "staff"
    )
    monkeypatch.setattr(projector_ai_api, "_db", lambda: DB())
    monkeypatch.setattr(projector_ai_api, "_now", lambda: now)
    monkeypatch.setattr(projector_ai_api, "_prune_expired", lambda _db, _now: None)
    monkeypatch.setattr(projector_ai_api, "_ensure_control", lambda *_args: None)
    monkeypatch.setattr(projector_ai_api, "_kiosk_seen_is_fresh", lambda *_args: True)
    monkeypatch.setattr(projector_ai_api, "_hardware_is_fresh", lambda *_args: True)
    monkeypatch.setattr(
        projector_ai_api,
        "_official_match",
        lambda _db, _match: {
            "match_id": "M1",
            "topic": "測試辯題",
            "pro_team": "甲隊",
            "con_team": "乙隊",
        },
    )
    monkeypatch.setattr(projector_ai_api, "_issue_command", lambda *_args, **_kwargs: 2)

    with pytest.raises(HTTPException) as raised:
        projector_ai_api.start_session(
            projector_ai_api.StartBody(
                display="main",
                match_id="M1",
                recording_notice_confirmed=True,
            ),
            SimpleNamespace(cookies={}),
        )

    assert raised.value.status_code == 409
    assert "場次不一致" in raised.value.detail
    assert any(
        sql.startswith("SELECT match_id FROM projector_state") and "FOR UPDATE" in sql
        for sql in statements
    )
    assert not any("INSERT INTO projector_ai_sessions" in sql for sql in statements)

    proxy_source = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
    state_setter = proxy_source.split(
        "async def projector_set_state(request: Request):", 1
    )[1].split("# ---------------------------------------------------------------------------", 1)[0]
    initial_state_read = state_setter.split("match_row =", 1)[0]
    assert "FROM projector_state WHERE display_key = :k FOR UPDATE" in initial_state_read


def test_completed_review_persists_control_then_session_under_one_transaction(
    monkeypatch,
):
    statements = []

    class Conn:
        def execute(self, statement, params):
            sql = " ".join(str(statement).split())
            statements.append(sql)
            if sql.startswith("SELECT display_key,command_revision"):
                row = SimpleNamespace(
                    _mapping={
                        "display_key": "main",
                        "command_revision": 12,
                        "current_session_id": "session-12345678901234567890",
                    }
                )
                return SimpleNamespace(fetchone=lambda: row)
            if sql.startswith("SELECT match_id,status"):
                row = SimpleNamespace(
                    _mapping={
                        "match_id": "M1",
                        "status": "processing",
                        "result_ciphertext": None,
                        "result_expires_at": None,
                    }
                )
                return SimpleNamespace(fetchone=lambda: row)
            return SimpleNamespace(fetchone=lambda: None)

    class DB:
        @contextmanager
        def transaction(self):
            yield Conn()

    monkeypatch.setattr(projector_ai_api, "_db", lambda: DB())
    monkeypatch.setattr(projector_ai_api, "_seal_json", lambda _payload: b"sealed")
    saved = projector_ai_api.persist_completed_review_for_projector(
        session_id="session-12345678901234567890",
        match_id="M1",
        markdown="完整評語",
        transcript="完整逐字稿",
        projector_summary="摘要",
        model_label="Gemini",
        audio={"duration_seconds": 60},
        recording_deleted=True,
    )

    assert saved["revision"] == 12
    assert statements[0].startswith("SELECT display_key,command_revision")
    assert "FROM projector_ai_controls" in statements[0]
    assert statements[1].startswith("SELECT match_id,status")
    assert "FROM projector_ai_sessions" in statements[1]
    assert any(
        sql.startswith("UPDATE projector_ai_sessions") for sql in statements[2:]
    )
    assert any(
        sql.startswith("UPDATE projector_ai_controls") for sql in statements[2:]
    )


def test_completed_review_cannot_write_back_after_processing_takeover(monkeypatch):
    statements = []

    class Conn:
        def execute(self, statement, params):
            sql = " ".join(str(statement).split())
            statements.append(sql)
            if sql.startswith("SELECT display_key,command_revision"):
                row = SimpleNamespace(
                    _mapping={
                        "display_key": "main",
                        "command_revision": 13,
                        "current_session_id": "session-12345678901234567890",
                    }
                )
                return SimpleNamespace(fetchone=lambda: row)
            if sql.startswith("SELECT match_id,status"):
                row = SimpleNamespace(
                    _mapping={
                        "match_id": "M1",
                        "status": "cancelled",
                        "result_ciphertext": None,
                        "result_expires_at": None,
                    }
                )
                return SimpleNamespace(fetchone=lambda: row)
            return SimpleNamespace(fetchone=lambda: None)

    class DB:
        @contextmanager
        def transaction(self):
            yield Conn()

    monkeypatch.setattr(projector_ai_api, "_db", lambda: DB())
    with pytest.raises(ValueError, match="cannot receive"):
        projector_ai_api.persist_completed_review_for_projector(
            session_id="session-12345678901234567890",
            match_id="M1",
            markdown="完整評語",
            transcript="完整逐字稿",
            projector_summary="摘要",
            model_label="Gemini",
            audio={"duration_seconds": 60},
            recording_deleted=True,
        )

    assert not any(sql.startswith("UPDATE projector_ai_") for sql in statements)
