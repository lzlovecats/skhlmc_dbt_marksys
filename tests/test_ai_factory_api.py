"""Focused offline HTTP-layer contracts for the debate data factory.

The provider and database are deliberately replaced with bounded fakes.  The
tests cover authority/readiness, exact-send confirmation and the release wire
format without making a network call or applying the optional schema.
"""

import asyncio
import json
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import HTTPException

from api import ai_factory_api as api
from core import ai_provider, r2_storage
from core.ai_data_factory import RAG_KNOWLEDGE_CARD_RECIPE
from core.ai_factory_store import FactoryStoreError
from core.schema_features import ABSENT, READY


class _Db:
    def __init__(self, frames=()):
        self.frames = list(frames)
        self.queries = []

    def query(self, sql, params=None):
        self.queries.append((" ".join(str(sql).split()), params or {}))
        if not self.frames:
            raise AssertionError(f"unexpected database query: {sql}")
        return self.frames.pop(0)


def _frame(*rows):
    return pd.DataFrame(list(rows))


def _source(*, content="公共交通免費可以減少市民交通開支。"):
    return {
        "id": "source-1",
        "source_kind": "admin_paste",
        "source_group_id": "source-group-1",
        "revision_no": 1,
        "data_type": "debate_material",
        "title": "公共交通政策",
        "topic_text": "本港應推行免費公共交通",
        "side": "pro",
        "source_note": "管理員提供嘅測試來源",
        "content_text": content,
        "content_sha256": api.sha256_text(content),
        "withdrawn_at": None,
    }


def _prompt():
    return SimpleNamespace(
        recipe_id=RAG_KNOWLEDGE_CARD_RECIPE,
        prompt_version="factory-prompt-v1",
        prompt_sha256="prompt-sha",
        system="EXACT SYSTEM PROMPT",
        user="EXACT USER PROMPT INCLUDING SOURCE",
        temperature=0.2,
    )


def _free_config():
    return {
        "provider": "gemini",
        "model": "gemini-3.5-flash",
        "api_key": "GEMINI_API_KEY",
        "input_price_per_million": 0,
        "audio_input_price_per_million": 0,
        "output_price_per_million": 0,
    }


def _signed_claim(job, *, user="manager"):
    return {
        "kind": "ai_factory_preview",
        "confirmation_version": api.CONFIRMATION_VERSION,
        "user_id": user,
        "job_id": str(job["id"]),
        "source_id": str(job["source_id"]),
        "source_sha256": str(job["content_sha256"]),
        "recipe_id": str(job["recipe_key"]),
        "prompt_version": "factory-prompt-v1",
        "prompt_sha256": str(job["preview_prompt_sha256"]),
        "input_sha256": str(job["preview_input_sha256"]),
        "preview_sha256": str(job["preview_sha256"]),
        "model_label": str(job["preview_model_label"]),
        "provider": str(job["preview_provider"]),
        "provider_model": str(job["preview_provider_model"]),
        "requested_count": int(job["requested_count"]),
        "side": "pro",
        "stage": "general",
        "topic_tag_ids": [],
        "topic_tag_labels": [],
        "estimate": {
            "estimated_cost_hkd": 0.0,
            "estimated_cost_usd": 0.0,
            "output_tokens": 3000,
        },
    }


def _job(*, content="公共交通免費可以減少市民交通開支。"):
    source = _source(content=content)
    return {
        "id": "job-1",
        "created_by": "manager",
        "source_id": source["id"],
        "recipe_key": RAG_KNOWLEDGE_CARD_RECIPE,
        "requested_count": 3,
        "instruction_text": "",
        "preview_model_label": "Gemini 3.5 Flash",
        "preview_provider": "gemini",
        "preview_provider_model": "gemini-3.5-flash",
        "preview_prompt_sha256": "prompt-sha",
        "preview_input_sha256": "input-sha",
        "preview_sha256": "preview-sha",
        "invalidated_at": None,
        "source_withdrawn_at": None,
        **source,
        "id": "job-1",
    }


