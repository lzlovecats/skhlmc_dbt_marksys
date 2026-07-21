"""Security, lifecycle and provider regressions for direct Solo Gemini Live."""

import asyncio
import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError

from api import ai_coach_api
from core import ai_provider, funds_logic, google_files, media_probe
import ai_model_config
import deploy.proxy as proxy
import system_limits


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_live_token_response_cache():
    proxy._clear_solo_live_token_response_cache()
    yield
    proxy._clear_solo_live_token_response_cache()


@pytest.fixture(autouse=True)
def _safe_bandwidth_default(monkeypatch):
    monkeypatch.setattr(proxy, "bandwidth_budget_status", lambda **_kwargs: {
        "total_bytes": 0,
        "warn_bytes": 3 * 1024**3,
        "stop_live_bytes": int(3.5 * 1024**3),
        "essential_only_bytes": 4 * 1024**3,
    })


def _request(country=None, host="testserver", query=b""):
    headers = []
    if country is not None:
        headers.append((b"cf-ipcountry", country.encode("ascii")))
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": query,
        "headers": headers, "scheme": "https", "server": (host, 443),
    })


def test_ai_coach_ui_prices_search_fallback_with_actual_default_model():
    source = (ROOT / "frontend/shared/ai-parity.js").read_text(encoding="utf-8")
    sync_model = source.split("function syncModel()", 1)[1].split(
        "async function prepareLive", 1,
    )[0]

    assert "const fallback = modelByLabel(meta?.external_default_model);" in source
    assert "if (model?.local_node) return null;" in source
    assert "const searchModel = effectiveSearchModel(model);" in sync_model
    assert 'searchEstimate("web_research")' in sync_model
    assert 'searchEstimate("fact_check")' in sync_model
    assert "下列估算已按替代模型顯示" in sync_model
    assert "Live 賽前搵料會自動改用" in sync_model
    assert "亦不會自動轉用雲端" in sync_model
    assert "!country.supported || !searchReady" in sync_model
    assert '$("researchForm"), $("factForm")' in sync_model


def test_speech_review_audio_estimate_includes_representative_audio_tokens():
    config = {
        "input_price_per_million": 0.30,
        "audio_input_price_per_million": 1.00,
        "output_price_per_million": 2.50,
    }

    text_only = ai_coach_api._estimate("speech_review", config)
    with_audio = ai_coach_api._estimate(
        "speech_review", config, has_audio=True
    )

    expected_usd = (
        2500 * 0.30
        + ai_coach_api.SPEECH_REVIEW_AUDIO_TOKEN_ESTIMATE * 1.00
        + 1800 * 2.50
    ) / 1_000_000
    assert with_audio == {
        "usd": round(expected_usd, 4),
        "hkd": round(expected_usd * 7.8, 4),
    }
    assert with_audio["usd"] > text_only["usd"]


def test_ai_coach_local_model_uses_selected_mode_without_cloud_fallback(monkeypatch):
    from core import lmc_ai_client

    captured = {}

    async def local(db, **kwargs):
        captured.update(db=db, **kwargs)
        kwargs["on_provider_attempt"]()
        return "自家回覆", {
            "input_tokens": 5,
            "output_tokens": 7,
            "cost_source": "local_zero_cost",
        }

    monkeypatch.setattr(lmc_ai_client, "generate_local_text", local)
    monkeypatch.setattr(proxy, "get_vote_db", lambda: "coach-db")
    attempted = []
    body = ai_coach_api.CoachRequest(
        feature="strategy", local_mode="deep",
    )
    config = ai_coach_api._config(ai_model_config.LMC_AI_INTERACTIVE_OPTION["label"])

    result, usage = asyncio.run(ai_coach_api._generate(
        config, "system", "user", body, "alice",
        on_provider_attempt=lambda: attempted.append(True),
    ))

    assert result == "自家回覆"
    assert usage["cost_source"] == "local_zero_cost"
    assert captured["db"] == "coach-db"
    assert captured["mode"] == "deep"
    assert attempted == [True]


def test_ai_coach_local_status_and_mode_gate_use_selected_runtime(monkeypatch):
    from core import lmc_ai_client

    async def status(_db):
        return {
            "available": True,
            "selected": True,
            "state": "online",
            "message": "自家 AI 已選用並在線。",
            "modes": [
                {
                    "id": "daily", "available": True,
                    "message": "自家 AI 已選用並在線。",
                },
                {
                    "id": "deep", "available": False,
                    "message": "目前選用的自家 AI 電腦未提供「深入思考」模式。",
                },
            ],
        }

    monkeypatch.setattr(lmc_ai_client, "local_ai_availability", status)
    monkeypatch.setattr(proxy, "get_vote_db", lambda: "coach-db")
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")

    response = asyncio.run(ai_coach_api.local_status(_request()))
    payload = json.loads(response.body)
    assert payload["available"] is True
    assert payload["selected"] is True
    assert response.headers["cache-control"] == "no-store"

    config = ai_coach_api._config(
        ai_model_config.LMC_AI_INTERACTIVE_OPTION["label"]
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(ai_coach_api._require_local_model_available(
            config, "coach-db", "deep",
        ))
    assert exc_info.value.status_code == 503
    assert "深入思考" in str(exc_info.value.detail)


def test_ai_coach_browser_disables_offline_local_and_unavailable_9b_mode():
    page = (ROOT / "frontend/ai_coach/index.html").read_text(encoding="utf-8")
    source = (ROOT / "frontend/shared/ai-parity.js").read_text(encoding="utf-8")
    prep = (ROOT / "frontend/shared/competition-prep.js").read_text(
        encoding="utf-8"
    )

    assert 'id="localAiStatus"' in page
    assert 'api("/api/ai-coach/local-status")' in source
    assert "localOption.disabled = !meta?.local_ai?.available" in source
    assert "option.disabled = !Boolean(" in source
    assert '$("localAiStatus").classList.toggle("hidden", !model.local_node)' in source
    assert "selectedModelStatus" in source
    assert 'setInterval(refreshLocalAiStatus, 10000)' in source
    assert "AICoachSelectedModelStatus" in prep
    assert 'document.addEventListener("ai-coach-model-status"' in prep


def test_speech_review_has_no_application_duration_or_two_mib_limit():
    source = (ROOT / "frontend/shared/ai-parity.js").read_text(encoding="utf-8")

    assert not hasattr(system_limits, "AI_COACH_MAX_AUDIO_SECONDS")
    assert not hasattr(system_limits, "AI_COACH_MAX_AUDIO_BYTES")
    assert "audioBitsPerSecond: 32_000" in source
    assert "recording-intent" in source and "recording-complete" in source
    assert "audio_base64" not in source
    assert "setTimeout(activeRecorder.stop" not in source
    assert ai_coach_api.CoachRequest(
        feature="speech_review",
        audio_duration_seconds=8 * 60 * 60,
        audio_intent_id="a" * 32,
    ).audio_duration_seconds == 8 * 60 * 60
    assert ai_coach_api.AudioIntentBody(
        mime_type="audio/webm", byte_size=3 * 1024 * 1024, sha256="a" * 64,
    ).byte_size > 2 * 1024 * 1024


def test_ai_coach_runtime_honours_provider_allowlist_and_default(monkeypatch):
    from core import config_store

    monkeypatch.setattr(
        config_store,
        "get_configs",
        lambda *_args, **_kwargs: {
            "ai_enabled_providers": ["openrouter"],
            "ai_default_model": "Haiku 4.5",
        },
    )
    providers, default_model = ai_coach_api._runtime_model_settings(object())
    assert providers == ("openrouter",)
    assert default_model == "Haiku 4.5"
    assert ai_coach_api._requested_model_label(
        ai_coach_api.CoachRequest(feature="strategy"), default_model,
    ) == "Haiku 4.5"

    disabled = ai_coach_api.AI_MODEL_OPTIONS["Gemini 2.5 Flash"]
    with pytest.raises(HTTPException, match="Provider"):
        ai_coach_api._require_enabled_model(
            "Gemini 2.5 Flash", disabled, providers,
        )


class _Result:
    def __init__(self, *, scalar_value=0, row=None):
        self._scalar = scalar_value
        self._row = row

    def scalar(self):
        return self._scalar

    def fetchone(self):
        return self._row


class _QuotaConnection:
    def __init__(self, engine):
        self.engine = engine
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).lower().split())
        params = params or {}
        if "set_config(" in sql:
            return _Result()
        if "pg_advisory_xact_lock" in sql:
            return _Result()
        if sql.startswith("select 1") and "left(error_message" in sql:
            match = next((row for row in self.engine.rows if (
                row["user"] == params["user"]
                and row["feature"] == params["feature"]
                and row["marker"].startswith(params["marker"])
            )), None)
            return _Result(row=(1,) if match else None)
        if sql.startswith("select error_message"):
            match = next((row for row in self.engine.rows if (
                row["user"] == params["user"]
                and row["feature"] == params["feature"]
                and row["marker"].startswith(params["marker"])
            )), None)
            return _Result(row=(match["marker"],) if match else None)
        if sql.startswith("select count"):
            matches = [row for row in self.engine.rows if row["feature"] == params["feature"]]
            if "user_id=:user" in sql:
                matches = [row for row in matches if row["user"] == params["user"]]
            return _Result(scalar_value=len(matches))
        if sql.startswith("insert into"):
            self.engine.insert_sql.append(sql)
            self.engine.rows.append({
                "user": params["user"], "feature": params["feature"],
                "marker": params["marker"],
            })
            return _Result()
        if sql.startswith("update"):
            match = next(row for row in self.engine.rows if (
                row["user"] == params["user"]
                and row["feature"] == params["feature"]
                and row["marker"].startswith(params["marker"])
            ))
            match["marker"] = params["new_marker"]
            return _Result()
        raise AssertionError(f"unexpected quota SQL: {sql}")


