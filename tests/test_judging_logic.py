import unittest

import pandas as pd

from core.judging_logic import normalise_side_data, submit_best_debater_rankings
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
    def test_score_values_are_recomputed_and_bounded(self):
        data = valid_side_data()
        result = normalise_side_data("正方", data)
        self.assertEqual(result["final_total"], result["total_a"] + result["total_b"] + 3)
        data["raw_df_a"][0][speech_col(SPEECH_CRITERIA[0])] = 11
        with self.assertRaisesRegex(ValueError, "必須介乎 0 至 10"):
            normalise_side_data("正方", data)

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