def _install_generation_fakes(monkeypatch, job):
    db = _Db([_frame(job)])
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "_preview_secret", lambda: "preview-secret")
    monkeypatch.setattr(
        r2_storage,
        "verify_upload_claim",
        lambda token, secret: _signed_claim(job),
    )
    monkeypatch.setattr(api, "_topic_tags", lambda _db, _ids: [])
    monkeypatch.setattr(api, "_model_config", lambda _db, _label: (_free_config(), "provider-key"))
    monkeypatch.setattr(api, "build_factory_prompt", lambda *_args, **_kwargs: _prompt())
    monkeypatch.setattr(api, "_preview_hashes", lambda *_args, **_kwargs: ("input-sha", "preview-sha"))
    monkeypatch.setattr(
        api,
        "estimate_factory_cost",
        lambda *_args, **_kwargs: {
            "estimated_cost_hkd": 0.0,
            "estimated_cost_usd": 0.0,
            "output_tokens": 3000,
        },
    )
    monkeypatch.setattr(api, "_account_provider_bytes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api, "_require_nonessential_bandwidth", lambda: None)
    return db


def test_non_manager_is_forbidden_before_any_factory_operation(monkeypatch):
    db = object()
    monkeypatch.setattr(api, "require_page_user_or_developer", lambda *_args: "member")
    monkeypatch.setattr("deploy.proxy.get_vote_db", lambda: db)
    monkeypatch.setattr(api, "is_ai_manager", lambda user, *, db: False)

    with pytest.raises(HTTPException) as denied:
        api._manager(None)

    assert denied.value.status_code == 403


@pytest.mark.parametrize("state", (ABSENT, "disabled", "partial"))
def test_factory_operations_fail_closed_until_schema_is_ready(monkeypatch, state):
    db = _Db()
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_factory_state", lambda _db: state)

    with pytest.raises(HTTPException) as unavailable:
        api.sources(None)

    assert unavailable.value.status_code == 503
    assert db.queries == []


def test_bootstrap_defaults_to_gemini_35_without_internal_data_marker(monkeypatch):
    db = _Db()
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_factory_state", lambda _db: ABSENT)
    monkeypatch.setattr(
        "core.config_store.get_configs",
        lambda *_args, **_kwargs: {
            "ai_enabled_providers": ["gemini"],
            "ai_default_model": "Gemini 2.5 Flash",
        },
    )
    monkeypatch.setattr("deploy.proxy._get_proxy_secret", lambda name: "configured-key")

    result = api.bootstrap(None)

    assert result["default_model"] == "Gemini 3.5 Flash"
    assert any(model["label"] == "Gemini 3.5 Flash" for model in result["models"])
    assert "internal_data_allowed" not in json.dumps(result, ensure_ascii=False)
    assert result["limits"]["source_note_max_chars"] == api.AI_FACTORY_SOURCE_NOTE_MAX_CHARS


def test_bootstrap_offers_explicit_free_only_router_without_changing_gemini_default(
    monkeypatch,
):
    db = _Db()
    monkeypatch.setattr(
        "core.config_store.get_configs",
        lambda *_args, **_kwargs: {
            "ai_enabled_providers": ["gemini", "openrouter"],
            "ai_default_model": "Gemini 2.5 Flash",
        },
    )
    monkeypatch.setattr("deploy.proxy._get_proxy_secret", lambda _name: "configured-key")

    models, default_model = api._runtime_models(db)

    free = next(model for model in models if model["label"] == "OpenRouter Free")
    assert free == {
        "label": "OpenRouter Free",
        "provider": "openrouter",
        "provider_model": "openrouter/free",
        "available": True,
        "pricing_label": "免費",
        "pricing_note": (
            "Provider: OpenRouter Free Models Router；每次由可用免費模型中選擇，"
            "供應、速度及輸出質素可能不同。"
        ),
    }
    assert default_model == "Gemini 3.5 Flash"
    assert api.AI_FACTORY_MODEL_OPTIONS["OpenRouter Free"]["billing_mode"] == "free_only"
    assert api.AI_FACTORY_MODEL_OPTIONS["OpenRouter Free"]["input_price_per_million"] == 0
    assert api.AI_FACTORY_MODEL_OPTIONS["OpenRouter Free"]["output_price_per_million"] == 0


def test_free_only_model_config_fails_closed_if_central_route_stops_being_free(
    monkeypatch,
):
    monkeypatch.setattr(
        api,
        "_runtime_models",
        lambda _db: (
            [{"label": "OpenRouter Free", "available": True}],
            "Gemini 3.5 Flash",
        ),
    )
    unsafe = dict(api.AI_FACTORY_MODEL_OPTIONS["OpenRouter Free"])
    unsafe["model"] = "openrouter/auto"
    monkeypatch.setitem(api.AI_FACTORY_MODEL_OPTIONS, "OpenRouter Free", unsafe)
    monkeypatch.setattr("deploy.proxy._get_proxy_secret", lambda _name: "configured-key")

    with pytest.raises(HTTPException) as unavailable:
        api._model_config(object(), "OpenRouter Free")

    assert unavailable.value.status_code == 503
    assert "Free Provider" in str(unavailable.value.detail)


def test_provider_allowlist_lookup_failure_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        "core.config_store.get_configs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("config unavailable")),
    )

    with pytest.raises(HTTPException) as unavailable:
        api._runtime_models(object())

    assert unavailable.value.status_code == 503