class _QuotaEngine:
    def __init__(self, rows=None):
        import threading

        self.rows = list(rows or [])
        self.insert_sql = []
        self.lock = threading.Lock()

    @contextmanager
    def begin(self):
        with self.lock:
            snapshot = [dict(row) for row in self.rows]
            connection = _QuotaConnection(self)
            yield connection
            if connection.rolled_back:
                self.rows[:] = snapshot


def _claim(user, practice, mode="free", seconds=None):
    return {
        "user_id": user,
        "practice_id": practice,
        "mode": mode,
        "session_seconds": list(seconds or [600]),
    }


def _install_quota_engine(monkeypatch, engine):
    monkeypatch.setattr(proxy, "_get_db_engine", lambda: engine)
    monkeypatch.setattr(proxy, "get_vote_db", lambda: object())
    monkeypatch.setattr(funds_logic, "prune_ai_usage", lambda _db: None)


def test_free_practices_have_no_usage_count_cap_and_practice_id_is_idempotent(monkeypatch):
    engine = _QuotaEngine()
    _install_quota_engine(monkeypatch, engine)
    first = _claim("alice", "practice_free_001")
    assert proxy._reserve_solo_live_slot(first) is None
    assert proxy._reserve_solo_live_slot(first) is None
    for index in range(2, 22):
        assert proxy._reserve_solo_live_slot(
            _claim("alice", f"practice_free_{index:03d}"),
        ) is None
    assert len(engine.rows) == 21
    assert all("gemini_live_token_reservation" in sql for sql in engine.insert_sql)
    assert all("relay" not in sql for sql in engine.insert_sql)


def test_mock_practices_have_no_weekly_or_monthly_count_cap(monkeypatch):
    engine = _QuotaEngine()
    _install_quota_engine(monkeypatch, engine)
    for index in range(20):
        assert proxy._reserve_solo_live_slot(
            _claim("alice", f"practice_mock_{index:03d}", "mock", [300, 600]),
        ) is None
    assert len(engine.rows) == 20


def test_solo_lifecycle_marker_is_concurrency_idempotent_without_global_cap(monkeypatch):
    race_engine = _QuotaEngine()
    _install_quota_engine(monkeypatch, race_engine)
    same = _claim("racer", "practice_free_race")
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(lambda _index: proxy._reserve_solo_live_slot(same), range(10)))
    assert results == [None] * 10
    assert len(race_engine.rows) == 1

    unique_engine = _QuotaEngine()
    _install_quota_engine(monkeypatch, unique_engine)
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(
            lambda index: proxy._reserve_solo_live_slot(
                _claim("racer", f"practice_free_{index:03d}"),
            ),
            range(10),
        ))
    assert results == [None] * 10
    assert len(unique_engine.rows) == 10


def test_mock_ledger_binds_claim_order_schedule_and_one_absolute_deadline(monkeypatch):
    now = [10_000]
    monkeypatch.setattr(proxy.time, "time", lambda: now[0])
    engine = _QuotaEngine()
    _install_quota_engine(monkeypatch, engine)
    claim = {
        **_claim("alice", "practice_mock_bound", "mock", [300, 300, 300]),
        "system_prompt": "server prompt",
    }

    assert proxy._reserve_solo_live_slot(claim, started_at=now[0]) is None
    state = proxy._solo_live_practice_state(claim)
    assert state["claim_matches"] is True
    assert state["lifecycle_matches"] is True
    assert state["issued"] == {0}
    assert state["started_at"] == 10_000
    assert state["deadline_at"] == (
        10_000 + 900 + system_limits.LIVE_MOCK_OVERALL_GRACE_SECONDS
    )

    _state, out_of_order = proxy._solo_live_token_gate(claim, 2, now_epoch=10_000)
    assert "次序" in out_of_order
    _state, too_early = proxy._solo_live_token_gate(claim, 1, now_epoch=10_001)
    assert "尚未到" in too_early
    now[0] = 10_300
    assert proxy._solo_live_token_gate(claim, 1, now_epoch=now[0])[1] is None
    assert proxy._mark_solo_live_token_issued(claim, 1) is True
    assert proxy._solo_live_token_issued(claim, 1) is True
    assert "尚未到" in proxy._solo_live_token_gate(claim, 2, now_epoch=now[0])[1]

    altered = {**claim, "system_prompt": "different prompt"}
    altered_state = proxy._solo_live_practice_state(altered)
    assert altered_state["claim_matches"] is False
    assert "不一致" in proxy._solo_live_token_gate(
        altered, 2, now_epoch=10_600,
    )[1]
    assert "伺服器時限" in proxy._solo_live_token_gate(
        claim, 2, now_epoch=state["deadline_at"] - 1,
    )[1]


def test_mock_jit_out_of_order_is_rejected_before_provider_mint(monkeypatch):
    now = 20_000
    monkeypatch.setattr(proxy.time, "time", lambda: now)
    engine = _QuotaEngine()
    _install_quota_engine(monkeypatch, engine)
    claim = {
        **_claim("alice", "practice_mock_order", "mock", [300, 300, 300]),
        "system_prompt": "server prompt", "exp": now + 7_200,
    }
    assert proxy._reserve_solo_live_slot(claim, started_at=now) is None
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(
        proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: claim,
    )
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(
        proxy, "_mint_gemini_live_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("out-of-order JIT must fail before provider mint"),
        ),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.mint_live_token(
            ai_coach_api.LiveTokenRequest(
                practice_id="x" * 40, session_index=2,
            ),
            _request("US"),
        ))
    assert raised.value.status_code == 409
    assert "次序" in raised.value.detail


def test_mock_next_index_cross_worker_mark_has_one_winner(monkeypatch):
    now = [30_000]
    monkeypatch.setattr(proxy.time, "time", lambda: now[0])
    engine = _QuotaEngine()
    _install_quota_engine(monkeypatch, engine)
    claim = {
        **_claim("alice", "practice_mock_race", "mock", [300, 300]),
        "system_prompt": "server prompt",
    }
    assert proxy._reserve_solo_live_slot(claim, started_at=now[0] - 300) is None
    now[0] += 300
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _index: proxy._mark_solo_live_token_issued(claim, 1),
            range(2),
        ))
    assert sorted(results) == [False, True]
    assert proxy._solo_live_practice_state(claim)["issued"] == {0, 1}


def test_delayed_mock_section_cannot_batch_mint_later_sections(monkeypatch):
    now = [10_000]
    monkeypatch.setattr(proxy.time, "time", lambda: now[0])
    engine = _QuotaEngine()
    _install_quota_engine(monkeypatch, engine)
    claim = {
        **_claim("alice", "practice_mock_delayed", "mock", [300, 300, 300]),
        "system_prompt": "server prompt",
    }
    assert proxy._reserve_solo_live_slot(claim, started_at=now[0]) is None

    # The absolute section-1 threshold is already in the past, but issuing it
    # late must start a fresh server cadence for section 2.
    now[0] += 400
    assert proxy._mark_solo_live_token_issued(claim, 1) is True
    state = proxy._solo_live_practice_state(claim)
    assert state["last_issued_at"] == now[0]
    assert "尚未到" in proxy._solo_live_gate_from_state(
        claim, 2, state, now_epoch=now[0] + 1,
    )
    assert proxy._solo_live_gate_from_state(
        claim, 2, state, now_epoch=now[0] + 300,
    ) is None


def test_identity_only_reload_guard_matches_enriched_reserved_claim(monkeypatch):
    now = 20_000
    monkeypatch.setattr(proxy.time, "time", lambda: now)
    engine = _QuotaEngine()
    _install_quota_engine(monkeypatch, engine)
    planned = {
        **_claim("alice", "practice_free_reload", "free", [600]),
        "system_prompt": "brief-backed server prompt",
    }
    launch = {
        "user_id": "alice", "practice_id": "practice_free_reload",
        "mode": "free", "session_seconds": [], "system_prompt": "",
    }
    assert proxy._reserve_solo_live_slot(planned, started_at=now) is None
    assert proxy._solo_live_practice_reserved(launch) is False
    assert proxy._solo_live_practice_exists(launch) is True

    requested_pages = []
    monkeypatch.setattr(
        proxy, "require_page_user",
        lambda _request, page: requested_pages.append(page) or "alice",
    )
    monkeypatch.setattr(
        proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: launch,
    )
    response = asyncio.run(proxy.appliance_ai_debate_live(_request(
        "US", query=b"mode=free&topic=test&practice_id=signed-claim",
    )))
    assert "練習憑證已簽發" in response.body.decode("utf-8")
    assert requested_pages == ["ai_coach"]


