import json
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core import auth_logic, funds_logic, home_logic, members, registration_logic


ROOT = Path(__file__).resolve().parents[1]


class QueryDb:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.queries = []

    def query(self, sql, params=None):
        self.queries.append((sql, dict(params or {})))
        return self.responses.pop(0) if self.responses else pd.DataFrame()


class LegacyAccountDb:
    def __init__(self):
        self.executed = []

    def query(self, sql, params=None):
        if "FROM app_config" in sql or "FROM system_config" in sql:
            return pd.DataFrame()
        if "FROM accounts" in sql:
            return pd.DataFrame([{"password_hash": "legacy-password"}])
        raise AssertionError(sql)

    def execute(self, sql, params=None):
        self.executed.append((sql, dict(params or {})))


class OperationsEfficiencyTests(unittest.TestCase):
    def test_member_count_and_single_member_check_stay_in_sql(self):
        count_db = QueryDb([pd.DataFrame([{"active_count": 7}])])
        self.assertEqual(members.count_active_members(count_db), 7)
        self.assertIn("COUNT(*)", count_db.queries[0][0])
        self.assertNotIn("participated_votes", count_db.queries[0][0])

        member_db = QueryDb([pd.DataFrame([{"is_active": True}])])
        state = members.member_activity("alice", member_db)
        self.assertTrue(state["is_active"])
        self.assertEqual(len(member_db.queries), 1)
        self.assertEqual(member_db.queries[0][1], {"user_id": "alice"})

    def test_home_configuration_is_loaded_in_one_query(self):
        db = QueryDb([pd.DataFrame([
            {"key": "maintenance_mode", "value": "true"},
            {"key": "maintenance_deadline", "value": "2026-07-20T12:00"},
        ])])
        values = home_logic._get_configs(
            db, ("maintenance_mode", "maintenance_deadline")
        )
        self.assertIs(values["maintenance_mode"], True)
        self.assertEqual(len(db.queries), 1)
        self.assertIn("key IN", db.queries[0][0])

    def test_lateness_period_totals_use_one_aggregate_query(self):
        db = QueryDb([pd.DataFrame([{
            "count": 3,
            "received": 12,
            "penalties": 20,
            "expenses": 4,
            "opening": 5,
        }])])
        result = funds_logic._lateness_totals(2025, db)
        self.assertEqual(result["opening"] + result["received"] - result["expenses"], 13)
        self.assertEqual(len(db.queries), 1)
        self.assertIn("opening_balance", db.queries[0][0])
        self.assertIn("SUM(amount_hkd)", db.queries[0][0])

    def test_ai_role_check_does_not_load_fund_aggregates(self):
        db = QueryDb([pd.DataFrame([{"value": '["alice"]'}])])
        self.assertTrue(funds_logic.is_ai_treasurer("alice", db))
        self.assertEqual(len(db.queries), 1)
        self.assertNotIn("ai_fund_transactions", db.queries[0][0])

    def test_ai_dashboard_combines_fund_aggregates(self):
        db = QueryDb([
            pd.DataFrame([{"balance": 80, "pending": 10, "recent_usage": 3}]),
            pd.DataFrame([{"user_id": "alice"}, {"user_id": "bob"}]),
        ])
        with patch(
            "core.funds_logic._configs",
            return_value={"ai_fund_treasurers": ["alice"]},
        ):
            result = funds_logic.ai_data("alice", db)
        self.assertEqual(len(db.queries), 2)
        aggregate_sql = db.queries[0][0]
        self.assertIn("AS balance", aggregate_sql)
        self.assertIn("AS pending", aggregate_sql)
        self.assertIn("AS recent_usage", aggregate_sql)
        self.assertEqual(result["summary"]["suggested_per_member_hkd"], 210)

    def test_non_finite_money_is_not_forwarded_to_database(self):
        self.assertEqual(funds_logic._float(float("nan")), 0.0)
        self.assertEqual(funds_logic._float(float("inf")), 0.0)

    def test_successful_legacy_account_login_upgrades_password_hash(self):
        db = LegacyAccountDb()
        self.assertTrue(auth_logic.check_login("alice", "legacy-password", db=db))
        self.assertEqual(len(db.executed), 1)
        sql, params = db.executed[0]
        self.assertIn("UPDATE accounts", sql)
        self.assertTrue(params["password_hash"].startswith("$2"))

    def test_successful_legacy_config_login_upgrades_typed_secret(self):
        from api.admin_console_api import _verify_config_password_and_upgrade

        class ConfigDb:
            def __init__(self):
                self.executed = []

            def execute(self, sql, params=None):
                self.executed.append((sql, dict(params or {})))

        db = ConfigDb()
        self.assertTrue(_verify_config_password_and_upgrade(
            db, "developer_password", "legacy-password", "legacy-password"
        ))
        self.assertEqual(len(db.executed), 1)
        sql, params = db.executed[0]
        self.assertIn("app_config", sql)
        self.assertTrue(json.loads(params["value"]).startswith("$2"))

        rejected = ConfigDb()
        self.assertFalse(_verify_config_password_and_upgrade(
            rejected, "developer_password", "wrong", "legacy-password"
        ))
        self.assertEqual(rejected.executed, [])

    def test_registration_admin_uses_typed_config_and_upgrades_plaintext(self):
        db = object()
        with patch("core.registration_logic.get_config", return_value="legacy-password"), \
             patch("core.registration_logic.set_config") as store, \
             patch("core.registration_logic.append_login_record") as audit:
            result = registration_logic.check_admin_password("legacy-password", db=db)
        self.assertEqual(result, {"ok": True})
        stored_hash = store.call_args.args[2]
        self.assertTrue(stored_hash.startswith("$2"))
        audit.assert_called_once()

    def test_html_sources_are_not_collapsed_into_oversized_lines(self):
        for path in sorted((ROOT / "frontend").glob("*/index.html")):
            with self.subTest(path=path.relative_to(ROOT)):
                longest = max((len(line) for line in path.read_text(encoding="utf-8").splitlines()), default=0)
                self.assertLessEqual(longest, 500)

if __name__ == "__main__":
    unittest.main()