def test_preview_returns_the_exact_signed_prompt_without_calling_provider(monkeypatch):
    source = _source()
    captured = {}
    provider_called = False
    db = _Db()
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "get_source", lambda _db, source_id: source)
    monkeypatch.setattr(api, "_model_config", lambda _db, _label: (_free_config(), "provider-key"))
    monkeypatch.setattr(api, "_topic_tags", lambda _db, _ids: [])
    monkeypatch.setattr(api, "build_factory_prompt", lambda *_args, **_kwargs: _prompt())
    monkeypatch.setattr(
        api,
        "create_or_refresh_job_preview",
        lambda *_args, **_kwargs: {"id": "job-1"},
    )
    monkeypatch.setattr(
        api,
        "estimate_factory_cost",
        lambda *_args, **_kwargs: {
            "estimated_cost_hkd": 0.0,
            "estimated_cost_usd": 0.0,
            "output_tokens": 3000,
        },
    )
    monkeypatch.setattr(api, "_preview_secret", lambda: "preview-secret")

    def sign(claim, secret, *, expires):
        captured.update(claim=claim, secret=secret, expires=expires)
        return "signed-exact-preview-token"

    async def forbidden_provider_call(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("preview must never call a provider")

    monkeypatch.setattr(r2_storage, "sign_upload_claim", sign)
    monkeypatch.setattr(ai_provider, "generate_text", forbidden_provider_call)

    result = api.preview_job(
        api.FactoryPreviewBody(
            source_id=source["id"],
            recipe_id=RAG_KNOWLEDGE_CARD_RECIPE,
            model_label="Gemini 3.5 Flash",
            item_count=3,
        ),
        None,
    )

    assert provider_called is False
    assert result["system_prompt"] == "EXACT SYSTEM PROMPT"
    assert result["user_prompt"] == "EXACT USER PROMPT INCLUDING SOURCE"
    assert result["provider_payload"] == {
        "model_label": "Gemini 3.5 Flash",
        "provider": "gemini",
        "provider_model": "gemini-3.5-flash",
        "system_prompt": result["system_prompt"],
        "user_prompt": result["user_prompt"],
        "temperature": 0.2,
        "max_output_tokens": 3000,
        "web_search": False,
        "structured_json": True,
        "require_complete": True,
    }
    assert result["preview_token"] == "signed-exact-preview-token"
    assert captured["claim"]["preview_sha256"] == result["preview_sha256"]
    assert captured["claim"]["source_sha256"] == result["source_sha256"]
    assert captured["claim"]["model_label"] == result["model_label"]
    assert captured["secret"] == "preview-secret"


def test_preview_fails_before_provider_when_recipe_exceeds_output_ceiling(monkeypatch):
    source = _source()
    db = _Db()
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "get_source", lambda _db, _source_id: source)
    monkeypatch.setattr(
        api, "_model_config", lambda _db, _label: (_free_config(), "provider-key")
    )
    monkeypatch.setattr(api, "_topic_tags", lambda _db, _ids: [])
    monkeypatch.setattr(api, "build_factory_prompt", lambda *_args, **_kwargs: _prompt())
    monkeypatch.setattr(
        api,
        "estimate_factory_cost",
        lambda *_args, **_kwargs: {
            "estimated_cost_hkd": 0.0,
            "estimated_cost_usd": 0.0,
            "output_tokens": 3000,
        },
    )
    monkeypatch.setattr(api, "AI_PROVIDER_OUTPUT_MAX_TOKENS", 1000)

    with pytest.raises(HTTPException) as unavailable:
        api.preview_job(
            api.FactoryPreviewBody(
                source_id=source["id"],
                recipe_id=RAG_KNOWLEDGE_CARD_RECIPE,
                model_label="Gemini 3.5 Flash",
                item_count=3,
            ),
            None,
        )

    assert unavailable.value.status_code == 503
    assert "輸出上限不足" in str(unavailable.value.detail)


