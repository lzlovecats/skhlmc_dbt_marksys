import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from tools import migrate_app_config as migration


class AppConfigMigrationTests(unittest.TestCase):
    def test_classification_lists_unknown_keys_without_values(self):
        report = migration.classify_keys(
            ["maintenance_mode", "admin_password", "unclassified_setting"]
        )
        self.assertEqual(report["unknown_keys"], ["unclassified_setting"])
        self.assertIn("admin_password", report["registered_keys"])
        self.assertNotIn("value", report)

    def test_invalid_confirmation_attempts_no_database_access(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch.object(
            migration, "get_db_engine"
        ) as get_engine:
            status = migration.main(["--apply", "--confirm", "wrong"])
        self.assertEqual(status, 2)
        get_engine.assert_not_called()
        self.assertIn("no database access", stderr.getvalue())

    def test_dry_run_never_calls_apply(self):
        engine = Mock()
        report = {
            "mode": "dry-run",
            "ready_to_apply": True,
            "legacy_table_will_be_preserved": True,
        }
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch.object(
            migration, "get_db_engine", return_value=engine
        ), patch.object(migration, "preflight", return_value=report), patch.object(
            migration, "apply_migration"
        ) as apply:
            status = migration.main([])
        self.assertEqual(status, 0)
        apply.assert_not_called()
        self.assertIn('"mode": "dry-run"', stdout.getvalue())

    def test_apply_requires_successful_preflight(self):
        engine = Mock()
        with patch.object(
            migration, "get_db_engine", return_value=engine
        ), patch.object(
            migration, "preflight", return_value={"ready_to_apply": False}
        ), patch.object(migration, "apply_migration") as apply:
            status = migration.main([
                "--apply", "--confirm", migration.CONFIRMATION
            ])
        self.assertEqual(status, 1)
        apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
