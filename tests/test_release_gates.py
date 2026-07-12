import ast
import pathlib
import unittest

from api.pagination import PAGE_SIZE, bounds, payload
from debate_timing import DEBATE_FORMATS, get_debate_timer_config


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PaginationContractTests(unittest.TestCase):
    def test_fixed_page_size_and_boundaries(self):
        self.assertEqual(PAGE_SIZE, 20)
        self.assertEqual(bounds(0), (1, 20, 0))
        self.assertEqual(bounds(2), (2, 20, 20))
        self.assertEqual(payload([], 1, 0)["total_pages"], 1)
        self.assertEqual(payload([1], 1, 1)["total_pages"], 1)
        self.assertEqual(payload(list(range(20)), 1, 20)["total_pages"], 1)
        self.assertEqual(payload(list(range(20)), 1, 21)["total_pages"], 2)
        self.assertEqual(payload(list(range(20)), 2, 40)["total_pages"], 2)
        self.assertEqual(payload(list(range(20)), 3, 41)["total_pages"], 3)
        self.assertIsNone(payload([{"nullable": float("nan")}], 1, 1)["items"][0]["nullable"])


class ChairpersonTimerTests(unittest.TestCase):
    def test_start_bells_only_fire_after_timer_starts(self):
        html = (ROOT / "frontend" / "chairperson" / "index.html").read_text(encoding="utf-8")
        self.assertIn("if(t.running)bells.forEach", html)

    def test_chairperson_uses_api_formats_and_legacy_timer_controls(self):
        html = (ROOT / "frontend" / "chairperson" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Array.isArray(cfg.formats)", html)
        self.assertIn("id=\"freeTest\"", html)
        self.assertIn("g.gain.value=5", html)
        self.assertIn("Math.floor(s*100)", html)

class MigrationParityRegressionTests(unittest.TestCase):
    def test_judging_uses_server_scoring_contract_and_submission_safeguards(self):
        html = (ROOT / "frontend" / "judging" / "index.html").read_text(encoding="utf-8")
        api = (ROOT / "api" / "judging_api.py").read_text(encoding="utf-8")
        self.assertIn('@router.get("/config")', api)
        self.assertIn("/api/judging/config", html)
        self.assertNotIn("const speech=[[", html)
        for marker in ("zeroWarnings", "預計結果", "雙方同分", "switchMatch", "上次儲存：", "S.saved[side]=false"):
            self.assertIn(marker, html)

    def test_ai_coach_has_global_model_and_standalone_mock(self):
        html = (ROOT / "frontend" / "ai_coach" / "index.html").read_text(encoding="utf-8")
        browser = (ROOT / "frontend" / "shared" / "ai-parity.js").read_text(encoding="utf-8")
        proxy = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn('id="globalModel"', html)
        self.assertIn('id="mockForm"', html)
        self.assertIn('/api/ai-coach/prepare-live', browser)
        self.assertIn('mode == "mock"', proxy)
        self.assertIn('split_mock_into_sessions', proxy)
        self.assertIn('selection_label', (ROOT / "api" / "ai_coach_api.py").read_text(encoding="utf-8"))
        self.assertNotIn('沿用現有 Gemini Live 引擎', html)
        self.assertIn('使用「練習發言」輸入文字稿或錄音', html)

    def test_ai_coach_live_research_and_usage_accounting(self):
        api = (ROOT / "api" / "ai_coach_api.py").read_text(encoding="utf-8")
        provider = (ROOT / "core" / "ai_provider.py").read_text(encoding="utf-8")
        proxy = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn('@router.post("/prepare-live")', api)
        self.assertIn('consume_live_brief', proxy)
        self.assertIn('record_live_usage', proxy)
        self.assertIn('research_brief=research_brief', proxy)
        self.assertIn('_LIVE_BRIEF_TABLE', api)
        self.assertNotIn('_LIVE_BRIEFS = {}', api)
        self.assertIn('from core.ai_provider import generate_text', api)
        self.assertIn('"openrouter:web_search"', provider)
        self.assertIn('## 可核查來源', provider)
        self.assertIn('actual.get("cost_source")', api)
        self.assertIn('def _match_context', api)

    def test_ai_coach_motion_sources_qa_timer_and_downloads(self):
        browser = (ROOT / "frontend" / "shared" / "ai-parity.js").read_text(encoding="utf-8")
        markdown = (ROOT / "frontend" / "shared" / "markdown.js").read_text(encoding="utf-8")
        html = (ROOT / "frontend" / "ai_coach" / "index.html").read_text(encoding="utf-8")
        for text in ("從辯題庫選擇", "從系統場次載入", "台下發問", "交互答問", "speechClock", "分析結果.txt"):
            self.assertIn(text, browser)
        self.assertIn('const manual = mode.value === "手動輸入"', browser)
        self.assertIn('document.querySelectorAll("[data-topic-source]").forEach(topicSource)', browser)
        self.assertIn('AI 正在賽前搵料，準備攻防', browser)
        self.assertIn('match_id: selectedMatchId', browser)
        self.assertIn('SafeMarkdown.render(data.markdown)', browser)
        self.assertLess(html.index('/shared/markdown.js'), html.index('/shared/ai-parity.js'))
        for marker in ('<table>', '<blockquote>', '<pre><code', 'escapeHtml'):
            self.assertIn(marker, markdown)

    def test_ai_coach_base_fallback_and_audio_guards(self):
        html = (ROOT / "frontend" / "ai_coach" / "index.html").read_text(encoding="utf-8")
        browser = (ROOT / "frontend" / "shared" / "ai-parity.js").read_text(encoding="utf-8")
        self.assertIn('meta = await api("/api/ai-coach/data")', browser)
        self.assertIn('$("mockForm").addEventListener("submit"', browser)
        self.assertIn('brief_id: data.brief_id', browser)
        self.assertIn("duration < 1 || duration > 60", browser)
        self.assertIn("blob.size > 15 * 1024 * 1024", browser)
        self.assertIn("bytes.subarray(index, index + 32768)", browser)
        self.assertNotIn("String.fromCharCode(...new Uint8Array", browser)
        self.assertLess(html.index('data-pane="fact"'), html.index('data-pane="research"'))

    def test_free_debate_room_formats_are_restricted_at_both_layers(self):
        html = (ROOT / "frontend" / "shared" / "ai-parity.js").read_text(encoding="utf-8")
        server = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn('free && ["星島", "基本法盃"].includes', html)
        self.assertIn("structure == \"free\" and debate_format not in FREE_DEBATE_FORMATS", server)

    def test_video_chapter_navigation_seeks_existing_player(self):
        html = (ROOT / "frontend" / "video_replay" / "index.html").read_text(encoding="utf-8")
        self.assertIn("player.seekTo", html)

    def test_recording_audio_checks_owner_before_streaming(self):
        source = (ROOT / "api" / "ai_training_api.py").read_text(encoding="utf-8")
        self.assertIn("speaker_user_id,audio_data,mime_type", source)
        self.assertIn("owner != str(user).strip() and not _is_admin", source)

    def test_ai_training_parity_endpoints_are_registered(self):
        source = (ROOT / "api" / "ai_training_api.py").read_text(encoding="utf-8")
        for route in ("/recordings/quality-check", "/manuscripts", "/coverage", "/inventory", "/regenerate-suggestions",
                      "/export/recordings.zip", "/export/llm.jsonl"):
            self.assertIn(route, source)
        browser = (
            (ROOT / "frontend" / "ai_training" / "index.html").read_text(encoding="utf-8")
            + (ROOT / "frontend" / "ai_training" / "app.js").read_text(encoding="utf-8")
        )
        self.assertIn("跳過此句", browser)
        self.assertIn("data-active-type", browser)


class StaticArchitectureTests(unittest.TestCase):
    def test_production_modules_do_not_import_streamlit(self):
        for folder in ("api", "core"):
            for path in (ROOT / folder).glob("*.py"):
                text = path.read_text(encoding="utf-8")
                tree = ast.parse(text)
                imports = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.append(node.module)
                self.assertFalse(any(name == "streamlit" or name.startswith("streamlit.") for name in imports), str(path))

    def test_all_html_table_pages_load_shared_pager(self):
        for path in (ROOT / "frontend").glob("*/index.html"):
            text = path.read_text(encoding="utf-8")
            if "<table" in text:
                self.assertIn('/shared/vote-ui.js', text, str(path))

    def test_streamlit_is_not_started_in_production(self):
        start = (ROOT / "deploy" / "start.sh").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("streamlit run", start)
        self.assertNotIn("streamlit>=", requirements.lower())


if __name__ == "__main__":
    unittest.main()
