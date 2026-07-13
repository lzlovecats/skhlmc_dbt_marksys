import json
import hashlib
import hmac
import datetime
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import HTTPException
from core.auth_logic import is_login_disabled

from api.admin_console_api import (
    BugUpdate,
    AccountAccessBody,
    JsonSettings,
    PasswordChange,
    change_system_password,
    developer_settings,
    set_account_access,
    update_bug,
)

ROOT = Path(__file__).resolve().parents[1]


class Transaction:
    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _statement, params):
        self.executed.append(dict(params))


class SettingsDb:
    def __init__(self, active_users=("alice", "bob")):
        self.active_users = list(active_users)
        self.executed = []

    def query(self, _sql, _params=None):
        return pd.DataFrame({"user_id": self.active_users})

    def transaction(self):
        return Transaction(self.executed)


class EmptyConfigSettingsDb(SettingsDb):
    def query(self, sql, params=None):
        if "FROM system_config" in sql:
            return pd.DataFrame()
        return super().query(sql, params)


class AccessDb:
    def __init__(self, configs):
        self.configs = configs
        self.executed = []

    def query(self, sql, params=None):
        if "SELECT 1 FROM accounts" in sql:
            return pd.DataFrame({"exists": [1]})
        if "FROM app_config" in sql:
            return pd.DataFrame(columns=["key", "value"])
        if "FROM system_config" in sql:
            if " IN (" in sql:
                keys = set((params or {}).values())
                return pd.DataFrame([
                    {"key": key, "value": value}
                    for key, value in self.configs.items() if key in keys
                ])
            key = (params or {}).get("key")
            return pd.DataFrame({"value": [self.configs[key]]}) if key in self.configs else pd.DataFrame()
        return pd.DataFrame()

    def transaction(self):
        return Transaction(self.executed)


class TokenRow:
    def __init__(self, key, value):
        self._mapping = {"key": key, "value": value}


class TokenResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class TokenConnection:
    def execute(self, statement, _params=None):
        if "FROM app_config" in str(statement):
            return TokenResult([])
        return TokenResult([
            TokenRow("cookie_secret", "secret"),
            TokenRow("login_disabled_accounts", '["alice"]'),
        ])


class TokenEngine:
    def begin(self):
        class Context:
            def __enter__(self):
                return TokenConnection()

            def __exit__(self, *_args):
                return False

        return Context()