def test_attack_defence_preview_rejects_non_debate_side_before_prompt_or_provider(
    monkeypatch,
):
    source = _source()
    source["side"] = "neutral"
    db = _Db()
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "get_source", lambda _db, _source_id: source)
    monkeypatch.setattr(api, "_model_config", lambda _db, _label: (_free_config(), "key"))
    monkeypatch.setattr(api, "_topic_tags", lambda _db, _ids: [])
    monkeypatch.setattr(api, "_preview_secret", lambda: "preview-secret")
    monkeypatch.setattr(
        api,
        "build_factory_prompt",
        lambda *_args, **_kwargs: pytest.fail(
            "an impossible attack-defence job must fail before prompt/provider work"
        ),
    )

    with pytest.raises(HTTPException) as rejected:
        api.preview_job(
            api.FactoryPreviewBody(
                source_id=source["id"],
                recipe_id=api.SFT_ATTACK_DEFENCE_RECIPE,
                model_label="Gemini 3.5 Flash",
            ),
            None,
        )

    assert rejected.value.status_code == 400
    assert "正方或反方" in str(rejected.value.detail)


@pytest.mark.parametrize(
    "missing",
    ("rights_confirmed", "anonymized_confirmed", "third_party_confirmed"),
)
def test_generate_requires_all_three_send_confirmations_before_provider(monkeypatch, missing):
    provider_called = False
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", object()))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "_require_nonessential_bandwidth", lambda: None)
    monkeypatch.setattr(api, "_preview_secret", lambda: "preview-secret")
    monkeypatch.setattr(
        r2_storage,
        "verify_upload_claim",
        lambda *_args: {
            "kind": "ai_factory_preview",
            "confirmation_version": api.CONFIRMATION_VERSION,
            "user_id": "manager",
            "job_id": "job-1",
        },
    )

    async def forbidden_provider_call(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("unconfirmed request must not call a provider")

    monkeypatch.setattr(ai_provider, "generate_text", forbidden_provider_call)
    confirmations = {
        "rights_confirmed": True,
        "anonymized_confirmed": True,
        "third_party_confirmed": True,
    }
    confirmations[missing] = False

    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            api.generate_job(
                "job-1",
                api.FactoryGenerateBody(preview_token="token", **confirmations),
                None,
            )
        )

    assert denied.value.status_code == 400
    assert provider_called is False


def test_pii_warning_requires_an_explicit_override_reason_before_claim(monkeypatch):
    job = _job(content="聯絡人電郵係 student@example.com，請用作辯論材料。")
    _install_generation_fakes(monkeypatch, job)
    monkeypatch.setattr(
        api,
        "claim_attempt",
        lambda *_args, **_kwargs: pytest.fail("PII warning must stop before claiming an attempt"),
    )
    monkeypatch.setattr(
        ai_provider,
        "generate_text",
        lambda *_args, **_kwargs: pytest.fail("PII warning must stop before provider"),
    )

    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            api.generate_job(
                "job-1",
                api.FactoryGenerateBody(
                    preview_token="token",
                    rights_confirmed=True,
                    anonymized_confirmed=True,
                    third_party_confirmed=True,
                ),
                None,
            )
        )

    assert denied.value.status_code == 400
    assert "覆寫理由" in str(denied.value.detail)


