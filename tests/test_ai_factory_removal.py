"""The retired AI data factory must not leave callable or provisioned surfaces."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / "migrations" / "20260723_0001_remove_ai_data_factory.up.sql"
DOWN = ROOT / "migrations" / "20260723_0001_remove_ai_data_factory.down.sql"

FACTORY_TABLES = (
    "ai_factory_transcript_segments",
    "ai_factory_transcript_attempts",
    "ai_factory_transcript_windows",
    "ai_factory_transcript_runs",
    "ai_factory_transcripts",
    "ai_factory_release_items",
    "ai_factory_releases",
    "ai_factory_item_tags",
    "ai_factory_topic_tags",
    "ai_factory_items",
    "ai_factory_attempts",
    "ai_factory_jobs",
    "ai_factory_sources",
)


def test_removal_migration_purges_history_and_drops_every_factory_table():
    up = UP.read_text("utf-8")
    assert "DELETE FROM public.ai_fund_usage_logs" in up
    assert "feature = 'data_factory_generation'" in up
    assert "DELETE FROM public.ai_training_audit" in up
    assert "LEFT(target_type, 11) = 'ai_factory_'" in up
    assert "LEFT(action, 8) = 'factory_'" in up
    assert "data_factory_generation" not in up.split(
        "ADD CONSTRAINT ai_fund_usage_logs_feature_check", 1
    )[1]
    assert "%" not in up

    positions = [up.index(f"DROP TABLE public.{table}") for table in FACTORY_TABLES]
    assert positions == sorted(positions)


def test_irreversible_purge_has_an_explicit_backup_only_rollback():
    down = DOWN.read_text("utf-8")
    assert "irreversible" in down
    assert "pre-migration database backup" in down
    assert "RAISE EXCEPTION" in down


def test_runtime_and_bootstrap_no_longer_define_the_factory():
    proxy = (ROOT / "deploy" / "proxy.py").read_text("utf-8")
    schema = (ROOT / "schema.py").read_text("utf-8")
    features = (ROOT / "core" / "schema_features.py").read_text("utf-8")

    assert "ai_factory_router" not in proxy
    assert "TABLE_AI_FACTORY_" not in schema
    assert "CREATE_AI_DATA_FACTORY" not in schema
    assert '"data_factory"' not in features


def test_factory_implementation_files_are_removed():
    removed = (
        "api/ai_factory_api.py",
        "core/ai_data_factory.py",
        "core/ai_factory_store.py",
        "core/ai_transcript_factory.py",
        "core/ai_transcript_store.py",
    )
    assert all(not (ROOT / path).exists() for path in removed)
