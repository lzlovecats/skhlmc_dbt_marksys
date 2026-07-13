import json
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException

from api.admin_console_api import JsonSettings, developer_settings

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


if __name__ == "__main__":
    unittest.main()
