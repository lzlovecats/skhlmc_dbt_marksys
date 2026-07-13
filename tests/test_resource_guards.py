import unittest
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.admin_console_api import _sql_cell, _unsafe
from api.ai_training_api import LLM_CONTENT_MAX_CHARS, LlmBody
from api.resource_limits import csv_response, require_row_limit
from api.vote_api import CastBody, _auto_resolve, vote_cast
from core.media_logic import parse_import_csv
from core.open_db_logic import category_vote_pass_rate
from core.schedule_logic import draw_schedule
from core.vote_logic import (
    COMMENT_HISTORY_LIMIT,
    fetch_comments,
    fetch_depose_data,
    fetch_vote_data,
    motion_transaction,
)
from deploy import proxy


class ResourceGuardTests(unittest.TestCase):
    def test_vote_cast_serializes_read_write_count_and_resolution(self):
        class TransactionDb:
            def __init__(self):
                self.calls = []
                self.ballot = None

            def query(self, sql, params=None):
                self.calls.append(("query", sql, params))
                if "ORDER BY CASE WHEN status = 'pending'" in sql:
                    return pd.DataFrame([{
                        "proposer_user_id": "proposer",
                        "status": "pending",
                        "approval_threshold": 1,
                        "category": "科技",
                        "difficulty": 1,
                    }])
                if "GROUP BY vote_choice" in sql:
                    return pd.DataFrame([{"vote_choice": self.ballot, "cnt": 1}])
                if "SELECT vote_choice" in sql:
                    return pd.DataFrame(columns=["vote_choice"])
                if "COUNT(*) AS total" in sql:
                    return pd.DataFrame([{"total": 0, "category_count": 0}])
                raise AssertionError(sql)

            def execute(self, sql, params=None):
                self.calls.append(("execute", sql, params))
                if "INSERT INTO topic_vote_ballots" in sql:
                    self.ballot = "agree"

            def execute_count(self, sql, params=None):
                self.calls.append(("execute_count", sql, params))
                self.assert_pending_update = "status = 'pending'" in sql
                return 1

        class Transaction:
            def __init__(self, owner, transaction_db):
                self.owner = owner
                self.transaction_db = transaction_db

            def __enter__(self):
                self.owner.entered = True
                return self.transaction_db

            def __exit__(self, exc_type, exc, traceback):
                self.owner.exited = exc_type is None
                return False

        class Db:
            def __init__(self):
                self.entered = False
                self.exited = False
                self.transaction_db = TransactionDb()

            def transaction(self):
                return Transaction(self, self.transaction_db)

            def query(self, *_args, **_kwargs):
                raise AssertionError("cast escaped its transaction")

            execute = query
            execute_count = query

        db = Db()

        def push_after_commit(*_args, **_kwargs):
            self.assertTrue(db.exited)

        with (
            patch("api.vote_api._vote_db", return_value=db),
            patch("api.vote_api._fire_resolution_push", side_effect=push_after_commit) as push,
        ):
            result = vote_cast(
                CastBody(mode="topic", topic="測試辯題", action="agree"),
                user_id="member",
            )

        self.assertTrue(db.entered)
        self.assertTrue(db.exited)
        self.assertEqual(result["resolved"], "passed")
        self.assertEqual(db.transaction_db.ballot, "agree")
        self.assertTrue(db.transaction_db.assert_pending_update)
        self.assertIn(
            "pg_advisory_xact_lock",
            db.transaction_db.calls[0][1],
        )
        push.assert_called_once()

    def test_motion_transaction_adapts_raw_session_and_keeps_legacy_fallback(self):
        class Result:
            rowcount = 1

            def fetchall(self):
                return [(1,)]

            def keys(self):
                return ["value"]

        class Session:
            def __init__(self):
                self.calls = []

            def execute(self, statement, params=None):
                self.calls.append((str(statement), params))
                return Result()

        class Transaction:
            def __init__(self, session):
                self.session = session

            def __enter__(self):
                return self.session

            def __exit__(self, exc_type, exc, traceback):
                return False

        class Db:
            def __init__(self):
                self.session = Session()

            def transaction(self):
                return Transaction(self.session)

        database = Db()
        with motion_transaction(database, "topic_votes", "測試") as executor:
            frame = executor.query("SELECT 1 AS value")
        self.assertEqual(frame.iloc[0]["value"], 1)
        self.assertIn("pg_advisory_xact_lock", database.session.calls[0][0])

        legacy = object()
        with motion_transaction(legacy, "topic_votes", "測試") as executor:
            self.assertIs(executor, legacy)

    def test_concurrent_vote_resolution_does_not_duplicate_push(self):
        class VoteLogic:
            @staticmethod
            def resolve_vote(agree, against, threshold):
                return "pass"

            @staticmethod
            def apply_topic_pass(*args, **kwargs):
                return 0

        with patch("api.vote_api._fire_resolution_push") as push:
            result = _auto_resolve(
                VoteLogic, "topic", "測試", {}, 5, 0, 5, object()
            )
        self.assertEqual(result, "passed")
        push.assert_not_called()

    def test_export_row_and_byte_limits_fail_before_download(self):
        with self.assertRaises(HTTPException) as row_error:
            require_row_limit([1, 2], limit=1)
        self.assertEqual(row_error.exception.status_code, 413)
        with self.assertRaises(HTTPException) as byte_error:
            csv_response("large.csv", ["value"], [["x" * 100]], max_bytes=20)
        self.assertEqual(byte_error.exception.status_code, 413)

    def test_sql_console_omits_binary_and_rejects_maintenance_commands(self):
        self.assertEqual(_sql_cell(memoryview(b"abc")), "<binary 3 bytes omitted>")
        self.assertIn("只可執行", _unsafe("VACUUM FULL"))
        self.assertIn("不可存取", _unsafe("SELECT * FROM app_config"))
        self.assertEqual(_unsafe("SELECT 1"), "")

    def test_llm_training_text_has_a_hard_storage_bound(self):
        with self.assertRaises(ValidationError):
            LlmBody(
                data_type="debate", content_text="x" * (LLM_CONTENT_MAX_CHARS + 1),
                anonymized=True, permission_confirmed=True,
            )

    def test_csv_parser_stops_after_guard_row(self):
        raw = "title,url\n" + "\n".join(f"v{i},https://youtu.be/{i}" for i in range(10))
        self.assertEqual(len(parse_import_csv(raw, max_rows=3)), 4)

    def test_schedule_draw_rejects_memory_amplifying_team_count(self):
        result = draw_schedule("\n".join(f"team-{index}" for index in range(129)))
        self.assertFalse(result["ok"])
        self.assertIn("128", result["message"])

    def test_open_db_aggregated_vote_stats_keep_weighted_counts(self):
        frame = pd.DataFrame([
            {"category": "科技", "status": "passed", "motion_count": 3},
            {"category": "科技", "status": "rejected", "motion_count": 1},
        ])
        result = category_vote_pass_rate(frame)
        self.assertEqual(int(result.iloc[0]["動議數量"]), 4)
        self.assertEqual(result.iloc[0]["投票通過率"], "75.0%")

    def test_vote_api_query_can_skip_all_resolved_motion_rows(self):
        class Db:
            def query(self, sql, params=None):
                if "FROM topic_votes" in sql and "JOIN" not in sql:
                    self.vote_sql = sql
                    self.vote_params = params
                    return pd.DataFrame([{
                        "topic_text": "測試", "proposer_user_id": "u", "status": "pending",
                        "created_at": "", "deadline_date": "", "approval_threshold": 2,
                        "category": "科技", "difficulty": 1,
                    }])
                self.ballot_sql = sql
                self.ballot_params = params
                return pd.DataFrame(columns=["topic_text", "user_id", "vote_choice", "against_reasons"])

        db = Db()
        pending, passed, rejected = fetch_vote_data(db=db, resolved_limit=0)
        self.assertIn("WHERE status='pending'", db.vote_sql)
        self.assertIn("LIMIT :pending_limit", db.vote_sql)
        self.assertIn("LIMIT :ballot_limit", db.ballot_sql)
        self.assertGreater(db.ballot_params["ballot_limit"], 0)
        self.assertEqual(len(pending), 1)
        self.assertEqual(passed, [])
        self.assertEqual(rejected, [])

    def test_depose_board_only_loads_pending_ballots_and_metadata(self):
        class Db:
            def __init__(self):
                self.queries = []

            def query(self, sql, params=None):
                self.queries.append((sql, params))
                if "FROM topic_removal_votes r" in sql and "LEFT JOIN topics" in sql:
                    return pd.DataFrame(columns=[
                        "topic_text", "proposer_user_id", "status", "removal_reasons",
                        "created_at", "deadline_date", "approval_threshold", "category", "difficulty",
                    ])
                return pd.DataFrame(columns=["topic_text", "user_id", "vote_choice"])

        db = Db()
        self.assertEqual(fetch_depose_data(db=db), [])
        self.assertEqual(len(db.queries), 2)
        self.assertIn("r.status = 'pending'", db.queries[0][0])
        self.assertIn("r.status='pending'", db.queries[1][0])
        self.assertIn("LIMIT :ballot_limit", db.queries[1][0])

    def test_comment_history_query_is_bounded(self):
        class Db:
            def query(self, sql, params=None):
                self.sql, self.params = sql, params
                return pd.DataFrame(columns=["user_id", "comment_text", "created_at"])

        db = Db()
        self.assertEqual(fetch_comments("topic_vote", "x", db=db), [])
        self.assertIn("LIMIT :limit", db.sql)
        self.assertEqual(db.params["limit"], COMMENT_HISTORY_LIMIT)

    def test_text_pages_are_gzipped_but_precompressed_media_is_not(self):
        client = TestClient(proxy.app)
        html = client.get("/vote", headers={"Accept-Encoding": "gzip"})
        icon = client.get("/app-icon-180.png", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(html.status_code, 200)
        self.assertEqual(html.headers.get("content-encoding"), "gzip")
        self.assertEqual(icon.headers.get("content-encoding"), "identity")

    def test_practice_throttle_prunes_expired_users(self):
        self.addCleanup(proxy._practice_live_hits.clear)
        proxy._practice_live_hits.clear()
        proxy._practice_live_hits.update({"old": [1.0], "current": [4999.0]})
        with patch("deploy.proxy.time.time", return_value=5000.0):
            self.assertIsNone(proxy._practice_live_rate_check("new"))
        self.assertNotIn("old", proxy._practice_live_hits)
        self.assertIn("current", proxy._practice_live_hits)


if __name__ == "__main__":
    unittest.main()
