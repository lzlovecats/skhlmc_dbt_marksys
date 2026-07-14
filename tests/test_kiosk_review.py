"""Kiosk-only login, millisecond timer and ephemeral full-match AI review."""

import asyncio
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
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


def test_practice_shell_starts_at_login_and_displays_milliseconds_and_mic_review():
    source = (ROOT / "templates" / "appliance_practice.html").read_text(
        encoding="utf-8"
    )
    assert 'id="kiosk-login-form"' in source
    assert 'value="kiosk" readonly' in source
    assert 'id="kiosk-app"' in source
    assert 'id="mode-review"' in source and 'id="panel-review"' in source
    assert 'id="review-mic"' in source and "getUserMedia" in source
    assert "new MediaRecorder" in source and "audioBitsPerSecond: 16000" in source
    assert 'id="single-display">0:00.000' in source
    assert 'id="free-pro-display">0:00.000' in source
    assert 'id="free-con-display">0:00.000' in source
    assert "String(ms).padStart(3" in source
    assert "/api/kiosk/match-review/upload-intent" in source
    assert "/api/kiosk/match-review/analyze" in source
    assert "AI 結果只屬第二意見" in source


def test_match_review_upload_is_direct_to_private_r2_and_bounded(monkeypatch):
    db = object()
    captured = {}
    monkeypatch.setattr(kiosk_api, "require_kiosk_user", lambda _request: "kiosk")
    monkeypatch.setattr(proxy, "get_vote_db", lambda: db)
    monkeypatch.setattr(proxy, "_get_relay_cookie_secret", lambda: "secret")
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
    assert claim["pending_r2_key"].startswith(
        "pending/audio/kiosk-match-review/"
    )
    assert captured["reservation"]["object_keys"] == [
        claim["pending_r2_key"]
    ]
    assert captured["reservation"]["declared_bytes"] == 4096
    assert captured["reservation"]["user_daily_limit"] == (
        system_limits.KIOSK_MATCH_REVIEW_DAILY_LIMIT
    )
    assert payload["limits"] == {
        "max_bytes": system_limits.KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES,
        "max_seconds": system_limits.KIOSK_MATCH_REVIEW_MAX_SECONDS,
    }


def _install_analysis_path(monkeypatch, *, cleanup=True, claim_once=True):
    audio = b"bounded-full-match-audio" * 100
    digest = hashlib.sha256(audio).hexdigest()
    key = "pending/audio/kiosk-match-review/2026/07/test.webm"
    db = object()
    events = []
    claim = {
        "kind": "kiosk_match_review",
        "intent_id": "intent-1",
        "user": "kiosk",
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
        topic="中學生應否使用人工智能",
        debate_format="校園隨想",
        pro_team="甲隊",
        con_team="乙隊",
        recording_notice_confirmed=True,
    )


def test_raw_recording_is_deleted_before_central_audio_model_runs(monkeypatch):
    audio, _digest, events, usage_logs = _install_analysis_path(monkeypatch)

    async def generate(config, system, user, **kwargs):
        events.append(("provider", config, system, user, kwargs))
        assert kwargs["audio_base64"]
        assert kwargs["audio_mime"] == "audio/webm"
        return "建議勝方：未能判定", {
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
    assert event_names.index("delete") < event_names.index("provider")
    assert event_names.count("delete") == 1
    provider = next(event for event in events if event[0] == "provider")
    assert provider[1]["model"] == "central-model"
    assert "正式賽果以評判團為準" in provider[3]
    assert "不可因聲線、性別、口音或身份" in provider[2]
    assert payload["recording_deleted"] is True
    assert payload["model_label"] == "Central audio model"
    assert payload["audio"]["duration_seconds"] == 120
    assert response.headers["cache-control"] == "no-store"
    assert len(usage_logs) == 1 and usage_logs[0][0][3] is True
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
    migration = (
        ROOT
        / "migrations"
        / "20260714_0004_add_kiosk_match_review_usage.up.sql"
    ).read_text(encoding="utf-8")
    assert "'kiosk_match_review'" in schema_source
    assert '"kiosk_match_review"' in funds_source
    assert "kiosk_match_review" in migration
    assert ai_model_config.get_feature_model("kiosk_match_review")[1][
        "supports_audio"
    ] is True
    assert "gemini-" not in (ROOT / "api" / "kiosk_api.py").read_text(
        encoding="utf-8"
    )