def test_delivery_window_failure_rolls_back_initial_ledger(monkeypatch):
    engine = _QuotaEngine()
    _install_quota_engine(monkeypatch, engine)
    claim = _claim("alice", "practice_free_delivery")
    error, created = proxy._reserve_solo_live_slot(
        claim,
        report_created=True,
        before_insert=lambda: None,
        after_insert=lambda: "provider start window expired",
    )
    assert (error, created) == ("provider start window expired", False)
    assert engine.rows == []


def test_endpoint_never_returns_or_caches_token_after_delivery_window(monkeypatch):
    claim = {
        "user_id": "alice", "mode": "free",
        "practice_id": "practice_free_slow_ledger",
        "session_seconds": [300], "system_prompt": "locked prompt",
        "exp": int(proxy.time.time()) + 7200,
    }
    monotonic_now = [0.0]
    monkeypatch.setattr(proxy.time, "monotonic", lambda: monotonic_now[0])
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(
        proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: claim,
    )
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(proxy, "_solo_live_practice_exists", lambda _claim: False)
    monkeypatch.setattr(proxy, "_practice_live_rate_check", lambda _user: None)
    monkeypatch.setattr(
        proxy, "_mint_gemini_live_token",
        lambda *_args, **_kwargs: ("slow-token", None),
    )

    def reserve(_claim, **kwargs):
        assert kwargs["before_insert"]() is None
        monotonic_now[0] = (
            system_limits.LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS
            - system_limits.LIVE_TOKEN_RESPONSE_CACHE_SAFETY_SECONDS
        )
        error = kwargs["after_insert"]()
        return error, False

    monkeypatch.setattr(proxy, "_reserve_solo_live_slot", reserve)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.mint_live_token(
            ai_coach_api.LiveTokenRequest(
                practice_id="x" * 40, session_index=0,
            ),
            _request("US"),
        ))
    assert raised.value.status_code == 502
    assert "未有建立練習記錄" in raised.value.detail
    assert proxy._get_cached_solo_live_token(claim, 0) == ""


def test_solo_quota_exemption_contract_is_fully_removed():
    assert not hasattr(proxy, "_solo_quota_exempt")
    assert not hasattr(proxy, "_solo_live_quota_error")
    source = "\n".join((
        (ROOT / "api/admin_console_api.py").read_text(encoding="utf-8"),
        (ROOT / "frontend/dev_settings/index.html").read_text(encoding="utf-8"),
        (ROOT / "system_limits.py").read_text(encoding="utf-8"),
    ))
    assert "solo_quota_exemptions" not in source


def test_ephemeral_token_is_single_use_short_start_and_constrained(monkeypatch):
    captured = {}

    class _AuthTokens:
        def create(self, *, config):
            captured.update(config)
            return SimpleNamespace(name="ephemeral-token")

    class _Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.auth_tokens = _AuthTokens()

    from google import genai

    monkeypatch.setattr(genai, "Client", _Client)
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda key, default="": "server-key")
    before = datetime.datetime.now(datetime.timezone.utc)
    token, error = proxy._mint_gemini_live_token(30, system_prompt="server prompt")
    after = datetime.datetime.now(datetime.timezone.utc)

    assert (token, error) == ("ephemeral-token", None)
    assert captured["uses"] == 1
    assert 0 < (captured["new_session_expire_time"] - before).total_seconds() <= 61
    assert (captured["expire_time"] - after).total_seconds() >= 30 * 60
    assert (
        captured["expire_time"] - captured["new_session_expire_time"]
    ).total_seconds() >= (
        system_limits.LIVE_FREE_SESSION_MAX_SECONDS
        + system_limits.LIVE_TOKEN_EXPIRY_GRACE_SECONDS
    )
    assert captured["lock_additional_fields"] == []
    assert captured["client"]["http_options"] == {
        "api_version": "v1alpha",
        "timeout": system_limits.LIVE_TOKEN_MINT_TIMEOUT_SECONDS * 1000,
    }
    constraints = captured["live_connect_constraints"]
    assert constraints["model"] == proxy.FREE_DEBATE_LIVE_MODEL
    config = constraints["config"]
    assert config["response_modalities"] == ["AUDIO"]
    assert config["system_instruction"]["parts"][0]["text"] == "server prompt"
    assert config["session_resumption"] == {}
    assert config["context_window_compression"] == {
        "trigger_tokens": system_limits.LIVE_CONTEXT_COMPRESSION_TRIGGER_TOKENS,
        "sliding_window": {
            "target_tokens": system_limits.LIVE_CONTEXT_COMPRESSION_TARGET_TOKENS,
        },
    }


def test_ephemeral_token_returned_after_start_window_is_discarded(monkeypatch):
    start = datetime.datetime(2026, 7, 14, tzinfo=datetime.timezone.utc)
    clock = iter([
        start,
        start + datetime.timedelta(
            seconds=system_limits.LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS,
        ),
    ])

    class _AuthTokens:
        def create(self, *, config):
            return SimpleNamespace(name="already-stale-token")

    class _Client:
        def __init__(self, **_kwargs):
            self.auth_tokens = _AuthTokens()

    from google import genai

    monkeypatch.setattr(genai, "Client", _Client)
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "key")
    monkeypatch.setattr(proxy, "_live_token_now_utc", lambda: next(clock))
    token, error = proxy._mint_gemini_live_token(
        30, system_prompt="server prompt",
    )
    assert token is None
    assert "逾時" in error


def test_ephemeral_token_expiry_respects_practice_absolute_cap(monkeypatch):
    captured = {}

    class _AuthTokens:
        def create(self, *, config):
            captured.update(config)
            return SimpleNamespace(name="capped-token")

    class _Client:
        def __init__(self, **_kwargs):
            self.auth_tokens = _AuthTokens()

    from google import genai

    monkeypatch.setattr(genai, "Client", _Client)
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "key")
    cap = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
    token, error = proxy._mint_gemini_live_token(
        30, system_prompt="server prompt", absolute_expire_at=cap,
    )
    assert (token, error) == ("capped-token", None)
    assert captured["expire_time"] <= cap
    assert captured["new_session_expire_time"] < captured["expire_time"]


def test_gemini_rest_keys_are_headers_never_urls(monkeypatch):
    secret = "gemini-super-secret"
    coach_call = {}
    rag_call = {}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def coach_post(_client, url, **kwargs):
        coach_call.update(url=url, kwargs=kwargs)
        return {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {},
        }

    monkeypatch.setattr(ai_provider.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ai_provider, "post_json_bounded", coach_post)
    text_value, _usage = asyncio.run(ai_provider.generate_text(
        {"provider": "gemini", "model": "gemini-test"},
        "system", "user", api_key=secret,
    ))
    assert text_value == "ok"
    assert secret not in coach_call["url"]
    assert "key=" not in coach_call["url"]
    assert coach_call["kwargs"]["headers"] == {"x-goog-api-key": secret}
    assert "params" not in coach_call["kwargs"]
    assert "maxOutputTokens" not in coach_call["kwargs"]["json"]["generationConfig"]

    from core import rag

    async def rag_post(_client, url, **kwargs):
        rag_call.update(url=url, kwargs=kwargs)
        return {"embedding": {"values": [0.0] * rag.EMBEDDING_DIMENSION}}

    monkeypatch.setattr(rag.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(rag, "post_json_bounded", rag_post)
    vector = asyncio.run(rag._embed("query", secret))
    assert len(vector) == rag.EMBEDDING_DIMENSION
    assert secret not in rag_call["url"]
    assert rag_call["kwargs"]["headers"] == {"x-goog-api-key": secret}
    assert "params" not in rag_call["kwargs"]


def test_provider_generation_overrides_are_bounded_without_token_ceiling(monkeypatch):
    captured = {"timeouts": [], "payloads": []}

    class _Client:
        def __init__(self, *, timeout):
            captured["timeouts"].append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def post(_client, _url, **kwargs):
        captured["payloads"].append(kwargs["json"])
        return {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "complete"}]},
            }],
            "usageMetadata": {},
        }

    monkeypatch.setattr(ai_provider.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ai_provider, "post_json_bounded", post)

    result, _usage = asyncio.run(ai_provider.generate_text(
        {"provider": "gemini", "model": "gemini-test"},
        "S" * 10,
        "U" * 10,
        api_key="key",
        max_prompt_chars=12,
        timeout_seconds=45,
        temperature=None,
        require_complete=True,
    ))
    assert result == "complete"
    first = captured["payloads"][0]
    assert first["system_instruction"]["parts"][0]["text"] == "S" * 10
    assert first["contents"][0]["parts"][0]["text"] == "U" * 2
    assert "generationConfig" not in first
    assert captured["timeouts"][0] == 45

    asyncio.run(ai_provider.generate_text(
        {"provider": "gemini", "model": "gemini-test"},
        "S" * 300_000,
        "U" * 300_000,
        api_key="key",
        max_prompt_chars=999_999,
        timeout_seconds=999_999,
        temperature=999,
    ))
    second = captured["payloads"][1]
    system_text = second["system_instruction"]["parts"][0]["text"]
    user_text = second["contents"][0]["parts"][0]["text"]
    assert len(system_text) + len(user_text) == 250_000
    assert second["generationConfig"] == {"temperature": 2.0}
    assert captured["timeouts"][1] == 300


