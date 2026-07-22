from pathlib import Path

from core.schema_features import FEATURE_CATALOG, FEATURE_MIGRATION_VERSIONS
from tools.database_health import compact_health_report
from version import APP_VERSION, REQUIRED_SCHEMA_MIGRATION


ROOT = Path(__file__).resolve().parents[1]


def test_release_schema_contract_tracks_repository_head():
    migrations = sorted((ROOT / "migrations").glob("*.up.sql"))
    assert APP_VERSION == "4.10.6"
    assert migrations[-1].name.startswith(REQUIRED_SCHEMA_MIGRATION)
    assert "eval" not in FEATURE_MIGRATION_VERSIONS


def test_repository_head_permanently_removes_retired_local_ai_comparison_data():
    up = (ROOT / "migrations/20260722_0001_remove_lmc_ai_eval.up.sql").read_text(
        encoding="utf-8"
    )
    down = (ROOT / "migrations/20260722_0001_remove_lmc_ai_eval.down.sql").read_text(
        encoding="utf-8"
    )
    for table in (
        "ai_eval_reviews", "ai_eval_outputs", "ai_eval_campaigns", "ai_eval_cases",
    ):
        assert f"DROP TABLE public.{table};" in up
    assert "DROP INDEX public.uq_ai_eval_usage_operation_stage;" in up
    assert "irreversible" in down


def test_optional_feature_catalog_owns_each_table_once():
    table_owners = {}
    for feature, definition in FEATURE_CATALOG.items():
        assert definition.tables
        assert definition.lifecycle in {"active", "disabled"}
        assert definition.retention
        for table in definition.tables:
            assert table not in table_owners, (table, feature, table_owners.get(table))
            table_owners[table] = feature
    assert set(FEATURE_CATALOG) == {
        "data_factory", "lmc_ai", "dataset_model", "rag",
    }


def test_unified_database_health_tool_is_read_only_and_covers_core_checks():
    source = (ROOT / "tools" / "database_health.py").read_text(encoding="utf-8")
    assert '"mode": "read-only"' in source
    assert "SET TRANSACTION READ ONLY" in source
    for field in (
        '"migrations"', '"schema_reconciliation"', '"access"', '"config"',
        '"features"', '"activity"', '"r2_coverage"',
    ):
        assert field in source
    for mutation in (" INSERT ", " UPDATE ", " DELETE ", " DROP ", " ALTER "):
        assert mutation not in source.upper()


def test_default_database_health_output_is_compact():
    report = {
        "operation": "database-health", "mode": "read-only", "healthy": True,
        "checks": {}, "release": {},
        "migrations": {"history_valid": True, "at_head": True},
        "schema": {},
        "table_sizes": [{"table_name": f"table-{index}", "total_bytes": index} for index in range(20)],
        "activity": {"totals": {"live_rows": 1}}, "config": {},
        "features": {"rag": {
            "state": "disabled", "lifecycle": "disabled", "migration_version": None,
            "retention": "not provisioned", "table_presence": [{"name": "rag", "present": False}],
        }},
        "r2_coverage": [],
    }

    compact = compact_health_report(report)

    assert len(compact["largest_tables"]) == 10
    assert "schema_reconciliation" not in compact
    assert compact["features"]["rag"]["missing_tables"] == ["rag"]
