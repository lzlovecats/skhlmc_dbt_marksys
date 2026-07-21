from pathlib import Path
import re

import schema
import system_limits
from core import funds_logic, schema_features
from core.db_migrations import browser_privilege_revokes, created_table_names


ROOT = Path(__file__).resolve().parents[1]
UP_PATH = ROOT / "migrations/20260720_0001_provision_ai_data_factory.up.sql"
DOWN_PATH = ROOT / "migrations/20260720_0001_provision_ai_data_factory.down.sql"
BUDGET_GATE_UP_PATH = (
    ROOT / "migrations/20260720_0008_remove_ai_factory_budget_gate.up.sql"
)
BUDGET_GATE_DOWN_PATH = (
    ROOT / "migrations/20260720_0008_remove_ai_factory_budget_gate.down.sql"
)
TRANSCRIPT_UP_PATH = (
    ROOT / "migrations/20260720_0009_add_transcript_structure_factory.up.sql"
)
TRANSCRIPT_DOWN_PATH = (
    ROOT / "migrations/20260720_0009_add_transcript_structure_factory.down.sql"
)
AI_TRAINING_API_PATH = ROOT / "api/ai_training_api.py"

FACTORY_TABLES = {
    "ai_factory_sources",
    "ai_factory_jobs",
    "ai_factory_attempts",
    "ai_factory_items",
    "ai_factory_topic_tags",
    "ai_factory_item_tags",
    "ai_factory_releases",
    "ai_factory_release_items",
}

TRANSCRIPT_TABLES = {
    "ai_factory_transcripts",
    "ai_factory_transcript_runs",
    "ai_factory_transcript_windows",
    "ai_factory_transcript_attempts",
    "ai_factory_transcript_segments",
}

PERMANENT_FACTORY_AUDIT_ACTIONS = {
    "factory_source_created",
    "factory_source_withdrawn",
    "factory_item_reviewed",
    "factory_item_withdrawn",
    "factory_item_invalidated",
    "factory_topic_tag_approved",
    "factory_topic_tag_retired",
    "factory_release_published",
    "factory_release_invalidated",
    "factory_transcript_withdrawn",
}


def test_data_factory_feature_is_explicit_and_old_optional_bundles_stay_disabled():
    assert schema_features.FEATURE_MIGRATION_VERSIONS == {
        "data_factory": "20260720_0009",
        "lmc_ai": "20260720_0010",
        "dataset_model": None,
        "eval": None,
        "rag": None,
    }


def test_factory_migration_creates_only_private_non_vector_factory_tables():
    up = UP_PATH.read_text(encoding="utf-8")

    assert created_table_names(up) == FACTORY_TABLES
    assert browser_privilege_revokes(up) == FACTORY_TABLES
    assert "skhlmc-feature:data_factory:20260720_0001" in up
    assert "CREATE EXTENSION" not in up.upper()
    assert " vector(" not in up.lower()
    assert "CREATE TABLE public.rag_" not in up
    assert "operation_id = job_id" in up
    assert "third_party_confirmed = TRUE" in up
    assert "pii_warning_count" in up
    assert "pii_override_reason" in up
    assert "provider_request_id" in up
    assert "resolved_provider_model" in up
    assert "attempt_no BETWEEN 1 AND 3" in up
    assert "'claimed', 'running', 'succeeded', 'failed', 'discarded'" in up
    assert "rag_knowledge_card_v1" in up
    assert "sft_attack_defence_v1" in up
    assert "idx_ai_factory_attempts_one_success" in up
    assert "idx_ai_factory_items_approved_hash" in up
    approved_index = up.split("idx_ai_factory_items_approved_hash", 1)[1].split(";", 1)[0]
    assert "WHERE review_status = 'approved'" in approved_index
    assert "invalidated_at" not in approved_index
    assert "jsonl_bytes = octet_length(jsonl_text)" in up
    assert "ON DELETE CASCADE" not in up


def test_factory_bootstrap_mirrors_tables_marker_and_privacy_boundary():
    ddl = schema.CREATE_AI_DATA_FACTORY
    lock = schema.LOCK_AI_DATA_FACTORY_PRIVILEGES

    for table in FACTORY_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
        assert table in lock
    assert "skhlmc-feature:data_factory:20260720_0009" in ddl
    assert "llm_submission" in ddl
    assert "admin_paste" in ddl
    assert "submission_confirmed" in ddl
    assert "yue-Hant-HK" in ddl
    assert "PUBLIC" in lock
    assert "'anon', 'authenticated'" in lock
    factory_index = schema.ALL_SCHEMAS.index(schema.CREATE_AI_DATA_FACTORY)
    assert schema.ALL_SCHEMAS[factory_index + 1] == schema.LOCK_AI_DATA_FACTORY_PRIVILEGES
    assert schema.ALL_SCHEMAS[factory_index + 2] == schema.CREATE_AI_FACTORY_TRANSCRIPT_WORKFLOW
    assert schema.ALL_SCHEMAS[factory_index + 3] == schema.LOCK_AI_FACTORY_TRANSCRIPT_PRIVILEGES


