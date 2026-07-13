import unittest

from tools import reconcile_db_schema as reconciliation


class DatabaseSchemaReconciliationTests(unittest.TestCase):
    def test_table_parser_ignores_constraints_and_nested_commas(self):
        ddl = """
        CREATE TABLE IF NOT EXISTS sample (
            id BIGSERIAL PRIMARY KEY,
            payload JSONB DEFAULT jsonb_build_object('left', 1, 'right', 2),
            label TEXT,
            CONSTRAINT sample_label_unique UNIQUE (label),
            CHECK (label IN ('a', 'b'))
        );
        ALTER TABLE sample ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
        """
        self.assertEqual(
            reconciliation._table_columns(ddl),
            {"sample": {"id", "payload", "label", "created_at"}},
        )

    def test_declared_inventory_contains_core_and_legacy_config_tables(self):
        inventory = reconciliation.declared_inventory()
        self.assertIn("accounts", inventory["tables"])
        self.assertIn("app_config", inventory["tables"])
        self.assertIn("system_config", inventory["tables"])
        self.assertIn("password_hash", inventory["columns"]["accounts"])
        self.assertIn(
            "committee_vote_activity_view",
            inventory["views"],
        )

    def test_report_separates_internal_inventory_and_column_drift(self):
        snapshot = {
            "schema_name": "public",
            "schema_checksum": "a" * 64,
            "schema": {
                "tables": [
                    {"table_name": "accounts"},
                    {"table_name": "production_extra"},
                    {"table_name": "schema_migrations"},
                ],
                "columns": [
                    {"table_name": "accounts", "column_name": "user_id"},
                    {"table_name": "accounts", "column_name": "legacy"},
                ],
                "views": [],
                "indexes": [],
            },
            "metrics": {
                "tables": [
                    {
                        "table_name": "production_extra",
                        "estimated_rows": 3,
                        "total_bytes": 8192,
                    }
                ],
            },
        }
        declared = {
            "tables": {"accounts", "code_extra"},
            "columns": {"accounts": {"user_id", "password_hash"}},
            "views": set(),
        }
        report = reconciliation.build_report(snapshot, declared)
        self.assertEqual(report["internal_tables"], ["schema_migrations"])
        self.assertEqual(report["production_only_tables"], ["production_extra"])
        self.assertEqual(
            report["production_only_table_metrics"]["production_extra"],
            {"estimated_rows": 3, "total_bytes": 8192},
        )
        self.assertEqual(report["code_only_tables"], ["code_extra"])
        self.assertEqual(
            report["column_name_drift"],
            [{
                "table_name": "accounts",
                "code_only_columns": ["password_hash"],
                "production_only_columns": ["legacy"],
            }],
        )

    def test_runtime_ddl_inventory_excludes_versioned_runner(self):
        inventory = reconciliation.runtime_ddl_inventory()
        sites = inventory["sites"]
        self.assertEqual(
            {site.split(":", 1)[0] for site in sites},
            {
                "api/ai_training_api.py",
                "core/config_store.py",
                "core/r2_storage.py",
                "deploy/proxy.py",
            },
        )
        self.assertEqual(len(sites), 21)
        self.assertEqual(
            set(inventory["references"]),
            reconciliation._RUNTIME_DDL_REFERENCE_ALLOWLIST,
        )
        self.assertEqual(
            set(inventory["direct_statements"]),
            {"ALTER TABLE"},
        )
        self.assertEqual(
            set(inventory["indirect_statements"]),
            {"ALTER TABLE", "CREATE INDEX", "CREATE TABLE"},
        )
        self.assertEqual(
            inventory["policy_violations"],
            {
                "unexpected_files": [],
                "unexpected_references": [],
                "unexpected_direct_statements": [],
                "unexpected_indirect_statements": [],
                "unexpected_indexes": [],
                "site_budget_exceeded_by": 0,
            },
        )
        self.assertFalse(any(site.startswith("core/db_migrations.py:") for site in sites))
        self.assertEqual(
            set(inventory["indexes"]),
            {"idx_ai_coach_prepare_usage_user_created"},
        )


if __name__ == "__main__":
    unittest.main()