def test_pii_scan_covers_source_metadata_and_manager_instruction():
    source = _source()
    source["source_note"] = "已由管理員整理"

    warnings = api._outbound_pii_warnings(
        source,
        "如有需要致電 9123 4567",
        ["student-tag@example.com"],
    )

    assert "可能包含電郵地址" in warnings
    assert "可能包含香港電話號碼" in warnings


def test_essential_bandwidth_mode_stops_generation_before_claim_or_provider(monkeypatch):
    job = _job()
    _install_generation_fakes(monkeypatch, job)
    monkeypatch.setattr(
        api,
        "_require_nonessential_bandwidth",
        lambda: (_ for _ in ()).throw(
            HTTPException(429, "網絡傳輸量已進入必要服務模式")
        ),
    )
    monkeypatch.setattr(
        api,
        "claim_attempt",
        lambda *_args, **_kwargs: pytest.fail(
            "bandwidth gate must stop generation before reserving an attempt"
        ),
    )

    async def forbidden_provider_call(*_args, **_kwargs):
        pytest.fail("bandwidth gate must stop generation before the provider call")

    monkeypatch.setattr(ai_provider, "generate_text", forbidden_provider_call)

    with pytest.raises(HTTPException) as blocked:
        asyncio.run(
            api.generate_job(
                "job-1",
                api.FactoryGenerateBody(
                    preview_token="token",
                    rights_confirmed=True,
                    anonymized_confirmed=True,
                    third_party_confirmed=True,
                ),
                None,
            )
        )

    assert blocked.value.status_code == 429
    assert blocked.value.detail == "網絡傳輸量已進入必要服務模式"


def test_nonessential_bandwidth_helper_maps_the_shared_gate_to_429(monkeypatch):
    monkeypatch.setattr(
        "deploy.proxy._bandwidth_essential_gate_error",
        lambda: "網絡傳輸量已進入必要服務模式",
    )

    with pytest.raises(HTTPException) as blocked:
        api._require_nonessential_bandwidth()

    assert blocked.value.status_code == 429
    assert blocked.value.detail == "網絡傳輸量已進入必要服務模式"


def test_zero_cost_provider_bypasses_budget_configuration_and_database():
    class NoDatabaseCalls:
        def query(self, *_args, **_kwargs):
            raise AssertionError("a free provider estimate must not query paid budgets")

    estimate = api.estimate_factory_cost(
        api.AI_FACTORY_MODEL_OPTIONS["OpenRouter Free"],
        _prompt(),
        requested_count=3,
        model_label="OpenRouter Free",
    )
    result = api._require_provider_budget(NoDatabaseCalls(), "openrouter", estimate)

    assert estimate["estimated_cost_usd"] == 0
    assert estimate["estimated_cost_hkd"] == 0
    assert result == {"estimated_cost_hkd": 0.0, "remaining_hkd": None, "free": True}


def test_missing_provider_usage_uses_the_reserved_preview_estimate():
    usage = api._actual_usage(
        {},
        _free_config(),
        "Gemini 3.5 Flash",
        "job-1",
        2,
        fallback_estimate={
            "estimated_cost_usd": 0.125,
            "estimated_cost_hkd": 0.975,
        },
    )

    assert usage["estimated_cost_usd"] == 0.125
    assert usage["estimated_cost_hkd"] == 0.975
    assert usage["operation_stage"] == "attempt_2"
    assert usage["cost_source"] == "factory_preflight_estimate_no_provider_usage"


def test_changed_price_estimate_invalidates_the_exact_preview_before_claim(monkeypatch):
    job = _job()
    _install_generation_fakes(monkeypatch, job)
    signed = _signed_claim(job)
    signed["estimate"] = {
        "estimated_cost_hkd": 1.0,
        "estimated_cost_usd": 0.1,
    }
    monkeypatch.setattr(r2_storage, "verify_upload_claim", lambda *_args: signed)
    monkeypatch.setattr(
        api,
        "claim_attempt",
        lambda *_args, **_kwargs: pytest.fail("changed price must require a new preview"),
    )

    with pytest.raises(HTTPException) as stale:
        asyncio.run(
            api.generate_job(
                "job-1",
                api.FactoryGenerateBody(
                    preview_token="token",
                    rights_confirmed=True,
                    anonymized_confirmed=True,
                    third_party_confirmed=True,
                ),
                None,
            )
        )

    assert stale.value.status_code == 409
    assert "成本估算" in str(stale.value.detail)


