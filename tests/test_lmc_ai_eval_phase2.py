import asyncio
import json
from pathlib import Path

import pytest

import schema
from api import lmc_ai_eval_api
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
    LMC_AI_EVAL_REVIEW_ASSIGNMENT_TTL_SECONDS,
    LMC_AI_EVAL_REVIEW_NOTE_MAX_CHARS, LMC_AI_EVAL_REVIEWS_PER_PAIR,
    LMC_AI_REQUEST_TIMEOUT_SECONDS,
)


ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / "migrations" / "20260721_0001_add_lmc_ai_eval.up.sql"
DOWN = ROOT / "migrations" / "20260721_0001_add_lmc_ai_eval.down.sql"
LIFECYCLE_UP = ROOT / "migrations" / "20260721_0002_simplify_lmc_ai_eval_lifecycle.up.sql"
LIFECYCLE_DOWN = ROOT / "migrations" / "20260721_0002_simplify_lmc_ai_eval_lifecycle.down.sql"


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
    assert FEATURE_MIGRATION_VERSIONS["eval"] == "20260721_0002"
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
    assert LMC_AI_EVAL_REVIEW_ASSIGNMENT_TTL_SECONDS == 24 * 60 * 60


def test_eval_lifecycle_migration_tracks_reservations_and_export_gate():
    up = LIFECYCLE_UP.read_text(encoding="utf-8")
    down = LIFECYCLE_DOWN.read_text(encoding="utf-8")
    for column in (
        "expires_at", "released_at", "released_by", "release_reason",
        "exported_at", "exported_by",
    ):
        assert column in up
        assert column in down
    assert "skhlmc-feature:eval:20260721_0002" in up
    assert "skhlmc-feature:eval:20260721_0001" in down
    assert "RAISE EXCEPTION" in down
    assert FEATURE_MIGRATION_VERSIONS["eval"] == "20260721_0002"


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


def test_eval_generation_rejects_runtime_identity_change(monkeypatch):
    digest = "a" * 64
    claim = {
        "mode": "daily",
        "campaign": {
            "bound_node_id": "node-1",
            "model_manifest": {
                "daily": {
                    "model": LMC_AI_DEFAULT_MODEL,
                    "digest": digest,
                    "thinking": False,
                    "runtime": "ollama",
                    "runtime_version": "1.0",
                },
            },
        },
    }
    snapshot = {
        "online": True, "ready": True, "draining": False, "busy": False,
        "queue_length": 0, "models": [LMC_AI_DEFAULT_MODEL],
        "model_digests": {LMC_AI_DEFAULT_MODEL: digest},
        "runtime": "ollama", "runtime_version": "2.0",
    }
    monkeypatch.setattr(lmc_ai_eval_api, "get_active_node_id", lambda _db: "node-1")
    with pytest.raises(ValueError, match="runtime"):
        lmc_ai_eval_api._validate_bound_identity(object(), claim, snapshot)


def test_member_frontend_has_clear_feedback_tab_without_manager_controls():
    html = (ROOT / "frontend/lmc_ai/index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend/lmc_ai/app.js").read_text(encoding="utf-8")
    assert 'id="abTestTab"' in html and 'role="tablist"' in html
    assert '>測試並回饋</button>' in html
    assert 'role="tabpanel"' in html and 'aria-selected="false"' in html
    assert "匿名回答比較" in html
    assert "閱讀題目" in html and "完成六項回饋" in html
    assert "abLeftAnswer" in html and "abRightAnswer" in html
    assert "SafeMarkdown.render" in js
    assert "abGeneration" in js and "campaign_id" in js
    assert '$("composer").classList.toggle("hidden", button.dataset.panel !== "chatPanel")' in js
    assert "reviewer_completed" in js
    for manager_control in (
        "abCampaignHistory", "abManagerActions", "abCreate", "abGenerate",
        "abOpenReview", "abClose", "abInvalidate", "abExport", "abPurge",
    ):
        assert manager_control not in html
    assert "/generate-next" not in js
    assert "/open-review" not in js
    assert "/invalidate" not in js
    assert "/export.json" not in js
    assert "/purge" not in js


def test_ai_training_has_separate_local_ai_eval_manager_tab_and_workflow():
    html = (ROOT / "frontend/ai_training/index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend/ai_training/app.js").read_text(encoding="utf-8")
    assert html.count('data-admin="local-ai-eval"') == 1
    assert "⚖️ 自家 AI 評測" in html
    assert 'id="local-ai-eval" class="admin-pane"' in html
    for label in ("準備回答", "收集回饋", "完成結果"):
        assert label in html
    for control in (
        "localEvalCreate", "localEvalGenerate", "localEvalOpen",
        "localEvalClose", "localEvalInvalidate", "localEvalHistory",
        "localEvalAssignments", "localEvalResults",
    ):
        assert f'id="{control}"' in html
        assert control in js
    for route in (
        "/generate-next", "/open-review", "/close", "/invalidate",
        "/assignments", "/export.json", "/purge",
    ):
        assert route in js
    assert 'b.dataset.admin === "local-ai-eval"' in js
    assert "先下載完整紀錄，清除按鈕先會開放" in js
    assert 'confirmationInput.setCustomValidity("");' in js


def test_api_routes_and_export_privacy_contract_are_present():
    source = (ROOT / "api/lmc_ai_eval_api.py").read_text(encoding="utf-8")
    for path in (
        '"/bootstrap"', '"/campaigns"', '"/campaigns/{campaign_id}/generate-next"',
        '"/campaigns/{campaign_id}/open-review"', '"/campaigns/{campaign_id}/reviews/next"',
        '"/reviews/{review_id}"', '"/campaigns/{campaign_id}/close"',
        '"/campaigns/{campaign_id}/invalidate"', '"/campaigns/{campaign_id}/results"',
        '"/campaigns/{campaign_id}/export.json"',
        '"/campaigns/{campaign_id}/assignments"',
        '"/campaigns/{campaign_id}/assignments/{review_id}/release"',
        '"/campaigns/{campaign_id}/purge"',
    ):
        assert path in source
    store = (ROOT / "core/lmc_ai_eval_store.py").read_text(encoding="utf-8")
    export_select = store.split("def manager_export", 1)[1]
    assert "review_id and reviewer_user_id" in export_select
    assert '"reviews"' in export_select


def test_store_expires_abandoned_assignments_and_requires_export_before_purge():
    store = (ROOT / "core/lmc_ai_eval_store.py").read_text(encoding="utf-8")
    assignment = store.split("def next_assignment", 1)[1].split("def preview_assignment", 1)[0]
    assert "expires_at>NOW()" in assignment
    assert "released_at IS NULL" in assignment
    assert "LMC_AI_EVAL_REVIEW_ASSIGNMENT_TTL_SECONDS" in assignment
    assert "def list_pending_assignments" in store
    assert "reviewer_user_id" not in store.split("def list_pending_assignments", 1)[1].split("def ", 1)[0]
    purge = store.split("def purge_campaign", 1)[1]
    assert "exported_at" in purge
    assert "DELETE FROM" in purge
    assert "eval_campaign_purged" in purge
