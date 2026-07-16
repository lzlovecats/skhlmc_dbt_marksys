"""Kiosk-only login, millisecond timer and ephemeral full-match AI review."""

import asyncio
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import pandas as pd
from fastapi import HTTPException, Request, Response

from account_access import KIOSK_ACCOUNT_ID, account_can_access
from api import kiosk_api
from core import ai_provider, auth_logic, r2_storage
import ai_model_config
import deploy.proxy as proxy
import system_limits
from tools import cleanup_r2_orphans


ROOT = Path(__file__).resolve().parents[1]


def _request():
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "scheme": "https",
        "server": ("testserver", 443),
    })


def test_kiosk_login_has_no_caller_selected_identity(monkeypatch):
    calls = []
    db = object()
    monkeypatch.setattr(proxy, "get_vote_db", lambda: db)
    monkeypatch.setattr(
        proxy,
        "_sign_committee_token",
        lambda user, **_kwargs: f"signed-{user}",
    )
    monkeypatch.setattr(
        auth_logic,
        "authenticate_login",
        lambda user, password, db=None: calls.append(
            ("check", user, password, db)
        )
        or "verified-hash",
    )
    monkeypatch.setattr(
        auth_logic,
        "record_login",
        lambda user, db=None: calls.append(("record", user, db)),
    )

    response = Response()
    result = kiosk_api.login(
        kiosk_api.KioskLoginBody(password="secret"), _request(), response,
    )

    assert result == {"status": "ok", "user_id": KIOSK_ACCOUNT_ID}
    assert calls == [
        ("check", KIOSK_ACCOUNT_ID, "secret", db),
        ("record", KIOSK_ACCOUNT_ID, db),
    ]
    cookie = response.headers["set-cookie"]
    assert "committee_user=signed-kiosk" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Secure" in cookie
    assert response.headers["cache-control"] == "no-store"
    assert set(kiosk_api.KioskLoginBody.model_fields) == {"password"}


def test_central_policy_allows_only_kiosk_identity_on_kiosk_page():
    assert account_can_access(KIOSK_ACCOUNT_ID, "kiosk") is True
    for identity in ("alice", "admin", "developer", "Gemini", ""):
        assert account_can_access(identity, "kiosk") is False


def test_practice_shell_keeps_millisecond_timers_and_moves_review_to_competition_day():
    source = (ROOT / "templates" / "appliance_practice.html").read_text(
        encoding="utf-8"
    )
    assert 'id="kiosk-login-form"' in source
    assert 'value="kiosk" readonly' in source
    assert 'id="kiosk-app"' in source
    assert 'id="mode-review"' not in source and 'id="panel-review"' not in source
    assert 'id="single-display">0:00.000' in source
    assert 'id="free-pro-display">0:00.000' in source
    assert 'id="free-con-display">0:00.000' in source
    assert "String(ms).padStart(3" in source
    contest = (ROOT / "templates" / "projector_display.html").read_text(
        encoding="utf-8"
    )
    control = (ROOT / "templates" / "projector_control.html").read_text(
        encoding="utf-8"
    )
    assert "AI評判易" in contest and "AI評判易" in control
    assert "audioBitsPerSecond: 16000" in contest
    assert "/api/kiosk/match-review/upload-intent" in contest
    assert "/api/kiosk/match-review/analyze" in contest
    assert "marksys-kiosk-recordings-v1" in contest
    assert "persistRecordingChunk" in contest
    assert "downloadPersistedRecording" in contest
    assert "uploadBlobWithRetry" in contest
    assert "/api/kiosk/match-review/upload-probe-intent" in contest
    assert "獨立硬件錄音機" in control
    assert "90:00.000" in control