def test_malformed_provider_response_fails_the_entire_attempt_without_items(monkeypatch):
    job = _job()
    _install_generation_fakes(monkeypatch, job)
    failed = []
    completed = []
    provider_kwargs = {}
    monkeypatch.setattr(
        api,
        "claim_attempt",
        lambda *_args, **_kwargs: {"id": "attempt-1", "attempt_no": 1},
    )
    monkeypatch.setattr(api, "mark_provider_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        api,
        "fail_attempt",
        lambda *args, **kwargs: failed.append((args, kwargs)),
    )
    monkeypatch.setattr(
        api,
        "complete_attempt",
        lambda *args, **kwargs: completed.append((args, kwargs)),
    )

    async def malformed(*_args, **kwargs):
        provider_kwargs.update(kwargs)
        kwargs["on_provider_attempt"]()
        return "this is not JSON", {"input_tokens": 5, "output_tokens": 4}

    monkeypatch.setattr(ai_provider, "generate_text", malformed)

    with pytest.raises(HTTPException) as failed_response:
        asyncio.run(
            api.generate_job(
                "job-1",
                api.FactoryGenerateBody(
                    preview_token="token",
                    rights_confirmed=True,
                    anonymized_confirmed=True,
                    third_party_confirmed=True,
                ),
                None,
            )
        )

    assert failed_response.value.status_code == 502
    assert "整批" in str(failed_response.value.detail)
    assert completed == []
    assert len(failed) == 1
    assert failed[0][1]["error_code"] == "invalid_provider_output"
    assert failed[0][1]["response_bytes"] == len("this is not JSON".encode("utf-8"))
    assert provider_kwargs["max_output_tokens"] == 3000


def test_terminal_provider_error_preserves_usage_and_resolved_model(monkeypatch):
    job = _job()
    _install_generation_fakes(monkeypatch, job)
    failed = []
    monkeypatch.setattr(
        api,
        "claim_attempt",
        lambda *_args, **_kwargs: {"id": "attempt-1", "attempt_no": 1},
    )
    monkeypatch.setattr(api, "mark_provider_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        api,
        "fail_attempt",
        lambda *args, **kwargs: failed.append((args, kwargs)),
    )

    async def truncated(*_args, **kwargs):
        kwargs["on_provider_attempt"]()
        error = ValueError("AI provider response was incomplete")
        error.usage = {
            "input_tokens": 17,
            "output_tokens": 29,
            "provider_request_id": "provider-request-789",
            "resolved_provider_model": "resolved/free-model",
            "cost_source": "openrouter_response_usage",
        }
        raise error

    monkeypatch.setattr(ai_provider, "generate_text", truncated)

    with pytest.raises(HTTPException) as unavailable:
        asyncio.run(
            api.generate_job(
                "job-1",
                api.FactoryGenerateBody(
                    preview_token="token",
                    rights_confirmed=True,
                    anonymized_confirmed=True,
                    third_party_confirmed=True,
                ),
                None,
            )
        )

    assert unavailable.value.status_code == 502
    assert len(failed) == 1
    usage = failed[0][1]["usage"]
    assert usage["input_tokens"] == 17
    assert usage["output_tokens"] == 29
    assert usage["provider_request_id"] == "provider-request-789"
    assert usage["resolved_provider_model"] == "resolved/free-model"


def test_stale_processing_job_is_listed_as_failed_for_manual_retry(monkeypatch):
    db = _Db([
        _frame({"total": 1}),
        _frame({
            "id": "job-stale",
            "source_id": "source-1",
            "recipe_key": RAG_KNOWLEDGE_CARD_RECIPE,
            "requested_count": 3,
            "instruction_text": "",
            "status": "failed",
            "model_label": "Gemini 3.5 Flash",
            "created_by": "manager",
            "created_at": "2026-07-20T00:00:00+00:00",
            "updated_at": "2026-07-20T00:00:00+00:00",
            "source_kind": "admin_paste",
            "source_title": "已停滯工作",
            "attempt_count": 1,
            "error_message": None,
        }),
    ])
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)

    result = api.jobs(None)

    assert result["items"][0]["status"] == "failed"
    query, params = db.queries[1]
    assert (
        "CASE WHEN j.status='processing' AND j.updated_at<:stale_cutoff "
        "THEN 'failed' ELSE j.status END AS status"
    ) in query
    assert "stale_cutoff" in params


