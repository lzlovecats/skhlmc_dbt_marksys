from contextlib import contextmanager
import unittest

import pandas as pd

from core.match_logic import delete_match, match_admin_data, save_match


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Row:
    def __init__(self, **values):
        self._mapping = values


class MatchDb:
    def __init__(self):
        self.session_calls = []
        self.changed = 1

    def query(self, sql, params=None):
        if "FROM matches ORDER BY" in sql:
            return pd.DataFrame([{
                "match_id": "測試場次", "match_date": None, "match_time": None,
                "topic_text": "", "pro_team": "", "con_team": "",
                "access_code_hash": None, "review_password_hash": None,
            }])
        if "FROM debaters" in sql:
            return pd.DataFrame(columns=["match_id", "side", "position", "debater_name"])
        if "FROM match_roster_links" in sql:
            return pd.DataFrame([
                {"side": "pro", "roster_token": "pro-token", "submitted_at": None, "created_at": None},
                {"side": "con", "roster_token": "con-token", "submitted_at": None, "created_at": None},
            ])
        raise AssertionError(sql)

    def execute(self, sql, params=None):
        return None

    def execute_count(self, sql, params=None):
        return self.changed

    @contextmanager
    def transaction(self):
        db = self

        class Session:
            def execute(self, statement, params=None):
                sql = str(statement)
                db.session_calls.append((sql, dict(params or {})))
                if "SELECT access_code_hash" in sql:
                    return Result(Row(access_code_hash=None, review_password_hash=None))
                return Result()

        yield Session()


class MatchLogicTests(unittest.TestCase):
    def test_new_match_payload_uses_streamlit_date_and_time_defaults(self):
        data = match_admin_data(db=MatchDb())
        self.assertRegex(data["default_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(data["default_time"], "16:00")

    def test_save_match_is_atomic_and_validates_time(self):
        db = MatchDb()
        invalid = save_match({"match_id": "測試場次", "match_time": "14:00"}, db=db)
        self.assertFalse(invalid["ok"])
        self.assertEqual(db.session_calls, [])

        result = save_match({"match_id": "測試場次", "match_date": "2026-07-12", "match_time": "16:00"}, db=db)
        self.assertTrue(result["ok"])
        self.assertEqual(len(db.session_calls), 10)
        self.assertIn("UPDATE matches", db.session_calls[1][0])
        self.assertEqual(sum("INSERT INTO debaters" in sql for sql, _ in db.session_calls), 8)

    def test_delete_missing_match_does_not_report_success(self):
        db = MatchDb()
        db.changed = 0
        result = delete_match("不存在", db=db)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
