import pathlib
import re
import unittest

from debate_timing import DEBATE_FORMATS, get_full_mock_sequence


ROOT = pathlib.Path(__file__).resolve().parents[1]


def compact(source):
    """Ignore formatter-only whitespace in inline HTML/CSS/JS contracts."""
    return re.sub(r"\s+", "", source)


class AppliancePracticeTests(unittest.TestCase):
    def test_timer_has_max_volume_and_speaker_routing_controls(self):
        html = (ROOT / "templates" / "appliance_practice.html").read_text(encoding="utf-8")
        for marker in (
            "網頁音量固定 100%",
            "selectAudioOutput",
            "ctx.setSinkId",
            "BELL_BOOST = 12.0",
            "createDynamicsCompressor",
        ):
            self.assertIn(marker, html)

        kiosk = (ROOT / "appliance" / "marksys-kiosk.sh").read_text(encoding="utf-8")
        self.assertIn("set_max_volume", kiosk)
        self.assertIn("wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.0", kiosk)
        self.assertIn("pactl set-sink-volume @DEFAULT_SINK@ 100%", kiosk)

    def test_ai_setup_exposes_topic_bank_free_de_and_full_mock(self):
        html = (ROOT / "templates" / "appliance_ai_debate.html").read_text(encoding="utf-8")
        for marker in (
            'name="mode" value="free"',
            'name="mode" value="mock"',
            "完整 Mock",
            "從辯題庫選擇",
            "/api/open-db/data",
            'name="topic"',
        ):
            self.assertIn(marker, html)
        for debate_format in DEBATE_FORMATS:
            self.assertIn(debate_format, html)

    def test_ai_setup_exposes_solo_and_all_network_room_modes(self):
        html = compact((ROOT / "templates" / "appliance_ai_debate.html").read_text(encoding="utf-8"))
        for marker in (
            'name="session_type" value="solo"',
            'name="session_type" value="room"',
            "真人對真人（AI 評判）",
            "多人一隊對 AI",
            "建立並進入房間",
            "加入房間",
            'api("/api/room/create"',
            'roomUrl(data.code)',
        ):
            self.assertIn(compact(marker), html)

    def test_live_and_room_pages_keep_large_touch_controls_and_vote_visuals(self):
        live = (ROOT / "templates" / "live_debate.html").read_text(encoding="utf-8")
        room = (ROOT / "templates" / "room_debate.html").read_text(encoding="utf-8")
        for source in (live, room):
            source = compact(source)
            self.assertIn("--panel:#262730", source)
            self.assertIn("min-height:56px", source)
            self.assertIn("border-radius:12px", source)
            self.assertIn(compact("返回 AI 練習"), source)

    def test_network_mock_uses_multiple_gemini_sessions(self):
        proxy = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
        for marker in (
            "self.gemini_session_index = 0",
            'sessions = split_mock_into_sessions(room.segments)',
            'room.segments = [',
            'target_session != room.gemini_session_index',
            '## 上一節接力內容',
            'start_delay_minutes=planned_elapsed_minutes',
            '"new_session_expire_time": new_session_expire',
        ):
            self.assertIn(marker, proxy)

    def test_all_mock_formats_have_complete_sequences(self):
        for debate_format in DEBATE_FORMATS:
            with self.subTest(debate_format=debate_format):
                segments = get_full_mock_sequence(debate_format)
                ids = [segment["id"] for segment in segments]
                self.assertIn("main_pro", ids)
                self.assertIn("main_con", ids)
                self.assertIn("closing_pro", ids)
                self.assertIn("closing_con", ids)
                self.assertTrue(all(segment["bells"] for segment in segments))

    def test_mock_relay_signs_every_session_token(self):
        proxy = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn("for live_token in dict.fromkeys([token, *(tokens or [])])", proxy)
        self.assertIn("token_sigs[live_token] = sig", proxy)


if __name__ == "__main__":
    unittest.main()