def test_review_rejects_schema_extras_before_persisting_approval(monkeypatch):
    source_text = "免費公共交通可以減少市民交通開支。"
    db = _Db([
        _frame({
            "review_status": "pending",
            "recipe_key": RAG_KNOWLEDGE_CARD_RECIPE,
            "content_text": source_text,
            "withdrawn_at": None,
            "invalidated_at": None,
            "job_invalidated_at": None,
        })
    ])
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(
        api,
        "review_item",
        lambda *_args, **_kwargs: pytest.fail("invalid review must not be saved"),
    )
    quote = "免費公共交通可以減少市民交通開支"
    ref = {"start": 0, "end": len(quote), "quote": quote}
    fact = {
        "text": quote,
        "fact_status": "source_backed",
        "source_refs": [ref],
        "synthetic_note": "",
    }
    candidate = {
        "title": "公共交通政策",
        "side": "pro",
        "stage": "general",
        "skills": ["evidence"],
        "topic_tags": ["交通"],
        "summary": fact,
        "claims": [fact],
        "limitations": [],
        "unexpected_provider_field": True,
    }

    with pytest.raises(HTTPException) as invalid:
        api.review(
            "item-1",
            api.FactoryReviewBody(
                reviewed_payload=candidate,
                status="approved",
                expected_revision=0,
            ),
            None,
        )

    assert invalid.value.status_code == 400


def test_sft_release_jsonl_line_contains_only_messages_and_leakage_group(monkeypatch):
    messages = [
        {"role": "system", "content": "固定教練指示"},
        {"role": "user", "content": "請評改呢段演辭"},
        {"role": "assistant", "content": "論點清楚，但要補證據。"},
    ]
    reviewed = {
        "title": "演辭評改",
        "side": "pro",
        "stage": "opening",
        "skills": ["evidence"],
        "topic_tags": ["交通"],
        "messages": messages,
        "message_provenance": [{"private": "must not leak into SFT JSONL"}],
    }
    row = {
        "id": "item-1",
        "attempt_id": "attempt-1",
        "attempt_no": 1,
        "original_sha256": "original-sha",
        "reviewed_json": reviewed,
        "reviewed_sha256": api.content_hash(reviewed),
        "review_status": "approved",
        "reviewed_by": "reviewer",
        "reviewed_at": "2026-07-20T12:00:00+00:00",
        "invalidated_at": None,
        "recipe_key": "sft_speech_critique_v1",
        "job_invalidated_at": None,
        "model_label": "OpenRouter Free",
        "provider": "openrouter",
        "requested_provider_model": "openrouter/free",
        "resolved_provider_model": "free-provider/resolved-model",
        "provider_request_id": "openrouter-request-456",
        "recipe_version": "ai-data-factory-2026-07-20-v1",
        "prompt_sha256": "prompt-sha",
        "response_sha256": "response-sha",
        "confirmation_version": api.CONFIRMATION_VERSION,
        "anonymization_confirmed": True,
        "rights_confirmed": True,
        "third_party_confirmed": True,
        "pii_warning_count": 1,
        "pii_override_used": True,
        "confirmed_by": "manager",
        "confirmed_at": "2026-07-20T11:00:00+00:00",
        "source_id": "source-1",
        "source_group_id": "source-group-1",
        "revision_no": 1,
        "source_sha256": "source-sha",
        "rights_basis": "permission",
        "rights_confirmed_by": "manager",
        "rights_confirmed_at": "2026-07-20T10:00:00+00:00",
        "source_withdrawn_at": None,
    }
    captured = {}
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", object()))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "_release_rows", lambda _db, _ids: [row])

    def save_release(*_args, **kwargs):
        captured.update(kwargs)
        return {"id": "sft-v000001"}

    monkeypatch.setattr(api, "create_release", save_release)

    result = api.publish_release(
        api.FactoryReleaseBody(dataset_kind="sft", item_ids=["item-1"]),
        None,
    )

    assert result == {"id": "sft-v000001"}
    line = json.loads(captured["jsonl_text"].strip())
    assert set(line) == {"messages", "leakage_group"}
    assert line["messages"] == messages
    assert line["leakage_group"] == "source:source-group-1"
    assert captured["schema_version"] == "ai-factory-sft-messages-jsonl-v1"
    lineage = captured["manifest"]["items"][0]
    assert lineage["source_rights"] == {
        "basis": "permission",
        "confirmed_by": "manager",
        "confirmed_at": "2026-07-20T10:00:00+00:00",
    }
    assert lineage["generation"]["requested_provider_model"] == "openrouter/free"
    assert lineage["generation"]["resolved_provider_model"] == (
        "free-provider/resolved-model"
    )
    assert lineage["generation"]["provider_request_id"] == (
        "openrouter-request-456"
    )
    assert lineage["generation"]["confirmation"]["pii_override_used"] is True
    assert lineage["review"] == {
        "original_sha256": "original-sha",
        "reviewed_by": "reviewer",
        "reviewed_at": "2026-07-20T12:00:00+00:00",
    }


