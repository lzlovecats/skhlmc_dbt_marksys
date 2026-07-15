import datetime as dt
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
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


def test_projector_result_requires_confirmed_recording_deletion(monkeypatch):
    monkeypatch.setattr(projector_ai_api, "_require_kiosk", lambda _request: "kiosk")
    body = projector_ai_api.KioskResultBody(
        display="main",
        session_id="session-1234567890123456",
        revision=1,
        markdown="完整評語",
        transcript="逐字稿",
        projector_summary="投影摘要",
        recording_deleted=False,
    )
    try:
        projector_ai_api.kiosk_result(body, SimpleNamespace(cookies={}))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
        assert "錄音已刪除" in str(getattr(exc, "detail", ""))
    else:  # pragma: no cover - privacy gate must fail before any DB access
        raise AssertionError("result accepted without recording deletion proof")


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
    assert 'id="kioskPrimeButton" type="button" style="display: none"' in display
    assert "投影文字並準備語音" in control
    assert "讀出／重新讀出" in control
    assert "投影並讀出" not in control


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
