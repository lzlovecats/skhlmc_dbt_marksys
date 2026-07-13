import unittest
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.admin_console_api import _sql_cell, _unsafe
from api.ai_training_api import LLM_CONTENT_MAX_CHARS, LlmBody
from api.resource_limits import csv_response, require_row_limit
from core.media_logic import parse_import_csv
from core.open_db_logic import category_vote_pass_rate
from core.schedule_logic import draw_schedule
from core.vote_logic import COMMENT_HISTORY_LIMIT, fetch_comments, fetch_vote_data
from deploy import proxy


class ResourceGuardTests(unittest.TestCase):
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
                    return pd.DataFrame([{
                        "topic_text": "測試", "proposer_user_id": "u", "status": "pending",
                        "created_at": "", "deadline_date": "", "approval_threshold": 2,
                        "category": "科技", "difficulty": 1,
                    }])
                return pd.DataFrame(columns=["topic_text", "user_id", "vote_choice", "against_reasons"])

        db = Db()
        pending, passed, rejected = fetch_vote_data(db=db, resolved_limit=0)
        self.assertIn("WHERE status='pending'", db.vote_sql)
        self.assertEqual(len(pending), 1)
        self.assertEqual(passed, [])
        self.assertEqual(rejected, [])

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
