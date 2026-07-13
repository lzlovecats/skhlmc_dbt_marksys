import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import patch

from tools import audit_db_schema as schema_audit


class DatabaseSchemaAuditTests(unittest.TestCase):
    def test_checksum_ignores_metrics_and_dictionary_order(self):
        schema_a = {
            "tables": [{"table_name": "accounts", "rls_enabled": False}],
            "columns": [{"column_name": "user_id", "ordinal_position": 1}],
        }
        schema_b = {
            "columns": [{"ordinal_position": 1, "column_name": "user_id"}],
            "tables": [{"rls_enabled": False, "table_name": "accounts"}],
        }
        first = schema_audit.build_snapshot(
            "public", "17", schema_a, [{"estimated_rows": 1}]
        )
        second = schema_audit.build_snapshot(
            "public", "17", schema_b, [{"estimated_rows": 999}]
        )
        self.assertEqual(first["schema_checksum"], second["schema_checksum"])
        self.assertNotEqual(first["metrics"], second["metrics"])

    def test_schema_change_changes_checksum(self):
        before = {"columns": [{"column_name": "user_id", "nullable": False}]}
        after = {"columns": [{"column_name": "user_id", "nullable": True}]}
        self.assertNotEqual(
            schema_audit.schema_checksum(before),
            schema_audit.schema_checksum(after),
        )

    def test_summary_contains_counts_without_schema_definitions(self):
        snapshot = schema_audit.build_snapshot(
            "public",
            "17",
            {
                "tables": [
                    {"table_name": "accounts", "rls_enabled": False},
                    {"table_name": "app_config", "rls_enabled": True},
                ],
                "columns": [{"column_name": "user_id"}],
                "functions": [{"definition": "secret implementation"}],
            },
            [{"estimated_rows": 5, "total_bytes": 1024}],
        )
        summary = schema_audit.snapshot_summary(snapshot)
        self.assertEqual(summary["object_counts"]["tables"], 2)
        self.assertEqual(summary["rls_enabled_tables"], ["app_config"])
        self.assertEqual(summary["estimated_table_rows"], 5)
        self.assertEqual(summary["total_relation_bytes"], 1024)
        self.assertNotIn("schema", summary)
        self.assertNotIn("secret implementation", str(summary))

    def test_json_normalization_is_deterministic(self):
        value = {
            "date": dt.date(2026, 7, 13),
            "whole": Decimal("12"),
            "fraction": Decimal("1.25"),
            "roles": ("member", "admin"),
        }
        self.assertEqual(
            schema_audit._json_safe(value),
            {
                "date": "2026-07-13",
                "whole": 12,
                "fraction": "1.25",
                "roles": ["member", "admin"],
            },
        )

    def test_identifier_quoting_cannot_escape(self):
        self.assertEqual(
            schema_audit._quote_identifier('odd"name'),
            '"odd""name"',
        )

    def test_default_queries_are_catalog_only(self):
        combined = "\n".join(schema_audit.CATALOG_QUERIES.values())
        self.assertIn("pg_class", combined)
        self.assertIn("pg_policy", combined)
        self.assertIn("function_grants", schema_audit.CATALOG_QUERIES)
        self.assertIn("schema_grants", schema_audit.CATALOG_QUERIES)
        self.assertIn("sequence_grants", schema_audit.CATALOG_QUERIES)
        self.assertIn("default_grants", schema_audit.CATALOG_QUERIES)
        self.assertIn("types", schema_audit.CATALOG_QUERIES)
        self.assertNotIn("SELECT *", combined.upper())
        self.assertNotIn("COUNT(*)", combined.upper())

    def test_invalid_schema_attempts_no_database_connection(self):
        with patch.object(schema_audit, "_get_db_engine") as get_engine:
            status = schema_audit.main(["--schema", "public;DROP"])
        self.assertEqual(status, 2)
        get_engine.assert_not_called()

    def test_exact_row_counts_are_opt_in(self):
        parser = schema_audit.build_parser()
        self.assertFalse(parser.parse_args([]).exact_row_counts)
        self.assertTrue(
            parser.parse_args(["--exact-row-counts"]).exact_row_counts
        )


if __name__ == "__main__":
    unittest.main()