def test_release_rows_load_generation_rights_and_review_lineage():
    db = _Db([
        _frame({
            "id": "item-1",
            "review_status": "approved",
            "invalidated_at": None,
            "job_invalidated_at": None,
            "source_withdrawn_at": None,
        }),
    ])

    rows = api._release_rows(db, ["item-1"])

    assert rows[0]["id"] == "item-1"
    sql, params = db.queries[0]
    assert "JOIN ai_factory_attempts a" in sql
    assert "a.resolved_provider_model" in sql
    assert "s.rights_basis" in sql
    assert "i.reviewed_by" in sql
    assert params == {"ids": ["item-1"]}


def test_admin_source_withdrawal_uses_the_soft_cascade(monkeypatch):
    captured = {}
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", object()))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)

    def soft_withdraw(_db, actor, source_id, reason):
        captured.update(actor=actor, source_id=source_id, reason=reason)
        return {"ok": True, "changed": True, "sources": [source_id]}

    monkeypatch.setattr(api, "withdraw_source", soft_withdraw)

    result = api.withdraw_factory_source(
        "src-1",
        None,
        api.FactoryWithdrawBody(reason="來源權利已撤回"),
    )

    assert result["changed"] is True
    assert captured == {
        "actor": "manager",
        "source_id": "src-1",
        "reason": "來源權利已撤回",
    }


@pytest.mark.parametrize("download", (api.download_jsonl, api.download_manifest))
def test_essential_bandwidth_mode_blocks_release_download_before_accounting(
    monkeypatch, download,
):
    release = {
        "id": "rag-v000001",
        "jsonl_text": "{}\n",
        "jsonl_sha256": "release-sha",
        "manifest_json": {},
        "manifest_sha256": "manifest-sha",
        "invalidated_at": None,
    }
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", object()))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "get_release_for_download", lambda *_args: release)
    monkeypatch.setattr(
        api,
        "_require_nonessential_bandwidth",
        lambda: (_ for _ in ()).throw(
            HTTPException(429, "網絡傳輸量已進入必要服務模式")
        ),
    )
    monkeypatch.setattr(
        api,
        "_account_download",
        lambda *_args: pytest.fail("blocked download must not be accounted as sent"),
    )

    with pytest.raises(HTTPException) as blocked:
        download("rag-v000001", None)

    assert blocked.value.status_code == 429
    assert blocked.value.detail == "網絡傳輸量已進入必要服務模式"


def test_invalidated_release_download_is_gone(monkeypatch):
    release = {
        "id": "rag-v000001",
        "jsonl_text": "{}\n",
        "jsonl_sha256": "release-sha",
        "manifest_json": {},
        "manifest_sha256": "manifest-sha",
        "invalidated_at": "2026-07-20T00:00:00+00:00",
    }
    db = _Db([_frame(release)])
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "_require_nonessential_bandwidth", lambda: None)

    with pytest.raises(HTTPException) as gone:
        api.download_jsonl("rag-v000001", None)

    assert gone.value.status_code == 410
    assert len(db.queries) == 1
