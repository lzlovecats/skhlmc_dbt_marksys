import unittest
from unittest.mock import patch

from core.schedule_logic import draw_schedule


class ScheduleLogicTests(unittest.TestCase):
    def test_validation_matches_streamlit(self):
        self.assertEqual(draw_schedule("甲")["message"], "至少需要 2 隊隊伍。")
        self.assertEqual(draw_schedule("甲\n甲")["message"], "隊伍名稱有重複，請檢查輸入。")

    def test_two_teams_go_directly_to_final(self):
        with patch("core.schedule_logic.random.shuffle", lambda teams: None):
            result = draw_schedule("甲\n乙")["result"]
        self.assertTrue(result["direct_final"])
        self.assertEqual(result["final_match"], {"left": "甲", "right": "乙"})
        self.assertEqual(len(result["summary_rows"]), 1)

    def test_existing_custom_format_keeps_loser_round_bye_data(self):
        with patch("core.schedule_logic.random.shuffle", lambda teams: None):
            result = draw_schedule("甲\n乙\n丙\n丁\n戊\n己")["result"]
        self.assertFalse(result["direct_final"])
        self.assertEqual(len(result["main_rounds"][0]["pairs"]), 3)
        self.assertEqual(result["loser_rounds"][0]["bye"], "第一輪場次3負方")
        self.assertTrue(any(row["階段"] == "負方賽" and row["場次"] == "輪空" for row in result["summary_rows"]))


if __name__ == "__main__":
    unittest.main()
