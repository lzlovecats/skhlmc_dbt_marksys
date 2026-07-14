from pathlib import Path
import asyncio

import pandas as pd
import pytest

import ai_model_config
from core import funds_logic
from deploy import proxy


ROOT = Path(__file__).resolve().parents[1]


class _RecordingDb:
    def __init__(self):
        self.executions = []
        self.queries = []

    def execute(self, sql, params=None):
        self.executions.append((" ".join(str(sql).split()), params or {}))

    def query(self, sql, params=None):
        self.queries.append((" ".join(str(sql).split()), params or {}))
        return pd.DataFrame()


def test_tts_pricing_metadata_is_central_configurable_and_content_free():
    provider, default = ai_model_config.resolve_tts_accounting_config("azure")
    assert provider == "azure"
    assert default["price_secret"] == (
        "AZURE_TTS_PRICE_PER_MILLION_CHARACTERS_USD"
    )
    assert default["price_per_million_characters_usd"] == 16.0
    assert default["cost_source"] == "documented_default_character_rate"
    assert "override" in default["pricing_note"].lower()

    _, configured = ai_model_config.resolve_tts_accounting_config(
        "azure", price_per_million_characters_usd="21.5"
    )
    assert configured["price_per_million_characters_usd"] == 21.5
    assert configured["cost_source"] == "configured_character_rate"

    _, invalid = ai_model_config.resolve_tts_accounting_config(
        "azure", price_per_million_characters_usd="not-a-rate"
    )
    assert invalid["price_per_million_characters_usd"] == 16.0
    assert invalid["cost_source"] == "documented_default_character_rate"

    usage = ai_model_config.build_tts_usage_metadata(
        "azure",
        "粵語！",
        price_per_million_characters_usd=20,
        operation_id="match-7",
        operation_stage="result_tts",
    )
    assert usage == {
        "provider": "azure",
        "model_label": "Azure Speech TTS",
        "billable_characters": 3,
        "estimated_cost_usd": pytest.approx(0.00006),
        "cost_source": "configured_character_rate",
        "operation_id": "match-7",
        "operation_stage": "result_tts",
    }
    assert "text" not in usage


def test_failed_tts_provider_attempt_keeps_pairing_and_estimated_usage(monkeypatch):
    db = _RecordingDb()
    monkeypatch.setattr(funds_logic, "prune_ai_usage", lambda _db: None)

    funds_logic.log_tts_usage(
        "kiosk",
        "kiosk_match_review_tts",
        False,
        provider="azure",
        text="完整評語",
        operation_id="review-operation-1",
        operation_stage="result_tts",
        price_per_million_characters_usd=20,
        error_message="provider timeout",
        db=db,
    )

    assert len(db.executions) == 1
    sql, params = db.executions[0]
    assert "billable_characters" in sql
    assert "operation_id" in sql and "operation_stage" in sql
    assert params["provider"] == "azure"
    assert params["feature"] == "kiosk_match_review_tts"
    assert params["characters"] == 4
    assert params["operation_id"] == "review-operation-1"
    assert params["operation_stage"] == "result_tts"
    assert params["status"] == "failed"
    assert params["usd"] == pytest.approx(0.00008)
    assert params["hkd"] == pytest.approx(0.000624)
    assert params["source"] == "configured_character_rate"
    assert params["error"] == "provider timeout"


def test_tts_accounting_requires_supported_feature_and_operation_id(monkeypatch):
    monkeypatch.setattr(funds_logic, "prune_ai_usage", lambda _db: None)
    with pytest.raises(ValueError, match="operation_id"):
        funds_logic.log_tts_usage(
            "member",
            "tts",
            True,
            provider="azure",
            text="內容",
            operation_id="",
            db=_RecordingDb(),
        )
    with pytest.raises(ValueError, match="TTS"):
        funds_logic.log_tts_usage(
            "member",
            "speech_review",
            True,
            provider="azure",
            text="內容",
            operation_id="op-1",
            db=_RecordingDb(),
        )


