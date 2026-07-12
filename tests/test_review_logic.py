import json
import unittest
from unittest.mock import patch

import pandas as pd

from core.review_logic import review_data
from scoring import FREE_DEBATE_CRITERIA, SPEECH_CRITERIA, free_debate_col, speech_col


def side_payload(name):
    return json.dumps({
        "team_name": name,
        "raw_df_a": [
            {"辯位": role, "姓名": role, **{speech_col(item): 5 for item in SPEECH_CRITERIA}}
            for role in ("主辯", "一副", "二副", "結辯")
        ],
        "raw_df_b": [{free_debate_col(item): 2 for item in FREE_DEBATE_CRITERIA}],
        "deduction": 1, "coherence": 4, "final_total": 223,
    }, ensure_ascii=False)


class ReviewDb:
    def __init__(self, has_scores=True):
        self.has_scores = has_scores

    def query(self, sql, params=None):
        if "FROM scores s" in sql:
            if not self.has_scores:
                return pd.DataFrame()
            return pd.DataFrame([{
                "match_id": "test", "judge_name": "評判甲",
                "pro_total_score": 223, "con_total_score": 220,
                "submitted_time": "16:30:00", "pro_free_debate_score": 10,
                "con_free_debate_score": 10, "pro_deduction_points": 1,
                "con_deduction_points": 1, "pro_coherence_score": 4,
                "con_coherence_score": 4, "pro_team": "正方隊", "con_team": "反方隊",
            }])
        if "FROM debater_scores" in sql:
            return pd.DataFrame([
                {"match_id": "test", "judge_name": "評判甲", "side": side,
                 "position": position, "debater_score": 50 + position}
                for side in ("pro", "con") for position in range(1, 5)
            ])
        if "FROM score_drafts" in sql:
            return pd.DataFrame([
                {"side": "正方", "score_payload": side_payload("正方隊"), "updated_at": "2026-01-01"},
                {"side": "反方", "score_payload": side_payload("反方隊"), "updated_at": "2026-01-01"},
            ])
        if "FROM debaters" in sql:
            return pd.DataFrame([
                {"side": side, "position": position, "debater_name": f"{side}{position}"}
                for side in ("pro", "con") for position in range(1, 5)
            ])
        if "FROM best_debater_rankings" in sql:
            return pd.DataFrame()
        raise AssertionError(sql)


class ReviewLogicTests(unittest.TestCase):
    def test_review_data_matches_streamlit_totals_and_ranking_contract(self):
        result = review_data("test", db=ReviewDb())
        self.assertTrue(result["has_scores"])
        self.assertEqual(result["judges"], ["評判甲"])
        self.assertEqual(result["sides"]["正方"]["raw_df_a"][0]["總分（100）"], 50)
        self.assertEqual(result["sides"]["正方"]["raw_df_b"][0]["總分（55）"], 10)
        self.assertEqual(len(result["best_debaters"]), 8)

    def test_empty_score_state_has_stable_frontend_contract(self):
        result = review_data("test", db=ReviewDb(has_scores=False))
        self.assertFalse(result["has_scores"])
        self.assertIsNone(result["record"])
        self.assertEqual(result["judges"], [])
        self.assertEqual(result["missing_sides"], [])

    def test_pdf_endpoint_returns_downloadable_streamlit_equivalent(self):
        from api.review_api import pdf

        class PdfDb:
            def query(self, sql, params=None):
                return pd.DataFrame([{
                    "match_id": "test", "match_date": "2026-01-01",
                    "match_time": "16:00", "topic_text": "測試辯題",
                }])

        payload = {
            "has_scores": True, "record": {"judge_name": "評判甲"},
            "selected_judge": "評判甲", "missing_sides": [],
            "sides": {"正方": {}, "反方": {}},
        }
        with (
            patch("api.review_api.scope", return_value="test"),
            patch("api.review_api.db", return_value=PdfDb()),
            patch("core.review_logic.review_data", return_value=payload),
            patch("score_sheet_pdf.build_score_sheet_pdf", return_value=b"%PDF-test"),
        ):
            response = pdf(object(), "評判甲")
        self.assertEqual(response.media_type, "application/pdf")
        self.assertEqual(response.body, b"%PDF-test")
        self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])


if __name__ == "__main__":
    unittest.main()