def test_match_review_upload_is_direct_to_private_r2_and_bounded(monkeypatch):
    db = object()
    captured = {}
    monkeypatch.setattr(kiosk_api, "require_kiosk_user", lambda _request: "kiosk")
    monkeypatch.setattr(proxy, "get_vote_db", lambda: db)
    monkeypatch.setattr(proxy, "_get_relay_cookie_secret", lambda: "secret")
    monkeypatch.setattr(
        kiosk_api,
        "_official_match",
        lambda _db, match_id: {
            "match_id": match_id,
            "topic": "中學生應否使用人工智能",
            "pro_team": "甲隊",
            "con_team": "乙隊",
            "debate_format": "校園隨想",
            "free_debate_minutes": None,
            "match_date": "2026-07-14",
            "match_time": "16:00",
        },
    )
    monkeypatch.setattr(kiosk_api, "_paid_gemini_project_confirmed", lambda: False)
    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(
        r2_storage,
        "storage_budget_status",
        lambda _db, refresh=False: {
            "blocked": False,
            "stop_bytes": 8_000_000_000,
        },
    )
    monkeypatch.setattr(
        r2_storage,
        "sign_upload_claim",
        lambda claim, secret, expires: captured.update(
            claim=claim, secret=secret, expires=expires
        )
        or "upload-token",
    )
    monkeypatch.setattr(
        r2_storage,
        "reserve_upload_intent",
        lambda _db, **kwargs: captured.update(reservation=kwargs) or (True, ""),
    )
    monkeypatch.setattr(
        r2_storage,
        "presign_put",
        lambda key, mime, sha, size: captured.update(
            put=(key, mime, sha, size)
        )
        or "https://r2.invalid/private-put",
    )
    digest = "a" * 64
    body = kiosk_api.MatchReviewUploadIntentBody(
        match_id="M1",
        mime_type="audio/webm;codecs=opus",
        byte_size=4096,
        sha256=digest,
        duration_seconds=120,
    )

    response = kiosk_api.match_review_upload_intent(body, _request())
    payload = json.loads(response.body)

    assert payload["url"] == "https://r2.invalid/private-put"
    assert payload["upload_token"] == "upload-token"
    assert response.headers["cache-control"] == "no-store"
    claim = captured["claim"]
    assert claim["kind"] == "kiosk_match_review" and claim["user"] == "kiosk"
    assert claim["match_id"] == "M1"
    assert claim["pending_r2_key"].startswith(
        "pending/audio/kiosk-match-review/"
    )
    assert captured["reservation"]["object_keys"] == [
        claim["pending_r2_key"]
    ]
    assert captured["reservation"]["declared_bytes"] == 4096
    assert "user_daily_limit" not in captured["reservation"]
    assert "global_monthly_limit" not in captured["reservation"]
    assert payload["limits"] == {
        "max_bytes": system_limits.KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
        "max_seconds": system_limits.KIOSK_MATCH_REVIEW_MAX_SECONDS,
    }


def test_kiosk_limit_is_ninety_minutes_without_raising_inline_byte_cap():
    assert system_limits.KIOSK_MATCH_REVIEW_MAX_SECONDS == 90 * 60
    assert system_limits.KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES == 12 * 1024 * 1024
    body = kiosk_api.MatchReviewUploadIntentBody(
        match_id="M1",
        byte_size=10_800_000,
        sha256="a" * 64,
        duration_seconds=90 * 60,
    )
    assert body.duration_seconds == 5400