def test_gemini_require_complete_rejects_truncated_text():
    response = {
        "candidates": [{
            "finishReason": "MAX_TOKENS",
            "content": {"parts": [{"text": "partial transcript"}]},
        }],
    }

    assert ai_provider._gemini_text(response) == "partial transcript"
    with pytest.raises(ValueError, match="incomplete"):
        ai_provider._gemini_text(response, require_complete=True)


@pytest.mark.parametrize("search_calls", [0, 1, 3])
def test_openrouter_usage_preserves_exact_provider_search_count(search_calls):
    usage = ai_provider._usage({
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "server_tool_use": {"web_search_requests": search_calls},
        },
    }, "openrouter", web_search=True)
    assert usage == {
        "input_tokens": 12,
        "output_tokens": 4,
        "audio_tokens": 0,
        "search_calls": search_calls,
        "cost_source": "openrouter_response_usage",
    }


def test_ai_coach_ledger_respects_explicit_zero_usage(monkeypatch):
    ledger = []

    def log(*_args, **kwargs):
        ledger.append(kwargs["usage"])

    monkeypatch.setattr(funds_logic, "log_ai_usage", log)
    ai_coach_api._usage(
        object(), "alice", "web_research", "OpenRouter", {
            "provider": "openrouter",
            "input_price_per_million": 1,
            "output_price_per_million": 1,
            "web_search_price_per_call": 0.01,
        }, True, actual={
            "input_tokens": 0,
            "output_tokens": 0,
            "audio_tokens": 0,
            "search_calls": 0,
            "cost_source": "openrouter_response_usage",
        },
    )
    assert len(ledger) == 1
    assert ledger[0]["input_tokens"] == 0
    assert ledger[0]["output_tokens"] == 0
    assert ledger[0]["audio_tokens"] == 0
    assert ledger[0]["search_calls"] == 0
    assert ledger[0]["estimated_cost_usd"] == 0


def test_ai_coach_ledger_estimates_audio_when_provider_omits_audio_usage(monkeypatch):
    ledger = []
    monkeypatch.setattr(
        funds_logic,
        "log_ai_usage",
        lambda *_args, **kwargs: ledger.append(kwargs["usage"]),
    )

    ai_coach_api._usage(
        object(), "alice", "speech_review", "Gemini", {
            "provider": "gemini",
            "input_price_per_million": 0.30,
            "audio_input_price_per_million": 1.00,
            "output_price_per_million": 2.50,
        }, True, actual={
            "input_tokens": 20,
            "output_tokens": 10,
            "cost_source": "provider_usage_without_audio_breakdown",
        }, has_audio=True,
    )

    assert ledger[0]["audio_tokens"] == (
        ai_coach_api.SPEECH_REVIEW_AUDIO_TOKEN_ESTIMATE
    )


def test_openrouter_web_search_payload_uses_bounded_result_caps(monkeypatch):
    captured = {}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def post(_client, _url, **kwargs):
        captured.update(kwargs["json"])
        return {
            "choices": [{"message": {"content": "有來源的結果"}}],
            "usage": {"server_tool_use": {"web_search_requests": 0}},
        }

    monkeypatch.setattr(ai_provider.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ai_provider, "post_json_bounded", post)
    asyncio.run(ai_provider.generate_text(
        {"provider": "openrouter", "model": "model"},
        "system", "question", api_key="key", web_search=True,
    ))
    parameters = captured["tools"][0]["parameters"]
    assert "max_tokens" not in captured
    assert parameters["max_results"] == system_limits.OPENROUTER_WEB_SEARCH_MAX_RESULTS
    assert parameters["max_total_results"] == system_limits.OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS
    assert parameters["max_total_results"] >= parameters["max_results"]


def test_mock_jit_token_requires_reserved_practice_and_does_not_reserve_again(monkeypatch):
    claim = {
        "user_id": "alice", "mode": "mock", "practice_id": "practice_mock_001",
        "session_seconds": [300, 600], "system_prompt": "locked prompt",
        "exp": int(proxy.time.time()) + 7200,
    }
    captured = {}
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: claim)
    monkeypatch.setattr(proxy, "_solo_live_practice_reserved", lambda _claim: True)
    monkeypatch.setattr(proxy, "_solo_live_token_issued", lambda _claim, _index: False)
    monkeypatch.setattr(proxy, "_solo_live_practice_state", lambda _claim: None)
    def mark(_claim, _index, *, report_reason=False, before_update=None,
             after_update=None):
        assert report_reason is True
        assert before_update() is None
        assert after_update() is None
        return True, None, {}

    monkeypatch.setattr(proxy, "_mark_solo_live_token_issued", mark)
    monkeypatch.setattr(proxy, "_practice_live_rate_check", lambda _user: None)
    monkeypatch.setattr(
        proxy, "_reserve_solo_live_slot",
        lambda _claim: (_ for _ in ()).throw(AssertionError("JIT must not consume quota")),
    )

    def mint(duration, **kwargs):
        captured.update(duration=duration, **kwargs)
        return "jit-token", None

    monkeypatch.setattr(proxy, "_mint_gemini_live_token", mint)
    response = asyncio.run(ai_coach_api.mint_live_token(
        ai_coach_api.LiveTokenRequest(practice_id="x" * 40, session_index=1),
        _request("US"),
    ))
    result = json.loads(response.body)
    assert result == {"token": "jit-token", "session_index": 1}
    assert response.headers["cache-control"] == "no-store"
    assert captured["duration"] == pytest.approx(10)
    assert captured["system_prompt"] == "locked prompt"


@pytest.mark.parametrize(
    ("mode", "sessions", "expected_minutes"),
    [("free", [300], 30), ("mock", [300, 600], 5)],
)
def test_initial_token_mints_and_reserves_only_on_start_with_same_token_retry(
    monkeypatch, mode, sessions, expected_minutes,
):
    claim = {
        "user_id": "alice", "mode": mode,
        "practice_id": f"practice_{mode}_001",
        "session_seconds": sessions, "system_prompt": "locked prompt",
        "exp": int(proxy.time.time()) + 7200,
    }
    issued = set()
    reserved = False
    events = []
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: claim)
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(proxy, "_practice_live_rate_check", lambda _user: None)
    monkeypatch.setattr(proxy, "_solo_live_practice_reserved", lambda _claim: reserved)
    monkeypatch.setattr(proxy, "_solo_live_practice_exists", lambda _claim: reserved)
    monkeypatch.setattr(
        proxy, "_solo_live_token_issued", lambda _claim, index: index in issued,
    )

    def mint(duration, **kwargs):
        events.append(("mint", duration, kwargs["system_prompt"]))
        return f"initial-{mode}-token", None

    def reserve(_claim, *, report_created=False, started_at=None,
                before_insert=None, after_insert=None):
        nonlocal reserved
        assert report_created is True
        assert isinstance(started_at, int)
        assert before_insert() is None
        events.append(("reserve",))
        reserved = True
        issued.add(0)
        assert after_insert() is None
        return None, True

    monkeypatch.setattr(proxy, "_mint_gemini_live_token", mint)
    monkeypatch.setattr(proxy, "_reserve_solo_live_slot", reserve)

    async def scenario():
        body = ai_coach_api.LiveTokenRequest(practice_id="x" * 40, session_index=0)
        first = await ai_coach_api.mint_live_token(body, _request("US"))
        second = await ai_coach_api.mint_live_token(body, _request("US"))
        return json.loads(first.body), json.loads(second.body)

    first, second = asyncio.run(scenario())
    expected = {"token": f"initial-{mode}-token", "session_index": 0}
    assert first == second == expected
    assert events[0] == ("mint", pytest.approx(expected_minutes), "locked prompt")
    assert events[1:] == [("reserve",)]

    proxy._clear_solo_live_token_response_cache()
    body = ai_coach_api.LiveTokenRequest(practice_id="x" * 40, session_index=0)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.mint_live_token(body, _request("US")))
    assert raised.value.status_code == 409
    assert len(events) == 2


def test_initial_cross_worker_loser_never_discloses_or_caches_extra_token(monkeypatch):
    claim = {
        "user_id": "alice", "mode": "free", "practice_id": "practice_free_001",
        "session_seconds": [300], "system_prompt": "locked prompt",
        "exp": int(proxy.time.time()) + 7200,
    }
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: claim)
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(proxy, "_practice_live_rate_check", lambda _user: None)
    monkeypatch.setattr(proxy, "_solo_live_practice_exists", lambda _claim: False)
    monkeypatch.setattr(proxy, "_solo_live_token_issued", lambda *_args: False)
    monkeypatch.setattr(
        proxy, "_mint_gemini_live_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cross-worker loser must not provision a token"),
        ),
    )
    monkeypatch.setattr(
        proxy, "_reserve_solo_live_slot",
        lambda _claim, **_kwargs: (None, False),
    )
    body = ai_coach_api.LiveTokenRequest(practice_id="x" * 40, session_index=0)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.mint_live_token(body, _request("US")))
    assert raised.value.status_code == 409
    assert proxy._get_cached_solo_live_token(claim, 0) == ""


