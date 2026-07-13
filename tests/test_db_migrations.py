import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from core import db_migrations
from tools import manage_db_migrations as manager


SOURCE_CHECKSUM = "a" * 64


def write_manifest(directory: Path, **overrides) -> Path:
    payload = {
        "captured_on": "2026-07-13",
        "name": "production_baseline",
        "schema_name": "public",
        "source_schema_checksum": SOURCE_CHECKSUM,
        "source_table_count": 42,
        "version": "20260713_0000",
    }
    payload.update(overrides)
    path = directory / "baseline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class DatabaseMigrationCatalogTests(unittest.TestCase):
    def test_repository_catalog_is_valid_and_has_no_unpaired_sql(self):
        baseline, migrations = manager.load_catalog()
        self.assertEqual(baseline.version, "20260713_0000")
        self.assertEqual(baseline.source_table_count, 42)
        self.assertEqual(len(baseline.source_schema_checksum), 64)
        self.assertTrue(all(item.version > baseline.version for item in migrations))
        self.assertEqual(
            [(item.version, item.name) for item in migrations],
            [
                ("20260713_0001", "provision_resource_guards"),
                ("20260713_0002", "lock_resource_guard_privileges"),
            ],
        )

    def test_resource_guard_migration_is_strict_additive_and_paired(self):
        _baseline, migrations = manager.load_catalog()
        migration = migrations[0]
        for table in (
            "practice_daily_usage",
            "bandwidth_usage_logs",
            "r2_upload_intents",
            "ai_coach_prepare_usage",
        ):
            self.assertIn(f"CREATE TABLE {table}", migration.up_sql)
            self.assertIn(f"DROP TABLE {table}", migration.down_sql)
        for index in (
            "idx_bandwidth_usage_created",
            "idx_r2_upload_intents_quota",
            "idx_ai_coach_prepare_usage_user_created",
        ):
            self.assertIn(f"CREATE INDEX {index}", migration.up_sql)
        self.assertNotIn("IF NOT EXISTS", migration.up_sql)
        self.assertNotIn("DROP ", migration.up_sql)
        self.assertNotIn("CASCADE", migration.down_sql)
        self.assertIn("fk_ai_coach_prepare_usage_user", migration.up_sql)

    def test_resource_guard_browser_privileges_are_explicitly_revoked(self):
        _baseline, migrations = manager.load_catalog()
        migration = migrations[1]
        self.assertEqual(migration.name, "lock_resource_guard_privileges")
        for role in ("PUBLIC", "anon", "authenticated"):
            self.assertIn(role, migration.up_sql)
        for object_kind in ("TABLE", "SEQUENCE"):
            self.assertIn(
                f"REVOKE ALL PRIVILEGES ON {object_kind}",
                migration.up_sql,
            )
        self.assertNotIn("TO PUBLIC", migration.down_sql)

    def test_discovers_paired_migration_and_hashes_both_directions(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "20260714_0001_add_widget.up.sql").write_text(
                "CREATE TABLE widget (id integer);\n",
                encoding="utf-8",
            )
            (directory / "20260714_0001_add_widget.down.sql").write_text(
                "DROP TABLE widget;\n",
                encoding="utf-8",
            )
            first = db_migrations.discover_migrations(directory)[0]
            original_checksum = first.checksum
            (directory / "20260714_0001_add_widget.down.sql").write_text(
                "DROP TABLE IF EXISTS widget;\n",
                encoding="utf-8",
            )
            second = db_migrations.discover_migrations(directory)[0]
        self.assertEqual(first.version, "20260714_0001")
        self.assertNotEqual(original_checksum, second.checksum)

    def test_rejects_orphan_direction(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "20260714_0001_add_widget.up.sql").write_text(
                "SELECT 1;\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "missing down"):
                db_migrations.discover_migrations(directory)

    def test_rejects_duplicate_version_with_different_names(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for name in ("add_widget", "add_gadget"):
                for direction in ("up", "down"):
                    (directory / f"20260714_0001_{name}.{direction}.sql").write_text(
                        "SELECT 1;\n",
                        encoding="utf-8",
                    )
            with self.assertRaisesRegex(ValueError, "duplicate migration version"):
                db_migrations.discover_migrations(directory)

    def test_rejects_explicit_transaction_control_but_allows_do_body(self):
        self.assertEqual(
            db_migrations.explicit_transaction_control(
                "-- owned by runner\nBEGIN;\nSELECT 1;\nCOMMIT;"
            ),
            "BEGIN",
        )
        self.assertIsNone(db_migrations.explicit_transaction_control(
            "DO $$\nBEGIN\n  PERFORM 1;\nEND\n$$;"
        ))

    def test_manifest_rejects_extra_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = write_manifest(Path(folder), unexpected=True)
            with self.assertRaisesRegex(ValueError, "fields"):
                db_migrations.load_baseline_manifest(path)

    def test_catalog_requires_versions_newer_than_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            baseline = db_migrations.load_baseline_manifest(
                write_manifest(Path(folder))
            )
        migration = db_migrations.Migration(
            version=baseline.version,
            name="duplicate",
            up_sql="SELECT 1;",
            down_sql="SELECT 1;",
            checksum="b" * 64,
        )
        with self.assertRaisesRegex(ValueError, "duplicate migration version"):
            db_migrations.validate_catalog(baseline, [migration])

    def test_catalog_rejects_new_table_without_browser_role_revoke(self):
        with tempfile.TemporaryDirectory() as folder:
            baseline = db_migrations.load_baseline_manifest(
                write_manifest(Path(folder))
            )
        migration = db_migrations.Migration(
            version="20260714_0001",
            name="unsafe_table",
            up_sql="CREATE TABLE unsafe_table (id integer);",
            down_sql="DROP TABLE unsafe_table;",
            checksum="b" * 64,
        )
        with self.assertRaisesRegex(ValueError, "without explicit"):
            db_migrations.validate_catalog(baseline, [migration])

    def test_browser_role_revoke_parser_requires_all_roles(self):
        sql = """
        REVOKE ALL PRIVILEGES ON TABLE public.safe_table
        FROM PUBLIC, anon, authenticated;
        REVOKE ALL PRIVILEGES ON TABLE partial_table FROM anon;
        """
        self.assertEqual(
            db_migrations.browser_privilege_revokes(sql),
            {"safe_table"},
        )


class DatabaseMigrationHistoryTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as folder:
            self.baseline = db_migrations.load_baseline_manifest(
                write_manifest(Path(folder))
            )
        self.first = db_migrations.Migration(
            version="20260714_0001",
            name="first_change",
            up_sql="SELECT 1;",
            down_sql="SELECT 1;",
            checksum="b" * 64,
        )
        self.second = db_migrations.Migration(
            version="20260714_0002",
            name="second_change",
            up_sql="SELECT 2;",
            down_sql="SELECT 2;",
            checksum="c" * 64,
        )

    def _row(self, version, name, checksum):
        return {
            "version": version,
            "name": name,
            "migration_checksum": checksum,
        }

    def test_plan_reports_unknown_and_checksum_drift(self):
        rows = [
            self._row(
                self.baseline.version,
                self.baseline.name,
                "0" * 64,
            ),
            self._row("20990101_0001", "unknown", "d" * 64),
        ]
        report = db_migrations.plan_history(
            self.baseline,
            [self.first],
            rows,
        )
        self.assertFalse(report["history_valid"])
        self.assertEqual(
            report["checksum_mismatches"],
            [self.baseline.version],
        )
        self.assertEqual(report["unknown_applied_versions"], ["20990101_0001"])

    def test_plan_rejects_history_gap(self):
        rows = [
            self._row(
                self.baseline.version,
                self.baseline.name,
                self.baseline.checksum,
            ),
            self._row(self.second.version, self.second.name, self.second.checksum),
        ]
        report = db_migrations.plan_history(
            self.baseline,
            [self.first, self.second],
            rows,
        )
        self.assertEqual(report["history_gaps"], [self.second.version])
        self.assertFalse(report["history_valid"])

    def test_matching_baseline_is_idempotent(self):
        conn = Mock()
        with patch.object(
            db_migrations, "lock_transaction"
        ), patch.object(
            db_migrations, "ledger_exists", return_value=True
        ), patch.object(
            db_migrations,
            "verify_existing_history",
            return_value={"baseline_applied": True},
        ), patch.object(db_migrations, "ensure_ledger") as ensure:
            created = db_migrations.record_baseline(conn, self.baseline, [])
        self.assertFalse(created)
        ensure.assert_not_called()

    def test_ledger_security_aggregates_restricted_role_access(self):
        conn = MagicMock()
        conn.execute.return_value.mappings.return_value.one.return_value = {
            "owner_is_current_user": True,
            "rls_enabled": False,
            "rls_forced": False,
            "public_has_privileges": False,
            "anon_has_privileges": False,
            "authenticated_has_privileges": True,
            "app_backend_has_privileges": False,
        }
        with patch.object(db_migrations, "ledger_exists", return_value=True):
            report = db_migrations.ledger_security(conn, "public")
        self.assertTrue(report["owner_is_current_user"])
        self.assertFalse(report["public_has_privileges"])
        self.assertTrue(report["restricted_roles_have_privileges"])


class DatabaseMigrationToolTests(unittest.TestCase):
    def test_invalid_confirmation_attempts_no_database_access(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch.object(
            manager, "get_db_engine"
        ) as get_engine, patch.object(manager, "load_catalog") as load_catalog:
            status = manager.main([
                "baseline",
                "--apply",
                "--confirm",
                "wrong",
            ])
        self.assertEqual(status, 2)
        load_catalog.assert_not_called()
        get_engine.assert_not_called()
        self.assertIn("no database access", stderr.getvalue())

    def test_status_is_unhealthy_until_baseline_is_recorded(self):
        engine = Mock()
        report = {
            "history_valid": True,
            "baseline_applied": False,
        }
        with patch.object(
            manager, "load_catalog", return_value=(Mock(), [])
        ), patch.object(
            manager, "get_db_engine", return_value=engine
        ), patch.object(
            manager, "status_report", return_value=report
        ), contextlib.redirect_stdout(io.StringIO()):
            status = manager.main(["status"])
        self.assertEqual(status, 1)

    def test_apply_baseline_checks_checksum_before_creating_ledger(self):
        baseline, migrations = manager.load_catalog()
        engine = MagicMock()
        conn = engine.begin.return_value.__enter__.return_value
        with patch.object(manager, "lock_transaction"), patch.object(
            manager, "ledger_exists", return_value=False
        ), patch.object(
            manager,
            "audit_connection",
            return_value={"schema_checksum": "0" * 64},
        ), patch.object(
            manager,
            "snapshot_summary",
            return_value={"object_counts": {"tables": 42}},
        ), patch.object(manager, "record_baseline") as record:
            with self.assertRaisesRegex(RuntimeError, "checksum changed"):
                manager.apply_baseline(engine, baseline, migrations)
        record.assert_not_called()
        conn.execute.assert_called()

    def test_apply_baseline_is_noop_when_matching_history_exists(self):
        baseline, migrations = manager.load_catalog()
        engine = MagicMock()
        with patch.object(manager, "lock_transaction"), patch.object(
            manager, "ledger_exists", return_value=True
        ), patch.object(
            manager,
            "verify_existing_history",
            return_value={"baseline_applied": True},
        ), patch.object(manager, "audit_connection") as audit, patch.object(
            manager, "record_baseline"
        ) as record:
            report = manager.apply_baseline(engine, baseline, migrations)
        self.assertFalse(report["ledger_created"])
        audit.assert_not_called()
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
