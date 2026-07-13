import unittest
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException, Response

from core.funds_logic import (
    add_ai_transaction, ai_usage_summary, add_lateness_record, fiscal_label,
    fiscal_start, is_lateness_manager, lateness_managers, save_ai_admin,
    update_lateness_paid,
)


class FundDb:
    def __init__(self, config=None, account_exists=True):
        self.config = config or {}
        self.account_exists = account_exists
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, dict(params or {})))

    def execute_count(self, sql, params=None):
        self.executed.append((sql, dict(params or {})))
        return 1

    def query(self, sql, params=None):
        if "FROM system_config" in sql:
            key = (params or {}).get("key")
            return pd.DataFrame([{"value": self.config[key]}]) if key in self.config else pd.DataFrame()
        if "FROM accounts" in sql and "SELECT 1" in sql:
            return pd.DataFrame([{"exists": 1}]) if self.account_exists else pd.DataFrame()
        raise AssertionError(sql)


class LatenessFundLogicTests(unittest.TestCase):
    def test_default_and_configured_managers(self):
        self.assertEqual(lateness_managers(FundDb()), ["leungph"])
        db = FundDb({"lateness_fund_managers": '["alice", " bob "]'})
        self.assertEqual(lateness_managers(db), ["alice", "bob"])
        self.assertTrue(is_lateness_manager("bob", db))
        self.assertEqual(lateness_managers(FundDb({"lateness_fund_managers": "[]"})), ["leungph"])

    def test_record_validation_rejects_negative_paid_invalid_date_and_member(self):
        with self.assertRaisesRegex(ValueError, "不能為負數"):
            add_lateness_record("staff", "2026-01-01", "member", 2, -1, "", db=FundDb())
        with self.assertRaisesRegex(ValueError, "日期無效"):
            add_lateness_record("staff", "not-a-date", "member", 2, 0, "", db=FundDb())
        with self.assertRaisesRegex(ValueError, "不存在或已停用"):
            add_lateness_record("staff", "2026-01-01", "member", 2, 0, "", db=FundDb(account_exists=False))

    def test_valid_record_and_paid_update_keep_non_negative_amounts(self):
        db = FundDb()
        add_lateness_record("staff", "2026-01-01", "member", 2, 3.5, " note ", db=db)
        insert = next(params for sql, params in db.executed if "INSERT INTO lateness_fund_records" in sql)
        self.assertEqual(insert["date"], "2026-01-01")
        self.assertEqual(insert["paid"], 3.5)
        with self.assertRaisesRegex(ValueError, "不能為負數"):
            update_lateness_paid(1, -0.01, db=db)

    def test_fiscal_year_contract(self):
        self.assertEqual(fiscal_start("2026-08-31"), 2025)
        self.assertEqual(fiscal_start("2026-09-01"), 2026)
        self.assertEqual(fiscal_label(2025), "2025-26")

    def test_manager_gate_rejects_direct_mutation(self):
        from api.funds_api import AmountBody, LatenessRecord, lateness_opening, lateness_record
        with patch("api.funds_api._context", return_value=("ordinary", FundDb())), patch("core.funds_logic.is_lateness_manager", return_value=False):
            with self.assertRaises(HTTPException) as caught:
                lateness_opening(2025, AmountBody(amount=0), object())
        self.assertEqual(caught.exception.status_code, 403)
        with patch("api.funds_api._lateness_context", return_value=("ordinary", FundDb())), patch("core.funds_logic.is_lateness_manager", return_value=False):
            with self.assertRaises(HTTPException) as paid:
                lateness_record(LatenessRecord(late_date="2026-01-01", member_user_id="member", late_minutes=1, paid_amount=1), object())
        self.assertEqual(paid.exception.status_code, 403)

    def test_records_csv_uses_hk_date_and_utf8_filename(self):
        from api.funds_api import lateness_records_csv

        class CsvDb:
            def query(self, sql, params=None):
                return pd.DataFrame([{
                    "id": 1, "late_date": "2026-02-03", "member_user_id": "member",
                    "late_minutes": 4, "late_no": 2, "penalty_amount": 8,
                    "paid_amount": 3, "record_balance": -5, "note": "測試",
                    "created_by": "staff", "created_at": "2026-02-03 10:00:00",
                    "updated_at": None,
                }])

        with patch("api.funds_api._lateness_context", return_value=("member", CsvDb())):
            response = lateness_records_csv(object(), 2025)
        text = response.body.decode("utf-8")
        self.assertIn("03/02/2026", text)
        self.assertIn("HKD 8.00", text)
        self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])


class AiFundLogicTests(unittest.TestCase):
    def test_member_deposit_rejects_unlisted_payment_method(self):
        with self.assertRaisesRegex(ValueError, "付款方式"):
            add_ai_transaction("member", "member_deposit", 10, payment_method="crypto", db=FundDb())

    def test_refund_types_are_distinct_and_require_positive_amounts(self):
        db = FundDb()
        add_ai_transaction("staff", "provider_refund", 12, note="provider credit", confirmed=True, db=db)
        add_ai_transaction("staff", "member_refund", 5, note="member repayment", confirmed=True, db=db)
        inserts = [params for sql, params in db.executed if "INSERT INTO ai_fund_transactions" in sql]
        self.assertEqual([row["type"] for row in inserts], ["provider_refund", "member_refund"])
        with self.assertRaisesRegex(ValueError, "金額不正確"):
            add_ai_transaction("staff", "member_refund", -1, db=db)

    def test_usage_summary_is_scoped_for_regular_member(self):
        class SummaryDb:
            def __init__(self): self.params = None
            def execute(self, sql, params=None): pass
            def query(self, sql, params=None):
                self.params = params
                self.sql = sql
                return pd.DataFrame()
        db = SummaryDb(); ai_usage_summary("member", False, db=db)
        self.assertIn("user_id=:user", db.sql)
        self.assertEqual(db.params, {"user": "member"})

    def test_admin_settings_reject_negative_values_and_reset_reports_count(self):
        with patch("core.funds_logic.is_ai_treasurer", return_value=True):
            with self.assertRaisesRegex(ValueError, "不能為負數"):
                save_ai_admin("staff", {"kind":"settings","target_hkd":-1,"low_balance_hkd":0}, db=FundDb())
            result = save_ai_admin("staff", {"kind":"reset_usage"}, db=FundDb())
        self.assertEqual(result, {"deleted": 1})

    def test_already_processed_deposit_returns_conflict(self):
        from api.funds_api import StatusBody, ai_status
        with patch("api.funds_api._context", return_value=("staff", FundDb())), \
             patch("core.funds_logic.is_ai_treasurer", return_value=True), \
             patch("core.funds_logic.set_ai_transaction_status", return_value=0):
            with self.assertRaises(HTTPException) as caught:
                ai_status(1, StatusBody(status="confirmed"), object())
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
