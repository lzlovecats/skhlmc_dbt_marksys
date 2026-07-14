import pathlib
import re
import unittest
from unittest.mock import patch

from debate_timing import (
    DEBATE_FORMATS,
    MOCK_SESSION_BUDGET_SECONDS,
    full_mock_total_seconds,
    get_full_mock_sequence,
    split_mock_into_sessions,
)


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

    def test_mock_session_budget_counts_both_free_debate_time_banks(self):
        segments = get_full_mock_sequence("聯中", free_debate_minutes=5)
        sessions = split_mock_into_sessions(segments)
        self.assertEqual(
            sum(session["planned_seconds"] for session in sessions),
            full_mock_total_seconds(segments),
        )
        self.assertTrue(
            all(session["planned_seconds"] <= MOCK_SESSION_BUDGET_SECONDS for session in sessions)
        )
        free_session = next(
            session for session in sessions
            if any(segment["id"] == "free" for segment in session["segments"])
        )
        self.assertEqual(free_session["planned_seconds"], 600)

    def test_mock_relay_signs_every_session_token(self):
        proxy = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn("for live_token in dict.fromkeys([token, *(tokens or [])])", proxy)
        self.assertIn("token_sigs[live_token] = sig", proxy)

    def test_mock_rebrand_does_not_rewrite_injected_payloads(self):
        """「自由辯論→Mock」只可以改 template 靜態文案；注入嘅 system prompt、
        runtime prompts 同 segment JSON 必須原封不動（prompts.py 檔頭寫明呢個
        contract）。injection 一定要行喺 rebrand 之後。"""
        import json

        from deploy.proxy import _render_live_debate_html
        from prompts import LIVE_RUNTIME_PROMPTS, build_full_mock_live_prompt

        prompt = build_full_mock_live_prompt(
            "測試辯題", "正方", "聯中", free_debate_minutes=5
        )
        segments = get_full_mock_sequence("聯中", free_debate_minutes=5)
        sessions = split_mock_into_sessions(segments)
        flat = [
            {**segment, "session": index}
            for index, session in enumerate(sessions)
            for segment in session["segments"]
        ]
        html = _render_live_debate_html(
            "tok-1", prompt, 60, [], False, segments=flat,
            tokens=["tok-1", "tok-2"],
            session_labels=[session["label"] for session in sessions],
            session_label="Mock",
        )
        # 注入 payload 必須逐字保留（prompt 內大量「自由辯論」字眼）。
        self.assertIn(json.dumps(prompt, ensure_ascii=False), html)
        self.assertIn(json.dumps(flat, ensure_ascii=False), html)
        self.assertIn(json.dumps(LIVE_RUNTIME_PROMPTS, ensure_ascii=False), html)
        # 靜態 UI 文案就要換晒做 Mock。
        self.assertIn("開始Mock", html)
        self.assertNotIn("開始自由辯論", html)

    def test_free_de_relay_deadline_has_overhead_allowance(self):
        """Free De relay deadline 係 wall-clock，除咗雙方發言仲要包 AI 回覆
        延遲同總結評價；唔可以再用冇 buffer 嘅 live_minutes*2 硬斬。"""
        proxy = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn(
            "max_seconds = max(60, min(30 * 60, int(math.ceil(live_minutes * 2 * 60)) + 180))",
            proxy,
        )

    def test_live_page_stop_and_segment_announce_contracts(self):
        live = (ROOT / "templates" / "live_debate.html").read_text(encoding="utf-8")
        # 評價完成或再按「停止」要真正斷線收 mic，唔可以齋等 token 過期。
        self.assertIn('stopLive(true, "自由辯論已停止。")', live)
        self.assertIn('stopLive(false, "")', live)
        self.assertIn("feedbackPromptShown", live)
        # AI 回合中轉環節要補發環節提示，唔可以靜靜跳過。
        self.assertIn("pendingAnnounceSeg = seg", live)
        self.assertIn("announceSegmentToAi(seg, false)", live)
        self.assertIn("responsePurpose !== null", live)
        # Stop撞正舊AI回覆時，要分清normal turn與feedback turn：舊turn完成
        # 不可重開mic，亦不可被誤當成最終評價完成。
        self.assertIn('responsePurpose = purpose || "normal"', live)
        self.assertIn("feedbackPromptQueued", live)
        self.assertIn("retryPromptQueued", live)
        self.assertIn("if (isDiscardingNormalResponse()) return false", live)
        self.assertIn('completedPurpose === "feedback",', live)
        self.assertIn(
            'if (!isFinalFeedbackTurn && responsePurpose === "feedback") return',
            live,
        )
        self.assertIn("expectedSessionEpoch !== sessionEpoch", live)
        self.assertIn("expectedResponseEpoch !== responseEpoch", live)
        self.assertIn("liveRunIsStale(expectedSessionEpoch)", live)
        self.assertIn("if (liveRunIsStale(startEpoch)) return", live)
        self.assertIn("disposeMicSetup(nextStream", live)
        self.assertIn("finishRetryAfterResponse()", live)
        self.assertIn("showFeedbackButton(!finalFeedbackRequested)", live)
        start_live = live[
            live.index("async function startLive") : live.index("function currentToken")
        ]
        for reset in (
            "finalFeedbackRequested = false",
            "finalFeedbackComplete = false",
            "feedbackPromptShown = false",
            "responsePurpose = null",
            "feedbackPromptQueued = false",
            "retryPromptQueued = false",
        ):
            self.assertIn(reset, start_live)
        start_handler = live[
            live.index('startBtn.addEventListener("click"') : live.index(
                'stopBtn.addEventListener("click"'
            )
        ]
        self.assertIn("cleanupMedia(true)", start_handler)
        switch = live[live.index("function switchToSession"):
                      live.index("function onSessionReconnected")]
        self.assertIn("clearActiveAiLine()", switch)
        self.assertIn("switchingSession = false", live)
        self.assertIn("if (feedbackPromptShown) return;", live)


class PracticeBriefConsumptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_mint_failure_does_not_consume_live_brief(self):
        from deploy import proxy

        class Request:
            query_params = {
                "topic": "測試辯題",
                "side": "正方",
                "format": "聯中",
                "mode": "free",
                "minutes": "5",
                "brief_id": "brief-1",
            }

        with patch("deploy.proxy._verify_committee_cookie", return_value="member"), \
             patch("deploy.proxy._practice_live_rate_check", return_value=None), \
             patch("deploy.proxy._solo_live_quota_error", return_value=None), \
             patch("deploy.proxy._mint_gemini_live_token", return_value=(None, "mint failed")), \
             patch("api.ai_coach_api.consume_live_brief") as consume:
            response = await proxy.appliance_ai_debate_live(Request())

        consume.assert_not_called()
        self.assertIn("未能開始", response.body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