def test_initial_digest_mismatch_is_rejected_by_identity_before_provider(monkeypatch):
    now = int(proxy.time.time())
    engine = _QuotaEngine()
    _install_quota_engine(monkeypatch, engine)
    reserved_claim = {
        "user_id": "alice", "mode": "free",
        "practice_id": "practice_free_same_identity",
        "session_seconds": [300], "system_prompt": "brief A",
    }
    stale_claim = {
        **reserved_claim,
        "system_prompt": "brief was already consumed",
        "exp": now + 7200,
    }
    assert proxy._reserve_solo_live_slot(
        reserved_claim, started_at=now,
    ) is None
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(
        proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: stale_claim,
    )
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(
        proxy, "_mint_gemini_live_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing identity must reject before provider"),
        ),
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.mint_live_token(
            ai_coach_api.LiveTokenRequest(
                practice_id="x" * 40, session_index=0,
            ),
            _request("US"),
        ))
    assert raised.value.status_code == 409


def test_initial_token_rejects_claim_that_cannot_cover_full_start_lifecycle(monkeypatch):
    now = 10_000
    sessions = [300, 600]
    claim = {
        "user_id": "alice", "mode": "mock", "practice_id": "practice_mock_001",
        "session_seconds": sessions, "system_prompt": "locked prompt",
        # One second short of planned Mock + ten-minute grace + safe start window.
        "exp": now + sum(sessions) + 600
        + system_limits.LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS - 1,
    }
    monkeypatch.setattr(proxy.time, "time", lambda: now)
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(
        proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: claim,
    )
    monkeypatch.setattr(
        proxy, "_mint_gemini_live_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("short-lived claim must fail before provider mint"),
        ),
    )
    monkeypatch.setattr(
        proxy, "_reserve_solo_live_slot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("short-lived claim must not create lifecycle row"),
        ),
    )

    body = ai_coach_api.LiveTokenRequest(practice_id="x" * 40, session_index=0)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.mint_live_token(body, _request("US")))

    assert raised.value.status_code == 409
    assert "未有建立練習記錄" in raised.value.detail


@pytest.mark.parametrize("mode", ["free", "mock"])
def test_initial_live_html_contains_claim_but_never_mints_or_reserves(monkeypatch, mode):
    launch = {
        "user_id": "alice", "mode": mode, "practice_id": f"practice_{mode}_001",
        "session_seconds": [], "system_prompt": "", "exp": 0,
    }
    monkeypatch.setattr(
        proxy, "require_page_user", lambda _request, _page: "alice",
    )
    monkeypatch.setattr(proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: launch)
    monkeypatch.setattr(proxy, "_solo_live_practice_exists", lambda _claim: False)
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(proxy, "_planned_live_practice_claim", lambda *_args: "planned-claim")
    monkeypatch.setattr(ai_coach_api, "consume_live_brief", lambda *_args: "")
    monkeypatch.setattr(
        proxy, "_practice_live_rate_check",
        lambda *_args: (_ for _ in ()).throw(AssertionError("GET must not consume rate hit")),
    )
    monkeypatch.setattr(
        proxy, "_mint_gemini_live_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("GET must not mint")),
    )
    monkeypatch.setattr(
        proxy, "_reserve_solo_live_slot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("GET must not reserve")),
    )
    response = asyncio.run(proxy.appliance_ai_debate_live(_request(
        "US", query=f"mode={mode}&topic=test&practice_id=signed-claim".encode(),
    )))
    html = response.body.decode("utf-8")
    assert 'const LIVE_PRACTICE_ID = "planned-claim"' in html
    assert 'const LIVE_TOKEN_URL = "/api/ai-coach/live-token"' in html
    assert "__LIVE_TOKEN__" not in html and "const LIVE_TOKEN =" not in html


def test_live_token_response_cache_is_ttl_bounded(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(proxy.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(proxy, "LIVE_TOKEN_RESPONSE_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(proxy, "LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS", 45)
    claims = [
        {"user_id": "alice", "practice_id": f"practice_{index:02d}"}
        for index in range(3)
    ]
    for index, claim in enumerate(claims):
        now[0] += 1
        proxy._cache_solo_live_token(claim, 0, f"token-{index}")
    assert len(proxy._solo_live_token_response_cache) == 2
    assert proxy._get_cached_solo_live_token(claims[0], 0) == ""
    assert proxy._get_cached_solo_live_token(claims[2], 0) == "token-2"
    now[0] += 46
    assert proxy._get_cached_solo_live_token(claims[2], 0) == ""


def test_unicode_live_prompt_roundtrips_with_one_canonical_cap(monkeypatch):
    long_prompt = "長" * (system_limits.LIVE_SYSTEM_PROMPT_MAX_CHARS + 500)
    bounded = proxy._bounded_live_system_prompt(long_prompt)
    assert len(bounded) == system_limits.LIVE_SYSTEM_PROMPT_MAX_CHARS
    monkeypatch.setattr(proxy, "_get_relay_cookie_secret", lambda: "secret")
    signed = proxy._sign_live_practice_claim(
        "alice", "mock", practice_id="practice_mock_001",
        session_seconds=[300, 600], system_prompt=long_prompt,
    )
    assert len(signed) <= system_limits.LIVE_PRACTICE_CLAIM_MAX_CHARS
    claim = proxy._verify_live_practice_claim(
        signed, expected_user_id="alice", expected_mode="mock",
    )
    assert claim["system_prompt"] == bounded


def test_lost_jit_response_retry_returns_same_token_without_second_mint(monkeypatch):
    claim = {
        "user_id": "alice", "mode": "mock", "practice_id": "practice_mock_001",
        "session_seconds": [300, 600], "system_prompt": "locked prompt",
    }
    issued = set()
    mint_calls = []
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: claim)
    monkeypatch.setattr(proxy, "_solo_live_practice_reserved", lambda _claim: True)
    monkeypatch.setattr(
        proxy, "_solo_live_token_issued",
        lambda _claim, index: index in issued,
    )
    monkeypatch.setattr(proxy, "_solo_live_practice_state", lambda _claim: None)
    def mark(_claim, index, *, report_reason=False, before_update=None,
             after_update=None):
        assert report_reason is True
        if index in issued:
            return False, "already issued", {}
        error = before_update()
        if error:
            return False, error, {}
        error = after_update()
        if error:
            return False, error, {}
        issued.add(index)
        return True, None, {}

    monkeypatch.setattr(proxy, "_mark_solo_live_token_issued", mark)
    monkeypatch.setattr(proxy, "_practice_live_rate_check", lambda _user: None)

    def mint(*_args, **_kwargs):
        mint_calls.append(1)
        return "one-token", None

    monkeypatch.setattr(proxy, "_mint_gemini_live_token", mint)

    async def scenario():
        body = ai_coach_api.LiveTokenRequest(practice_id="x" * 40, session_index=1)
        first = await ai_coach_api.mint_live_token(body, _request("US"))
        second = await ai_coach_api.mint_live_token(body, _request("US"))
        return json.loads(first.body), json.loads(second.body)

    first, second = asyncio.run(scenario())
    assert first == second == {"token": "one-token", "session_index": 1}
    assert len(mint_calls) == 1


def test_expired_jit_retry_cache_never_remints_issued_section(monkeypatch):
    claim = {
        "user_id": "alice", "mode": "mock", "practice_id": "practice_mock_001",
        "session_seconds": [300, 600], "system_prompt": "locked prompt",
    }
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: claim)
    monkeypatch.setattr(proxy, "_solo_live_practice_reserved", lambda _claim: True)
    monkeypatch.setattr(proxy, "_solo_live_token_issued", lambda _claim, _index: True)
    monkeypatch.setattr(
        proxy, "_mint_gemini_live_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not remint")),
    )
    body = ai_coach_api.LiveTokenRequest(practice_id="x" * 40, session_index=1)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.mint_live_token(body, _request("US")))
    assert raised.value.status_code == 409
    assert "安全重試時限已過" in raised.value.detail