def test_generic_failed_call_without_metadata_retains_legacy_zero_cost(monkeypatch):
    db = _RecordingDb()
    monkeypatch.setattr(funds_logic, "prune_ai_usage", lambda _db: None)
    funds_logic.log_ai_usage(
        "member", "speech_review", False, error_message="failed", db=db
    )
    params = db.executions[0][1]
    assert params["status"] == "failed"
    assert params["usd"] == params["hkd"] == 0
    assert params["characters"] == 0
    assert params["operation_id"] is None
    assert params["source"] == "failed"


def test_summary_keeps_generic_call_count_and_adds_kiosk_task_count():
    db = _RecordingDb()
    funds_logic.ai_usage_summary("treasurer", treasurer=True, db=db)
    sql = db.queries[0][0]
    assert "COUNT(*) FILTER (WHERE status='success') AS uses" in sql
    assert "COUNT(*) AS provider_calls" in sql
    assert "COUNT(DISTINCT CASE" in sql
    assert "feature IN ('kiosk_match_review','kiosk_match_review_tts')" in sql
    assert "THEN operation_id" in sql
    assert "SUM(billable_characters)" in sql


def test_custom_tts_readiness_includes_the_real_azure_fallback(monkeypatch):
    custom = ai_model_config.TTS_PROVIDER_OPTIONS[ai_model_config.CUSTOM_TTS_PROVIDER]
    azure = ai_model_config.TTS_PROVIDER_OPTIONS[ai_model_config.AZURE_TTS_PROVIDER]
    secrets = {
        custom["url_secret"]: "https://tts.invalid",
        custom["api_key_secret"]: "custom-key",
        custom["model_secret"]: "not-deployable",
        azure["speech_key_secret"]: "azure-key",
        azure["region_secret"]: "eastasia",
    }
    monkeypatch.setattr(
        proxy,
        "_selected_tts_provider",
        lambda: (ai_model_config.CUSTOM_TTS_PROVIDER, custom),
    )
    monkeypatch.setattr(
        proxy,
        "_get_proxy_secret",
        lambda name, default="": secrets.get(name, default),
    )
    monkeypatch.setattr(proxy, "_model_is_deployable", lambda *_args: False)

    assert proxy.tts_provider_configured() is True

    secrets[azure["speech_key_secret"]] = ""
    assert proxy.tts_provider_configured() is False


def test_tts_fallback_accounts_each_real_attempt_under_one_operation(monkeypatch):
    attempts = []
    custom = ai_model_config.TTS_PROVIDER_OPTIONS[ai_model_config.CUSTOM_TTS_PROVIDER]
    azure = ai_model_config.TTS_PROVIDER_OPTIONS[ai_model_config.AZURE_TTS_PROVIDER]
    secrets = {
        custom["url_secret"]: "https://custom.invalid/synthesize",
        custom["api_key_secret"]: "custom-key",
        custom["model_secret"]: "voice-v1",
        azure["speech_key_secret"]: "azure-key",
        azure["region_secret"]: "eastasia",
    }
    monkeypatch.setattr(
        proxy,
        "_selected_tts_provider",
        lambda: (ai_model_config.CUSTOM_TTS_PROVIDER, custom),
    )
    monkeypatch.setattr(proxy, "_preprocess_tts_text", lambda value: value)
    monkeypatch.setattr(
        proxy,
        "_get_proxy_secret",
        lambda name, default="": secrets.get(name, default),
    )
    monkeypatch.setattr(proxy, "_model_is_deployable", lambda *_args: True)

    class _Stream:
        status_code = 200
        headers = {"content-length": "5", "content-type": "audio/mpeg"}

        async def aiter_bytes(self):
            yield b"audio"

        async def aclose(self):
            return None

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def build_request(self, _method, url, **_kwargs):
            return {"url": str(url)}

        async def send(self, request, *, stream=False):
            assert stream is True
            if request["url"].startswith("https://custom.invalid"):
                raise proxy.httpx.ConnectError("custom offline")
            return _Stream()

    async def record(accounting, **kwargs):
        attempts.append((dict(accounting or {}), kwargs))

    monkeypatch.setattr(proxy.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(proxy, "_record_tts_attempt", record)
    audio, mime, metadata = asyncio.run(
        proxy.synthesize_tts_accounted(
            "粵語評語",
            user_id="kiosk",
            feature="kiosk_match_review_tts",
            operation_id="session-1",
            operation_stage="result_tts",
        )
    )

    assert (audio, mime) == (b"audio", "audio/mpeg")
    assert [item[1]["provider"] for item in attempts] == ["custom", "azure"]
    assert [item[1]["success"] for item in attempts] == [False, True]
    assert {item[0]["operation_id"] for item in attempts} == {"session-1"}
    assert {item[0]["operation_stage"] for item in attempts} == {"result_tts"}
    assert metadata == {
        "operation_id": "session-1",
        "feature": "kiosk_match_review_tts",
    }


def test_unconfigured_tts_makes_no_provider_attempt_or_fund_entry(monkeypatch):
    custom = ai_model_config.TTS_PROVIDER_OPTIONS[ai_model_config.CUSTOM_TTS_PROVIDER]
    attempts = []
    monkeypatch.setattr(
        proxy,
        "_selected_tts_provider",
        lambda: (ai_model_config.CUSTOM_TTS_PROVIDER, custom),
    )
    monkeypatch.setattr(proxy, "_preprocess_tts_text", lambda value: value)
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "")

    async def record(*_args, **_kwargs):
        attempts.append(True)

    monkeypatch.setattr(proxy, "_record_tts_attempt", record)
    with pytest.raises(proxy.TtsUnavailable, match="not configured"):
        asyncio.run(
            proxy.synthesize_tts_accounted(
                "粵語評語",
                user_id="kiosk",
                feature="kiosk_match_review_tts",
                operation_id="session-1",
                operation_stage="result_tts",
            )
        )
    assert attempts == []