def test_transcript_factory_migration_is_private_versioned_and_reversible():
    up = TRANSCRIPT_UP_PATH.read_text(encoding="utf-8")
    down = TRANSCRIPT_DOWN_PATH.read_text(encoding="utf-8")

    assert created_table_names(up) == TRANSCRIPT_TABLES
    assert browser_privilege_revokes(up) == TRANSCRIPT_TABLES
    assert "skhlmc-feature:data_factory:20260720_0009" in up
    assert "transcript_structure_v1" in up
    assert "char_length(content_text) BETWEEN 1 AND 200000" in up
    assert "attempt_no BETWEEN 1 AND 3" in up
    assert "status IN ('draft', 'invalidated')" in up
    assert "factory_transcript_withdrawn" in up
    assert "factory_transcript_withdrawn" in schema.CREATE_INDICES
    assert "ON DELETE CASCADE" not in up
    assert "refusing to remove used transcript factory data" in down
    assert "DELETE FROM" not in down.upper()
    assert "skhlmc-feature:data_factory:20260720_0001" in down


def test_transcript_factory_bootstrap_matches_migration_tables_and_privacy():
    pattern = re.compile(
        r"CREATE TABLE(?: IF NOT EXISTS)? (?:public\.)?"
        r"(ai_factory_[a-z_]+) \((.*?)\n\);",
        re.DOTALL,
    )

    def definitions(sql):
        values = {}
        for name, body in pattern.findall(sql):
            if name not in TRANSCRIPT_TABLES:
                continue
            normalized = " ".join(body.replace("public.", "").split())
            normalized = re.sub(r"\(\s+", "(", normalized)
            normalized = re.sub(r"\s+\)", ")", normalized)
            values[name] = normalized
        return values

    migrated = definitions(TRANSCRIPT_UP_PATH.read_text(encoding="utf-8"))
    bootstrapped = definitions(schema.CREATE_AI_FACTORY_TRANSCRIPT_WORKFLOW)
    assert migrated == bootstrapped
    assert set(migrated) == TRANSCRIPT_TABLES
    for table in TRANSCRIPT_TABLES:
        assert table in schema.LOCK_AI_FACTORY_TRANSCRIPT_PRIVILEGES


def test_factory_bootstrap_table_definitions_match_the_migration():
    pattern = re.compile(
        r"CREATE TABLE(?: IF NOT EXISTS)? (?:public\.)?"
        r"(ai_factory_[a-z_]+) \((.*?)\n\);",
        re.DOTALL,
    )

    def definitions(sql):
        return {
            name: " ".join(body.replace("public.", "").split())
            for name, body in pattern.findall(sql)
        }

    migrated = definitions(UP_PATH.read_text(encoding="utf-8"))
    bootstrapped = definitions(schema.CREATE_AI_DATA_FACTORY)
    legacy_budget_constraint = " ".join(
        """CONSTRAINT ai_factory_attempts_budget_reservation
        CHECK (
            (estimated_cost_hkd = 0
                AND budget_provider_name IS NULL
                AND budget_period_month IS NULL
                AND budget_window_start IS NULL)
            OR
            (estimated_cost_hkd > 0
                AND char_length(budget_provider_name) BETWEEN 1 AND 80
                AND budget_period_month IS NOT NULL
                AND budget_window_start IS NOT NULL)
        ),""".split()
    )
    assert legacy_budget_constraint in migrated["ai_factory_attempts"]
    migrated["ai_factory_attempts"] = migrated["ai_factory_attempts"].replace(
        legacy_budget_constraint, ""
    )
    migrated["ai_factory_attempts"] = " ".join(
        migrated["ai_factory_attempts"].split()
    )
    assert migrated == bootstrapped
    assert set(migrated) == FACTORY_TABLES


def test_factory_head_removes_only_the_ai_fund_budget_gate():
    up = BUDGET_GATE_UP_PATH.read_text(encoding="utf-8")
    down = BUDGET_GATE_DOWN_PATH.read_text(encoding="utf-8")
    bootstrap = schema.CREATE_AI_DATA_FACTORY

    assert "DROP CONSTRAINT ai_factory_attempts_budget_reservation" in up
    assert "monthly_resource_limits" not in up
    assert "ai_fund_usage_logs" not in up
    assert "DROP COLUMN" not in up
    assert "estimated_cost_hkd" not in up
    assert "ai_factory_attempts_budget_reservation" not in bootstrap
    assert "estimated_cost_hkd" in bootstrap
    assert "ADD CONSTRAINT ai_factory_attempts_budget_reservation" in down
    assert "NOT VALID" in down