def test_jit_provider_failure_can_retry_without_duplicate_marker(monkeypatch):
    claim = {
        "user_id": "alice", "mode": "mock", "practice_id": "practice_mock_001",
        "session_seconds": [300, 600], "system_prompt": "locked prompt",
    }
    issued = set()
    attempts = 0
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: claim)
    monkeypatch.setattr(proxy, "_solo_live_practice_reserved", lambda _claim: True)
    monkeypatch.setattr(proxy, "_solo_live_token_issued", lambda _claim, index: index in issued)
    monkeypatch.setattr(proxy, "_solo_live_practice_state", lambda _claim: None)
    def mark(_claim, index, *, report_reason=False, before_update=None,
             after_update=None):
        assert report_reason is True
        if index in issued:
            return False, "already issued", {}
        error = before_update()
        if error:
            return False, error, {}
        error = after_update()
        if error:
            return False, error, {}
        issued.add(index)
        return True, None, {}

    monkeypatch.setattr(proxy, "_mark_solo_live_token_issued", mark)
    monkeypatch.setattr(proxy, "_practice_live_rate_check", lambda _user: None)

    def mint(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return (None, "provider failed") if attempts == 1 else ("retry-token", None)

    monkeypatch.setattr(proxy, "_mint_gemini_live_token", mint)

    async def scenario():
        body = ai_coach_api.LiveTokenRequest(practice_id="x" * 40, session_index=1)
        with pytest.raises(HTTPException) as raised:
            await ai_coach_api.mint_live_token(body, _request("US"))
        assert raised.value.status_code == 502
        return await ai_coach_api.mint_live_token(body, _request("US"))

    response = asyncio.run(scenario())
    assert json.loads(response.body)["token"] == "retry-token"
    assert attempts == 2 and issued == {1}


def test_reload_of_reserved_initial_practice_never_mints_again(monkeypatch):
    launch = {
        "user_id": "alice", "mode": "free", "practice_id": "practice_free_001",
        "session_seconds": [], "system_prompt": "",
    }
    monkeypatch.setattr(
        proxy, "require_page_user", lambda _request, _page: "alice",
    )
    monkeypatch.setattr(proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: launch)
    monkeypatch.setattr(proxy, "_solo_live_practice_exists", lambda _claim: True)
    monkeypatch.setattr(
        proxy, "_mint_gemini_live_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("reload remint")),
    )
    response = asyncio.run(proxy.appliance_ai_debate_live(_request(
        "US", query=b"mode=free&topic=test&practice_id=signed-claim",
    )))
    body = response.body.decode("utf-8")
    assert "練習憑證已簽發" in body
    assert "不可重新載入" in body


def test_country_gate_hk_supported_and_unknown_environment(monkeypatch):
    hk = proxy._solo_live_country_status(_request("HK"))
    assert hk["status"] == "blocked" and hk["supported"] is False
    assert "VPN" in hk["message"]
    supported = proxy._solo_live_country_status(_request("US"))
    assert supported == {"code": "US", "status": "supported", "supported": True, "message": ""}
    unknown_local = proxy._solo_live_country_status(_request())
    assert unknown_local["status"] == "unknown" and unknown_local["supported"] is True

    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("RENDER_SERVICE_NAME", "marksys-production")
    unknown_prod = proxy._solo_live_country_status(_request(host="marksys.example"))
    assert unknown_prod["status"] == "blocked" and unknown_prod["supported"] is False

    # Production markers override a spoofed localhost/testserver Host header.
    monkeypatch.delenv("RENDER_SERVICE_NAME", raising=False)
    monkeypatch.setenv("RENDER", "true")
    render_unknown = proxy._solo_live_country_status(_request(host="localhost"))
    assert render_unknown["status"] == "blocked" and render_unknown["supported"] is False
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-production")
    service_unknown = proxy._solo_live_country_status(_request(host="testserver"))
    assert service_unknown["status"] == "blocked" and service_unknown["supported"] is False


def test_prepare_live_country_gate_precedes_model_and_provider(monkeypatch):
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(
        ai_coach_api, "_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("model config must not run")),
    )
    body = ai_coach_api.LivePrepareRequest(topic="辯題", mode="free")
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.prepare_live(body, _request("HK")))
    assert raised.value.status_code == 403
    assert "VPN" in raised.value.detail
    with pytest.raises(ValidationError):
        ai_coach_api.LivePrepareRequest(topic="辯題", mode="other")


def test_prepare_live_validates_claim_then_4gb_gate_before_paid_work(monkeypatch):
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(proxy, "_new_live_practice_claim", lambda *_args: "")
    monkeypatch.setattr(
        proxy, "get_vote_db",
        lambda: (_ for _ in ()).throw(AssertionError("DB/provider work must not start")),
    )
    body = ai_coach_api.LivePrepareRequest(topic="辯題", mode="free")
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.prepare_live(body, _request("US")))
    assert raised.value.status_code == 503

    monkeypatch.setattr(proxy, "_new_live_practice_claim", lambda *_args: "signed-claim")
    monkeypatch.setattr(
        proxy, "_bandwidth_essential_gate_error",
        lambda: "本月全系統網絡傳輸量已達4GB，只保留必要功能。",
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.prepare_live(body, _request("US")))
    assert raised.value.status_code == 429
    assert "4GB" in raised.value.detail


def test_prepare_live_provider_failure_has_safe_ledger_reason_and_no_store(monkeypatch):
    ledger = []

    class _Db:
        def execute(self, *_args, **_kwargs):
            return None

    async def no_rag(*_args, **_kwargs):
        return ""

    async def failed_provider(*_args, **_kwargs):
        raise HTTPException(502, "upstream exposed gemini-super-secret")

    from core import rag

    db = _Db()
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(proxy, "_new_live_practice_claim", lambda *_args: "signed-claim")
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(proxy, "get_vote_db", lambda: db)
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "key")
    monkeypatch.setattr(rag, "retrieve_rag_context", no_rag)
    monkeypatch.setattr(ai_coach_api, "_generate", failed_provider)
    monkeypatch.setattr(
        ai_coach_api, "_usage",
        lambda *args, **kwargs: ledger.append((args, kwargs)),
    )
    response = asyncio.run(ai_coach_api.prepare_live(
        ai_coach_api.LivePrepareRequest(
            topic="辯題", mode="free", model_label="Gemini 2.5 Flash",
        ),
        _request("US"),
    ))
    payload = json.loads(response.body)
    assert response.headers["cache-control"] == "no-store"
    assert payload["research_ready"] is False
    assert len(ledger) == 1
    args, kwargs = ledger[0]
    assert args[5] is False
    assert args[6] == ai_coach_api.AI_PROVIDER_PUBLIC_ERROR
    assert "gemini-super-secret" not in repr((args, kwargs, payload))


def test_live_brief_is_atomic_single_use_across_concurrent_consumers(monkeypatch):
    import threading

    class _BriefDb:
        def __init__(self):
            self.lock = threading.Lock()
            self.rows = {
                "brief-1": {
                    "user_id": "alice", "brief": "single-use research",
                    "expires_at": "9999-12-31 23:59:59",
                },
            }

        @contextmanager
        def transaction(self):
            with self.lock:
                db = self

                class _Connection:
                    def execute(self, statement, params):
                        sql = " ".join(str(statement).lower().split())
                        if "returning brief" not in sql:
                            expired = [
                                key for key, row in db.rows.items()
                                if row["expires_at"] < params["now"]
                            ]
                            for key in expired:
                                db.rows.pop(key, None)
                            return _Result()
                        row = db.rows.get(params["brief_id"])
                        if (
                            row
                            and row["user_id"] == params["user_id"]
                            and row["expires_at"] >= params["now"]
                        ):
                            db.rows.pop(params["brief_id"])
                            return _Result(row=(row["brief"],))
                        return _Result(row=None)

                yield _Connection()

    db = _BriefDb()
    monkeypatch.setattr(proxy, "get_vote_db", lambda: db)
    # A different user cannot consume or erase the brief.
    assert ai_coach_api.consume_live_brief("brief-1", "mallory") == ""
    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(
            lambda _index: ai_coach_api.consume_live_brief("brief-1", "alice"),
            range(2),
        ))
    assert sorted(values) == ["", "single-use research"]


def test_ai_coach_data_is_private_no_store(monkeypatch):
    class _Frame:
        def __init__(self, rows):
            self.rows = rows

        def to_dict(self, _orient):
            return list(self.rows)

        @property
        def iloc(self):
            return self

        def __getitem__(self, index):
            return self.rows[index]

    class _Db:
        def query(self, sql, _params=None):
            if "ai_fund_transactions" in sql:
                return _Frame([{"balance": 0}])
            return _Frame([])

    from core import config_store

    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(proxy, "get_vote_db", lambda: _Db())
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(config_store, "get_config", lambda *_args, **_kwargs: 100)
    monkeypatch.setattr(proxy, "tts_provider_configured", lambda: False)
    monkeypatch.setattr(proxy, "bandwidth_budget_status", lambda **_kwargs: {
        "total_bytes": 0, "stop_live_bytes": 3_500_000_000,
    })
    response = ai_coach_api.data(_request("US"))
    assert response.headers["cache-control"] == "no-store"
    payload = json.loads(response.body)
    assert payload["country_status"]["supported"] is True
    assert payload["server_tts_configured"] is False


def test_actual_audio_over_six_minutes_is_allowed_up_to_google_boundary(monkeypatch, tmp_path):
    source = tmp_path / "long.webm"
    source.write_bytes(b"bounded-test-audio")
    ffprobe = SimpleNamespace(
        returncode=0,
        stdout=(
            "{\"format\":{\"format_name\":\"matroska,webm\",\"duration\":\"361.0\"},"
            "\"streams\":[{\"codec_type\":\"audio\",\"sample_rate\":\"16000\",\"channels\":1}]}"
        ),
    )
    monkeypatch.setattr(media_probe.subprocess, "run", lambda *_args, **_kwargs: ffprobe)
    result = media_probe.probe_audio_file(
        str(source), "audio/webm", 361,
        max_seconds=google_files.GOOGLE_AUDIO_MAX_SECONDS,
    )
    assert result["duration"] == 361.0
    assert google_files.GOOGLE_AUDIO_MAX_SECONDS == int(9.5 * 60 * 60)


