import unittest

from tools.audit_app_config import build_report


class AppConfigAuditTests(unittest.TestCase):
    def test_report_checks_classification_without_secret_values(self):
        rows = [
            {
                "key": "admin_password", "namespace": "auth",
                "value_type": "string", "is_secret": True,
                "json_type": "string", "credential_is_bcrypt": True,
            },
            {
                "key": "developer_password", "namespace": "auth",
                "value_type": "string", "is_secret": True,
                "json_type": "string", "credential_is_bcrypt": True,
            },
            {
                "key": "sql_password", "namespace": "auth",
                "value_type": "string", "is_secret": True,
                "json_type": "string", "credential_is_bcrypt": True,
            },
            {
                "key": "cookie_secret", "namespace": "auth",
                "value_type": "string", "is_secret": True,
                "json_type": "string", "cookie_secret_strong": True,
            },
        ]
        report = build_report([row["key"] for row in rows], rows)
        self.assertTrue(report["migration_complete"])
        self.assertTrue(report["rotation_complete"])
        self.assertTrue(report["bridge_removal_ready"])
        self.assertNotIn("value", report)

    def test_report_blocks_missing_and_misclassified_keys(self):
        report = build_report(
            ["admin_password", "maintenance_mode"],
            [{
                "key": "admin_password", "namespace": "runtime",
                "value_type": "string", "is_secret": False,
                "json_type": "string", "credential_is_bcrypt": False,
            }],
        )
        self.assertEqual(report["missing_in_typed"], ["maintenance_mode"])
        self.assertTrue(report["metadata_mismatches"])
        self.assertFalse(report["bridge_removal_ready"])


if __name__ == "__main__":
    unittest.main()