def test_hardware_probe_exercises_browser_to_r2_and_deletes_object(monkeypatch):
    db = object()
    digest = "b" * 64
    key = "pending/probe/kiosk/probe-1.bin"
    captured = {}
    monkeypatch.setattr(kiosk_api, "require_kiosk_user", lambda _request: "kiosk")
    monkeypatch.setattr(proxy, "get_vote_db", lambda: db)
    monkeypatch.setattr(proxy, "_get_relay_cookie_secret", lambda: "secret")
    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(
        r2_storage,
        "storage_budget_status",
        lambda _db, refresh=False: {"blocked": False},
    )
    monkeypatch.setattr(
        r2_storage,
        "sign_upload_claim",
        lambda claim, secret, expires: captured.update(claim=claim) or "p" * 40,
    )
    monkeypatch.setattr(
        r2_storage,
        "presign_put",
        lambda object_key, mime, sha, size: captured.update(
            put=(object_key, mime, sha, size)
        )
        or "https://r2.invalid/probe",
    )
    monkeypatch.setattr(
        r2_storage,
        "reserve_upload_intent",
        lambda _db, **kwargs: captured.update(reservation=kwargs) or (True, ""),
    )

    response = kiosk_api.match_review_upload_probe_intent(
        kiosk_api.MatchReviewUploadProbeIntentBody(byte_size=1024, sha256=digest),
        _request(),
    )
    payload = json.loads(response.body)
    claim = captured["claim"]
    assert claim["kind"] == "kiosk_upload_probe"
    assert claim["pending_r2_key"].startswith("pending/probe/kiosk/")
    assert captured["put"][1:] == ("application/octet-stream", digest, 1024)
    assert captured["reservation"]["media_kind"] == "kiosk_upload_probe"
    assert payload["url"] == "https://r2.invalid/probe"

    monkeypatch.setattr(
        r2_storage,
        "verify_upload_claim",
        lambda _token, _secret: {**claim, "pending_r2_key": key},
    )
    monkeypatch.setattr(
        r2_storage,
        "head",
        lambda _key: {
            "ContentLength": 1024,
            "ContentType": "application/octet-stream",
            "Metadata": {"sha256": digest},
        },
    )
    monkeypatch.setattr(
        r2_storage,
        "delete_intent_objects",
        lambda _db, intent, keys: captured.update(deleted=(intent, keys)) or True,
    )
    completed = asyncio.run(
        kiosk_api.match_review_upload_probe_complete(
            kiosk_api.MatchReviewUploadProbeCompleteBody(
                upload_token=payload["upload_token"]
            ),
            _request(),
        )
    )
    assert json.loads(completed.body) == {"ok": True, "probe_deleted": True}
    assert captured["deleted"] == (claim["intent_id"], (key,))


def test_official_match_endpoint_exposes_no_passwords_or_roster_tokens(monkeypatch):
    queries = []

    class _Db:
        def query(self, sql, params):
            queries.append((sql, params))
            if "FROM debaters" in sql:
                return pd.DataFrame(
                    [
                        {
                            "match_id": "M1",
                            "side": "pro",
                            "position": 1,
                            "debater_name": "陳同學",
                        },
                        {
                            "match_id": "M1",
                            "side": "pro",
                            "position": 4,
                            "debater_name": "陳同學",
                        },
                        {
                            "match_id": "M1",
                            "side": "con",
                            "position": 1,
                            "debater_name": "李同學",
                        },
                    ]
                )
            return pd.DataFrame(
                [
                    {
                        "match_id": "M1",
                        "match_date": "2026-07-14",
                        "match_time": "16:00",
                        "topic_text": "正式辯題",
                        "pro_team": "甲隊",
                        "con_team": "乙隊",
                        "debate_format": "聯中",
                        "free_debate_minutes": 5,
                    }
                ]
            )

    monkeypatch.setattr(kiosk_api, "require_kiosk_user", lambda _request: "kiosk")
    monkeypatch.setattr(proxy, "get_vote_db", lambda: _Db())
    response = kiosk_api.match_review_matches(_request())
    payload = json.loads(response.body)

    match = payload["matches"][0]
    assert {key: match[key] for key in (
        "match_id", "match_date", "match_time", "topic", "pro_team",
        "con_team", "debate_format", "free_debate_minutes",
    )} == {
        "match_id": "M1",
        "match_date": "2026-07-14",
        "match_time": "16:00",
        "topic": "正式辯題",
        "pro_team": "甲隊",
        "con_team": "乙隊",
        "debate_format": "聯中",
        "free_debate_minutes": 5.0,
    }
    assert match["listed_roster_slot_count"] == 3
    assert match["listed_unique_participant_count"] == 2
    assert match["roster_slots"][1]["role_label"] == "正方結辯"
    assert match["duplicate_role_assignments"] == [
        {
            "debater_name": "陳同學",
            "assignments": [
                {
                    "side": "pro",
                    "side_label": "正方",
                    "position": 1,
                    "role": "主辯",
                    "role_label": "正方主辯",
                },
                {
                    "side": "pro",
                    "side_label": "正方",
                    "position": 4,
                    "role": "結辯",
                    "role_label": "正方結辯",
                },
            ],
        }
    ]
    sql = queries[0][0].lower()
    assert "access_code" not in sql
    assert "review_password" not in sql
    assert "roster" not in sql
    assert "roster_token" not in queries[1][0].lower()
    assert response.headers["cache-control"] == "no-store"