def _install_custom_fallback_test_path(monkeypatch, ledger):
    custom = {
        "provider": "custom", "model": "school-model",
        "api_key": "CUSTOM_LLM_API_KEY", "supports_audio": False,
    }
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(ai_coach_api, "_config", lambda *_args, **_kwargs: custom)
    monkeypatch.setattr(ai_coach_api, "_message", lambda *_args, **_kwargs: ("system", "user"))
    monkeypatch.setattr(proxy, "get_vote_db", lambda: object())
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "gemini-super-secret")
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(
        ai_coach_api, "_usage",
        lambda *args, **kwargs: ledger.append((args, kwargs)),
    )
    return ai_coach_api.CoachRequest(
        feature="fact_check", model_label="自家辯論 LLM", text="check",
    )


def test_custom_web_research_uses_grounded_default_only(monkeypatch):
    ledger = []
    body = _install_custom_fallback_test_path(monkeypatch, ledger)
    calls = 0

    async def provider(config, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if config["provider"] == "custom":
            request = ai_provider.httpx.Request(
                "POST", "https://provider.invalid/generate?key=gemini-super-secret",
            )
            ai_provider.httpx.Response(403, request=request).raise_for_status()
        return "fallback ok", {
            "input_tokens": 3, "output_tokens": 4,
            "cost_source": "gemini_usage_metadata",
        }

    monkeypatch.setattr(ai_provider, "generate_text", provider)
    result = asyncio.run(ai_coach_api.run(body, _request("US")))
    assert json.loads(result.body) == {"ok": True, "markdown": "fallback ok"}
    assert result.headers["cache-control"] == "no-store"
    assert calls == 1 and len(ledger) == 1
    args, kwargs = ledger[0]
    assert args[3] == ai_coach_api.DEFAULT_AI_MODEL
    assert args[4]["provider"] == "gemini" and args[5] is True
    assert kwargs["actual"]["cost_source"] == "gemini_usage_metadata"
    assert "gemini-super-secret" not in repr(ledger)


def test_competition_prep_coach_claims_and_saves_before_usage(monkeypatch):
    from core import competition_prep_logic as prep_logic

    events = []
    snapshots = []
    bundle = {
        "project": {
            "topic_text": "測試辯題", "our_side": "pro",
            "debate_format": "聯中", "revision": 3,
        },
        "role": "owner", "manuscripts": [], "strategy_cards": [],
        "evidence_cards": [], "weaknesses": [],
    }
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(ai_coach_api, "_runtime_model_settings", lambda _db: (("model",), "model"))
    monkeypatch.setattr(ai_coach_api, "_config", lambda *_args, **_kwargs: {
        "provider": "gemini", "api_key": "GEMINI_API_KEY",
    })
    monkeypatch.setattr(ai_coach_api, "_require_enabled_model", lambda *_args: None)
    monkeypatch.setattr(ai_coach_api, "_message", lambda *_args, **_kwargs: ("system", "user"))
    monkeypatch.setattr(proxy, "get_vote_db", lambda: object())
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args: "key")
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(prep_logic, "project_bundle", lambda *_args: bundle)
    def claim(*args, **_kwargs):
        events.append("claim")
        snapshots.append(args[6])
        return {"state": "claimed"}

    monkeypatch.setattr(prep_logic, "claim_ai_run", claim)
    monkeypatch.setattr(
        prep_logic, "complete_ai_run",
        lambda *_args, **_kwargs: events.append("complete"),
    )
    monkeypatch.setattr(prep_logic, "release_ai_run", lambda *_args: events.append("release"))

    async def generate(*_args, on_provider_attempt=None, **_kwargs):
        events.append("provider")
        on_provider_attempt()
        return "result", {}

    monkeypatch.setattr(ai_coach_api, "_generate", generate)
    monkeypatch.setattr(ai_coach_api, "_usage", lambda *_args, **_kwargs: events.append("usage"))
    body = ai_coach_api.CoachRequest(
        feature="strategy", model_label="model", prep_project_id=3,
        operation_id="prep-operation-67890",
    )

    response = asyncio.run(ai_coach_api.run(body, _request("US")))

    assert json.loads(response.body)["markdown"] == "result"
    assert events == ["claim", "provider", "complete", "usage"]
    assert snapshots[0]["project_revision"] == 3
    assert len(snapshots[0]["input_sha256"]) == 64

    monkeypatch.setattr(
        prep_logic, "claim_ai_run",
        lambda *_args, **_kwargs: {"state": "completed", "output": "cached"},
    )

    async def should_not_call_provider(*_args, **_kwargs):
        raise AssertionError("completed operation must not call provider again")

    monkeypatch.setattr(ai_coach_api, "_generate", should_not_call_provider)
    cached = asyncio.run(ai_coach_api.run(body, _request("US")))
    assert json.loads(cached.body) == {
        "ok": True, "markdown": "cached", "cached": True,
    }


def test_custom_fallback_failure_logs_safe_fallback_failure(monkeypatch):
    ledger = []
    body = _install_custom_fallback_test_path(monkeypatch, ledger)

    async def provider(*_args, **_kwargs):
        request = ai_provider.httpx.Request(
            "POST", "https://provider.invalid/generate?key=gemini-super-secret",
        )
        ai_provider.httpx.Response(403, request=request).raise_for_status()

    monkeypatch.setattr(ai_provider, "generate_text", provider)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.run(body, _request("US")))
    assert raised.value.status_code == 502
    assert raised.value.detail == ai_coach_api.AI_PROVIDER_PUBLIC_ERROR
    assert "gemini-super-secret" not in raised.value.detail
    assert len(ledger) == 1
    args, kwargs = ledger[0]
    assert args[3] == ai_coach_api.DEFAULT_AI_MODEL
    assert args[4]["provider"] == "gemini" and args[5] is False
    assert args[6] == ai_coach_api.AI_PROVIDER_PUBLIC_ERROR
    assert "gemini-super-secret" not in repr((args, kwargs))


def test_custom_strategy_fallback_logs_both_real_attempts_as_one_operation(monkeypatch):
    ledger = []
    custom = {
        "provider": "custom",
        "model": "school-model",
        "api_key": "CUSTOM_LLM_API_KEY",
        "supports_audio": False,
        "supports_web_search": False,
        "input_price_per_million": 0,
        "output_price_per_million": 0,
    }
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(ai_coach_api, "_config", lambda *_args, **_kwargs: custom)
    monkeypatch.setattr(
        ai_coach_api, "_message", lambda *_args, **_kwargs: ("system", "user")
    )
    monkeypatch.setattr(proxy, "get_vote_db", lambda: object())
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "key")
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(
        ai_coach_api,
        "_usage",
        lambda *args, **kwargs: ledger.append((args, kwargs)),
    )
    calls = []

    async def provider(config, *_args, **_kwargs):
        calls.append(config["provider"])
        if config["provider"] == "custom":
            raise RuntimeError("custom unavailable")
        return "fallback ok", {
            "input_tokens": 3,
            "output_tokens": 4,
            "audio_tokens": 0,
            "search_calls": 0,
            "cost_source": "gemini_usage_metadata",
        }

    monkeypatch.setattr(ai_provider, "generate_text", provider)
    body = ai_coach_api.CoachRequest(
        feature="strategy",
        model_label="自家辯論 LLM",
        topic="測試辯題",
    )
    response = asyncio.run(ai_coach_api.run(body, _request("US")))

    assert json.loads(response.body)["markdown"] == "fallback ok"
    assert calls == ["custom", "gemini"]
    assert [(item[0][4]["provider"], item[0][5]) for item in ledger] == [
        ("custom", False),
        ("gemini", True),
    ]
    assert [item[1]["operation_stage"] for item in ledger] == [
        "primary",
        "fallback",
    ]
    operation_ids = {item[1]["operation_id"] for item in ledger}
    assert len(operation_ids) == 1
    assert next(iter(operation_ids)).startswith("coach-")


def test_custom_audio_uri_falls_back_without_phantom_custom_attempt(monkeypatch):
    ledger = []
    custom = {
        "provider": "custom", "model": "school-model",
        "api_key": "CUSTOM_LLM_API_KEY", "supports_audio": False,
        "supports_web_search": False,
    }
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(ai_coach_api, "_config", lambda *_args, **_kwargs: custom)
    monkeypatch.setattr(ai_coach_api, "_message", lambda *_args, **_kwargs: ("system", "user"))
    monkeypatch.setattr(proxy, "get_vote_db", lambda: object())
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "key")
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(
        ai_coach_api, "_usage",
        lambda *args, **kwargs: ledger.append((args, kwargs)),
    )

    async def generate(config, *_args, on_provider_attempt=None, **_kwargs):
        if config["provider"] == "custom":
            raise HTTPException(400, "錄音分析只支援 Google Gemini Files API。")
        on_provider_attempt()
        return "fallback ok", {}

    monkeypatch.setattr(ai_coach_api, "_generate", generate)
    body = ai_coach_api.CoachRequest(
        feature="speech_review", model_label="自家辯論 LLM",
        topic="測試辯題", audio_intent_id="a" * 32,
        audio_duration_seconds=361,
    )
    response = asyncio.run(ai_coach_api.run(body, _request("US")))
    assert json.loads(response.body)["markdown"] == "fallback ok"
    assert len(ledger) == 1
    assert ledger[0][0][4]["provider"] == "gemini"
    assert ledger[0][0][5] is True
    assert ledger[0][1]["operation_stage"] == "fallback"


