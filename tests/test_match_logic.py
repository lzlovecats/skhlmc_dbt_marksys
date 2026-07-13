from contextlib import contextmanager
import unittest

import pandas as pd

from core.match_logic import (
    create_match,
    delete_match,
    draw_sides,
    ensure_roster_links,
    match_admin_data,
    reopen_link,
    save_match,
)


class Result:
    def __init__(self, row=None, rowcount=1):
        self.row = row
        self.rowcount = rowcount

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
        if "FROM debaters" in sql:
            return pd.DataFrame(columns=["match_id", "side", "position", "debater_name"])
        if "FROM matches ORDER BY" in sql:
            return pd.DataFrame([{
                "match_id": "測試場次", "match_date": None, "match_time": None,
                "topic_text": "", "pro_team": "", "con_team": "",
                "access_code_hash": None, "review_password_hash": None,
            }])
        if "JOIN matches" in sql:
            return pd.DataFrame([{
                "match_id": "測試場次", "side": "pro", "roster_token": "pro-token",
                "submitted_at": None, "match_date": None, "match_time": None,
                "topic_text": "", "pro_team": "", "con_team": "",
            }])
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
                recorded = [dict(item) for item in params] if isinstance(params, list) else dict(params or {})
                db.session_calls.append((sql, recorded))
                if "SELECT access_code_hash" in sql:
                    return Result(Row(access_code_hash=None, review_password_hash=None))
                return Result(rowcount=db.changed)

        yield Session()


class MatchLogicTests(unittest.TestCase):
    def test_create_uses_conflict_safe_single_write(self):
        class Db:
            def __init__(self, changed):
                self.changed = changed

            def query(self, sql, params=None):
                return pd.DataFrame([{"n": 1}])

            def execute_count(self, sql, params=None):
                self.sql = sql
                return self.changed

        created = Db(1)
        self.assertTrue(create_match("M1", db=created)["ok"])
        self.assertIn("ON CONFLICT (match_id) DO NOTHING", created.sql)
        duplicate = Db(0)
        self.assertFalse(create_match("M1", db=duplicate)["ok"])

    def test_draw_rejects_the_same_team_on_both_sides(self):
        result = draw_sides("同一隊", " 同一隊 ")
        self.assertFalse(result["ok"])
        self.assertIn("不能相同", result["message"])

    def test_roster_ddl_runs_once_per_proxy_engine(self):
        class Db:
            def __init__(self):
                self._engine = object()
                self.executed = []

            def execute(self, sql, params=None):
                self.executed.append(sql)

        db = Db()
        ensure_roster_links(db)
        ensure_roster_links(db)
        self.assertEqual(len(db.executed), 1)

    def test_new_match_payload_uses_current_date_and_time_defaults(self):
        data = match_admin_data(db=MatchDb())
        self.assertRegex(data["default_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(data["default_time"], "16:00")

    def test_compact_inventory_only_returns_selected_match_details(self):
        class CompactDb(MatchDb):
            def query(self, sql, params=None):
                if "FROM debaters" in sql:
                    self.debater_params = params
                    return pd.DataFrame([{
                        "match_id": "M2", "side": "pro", "position": 1,
                        "debater_name": "甲",
                    }])
                if "FROM matches ORDER BY" in sql:
                    return pd.DataFrame([
                        {
                            "match_id": match_id, "match_date": None, "match_time": None,
                            "topic_text": "", "pro_team": "", "con_team": "",
                            "access_code_hash": None, "review_password_hash": None,
                        }
                        for match_id in ("M1", "M2")
                    ])
                return super().query(sql, params)

        db = CompactDb()
        data = match_admin_data("M2", db=db, compact=True)
        self.assertEqual(db.debater_params, {"detail_match_id": "M2"})
        self.assertEqual(data["matches"][0], {"match_id": "M1"})
        self.assertEqual(data["matches"][1]["pro_1"], "甲")

    def test_save_match_is_atomic_and_validates_time(self):
        db = MatchDb()
        invalid = save_match({"match_id": "測試場次", "match_time": "14:00"}, db=db)
        self.assertFalse(invalid["ok"])
        self.assertEqual(db.session_calls, [])

        result = save_match({"match_id": "測試場次", "match_date": "2026-07-12", "match_time": "16:00"}, db=db)
        self.assertTrue(result["ok"])
        self.assertEqual(len(db.session_calls), 3)
        self.assertIn("UPDATE matches", db.session_calls[1][0])
        debater_write = next(params for sql, params in db.session_calls if "INSERT INTO debaters" in sql)
        self.assertEqual(len(debater_write), 8)

    def test_delete_missing_match_does_not_report_success(self):
        db = MatchDb()
        db.changed = 0
        result = delete_match("不存在", db=db)
        self.assertFalse(result["ok"])

    def test_reopen_missing_link_does_not_report_success(self):
        db = MatchDb()
        db.changed = 0
        result = reopen_link("M1", "pro", db=db)
        self.assertFalse(result["ok"])

    def test_roster_claim_and_all_writes_share_one_transaction(self):
        from core.match_logic import save_roster

        db = MatchDb()
        result = save_roster("pro-token", {
            "team_name": "測試隊", "debater_1": "甲", "debater_2": "乙",
            "debater_3": "丙", "debater_4": "丁",
        }, db=db)
        self.assertTrue(result["ok"])
        self.assertEqual(len(db.session_calls), 3)
        self.assertIn("UPDATE match_roster_links", db.session_calls[0][0])
        debater_write = next(params for sql, params in db.session_calls if "INSERT INTO debaters" in sql)
        self.assertEqual(len(debater_write), 4)


if __name__ == "__main__":
    unittest.main()