class DeveloperSettingsTests(unittest.TestCase):
    def test_developer_page_uses_searchable_account_pickers(self):
        html = (ROOT / "frontend" / "dev_settings" / "index.html").read_text(encoding="utf-8")
        adapter = (ROOT / "frontend" / "dev_settings" / "lateness-managers.js").read_text(encoding="utf-8")
        for marker in (
            'id="ttsAllowedSearch"', 'id="ttsAllowedOptions"',
            'id="ttsReviewersSearch"', 'id="ttsReviewersOptions"',
            "selectedAccounts", "至少保留一位 AI 訓練管理員",
        ):
            self.assertIn(marker, html)
        self.assertNotIn('textarea id="ttsAllowed"', html)
        self.assertIn('selected("ttsReviewers")', adapter)

    def test_page_exposes_admin_controls_without_browser_prompts(self):
        html = (ROOT / "frontend" / "dev_settings" / "index.html").read_text(encoding="utf-8")
        for marker in (
            'id="pushForm"',
            'id="initDb"',
            'id="bypassUsers"',
            'id="providerOptions"',
            'id="actionDialog"',
            'class="confirm"',
        ):
            self.assertIn(marker, html)
        self.assertNotIn("prompt(", html)
        self.assertNotIn("confirm(", html)
        self.assertNotIn("data-delete", html)
        self.assertIn("停用帳戶", html)
        self.assertIn("重新啟用", html)

    def test_maintenance_controls_include_editable_deadline(self):
        developer_html = (ROOT / "frontend" / "dev_settings" / "index.html").read_text(encoding="utf-8")
        home_html = (ROOT / "frontend" / "home" / "index.html").read_text(encoding="utf-8")
        maintenance_card = home_html.split('<section id="maintenance"', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="maintenanceDeadline" type="datetime-local"', developer_html)
        self.assertIn('id="saveMaintDeadline"', developer_html)
        self.assertIn('href="/dev-settings"', maintenance_card)
        self.assertNotIn("2026年4月3日", home_html)

    def test_maintenance_deadline_is_saved_with_mode(self):
        db = SettingsDb()
        deadline = (
            datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
            + datetime.timedelta(hours=2)
        ).strftime("%Y-%m-%dT%H:%M")
        body = JsonSettings(values={
            "maintenance_mode": "true",
            "maintenance_deadline": deadline,
        })
        with patch("api.admin_console_api._require"), patch("api.admin_console_api._db", return_value=db):
            self.assertEqual(developer_settings(body, object()), {"ok": True})
        stored = {row["key"]: json.loads(row["value"]) for row in db.executed}
        self.assertIs(stored["maintenance_mode"], True)
        self.assertEqual(stored["maintenance_deadline"], deadline)

    def test_maintenance_mode_requires_a_deadline(self):
        db = EmptyConfigSettingsDb()
        body = JsonSettings(values={"maintenance_mode": "true"})
        with patch("api.admin_console_api._require"), patch("api.admin_console_api._db", return_value=db):
            with self.assertRaises(HTTPException) as caught:
                developer_settings(body, object())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("預期完成時間", caught.exception.detail)
        self.assertEqual(db.executed, [])

    def test_past_maintenance_deadline_is_rejected(self):
        db = SettingsDb()
        deadline = (
            datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
            - datetime.timedelta(minutes=1)
        ).strftime("%Y-%m-%dT%H:%M")
        body = JsonSettings(values={"maintenance_deadline": deadline})
        with patch("api.admin_console_api._require"), patch("api.admin_console_api._db", return_value=db):
            with self.assertRaises(HTTPException) as caught:
                developer_settings(body, object())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("必須在未來", caught.exception.detail)
        self.assertEqual(db.executed, [])

    def test_role_lists_are_deduplicated_before_atomic_save(self):
        db = SettingsDb()
        body = JsonSettings(values={
            "tts_recording_allowed_users": [" alice ", "alice", "bob"],
            "tts_recording_reviewers": ["bob"],
        })
        with patch("api.admin_console_api._require"), patch("api.admin_console_api._db", return_value=db):
            self.assertEqual(developer_settings(body, object()), {"ok": True})
        stored = {row["key"]: json.loads(row["value"]) for row in db.executed}
        self.assertEqual(stored["tts_recording_allowed_users"], ["alice", "bob"])
        self.assertEqual(stored["tts_recording_reviewers"], ["bob"])

    def test_unknown_or_disabled_role_account_is_rejected_before_any_write(self):
        db = SettingsDb(active_users=("alice",))
        body = JsonSettings(values={"tts_recording_reviewers": ["disabled-user"]})
        with patch("api.admin_console_api._require"), patch("api.admin_console_api._db", return_value=db):
            with self.assertRaises(HTTPException) as caught:
                developer_settings(body, object())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("不存在或已停用", caught.exception.detail)
        self.assertEqual(db.executed, [])

    def test_lateness_manager_list_still_requires_one_account(self):
        db = SettingsDb()
        body = JsonSettings(values={"lateness_fund_managers": []})
        with patch("api.admin_console_api._require"), patch("api.admin_console_api._db", return_value=db):
            with self.assertRaises(HTTPException) as caught:
                developer_settings(body, object())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(db.executed, [])

    def test_ai_training_reviewer_list_requires_one_account(self):
        db = SettingsDb()
        body = JsonSettings(values={"tts_recording_reviewers": []})
        with patch("api.admin_console_api._require"), patch("api.admin_console_api._db", return_value=db):
            with self.assertRaises(HTTPException) as caught:
                developer_settings(body, object())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("至少保留一位 AI 訓練管理員", caught.exception.detail)
        self.assertEqual(db.executed, [])

    def test_ai_default_model_must_belong_to_enabled_provider(self):
        db = SettingsDb()
        body = JsonSettings(values={
            "ai_enabled_providers": ["gemini"],
            "ai_default_model": "DeepSeek V4 Pro",
        })
        with patch("api.admin_console_api._require"), patch("api.admin_console_api._db", return_value=db):
            with self.assertRaises(HTTPException) as caught:
                developer_settings(body, object())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(db.executed, [])

    def test_password_change_requires_matching_confirmation(self):
        body = PasswordChange(new_password="new-secret", confirm_password="different")
        with patch("api.admin_console_api._require"):
            with self.assertRaises(HTTPException) as caught:
                change_system_password("admin_password", body, object())
        self.assertEqual(caught.exception.status_code, 400)

    def test_bug_status_is_allowlisted_before_database_write(self):
        body = BugUpdate(status="arbitrary")
        with patch("api.admin_console_api._require"):
            with self.assertRaises(HTTPException) as caught:
                update_bug(1, body, object())
        self.assertEqual(caught.exception.status_code, 400)

    def test_cannot_disable_the_only_ai_training_reviewer(self):
        db = AccessDb({"tts_recording_reviewers": '["alice"]'})
        with patch("api.admin_console_api._require"), patch("api.admin_console_api._db", return_value=db):
            with self.assertRaises(HTTPException) as caught:
                set_account_access("alice", AccountAccessBody(disabled=True), object())
        self.assertIn("請先加入另一位AI 訓練管理員", caught.exception.detail)
        self.assertEqual(db.executed, [])

    def test_disabling_account_preserves_history_and_removes_live_privileges(self):
        db = AccessDb({
            "tts_recording_reviewers": '["alice", "bob"]',
            "lateness_fund_managers": '["bob"]',
            "tts_recording_allowed_users": '["alice"]',
            "ai_fund_treasurers": '["alice"]',
            "bypass_active_check_until": '{"alice": "2099-01-01 00:00"}',
        })
        with patch("api.admin_console_api._require"), patch("api.admin_console_api._db", return_value=db):
            result = set_account_access("alice", AccountAccessBody(disabled=True), object())
        self.assertTrue(result["disabled"])
        stored = {row["key"]: json.loads(row["value"]) for row in db.executed if "key" in row}
        self.assertEqual(stored["tts_recording_reviewers"], ["bob"])
        self.assertEqual(stored["tts_recording_allowed_users"], [])
        self.assertEqual(stored["ai_fund_treasurers"], [])
        self.assertEqual(stored["bypass_active_check_until"], {})
        self.assertEqual(stored["login_disabled_accounts"], ["alice"])
        self.assertTrue(any(row.get("uid") == "alice" for row in db.executed))

    def test_login_block_is_separate_from_dormant_account_flag(self):
        db = AccessDb({"login_disabled_accounts": '["alice"]'})
        self.assertTrue(is_login_disabled("alice", db=db))
        self.assertFalse(is_login_disabled("bob", db=db))

    def test_existing_signed_cookie_is_rejected_immediately_after_disable(self):
        from deploy.proxy import _verify_committee_token

        def token(user):
            signature = hmac.new(b"secret", user.encode(), hashlib.sha256).hexdigest()
            return f"{user}:{signature}"

        with patch("deploy.proxy._get_db_engine", return_value=TokenEngine()):
            self.assertIsNone(_verify_committee_token(token("alice")))
            self.assertEqual(_verify_committee_token(token("bob")), "bob")


if __name__ == "__main__":
    unittest.main()