def test_factory_down_refuses_used_bundle_and_drops_in_dependency_order():
    down = DOWN_PATH.read_text(encoding="utf-8")

    assert "refusing to remove a used AI data factory" in down
    assert "feature = 'data_factory_generation'" in down
    assert "DELETE FROM" not in down.upper()
    order = [
        "DROP TABLE public.ai_factory_release_items",
        "DROP TABLE public.ai_factory_releases",
        "DROP TABLE public.ai_factory_item_tags",
        "DROP TABLE public.ai_factory_topic_tags",
        "DROP TABLE public.ai_factory_items",
        "DROP TABLE public.ai_factory_attempts",
        "DROP TABLE public.ai_factory_jobs",
        "DROP TABLE public.ai_factory_sources",
    ]
    offsets = [down.index(statement) for statement in order]
    assert offsets == sorted(offsets)


def test_factory_governance_audit_actions_are_permanent_in_schema_and_migration():
    migrations = "\n".join((
        UP_PATH.read_text(encoding="utf-8"),
        TRANSCRIPT_UP_PATH.read_text(encoding="utf-8"),
    ))
    runtime_maintenance = AI_TRAINING_API_PATH.read_text(encoding="utf-8")
    for action in PERMANENT_FACTORY_AUDIT_ACTIONS:
        assert action in migrations
        assert action in schema.CREATE_INDICES
        assert action in runtime_maintenance


def test_factory_generation_uses_shared_ai_fund_accounting(monkeypatch):
    assert (
        funds_logic.AI_FEATURE_LABELS["data_factory_generation"]
        == "辯論LLM資料工廠·生成"
    )
    assert "data_factory_generation" in funds_logic.AI_USAGE_FEATURES
    assert "data_factory_generation" in schema.CREATE_AI_FUND_USAGE_LOGS
    assert "data_factory_generation" in UP_PATH.read_text(encoding="utf-8")

    class RecordingDb:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((sql, params or {}))

    db = RecordingDb()
    monkeypatch.setattr(funds_logic, "prune_ai_usage", lambda _db: None)
    funds_logic.log_ai_usage(
        "manager",
        "data_factory_generation",
        True,
        {
            "operation_id": "factory-job-1",
            "operation_stage": "attempt_1",
            "provider": "gemini",
        },
        db=db,
    )
    params = db.calls[0][1]
    assert params["feature"] == "data_factory_generation"
    assert params["operation_id"] == "factory-job-1"
    assert params["operation_stage"] == "attempt_1"


def test_factory_resource_limits_match_the_reviewed_v0_contract():
    assert system_limits.AI_FACTORY_SOURCE_MAX_CHARS == 20_000
    assert system_limits.AI_FACTORY_INSTRUCTION_MAX_CHARS == 500
    assert system_limits.AI_FACTORY_CANDIDATE_DEFAULT == 3
    assert system_limits.AI_FACTORY_CANDIDATE_MAX == 5
    assert system_limits.AI_FACTORY_RAG_CONTENT_MAX_CHARS == 3_000
    assert system_limits.AI_FACTORY_RAG_CLAIM_MAX == 8
    assert system_limits.AI_FACTORY_SFT_USER_MAX_CHARS == 4_000
    assert system_limits.AI_FACTORY_SFT_ASSISTANT_MAX_CHARS == 6_000
    assert system_limits.AI_FACTORY_PREVIEW_TTL_SECONDS == 900
    assert system_limits.AI_FACTORY_ATTEMPT_MAX == 3
    assert system_limits.AI_FACTORY_CONCURRENCY == 2
    assert system_limits.AI_FACTORY_MANAGER_CONCURRENCY == 1
    assert system_limits.AI_FACTORY_TOPIC_TAG_MAX == 5
    assert system_limits.AI_FACTORY_TOPIC_TAG_MAX_CHARS == 40
    assert system_limits.AI_FACTORY_RELEASE_MAX_ITEMS == 500
    assert system_limits.AI_FACTORY_RELEASE_MAX_BYTES == 5 * 1024 * 1024
    assert system_limits.AI_PROVIDER_OUTPUT_MAX_TOKENS >= 18_000

    specs = system_limits.effective_limits()
    assert specs["AI_FACTORY_ATTEMPT_MAX"]["maximum"] == 3
    assert specs["AI_FACTORY_CONCURRENCY"]["maximum"] == 2
    assert specs["AI_FACTORY_MANAGER_CONCURRENCY"]["maximum"] == 1
    assert specs["AI_FACTORY_SOURCE_MAX_TOTAL"]["maximum"] == 2_000
    assert specs["AI_FACTORY_JOB_MAX_TOTAL"]["maximum"] == 10_000
    assert specs["AI_FACTORY_ITEM_MAX_TOTAL"]["maximum"] == 50_000
    assert specs["AI_FACTORY_RELEASE_MAX_TOTAL"]["maximum"] == 200