def test_both_tts_http_aliases_share_the_four_gb_provider_gate(monkeypatch):
    message = "本月網絡傳輸量已達4GB"
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: message)

    async def forbidden(_request):
        raise AssertionError("blocked TTS alias must not reach its endpoint")

    for path in ("/api/tts/azure", "/api/tts/synthesize"):
        request = type("Request", (), {"url": type("Url", (), {"path": path})()})()
        response = asyncio.run(
            proxy.enforce_essential_only_budget(request, forbidden)
        )
        assert response.status_code == 429
        assert message.encode("utf-8") in response.body


def test_schema_migration_and_ai_fund_ui_expose_operation_accounting():
    schema = (ROOT / "schema.py").read_text(encoding="utf-8")
    up = (
        ROOT
        / "migrations"
        / "20260714_0008_ai_usage_operation_metadata.up.sql"
    ).read_text(encoding="utf-8")
    down = (
        ROOT
        / "migrations"
        / "20260714_0008_ai_usage_operation_metadata.down.sql"
    ).read_text(encoding="utf-8")
    frontend = (ROOT / "frontend" / "ai_fund" / "index.html").read_text(
        encoding="utf-8"
    )
    funds_api = (ROOT / "api" / "funds_api.py").read_text(encoding="utf-8")

    for source in (schema, up):
        assert "billable_characters" in source
        assert "operation_id" in source
        assert "operation_stage" in source
        assert "kiosk_match_review_tts" in source
        assert "'tts'" in source
    assert "DROP COLUMN operation_stage" in down
    assert 'azure: "Azure Speech"' in frontend
    assert 'tts: "粵語語音合成"' in frontend
    assert 'kiosk_match_review_tts: "AI評判易·粵語讀出"' in frontend
    assert 'http_synthesis: "HTTP 語音合成／硬件測試"' in frontend
    assert 'room_synthesis: "AI 房間語音合成"' in frontend
    assert 'primary: "主要模型"' in frontend
    assert 'fallback: "後備模型"' in frontend
    assert '["任務／場次", (r) => r.tasks ?? r.uses]' in frontend
    assert '"Provider calls"' in frontend
    for field in ("billable_characters", "operation_id", "operation_stage"):
        assert field in funds_api
    assert '"任務ID","任務階段"' in funds_api
    assert '"任務數","成功呼叫","Provider呼叫","TTS計費字元"' in funds_api