def test_room_judgement_uses_two_mib_bounded_reader(monkeypatch):
    captured = []
    broadcasts = []
    ledger = []

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def bounded(_client, url, *, max_bytes, **kwargs):
        captured.append({"url": url, "max_bytes": max_bytes, "kwargs": kwargs})
        raise ValueError("AI provider response exceeds server limit")

    async def broadcast(_room, message):
        broadcasts.append(message)

    async def log(*args, **kwargs):
        ledger.append((args, kwargs))

    monkeypatch.setattr(proxy.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(proxy, "post_json_bounded", bounded)
    monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
    monkeypatch.setattr(proxy, "_log_room_judgement_attempt", log)
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "key")
    monkeypatch.setattr(proxy, "ROOM_JUDGEMENT_MODELS", ("model-a",))
    room = SimpleNamespace(
        topic="辯題", debate_format="校園隨想", structure="free",
        transcript=[
            {"side": "正方", "text": "正方內容"},
            {"side": "反方", "text": "反方內容"},
        ], judgement="",
        judgement_lock=asyncio.Lock(),
    )
    asyncio.run(proxy._room_request_judgement(room))
    assert len(captured) == 1
    assert captured[0]["max_bytes"] == 2 * 1024 * 1024
    assert "key=" not in captured[0]["url"]
    assert captured[0]["kwargs"]["headers"] == {"x-goog-api-key": "key"}
    assert "params" not in captured[0]["kwargs"]
    assert captured[0]["kwargs"]["json"]["generationConfig"] == {
        "temperature": 0.2,
    }
    assert "2MiB" in room.judgement
    assert broadcasts[-1] == {"type": "judgement", "text": room.judgement}
    assert len(ledger) == 1
    assert ledger[0][0][2] is False


def test_room_judgement_fallback_logs_each_attempt_under_one_operation(monkeypatch):
    models = ai_model_config.model_slugs_for_feature("room_judgement")[:2]
    calls = []
    ledger = []
    broadcasts = []

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def bounded(_client, url, **_kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise proxy.httpx.ReadTimeout("first model timed out")
        return {
            "candidates": [
                {"content": {"parts": [{"text": "建議勝方：正方"}]}}
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 20,
            },
        }

    async def broadcast(_room, message):
        broadcasts.append(message)

    def log(*args, **kwargs):
        ledger.append((args, kwargs))

    monkeypatch.setattr(proxy.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(proxy, "post_json_bounded", bounded)
    monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "key")
    monkeypatch.setattr(proxy, "ROOM_JUDGEMENT_MODELS", models)
    monkeypatch.setattr(proxy, "get_vote_db", lambda: object())
    monkeypatch.setattr(funds_logic, "log_ai_usage", log)
    room = SimpleNamespace(
        code="ROOM1",
        created_by="alice",
        topic="辯題",
        debate_format="校園隨想",
        structure="mock",
        transcript=[
            {"side": "正方", "text": "正方內容"},
            {"side": "反方", "text": "反方內容"},
        ],
        transcript_revision=7,
        judgement="",
        judgement_lock=asyncio.Lock(),
    )

    asyncio.run(proxy._room_request_judgement(room))

    assert len(calls) == len(ledger) == 2
    assert room.judgement == "建議勝方：正方"
    assert [(item[0][1], item[0][2]) for item in ledger] == [
        ("full_mock_live", False),
        ("full_mock_live", True),
    ]
    usage_rows = [item[1]["usage"] for item in ledger]
    assert [row["operation_stage"] for row in usage_rows] == [
        "judgement_attempt_1",
        "judgement_attempt_2",
    ]
    assert len({row["operation_id"] for row in usage_rows}) == 1
    assert usage_rows[0]["cost_source"] == "provider_attempt_unknown_usage"
    assert usage_rows[1]["input_tokens"] == 100
    assert usage_rows[1]["output_tokens"] == 20
    assert usage_rows[1]["estimated_cost_usd"] > 0
    assert broadcasts[-1] == {"type": "judgement", "text": room.judgement}


def test_room_judgement_missing_key_does_not_log_phantom_provider_call(monkeypatch):
    ledger = []

    async def broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        funds_logic, "log_ai_usage", lambda *args, **kwargs: ledger.append((args, kwargs))
    )
    room = SimpleNamespace(
        created_by="alice",
        topic="辯題",
        debate_format="校園隨想",
        structure="free",
        transcript=[
            {"side": "正方", "text": "正方內容"},
            {"side": "反方", "text": "反方內容"},
        ],
        judgement="",
        judgement_lock=asyncio.Lock(),
    )

    asyncio.run(proxy._room_request_judgement(room))
    assert "未設定 GEMINI_API_KEY" in room.judgement
    assert ledger == []


def test_room_judgement_unexpected_error_never_broadcasts_secret(monkeypatch):
    broadcasts = []
    ledger = []
    secret = "gemini-room-super-secret"

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def bounded(*_args, **_kwargs):
        raise RuntimeError(f"authenticated request failed: {secret}")

    async def broadcast(_room, message):
        broadcasts.append(message)

    async def log(*args, **kwargs):
        ledger.append((args, kwargs))

    monkeypatch.setattr(proxy.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(proxy, "post_json_bounded", bounded)
    monkeypatch.setattr(proxy, "_room_broadcast", broadcast)
    monkeypatch.setattr(proxy, "_log_room_judgement_attempt", log)
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: secret)
    monkeypatch.setattr(proxy, "ROOM_JUDGEMENT_MODELS", ("model-a",))
    room = SimpleNamespace(
        topic="辯題", debate_format="校園隨想", structure="free",
        transcript=[
            {"side": "正方", "text": "正方內容"},
            {"side": "反方", "text": "反方內容"},
        ], judgement="",
        judgement_lock=asyncio.Lock(),
    )
    asyncio.run(proxy._room_request_judgement(room))
    assert secret not in room.judgement
    assert secret not in repr(broadcasts)
    assert "上游服務連線錯誤" in room.judgement
    assert len(ledger) == 1
    assert ledger[0][0][2] is False


def test_bandwidth_35gb_blocks_solo_server_tts_not_direct_quota(monkeypatch):
    monkeypatch.setattr(proxy, "_require_committee_user", lambda _request: "alice")
    monkeypatch.setattr(proxy, "_bandwidth_live_gate_error", lambda: "at 3.5GB")
    with pytest.raises(HTTPException) as raised:
        asyncio.run(proxy.azure_tts(_request("US")))
    assert raised.value.status_code == 429
    assert "Gemini原生聲音" in raised.value.detail



def test_bandwidth_4gb_blocks_initial_and_jit_token_provider_calls(monkeypatch):
    message = "由於本月全系統網絡傳輸量已達4GB，只保留必要功能。"
    launch = {
        "user_id": "alice", "mode": "free", "practice_id": "practice-free",
        "session_seconds": [], "system_prompt": "",
    }
    monkeypatch.setattr(
        proxy, "require_page_user", lambda _request, _page: "alice",
    )
    monkeypatch.setattr(proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: launch)
    monkeypatch.setattr(proxy, "_solo_live_practice_exists", lambda _claim: False)
    monkeypatch.setattr(proxy, "_practice_live_rate_check", lambda _user: None)
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: message)
    monkeypatch.setattr(
        proxy, "_mint_gemini_live_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("4GB gate must precede provider token mint"),
        ),
    )
    response = asyncio.run(proxy.appliance_ai_debate_live(_request(
        "US", query=b"mode=free&topic=test&practice_id=signed-claim",
    )))
    assert "4GB" in response.body.decode("utf-8")

    mock_claim = {
        "user_id": "alice", "mode": "mock", "practice_id": "practice-mock",
        "session_seconds": [300, 600], "system_prompt": "locked prompt",
    }
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(proxy, "_verify_live_practice_claim", lambda *_args, **_kwargs: mock_claim)
    body = ai_coach_api.LiveTokenRequest(practice_id="x" * 40, session_index=1)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.mint_live_token(body, _request("US")))
    assert raised.value.status_code == 429
    assert "4GB" in raised.value.detail


def test_solo_relay_route_limits_and_env_are_removed():
    assert not [route for route in proxy.app.routes if getattr(route, "path", "") == "/gemini-live"]
    checked = [
        ROOT / "deploy" / "proxy.py",
        ROOT / "api" / "ai_coach_api.py",
        ROOT / "system_limits.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in checked)
    assert "LIVE_RELAY_WS_BASE" not in source
    assert "GEMINI_RELAY_" not in source
    assert "solo_gemini_relay" not in source
