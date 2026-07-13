import unittest

import pandas as pd

from core.judging_logic import (
    matches_for_judging,
    normalise_side_data,
    save_draft,
    submit_best_debater_rankings,
)
from scoring import FREE_DEBATE_CRITERIA, SPEECH_CRITERIA, free_debate_col, speech_col


def valid_side_data():
    return {
        "team_name": "測試隊",
        "raw_df_a": [
            {
                "辯位": role,
                "姓名": name,
                **{speech_col(criterion): 5 for criterion in SPEECH_CRITERIA},
            }
            for role, name in zip(("主辯", "一副", "二副", "結辯"), ("甲", "乙", "丙", "丁"))
        ],
        "raw_df_b": [
            {free_debate_col(criterion): 3 for criterion in FREE_DEBATE_CRITERIA}
        ],
        "deduction": 0,
        "coherence": 3,
    }


class _Session:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((statement, params))


class _Transaction:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, traceback):
        return False


class _RankingDb:
    def __init__(self):
        self.session = _Session()

    def query(self, sql, params=None):
        return pd.DataFrame([{"exists": 1}])

    def transaction(self):
        return _Transaction(self.session)


class JudgingLogicTests(unittest.TestCase):
    def test_login_inventory_omits_score_sheet_details(self):
        class Db:
            def query(self, sql, params=None):
                self.sql = sql
                return pd.DataFrame([{"match_id": "M1", "access_code_hash": "hash"}])

        db = Db()
        self.assertEqual(
            matches_for_judging(db=db, summaries=True),
            [{"match_id": "M1", "is_open": True}],
        )
        self.assertNotIn("debaters", db.sql)
        self.assertNotIn("topic_text", db.sql)

    def test_score_values_are_recomputed_and_bounded(self):
        data = valid_side_data()
        result = normalise_side_data("正方", data)
        self.assertEqual(result["final_total"], result["total_a"] + result["total_b"] + 3)
        data["raw_df_a"][0][speech_col(SPEECH_CRITERIA[0])] = 11
        with self.assertRaisesRegex(ValueError, "必須介乎 0 至 10"):
            normalise_side_data("正方", data)

    def test_dataframe_drafts_follow_the_same_validation_path(self):
        data = valid_side_data()
        data["raw_df_a"] = pd.DataFrame(data["raw_df_a"])
        data["raw_df_b"] = pd.DataFrame(data["raw_df_b"])
        result = normalise_side_data("正方", data)
        self.assertEqual(len(result["raw_df_a"]), 4)
        self.assertEqual(len(result["raw_df_b"]), 1)

    def test_draft_check_and_write_share_the_final_submission_lock(self):
        class CapacityResult:
            def fetchone(self):
                class Row:
                    _mapping = {"submitted": False, "n": 1, "current": True}

                return Row()

        class Session:
            def __init__(self):
                self.calls = []

            def execute(self, statement, params=None):
                self.calls.append((str(statement), params))
                return CapacityResult()

        session = Session()

        class Db:
            def transaction(self):
                return _Transaction(session)

        result = save_draft("M1", "Judge", "正方", valid_side_data(), db=Db())
        self.assertEqual(result["team_name"], "測試隊")
        self.assertEqual(len(session.calls), 3)
        self.assertIn("pg_advisory_xact_lock", session.calls[0][0])
        self.assertIn("EXISTS", session.calls[1][0])
        self.assertIn("INSERT INTO score_drafts", session.calls[2][0])

    def test_rankings_require_all_slots_and_unique_ranks(self):
        db = _RankingDb()
        rankings = [
            {"side": side, "position": position, "rank": rank}
            for rank, (side, position) in enumerate(
                [(side, position) for side in ("pro", "con") for position in range(1, 5)], 1
            )
        ]
        submit_best_debater_rankings("M1", "Judge", rankings, db=db)
        self.assertEqual(len(db.session.calls), 1)
        self.assertEqual(len(db.session.calls[0][1]), 8)

        rankings[-1] = {"side": "pro", "position": 1, "rank": 8}
        with self.assertRaisesRegex(ValueError, "完整包含正反方各四個辯位"):
            submit_best_debater_rankings("M1", "Judge", rankings, db=_RankingDb())


if __name__ == "__main__":
    unittest.main()
