import asyncio
import json
from pathlib import Path

import pytest

import schema
from ai_model_config import LMC_AI_DEFAULT_MODEL, LMC_AI_DEEP_MODEL, LMC_AI_MODEL_PROFILE_VERSION
from core.ai_eval_defaults import (
    EVAL_SUITE_ID, case_content_hash, load_eval_cases, suite_hash,
)
from core.lmc_ai_eval import (
    EVAL_MODES, REVIEW_DIMENSIONS, aggregate_campaign, build_eval_prompt,
    generation_order, prompt_fingerprint, validate_review_payload,
)
from core.lmc_ai_runtime import LocalAIRuntime, backend_fingerprint
from core.schema_features import FEATURE_MIGRATION_VERSIONS
from system_limits import (
    LMC_AI_EVAL_CAMPAIGN_MAX, LMC_AI_EVAL_GENERATION_ATTEMPT_MAX,
    LMC_AI_EVAL_OUTPUT_MAX_BYTES, LMC_AI_EVAL_PROCESSING_LEASE_SECONDS,
    LMC_AI_EVAL_REVIEW_NOTE_MAX_CHARS, LMC_AI_EVAL_REVIEWS_PER_PAIR,
    LMC_AI_REQUEST_TIMEOUT_SECONDS,
)


ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / "migrations" / "20260721_0001_add_lmc_ai_eval.up.sql"
DOWN = ROOT / "migrations" / "20260721_0001_add_lmc_ai_eval.down.sql"


def _migration_seed():
    sql = UP.read_text(encoding="utf-8")
    return json.loads(sql.split("$eval_cases$", 2)[1])


def test_fixed_suite_asset_migration_and_bootstrap_hashes_match():
    cases = load_eval_cases()
    seeded = _migration_seed()
    assert len(cases) == len(seeded) == 30
    assert len({case["case_id"] for case in cases}) == 30
    assert [case["case_id"] for case in cases] == [case["case_id"] for case in seeded]
    assert [case["content_hash"] for case in cases] == [case["content_hash"] for case in seeded]
    assert all(case_content_hash(case) == case["content_hash"] for case in cases)
    assert len(suite_hash()) == len(prompt_fingerprint()) == 64


def test_eval_migration_is_private_versioned_and_rollback_refuses_data_loss():
    up = UP.read_text(encoding="utf-8")
    down = DOWN.read_text(encoding="utf-8")
    for table in ("ai_eval_cases", "ai_eval_campaigns", "ai_eval_outputs", "ai_eval_reviews"):
        assert f"CREATE TABLE public.{table}" in up
        assert f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM PUBLIC" in up
        assert table in down
    assert "skhlmc-feature:eval:20260721_0001" in up
    assert "feature='lmc_ai_eval'" in down
    assert "RAISE EXCEPTION" in down
    assert "uq_ai_eval_usage_operation_stage" in up
    assert FEATURE_MIGRATION_VERSIONS["eval"] == "20260721_0001"
    assert FEATURE_MIGRATION_VERSIONS["dataset_model"] is None
    assert FEATURE_MIGRATION_VERSIONS["rag"] is None
    assert schema.TABLE_AI_EVAL_RUNS == schema.TABLE_AI_EVAL_CAMPAIGNS


def test_phase2_limits_are_central_and_bounded():
    assert LMC_AI_EVAL_CAMPAIGN_MAX == 10
    assert LMC_AI_EVAL_OUTPUT_MAX_BYTES == 16 * 1024
    assert LMC_AI_EVAL_GENERATION_ATTEMPT_MAX == 3
    assert LMC_AI_EVAL_REVIEW_NOTE_MAX_CHARS == 500
    assert LMC_AI_EVAL_REVIEWS_PER_PAIR == 3
    assert LMC_AI_EVAL_PROCESSING_LEASE_SECONDS > LMC_AI_REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize("case", load_eval_cases(), ids=lambda case: case["case_id"])
def test_prompts_are_deterministic_and_never_include_reference(case):
    first = build_eval_prompt(case["task_type"], case["input"])
    second = build_eval_prompt(case["task_type"], dict(case["input"]))
    assert first == second
    assert case["reference_text"] not in first
    assert first.strip()


