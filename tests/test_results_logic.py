import unittest

import pandas as pd

from core.results_logic import RANK_COLUMNS, _best_debaters, results_data


class RankingDb:
    def __init__(self, rankings):
        self.rankings = rankings

    def query(self, sql, params=None):
        if "FROM debaters" in sql:
            return pd.DataFrame(columns=["side", "position", "debater_name"])
        if "FROM best_debater_rankings" in sql:
            return pd.DataFrame(self.rankings, columns=["judge_name", "side", "position", "rank"])
        raise AssertionError(sql)


def score_frame():
    row = {"judge_name": "評判甲"}
    row.update({column: 80 - index * 10 for index, column in enumerate(RANK_COLUMNS)})
    return pd.DataFrame([row])


class BestDebaterRankingTests(unittest.TestCase):
    def test_preselected_unscored_match_does_not_fall_back_to_other_results(self):
        class EmptySelectedDb:
            def query(self, sql, params=None):
                if "FROM scores s" in sql:
                    self.selected = params["match_id"]
                    return pd.DataFrame()
                raise AssertionError(sql)

        db = EmptySelectedDb()
        result = results_data("current", db=db, match_ids=["current", "old-scored"])
        self.assertEqual(db.selected, "current")
        self.assertEqual(result["selected_match_id"], "current")
        self.assertFalse(result["has_scores"])

    def test_partial_explicit_ranking_falls_back_to_speech_scores(self):
        rankings = [{"judge_name": "評判甲", "side": "pro", "position": 1, "rank": 1}]
        rows, best = _best_debaters("test", score_frame(), RankingDb(rankings))
        self.assertEqual(best["role"], "正方主辯")
        self.assertEqual(best["rank_sum"], 1)
        self.assertEqual(len(rows), 8)

    def test_complete_unique_explicit_ranking_is_used(self):
        rankings = [
            {"judge_name": "評判甲", "side": side, "position": position, "rank": rank}
            for rank, (side, position) in enumerate(
                [("con", 4), ("con", 3), ("con", 2), ("con", 1),
                 ("pro", 4), ("pro", 3), ("pro", 2), ("pro", 1)], 1
            )
        ]
        _, best = _best_debaters("test", score_frame(), RankingDb(rankings))
        self.assertEqual(best["role"], "反方結辯")
        self.assertEqual(best["rank_sum"], 1)


if __name__ == "__main__":
    unittest.main()