def test_preflight_is_read_only_and_reports_system_resource_gates(monkeypatch):
    db = object()
    monkeypatch.setattr(kiosk_api, "require_kiosk_user", lambda _request: "kiosk")
    monkeypatch.setattr(proxy, "get_vote_db", lambda: db)
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda _name: "key")
    monkeypatch.setattr(kiosk_api, "_paid_gemini_project_confirmed", lambda: False)
    monkeypatch.setattr(
        kiosk_api,
        "_official_match_records",
        lambda _db: [{"match_id": "M1"}],
    )
    monkeypatch.setattr(kiosk_api.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(r2_storage, "connection_ready", lambda: True)
    monkeypatch.setattr(
        r2_storage,
        "storage_budget_status",
        lambda _db, refresh=False: {"blocked": False},
    )
    monkeypatch.setattr(
        ai_model_config,
        "get_feature_model",
        lambda _feature: (
            "Central model",
            {
                "provider": "gemini",
                "supports_audio": True,
                "api_key": "GEMINI_API_KEY",
            },
        ),
    )

    response = asyncio.run(kiosk_api.match_review_preflight(_request()))
    payload = json.loads(response.body)
    assert payload["ok"] is True
    assert payload["does_not_call_ai"] is True
    assert "quota" not in payload and "quota" not in payload["checks"]
    assert payload["checks"]["r2"]["ok"] is True
    assert payload["checks"]["bandwidth"]["ok"] is True
    assert payload["checks"]["privacy"]["ok"] is True
    assert payload["checks"]["privacy"]["paid_tier_confirmed"] is False
    assert payload["checks"]["privacy"]["free_tier_test_allowed"] is True
    assert "允許 Free Tier" in payload["checks"]["privacy"]["detail"]
    assert response.headers["cache-control"] == "no-store"


def test_marker_validation_rejects_reverse_or_out_of_recording_order():
    with pytest.raises(HTTPException, match="時間先後"):
        kiosk_api._validated_markers(
            [
                kiosk_api.MatchReviewSpeakerMarker(
                    offset_seconds=20, side="pro", segment="主辯"
                ),
                kiosk_api.MatchReviewSpeakerMarker(
                    offset_seconds=10, side="con", segment="主辯"
                ),
            ],
            120,
        )
    with pytest.raises(HTTPException, match="超出實際錄音"):
        kiosk_api._validated_markers(
            [
                kiosk_api.MatchReviewSpeakerMarker(
                    offset_seconds=121.5, side="pro", segment="主辯"
                )
            ],
            120,
        )


def _install_analysis_path(monkeypatch, *, cleanup=True, claim_once=True):
    audio = b"bounded-full-match-audio" * 100
    digest = hashlib.sha256(audio).hexdigest()
    key = "pending/audio/kiosk-match-review/2026/07/test.webm"
    db = object()
    events = []
    claim = {
        "kind": "kiosk_match_review",
        "intent_id": "intent-1",
        "operation_id": "session-1",
        "user": "kiosk",
        "match_id": "M1",
        "pending_r2_key": key,
        "mime_type": "audio/webm",
        "byte_size": len(audio),
        "sha256": digest,
        "duration_seconds": 120,
    }
    config = {
        "provider": "gemini",
        "model": "central-model",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "input_price_per_million": 1,
        "audio_input_price_per_million": 2,
        "output_price_per_million": 3,
    }
    monkeypatch.setattr(kiosk_api, "require_kiosk_user", lambda _request: "kiosk")
    monkeypatch.setattr(kiosk_api, "_paid_gemini_project_confirmed", lambda: False)
    monkeypatch.setattr(
        kiosk_api,
        "_official_match",
        lambda _db, match_id: {
            "match_id": match_id,
            "match_date": "2026-07-14",
            "match_time": "16:00",
            "topic": "中學生應否使用人工智能",
            "pro_team": "甲隊",
            "con_team": "乙隊",
            "debate_format": "校園隨想",
            "free_debate_minutes": None,
            "roster_slots": [
                {
                    "side": "pro",
                    "side_label": "正方",
                    "position": 1,
                    "role": "主辯",
                    "role_label": "正方主辯",
                    "debater_name": "陳同學",
                },
                {
                    "side": "pro",
                    "side_label": "正方",
                    "position": 4,
                    "role": "結辯",
                    "role_label": "正方結辯",
                    "debater_name": "陳同學",
                },
            ],
            "listed_roster_slot_count": 2,
            "listed_unique_participant_count": 1,
            "duplicate_role_assignments": [
                {
                    "debater_name": "陳同學",
                    "assignments": ["正方主辯", "正方結辯"],
                }
            ],
        },
    )
    monkeypatch.setattr(proxy, "get_vote_db", lambda: db)
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(proxy, "_get_relay_cookie_secret", lambda: "secret")
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda _name: "provider-key")
    monkeypatch.setattr(
        proxy,
        "record_bandwidth_usage",
        lambda *args, **kwargs: events.append(("bandwidth", args, kwargs)),
    )
    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(
        r2_storage, "verify_upload_claim", lambda token, secret: claim
    )
    monkeypatch.setattr(
        r2_storage,
        "head",
        lambda _key: {
            "ContentLength": len(audio),
            "ContentType": "audio/webm",
            "Metadata": {"sha256": digest},
        },
    )
    monkeypatch.setattr(
        r2_storage,
        "download_bytes",
        lambda _key, max_bytes: events.append(("download", max_bytes)) or audio,
    )
    monkeypatch.setattr(
        r2_storage,
        "delete_intent_objects",
        lambda _db, intent, keys: events.append(("delete", intent, keys))
        or cleanup,
    )
    monkeypatch.setattr(
        kiosk_api,
        "_claim_review_intent",
        lambda _db, intent: events.append(("claim", intent)) or claim_once,
    )
    monkeypatch.setattr(
        kiosk_api,
        "_set_review_intent_provider_status",
        lambda _db, intent, status: events.append(("intent_status", intent, status)),
    )
    monkeypatch.setattr(
        kiosk_api,
        "probe_audio",
        lambda data, mime, claimed, max_seconds: {
            "duration": 120,
            "sample_rate": 16000,
            "channels": 1,
            "format": "matroska,webm",
            "mime": mime,
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    monkeypatch.setattr(
        kiosk_api,
        "transcode_audio_for_provider",
        lambda data, mime, max_output_bytes: events.append(
            ("transcode", mime, max_output_bytes)
        )
        or (data, "audio/mpeg"),
    )
    monkeypatch.setattr(
        ai_model_config,
        "get_feature_model",
        lambda feature: events.append(("model", feature))
        or ("Central audio model", config),
    )
    usage_logs = []
    monkeypatch.setattr(
        kiosk_api,
        "_log_review_usage",
        lambda *args, **kwargs: usage_logs.append((args, kwargs)),
    )
    return audio, digest, events, usage_logs


def _analysis_body():
    return kiosk_api.MatchReviewBody(
        upload_token="x" * 40,
        match_id="M1",
        speaker_markers=[
            {"offset_seconds": 0, "side": "unknown", "segment": "開場"},
            {"offset_seconds": 10, "side": "pro", "segment": "主辯"},
        ],
        recording_notice_confirmed=True,
    )


def test_raw_recording_is_deleted_and_both_provider_passes_receive_audio(monkeypatch):
    audio, _digest, events, usage_logs = _install_analysis_path(monkeypatch)

    async def generate(config, system, user, **kwargs):
        events.append(("provider", config, system, user, kwargs))
        assert kwargs["audio_base64"]
        assert kwargs["audio_mime"] == "audio/mpeg"
        assert kwargs["require_complete"] is True
        assert "max_output_tokens" not in kwargs
        text = (
            "[00:00.000–00:10.000] [未能確定] [S01] 開場\n"
            if "專業粵語逐字員" in system
            else "建議勝方：未能判定"
        )
        return text, {
            "input_tokens": 10,
            "audio_tokens": 20,
            "output_tokens": 30,
            "cost_source": "gemini_usage_metadata",
        }

    monkeypatch.setattr(ai_provider, "generate_text", generate)
    response = asyncio.run(kiosk_api.analyze_match_review(_analysis_body(), _request()))
    payload = json.loads(response.body)

    event_names = [event[0] for event in events]
    assert event_names.index("claim") < event_names.index("download")
    assert event_names.index("download") < event_names.index("transcode")
    assert event_names.index("delete") < event_names.index("provider")
    assert event_names.count("delete") == 1
    providers = [event for event in events if event[0] == "provider"]
    assert len(providers) == 2
    assert providers[0][1]["model"] == "central-model"
    assert "專業粵語逐字員" in providers[0][2]
    assert providers[0][4]["audio_base64"]
    assert "陳同學" in providers[0][3]
    assert "duplicate_role_assignments" in providers[0][3]
    assert providers[1][4]["audio_base64"]
    assert "正式賽果以評判團為準" in providers[1][3]
    assert "自行訂立 4 至 6 項「本場判準」" in providers[1][2]
    assert "雙方主要討論範圍之內" in providers[1][2]
    assert "雙方主要討論範圍之外" in providers[1][2]
    assert "表現突出同學" in providers[1][2]
    assert "transcript_evidence" in providers[1][3]
    assert "陳同學" in providers[1][3]
    assert "listed_unique_participant_count" in providers[1][3]
    assert payload["recording_deleted"] is True
    assert payload["model_label"] == "Central audio model"
    assert payload["audio"]["duration_seconds"] == 120
    assert payload["match"]["match_id"] == "M1"
    assert payload["speaker_marker_count"] == 2
    assert "S01" in payload["transcript"]
    assert payload["judgement_evidence_mode"] == "audio_and_transcript"
    assert response.headers["cache-control"] == "no-store"
    assert len(usage_logs) == 2
    assert all(item[0][3] is True for item in usage_logs)
    assert [item[1]["operation_id"] for item in usage_logs] == [
        "session-1",
        "session-1",
    ]
    assert [item[1]["operation_stage"] for item in usage_logs] == [
        "transcription",
        "judgement",
    ]
    first_status = next(
        index
        for index, event in enumerate(events)
        if event[0] == "intent_status" and event[2] == "provider_processing"
    )
    first_provider = event_names.index("provider")
    first_bandwidth = event_names.index("bandwidth")
    assert first_bandwidth < first_status < first_provider
    assert audio not in response.body


def test_privacy_delete_failure_cancels_provider_call(monkeypatch):
    _audio, _digest, events, usage_logs = _install_analysis_path(
        monkeypatch, cleanup=False
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("provider must not receive audio retained in R2")

    monkeypatch.setattr(ai_provider, "generate_text", forbidden)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(kiosk_api.analyze_match_review(_analysis_body(), _request()))
    assert raised.value.status_code == 502
    assert "保障私隱" in raised.value.detail
    assert "provider" not in [event[0] for event in events]
    assert usage_logs == []


def test_pre_provider_size_failure_cleans_object_without_ai_fund_call(monkeypatch):
    _audio, _digest, events, usage_logs = _install_analysis_path(monkeypatch)
    monkeypatch.setattr(kiosk_api, "GEMINI_INLINE_REQUEST_SAFE_BYTES", 1)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("provider must not run before local size gate passes")

    monkeypatch.setattr(ai_provider, "generate_text", forbidden)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(kiosk_api.analyze_match_review(_analysis_body(), _request()))

    assert raised.value.status_code == 400
    assert [event[0] for event in events].count("delete") == 1
    assert "provider" not in [event[0] for event in events]
    assert "intent_status" not in [event[0] for event in events]
    assert usage_logs == []


def test_pre_provider_bandwidth_failure_cleans_object_without_phantom_attempt(
    monkeypatch,
):
    _audio, _digest, events, usage_logs = _install_analysis_path(monkeypatch)

    def fail_bandwidth(*_args, **_kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(proxy, "record_bandwidth_usage", fail_bandwidth)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("provider must not run before bandwidth gate passes")

    monkeypatch.setattr(ai_provider, "generate_text", forbidden)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(kiosk_api.analyze_match_review(_analysis_body(), _request()))

    assert raised.value.status_code == 502
    assert "provider" not in [event[0] for event in events]
    assert "intent_status" not in [event[0] for event in events]
    assert usage_logs == []


def test_judgement_preflight_failure_keeps_transcript_spend_without_phantom_call(
    monkeypatch,
):
    _audio, _digest, events, usage_logs = _install_analysis_path(monkeypatch)
    provider_calls = []

    async def transcribe_only(*_args, **_kwargs):
        provider_calls.append(True)
        return "[00:00.000] [正方] [講者 A] 測試", {
            "input_tokens": 10,
            "audio_tokens": 20,
            "output_tokens": 30,
        }

    monkeypatch.setattr(ai_provider, "generate_text", transcribe_only)
    monkeypatch.setattr(
        kiosk_api,
        "_match_review_prompts",
        lambda *_args: ("X" * (kiosk_api.GEMINI_INLINE_REQUEST_SAFE_BYTES + 1), ""),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(kiosk_api.analyze_match_review(_analysis_body(), _request()))

    assert raised.value.status_code == 400
    assert len(provider_calls) == 1
    assert [(item[0][3], item[1]["operation_stage"]) for item in usage_logs] == [
        (True, "transcription")
    ]
    assert [event[2] for event in events if event[0] == "intent_status"] == [
        "provider_processing",
        "consumed",
    ]


def test_failed_judgement_provider_attempt_is_logged_in_same_operation(monkeypatch):
    _audio, _digest, events, usage_logs = _install_analysis_path(monkeypatch)
    calls = 0

    async def generate(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "[00:00.000] [正方] [講者 A] 測試", {
                "input_tokens": 10,
                "audio_tokens": 20,
                "output_tokens": 30,
            }
        raise RuntimeError("provider timeout")

    monkeypatch.setattr(ai_provider, "generate_text", generate)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(kiosk_api.analyze_match_review(_analysis_body(), _request()))

    assert raised.value.status_code == 502
    assert calls == 2
    assert [(item[0][3], item[1]["operation_stage"]) for item in usage_logs] == [
        (True, "transcription"),
        (False, "judgement"),
    ]
    assert {item[1]["operation_id"] for item in usage_logs} == {"session-1"}
    assert [event[2] for event in events if event[0] == "intent_status"] == [
        "provider_processing",
        "consumed",
    ]


def test_missing_provider_key_is_not_recorded_as_a_provider_attempt(monkeypatch):
    _audio, _digest, events, usage_logs = _install_analysis_path(monkeypatch)
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda _name: "")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("provider must not run without an API key")

    monkeypatch.setattr(ai_provider, "generate_text", forbidden)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(kiosk_api.analyze_match_review(_analysis_body(), _request()))

    assert raised.value.status_code == 503
    assert "provider" not in [event[0] for event in events]
    assert "intent_status" not in [event[0] for event in events]
    assert usage_logs == []


def test_replayed_review_token_is_rejected_before_download_or_provider(monkeypatch):
    _audio, _digest, events, _usage_logs = _install_analysis_path(
        monkeypatch, claim_once=False
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("provider must not run for a replayed token")

    monkeypatch.setattr(ai_provider, "generate_text", forbidden)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(kiosk_api.analyze_match_review(_analysis_body(), _request()))
    assert raised.value.status_code == 409
    assert "重複提交" in raised.value.detail
    assert [event[0] for event in events] == ["claim"]


def test_review_claim_stays_orphan_cleanable_until_raw_delete():
    statements = []

    class _Result:
        def fetchone(self):
            return ("intent-1",)

    class _Connection:
        def execute(self, statement, params):
            statements.append((str(statement), params))
            return _Result()

    class _Db:
        @contextmanager
        def transaction(self):
            yield _Connection()

    assert kiosk_api._claim_review_intent(_Db(), "intent-1") is True
    sql = " ".join(statements[0][0].split())
    assert "SET status='processing',completed_at=NULL" in sql
    assert "status='issued'" in sql
    assert "status='completed'" not in sql


def test_orphan_sweeper_includes_interrupted_processing_intents():
    queries = []

    class _Rows:
        def iterrows(self):
            return iter(())

    class _Db:
        def execute(self, _statement):
            return None

        def query(self, statement):
            queries.append(" ".join(statement.split()))
            return _Rows()

    assert cleanup_r2_orphans._issued_intents(_Db()) == {}
    assert "status IN ('issued','processing')" in queries[0]


def test_usage_schema_and_migration_register_kiosk_review():
    schema_source = (ROOT / "schema.py").read_text(encoding="utf-8")
    funds_source = (ROOT / "core" / "funds_logic.py").read_text(
        encoding="utf-8"
    )
    migration_up = (
        ROOT
        / "migrations"
        / "20260714_0004_add_kiosk_match_review_usage.up.sql"
    ).read_text(encoding="utf-8")
    migration_down = (
        ROOT
        / "migrations"
        / "20260714_0004_add_kiosk_match_review_usage.down.sql"
    ).read_text(encoding="utf-8")
    assert "'kiosk_match_review'" in schema_source
    assert '"kiosk_match_review"' in funds_source
    assert "kiosk_match_review" in migration_up
    assert "DROP CONSTRAINT IF EXISTS chk_ai_fund_usage_feature" in migration_up
    assert "DROP CONSTRAINT IF EXISTS ai_fund_usage_logs_feature_check" in migration_up
    assert "ADD CONSTRAINT ai_fund_usage_logs_feature_check" in migration_up
    assert "ADD CONSTRAINT chk_ai_fund_usage_feature" in migration_down
    assert '"kiosk_match_review": "AI評判易（Kiosk）"' in funds_source
    ai_fund_source = (ROOT / "frontend" / "ai_fund" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'kiosk_match_review: "AI評判易（Kiosk）"' in ai_fund_source
    prompt_source = (ROOT / "prompts.py").read_text(
        encoding="utf-8"
    )
    assert "以下『AI評判易』結果只屬 AI 輔助評語" in prompt_source
    kiosk_source = (ROOT / "api" / "kiosk_api.py").read_text(encoding="utf-8")
    assert "build_kiosk_transcript_prompts" in kiosk_source
    assert "build_kiosk_match_review_prompts" in kiosk_source
    assert "你是香港中學中文辯論比賽的資深評判" not in kiosk_source
    assert ai_model_config.get_feature_model("kiosk_match_review")[1][
        "supports_audio"
    ] is True
    assert "gemini-" not in (ROOT / "api" / "kiosk_api.py").read_text(
        encoding="utf-8"
    )