def test_generation_order_is_deterministic_permutation_and_balanced():
    orders = [generation_order(case["content_hash"]) for case in load_eval_cases()]
    assert all(set(order) == set(EVAL_MODES) and len(order) == 3 for order in orders)
    assert orders == [generation_order(case["content_hash"]) for case in load_eval_cases()]
    first_counts = {mode: sum(order[0] == mode for order in orders) for mode in EVAL_MODES}
    assert max(first_counts.values()) - min(first_counts.values()) <= 5


def test_review_validation_and_both_bad_denominator():
    choices = {dimension: "both_bad" for dimension in REVIEW_DIMENSIONS}
    assert validate_review_payload(choices) == choices
    cases = {"case": {"task_type": "speech_review"}}
    reviews = [{"case_id": "case", "left_mode": "daily", "right_mode": "complex", **choices}]
    outputs = [
        {"status": "succeeded", "attempt_count": 1, "input_tokens": 10, "output_tokens": 4, "duration_ms": 100},
        {"status": "succeeded", "attempt_count": 1, "input_tokens": 10, "output_tokens": 4, "duration_ms": 200},
    ]
    summary = aggregate_campaign(reviews, outputs, cases)
    assert summary["mode_scores"]["daily"]["overall"] == 0
    assert summary["mode_scores"]["complex"]["overall"] == 0
    assert summary["both_bad_cases"] == ["case"]
    assert summary["safety_failure_cases"] == ["case"]
    assert "denominator includes both_bad" in summary["scoring"]


def _hello(digests=True):
    models = [LMC_AI_DEFAULT_MODEL, LMC_AI_DEEP_MODEL]
    return {
        "type": "hello", "protocol": 1,
        "model_profile_version": LMC_AI_MODEL_PROFILE_VERSION,
        "name": "node", "runtime": "ollama", "runtime_version": "1",
        "model": LMC_AI_DEFAULT_MODEL, "models": models,
        "model_digests": {model: ("a" if model == LMC_AI_DEFAULT_MODEL else "b") * 64 for model in models} if digests else None,
        "ready": True, "draining": False,
        "capabilities": {"chat": True, "rag": False, "fine_tuned": False, "thinking_control": True},
    }


def test_node_exact_digest_changes_backend_fingerprint_and_is_exposed_privately():
    clean = LocalAIRuntime.validate_hello(_hello())
    assert clean["model_digests"][LMC_AI_DEFAULT_MODEL] == "a" * 64
    assert backend_fingerprint("node", LMC_AI_DEFAULT_MODEL, model_digest="a" * 64) != backend_fingerprint(
        "node", LMC_AI_DEFAULT_MODEL, model_digest="b" * 64,
    )
    invalid = _hello()
    invalid["model_digests"].pop(LMC_AI_DEEP_MODEL)
    with pytest.raises(ValueError):
        LocalAIRuntime.validate_hello(invalid)


def test_frontend_has_accessible_third_tab_blind_form_and_stale_guard():
    html = (ROOT / "frontend/lmc_ai/index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend/lmc_ai/app.js").read_text(encoding="utf-8")
    assert 'id="abTestTab"' in html and 'role="tablist"' in html
    assert 'role="tabpanel"' in html and 'aria-selected="false"' in html
    assert "abLeftAnswer" in html and "abRightAnswer" in html
    assert "SafeMarkdown.render" in js
    assert "abGeneration" in js and "campaign_id" in js
    assert '$("composer").classList.toggle("hidden", button.dataset.panel !== "chatPanel")' in js


def test_api_routes_and_export_privacy_contract_are_present():
    source = (ROOT / "api/lmc_ai_eval_api.py").read_text(encoding="utf-8")
    for path in (
        '"/bootstrap"', '"/campaigns"', '"/campaigns/{campaign_id}/generate-next"',
        '"/campaigns/{campaign_id}/open-review"', '"/campaigns/{campaign_id}/reviews/next"',
        '"/reviews/{review_id}"', '"/campaigns/{campaign_id}/close"',
        '"/campaigns/{campaign_id}/invalidate"', '"/campaigns/{campaign_id}/results"',
        '"/campaigns/{campaign_id}/export.json"',
    ):
        assert path in source
    store = (ROOT / "core/lmc_ai_eval_store.py").read_text(encoding="utf-8")
    export_select = store.split("def manager_export", 1)[1]
    assert "review_id and reviewer_user_id" in export_select
    assert '"reviews"' in export_select
