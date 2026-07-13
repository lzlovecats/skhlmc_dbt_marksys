import ast
import pathlib
import re
import unittest

from api.pagination import PAGE_SIZE, bounds, payload
from debate_timing import DEBATE_FORMATS, get_debate_timer_config


ROOT = pathlib.Path(__file__).resolve().parents[1]


def compact(source):
    """Ignore formatter-only whitespace in inline HTML/CSS/JS contracts."""
    return re.sub(r"\s+", "", source)


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
        html = compact((ROOT / "frontend" / "chairperson" / "index.html").read_text(encoding="utf-8"))
        self.assertIn("if(t.running)bells.forEach", html)

    def test_chairperson_uses_api_formats_and_legacy_timer_controls(self):
        html = compact((ROOT / "frontend" / "chairperson" / "index.html").read_text(encoding="utf-8"))
        proxy = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn("Array.isArray(cfg.formats)", html)
        self.assertIn("id=\"freeTest\"", html)
        self.assertIn("g.gain.value=5", html)
        self.assertIn("Math.floor(s*100)", html)
        for marker in ("resetTimers", "freeButton(other", "formSnapshot", "SafeMarkdown.render"):
            self.assertIn(compact(marker), html)
        self.assertNotIn("user-scalable=no", html)
        self.assertIn("max(2.0, free_minutes)", proxy)
        self.assertIn("max(0.5, prep_minutes)", proxy)
        markdown = (ROOT / "frontend" / "shared" / "markdown.js").read_text(encoding="utf-8")
        self.assertNotIn('replace(/__([^_]+)__/g', markdown)
        self.assertIn('start="${start}"', markdown)

class MigrationParityRegressionTests(unittest.TestCase):
    def test_all_html_pages_share_the_visual_system_and_allow_mobile_zoom(self):
        for html_path in sorted((ROOT / "frontend").glob("*/index.html")):
            html = html_path.read_text(encoding="utf-8")
            with self.subTest(page=html_path.parent.name):
                self.assertTrue("/shared/app-shell.css" in html or "/shared/vote-ui.css" in html)
                self.assertNotIn("user-scalable=no", html)
                self.assertNotIn("maximum-scale=1", html)

    def test_ai_practice_has_vote_aligned_mobile_controls(self):
        coach = (ROOT / "frontend" / "ai_coach" / "index.html").read_text(encoding="utf-8")
        appliance = (ROOT / "templates" / "appliance_ai_debate.html").read_text(encoding="utf-8")
        live = (ROOT / "templates" / "live_debate.html").read_text(encoding="utf-8")
        room = (ROOT / "templates" / "room_debate.html").read_text(encoding="utf-8")
        for marker in ("scroll-snap-type:x proximity", "height:max(42rem,calc(100dvh - 9rem))"):
            self.assertIn(compact(marker), compact(coach))
        for marker in ("--bg:#0e1117", "font-size:16px", "border-radius:12px"):
            self.assertIn(compact(marker), compact(appliance))
            self.assertIn(compact(marker), compact(live))
            self.assertIn(compact(marker), compact(room))
        self.assertIn("position:sticky", compact(live))

    def test_public_registration_uses_server_edition_and_mobile_form_contract(self):
        core = (ROOT / "core" / "registration_logic.py").read_text(encoding="utf-8")
        html = (ROOT / "frontend" / "registration" / "index.html").read_text(encoding="utf-8")
        self.assertIn('current_edition = int(latest_status["settings"]["competition_edition"])', core)
        self.assertIn("submitted_edition != current_edition", core)
        for marker in ('required autocomplete="organization"', 'pattern="[0-9]{8}"', "SafeMarkdown.render", "參賽隊伍"):
            self.assertIn(marker, html)
        self.assertNotIn("user-scalable=no", html)
        self.assertNotIn("請至側邊欄查閱賽規", html)
        self.assertIn("只供賽會處理本次報名、聯絡及跟進用途", html)

    def test_registration_admin_loads_paged_records_and_server_csv(self):
        html = (ROOT / "frontend" / "registration_admin" / "index.html").read_text(encoding="utf-8")
        core = (ROOT / "core" / "registration_logic.py").read_text(encoding="utf-8")
        for marker in ("loadRecords", "VoteUI.serverPaged", "/api/registration-admin/records", "/api/registration-admin/export", 'id="recordSearch"', "search=${encodeURIComponent(search)}"):
            self.assertIn(marker, html)
        api = (ROOT / "api" / "registration_admin_api.py").read_text(encoding="utf-8")
        for marker in ("def _record_filters", "team_name ILIKE :search", "contact_phone ILIKE :search"):
            self.assertIn(marker, api)
        self.assertNotIn("user-scalable=no", html)
        self.assertIn("changed = db.execute_count", core)
        self.assertIn("比賽屆數必須為正整數", core)

    def test_match_info_defaults_are_streamlit_aligned_and_saves_are_atomic(self):
        html = (ROOT / "frontend" / "match_info" / "index.html").read_text(encoding="utf-8")
        core = (ROOT / "core" / "match_logic.py").read_text(encoding="utf-8")
        for marker in ("state.default_date", "state.default_time", "難度篩選", "隊伍名稱1", "留空代表保留現有密碼", "hasUnsavedChanges", "尚未儲存的修改", "beforeunload"):
            self.assertIn(marker, html)
        self.assertNotIn("user-scalable=no", html)
        self.assertIn("with db.transaction() as session", core)
        self.assertIn('"default_time": "16:00"', core)

    def test_team_roster_submission_is_atomic_and_uses_required_mobile_fields(self):
        html = (ROOT / "frontend" / "team_roster" / "index.html").read_text(encoding="utf-8")
        core = (ROOT / "core" / "match_logic.py").read_text(encoding="utf-8")
        self.assertEqual(html.count('autocomplete="name" required'), 4)
        self.assertIn('autocomplete="organization" required', html)
        for marker in ("confirmSubmitDialog", "確認提交名單？", "pendingPayload", "if (submitting) return"):
            self.assertIn(marker, html)
        self.assertIn("claimed=session.execute", core)
        self.assertIn("with db.transaction() as session", core)

    def test_judging_uses_server_scoring_contract_and_submission_safeguards(self):
        html = (ROOT / "frontend" / "judging" / "index.html").read_text(encoding="utf-8")
        ux = (ROOT / "frontend" / "shared" / "judging-ux.js").read_text(encoding="utf-8")
        api = (ROOT / "api" / "judging_api.py").read_text(encoding="utf-8")
        core = (ROOT / "core" / "judging_logic.py").read_text(encoding="utf-8")
        self.assertIn('@router.get("/config")', api)
        self.assertIn("/api/judging/config", html)
        self.assertIn('/shared/judging-ux.js?v=4.0.3-judging', html)
        self.assertNotIn("const speech=[[", html)
        for marker in ("zeroWarnings", "預計結果", "雙方同分", "switchMatch", "上次儲存：", "S.saved[side]=false"):
            self.assertIn(compact(marker), compact(html))
        for marker in ("submitBottom", "completionHint", "請輸入中文全名", "手機建議橫向使用"):
            self.assertIn(marker, html)
        for marker in ("確認登出", "確認切換場次", "每個名次（1–8）必須恰好使用一次", "updateMatchAvailability", "dirtySides", "略過未修改的"):
            self.assertIn(marker, ux)
        self.assertIn('raise HTTPException(400, str(exc))', api)
        self.assertIn('expected_slots', core)
        self.assertIn('with db.transaction() as session', core)

    def test_review_restores_streamlit_score_sheet_features(self):
        html = (ROOT / "frontend" / "review" / "index.html").read_text(encoding="utf-8")
        api = (ROOT / "api" / "review_api.py").read_text(encoding="utf-8")
        core = (ROOT / "core" / "review_logic.py").read_text(encoding="utf-8")
        for marker in ("匯出 PDF", "查看最佳辯論員排名", "香港時間／HKT", "此場次尚未設定查閱分紙密碼", "目前未有可查閱的評分紀錄"):
            self.assertIn(marker, html)
        self.assertIn("@router.get('/pdf')", api)
        self.assertIn("build_score_sheet_pdf", api)
        self.assertIn('row["總分（100）"]', core)
        self.assertIn('f"總分（{FREE_DEBATE_MAX}）"', core)

    def test_management_has_complete_ranking_guard_and_logout(self):
        html = (ROOT / "frontend" / "management" / "index.html").read_text(encoding="utf-8")
        core = (ROOT / "core" / "results_logic.py").read_text(encoding="utf-8")
        api = (ROOT / "api" / "registration_admin_api.py").read_text(encoding="utf-8")
        for marker in ("使用賽會人員密碼登入後", 'id="logout"', "/api/registration-admin/logout", "🏆勝方：正方"):
            self.assertIn(marker, html)
        for marker in ("expected_slots", "complete_ranking", "ranks == set(range(1, 9))"):
            self.assertIn(marker, core)
        self.assertIn('@router.post("/logout")', api)

    def test_schedule_page_preserves_normalized_snapshot_and_loser_byes(self):
        html = (ROOT / "frontend" / "draw_match_schedule" / "index.html").read_text(encoding="utf-8")
        for marker in ("snapshotTeams = teams()", "sameTeams", "invalidated = true", "byes[round.round] || round.bye", "match-caption", 'id="logout"', "/api/registration-admin/logout"):
            self.assertIn(marker, html)
        for caption in ("使用賽會人員密碼登入後", "舊抽籤結果會自動失效", "方便稍後抄錄到"):
            self.assertIn(caption, html)

    def test_lateness_fund_has_manager_gates_and_streamlit_features(self):
        html = (ROOT / "frontend" / "lateness_fund" / "index.html").read_text(encoding="utf-8")
        api = (ROOT / "api" / "funds_api.py").read_text(encoding="utf-8")
        core = (ROOT / "core" / "funds_logic.py").read_text(encoding="utf-8")
        admin = (ROOT / "api" / "admin_console_api.py").read_text(encoding="utf-8")
        for marker in ("outstanding_members", "member-summary", "export/records.csv", "export/expenses.csv", "notifyTarget", "actionDialog", "todayHK", "late_date="):
            self.assertIn(marker, html)
        self.assertNotIn('/shared/server-tables.js', html)
        for marker in ("_lateness_context(request, manager=True)", 'Field(ge=0)', 'notify_committee', 'records.csv', 'expenses.csv'):
            self.assertIn(marker, api)
        for marker in ("LATENESS_FUND_MANAGERS_DEFAULT", "lateness_managers", "is_lateness_manager", "_today_hk", "已繳金額不能為負數"):
            self.assertIn(marker, core)
        self.assertIn("lateness_fund_managers", admin)

    def test_ai_fund_has_streamlit_exports_summary_and_safe_admin_actions(self):
        html = (ROOT / "frontend" / "ai_fund" / "index.html").read_text(encoding="utf-8")
        api = (ROOT / "api" / "funds_api.py").read_text(encoding="utf-8")
        core = (ROOT / "core" / "funds_logic.py").read_text(encoding="utf-8")
        for marker in ("AI基金使用指南", "usage-summary", "export/transactions.csv", "export/usage.csv", "statusDialog", "resetConfirm", "loadCollections", 'id="logout"'):
            self.assertIn(marker, html)
        self.assertNotIn('/shared/server-tables.js', html)
        for marker in ('usage-summary', 'transactions.csv', 'usage.csv', 'HTTPException(409', 'result or {}'):
            self.assertIn(marker, api)
        for marker in ('AI_PAYMENT_METHODS', 'COALESCE(account_disabled,FALSE)=FALSE', 'def ai_usage_summary', '不能為負數', '_AI_SCHEMA_LOCK'):
            self.assertIn(marker, core)
        self.assertNotIn('DROP CONSTRAINT IF EXISTS chk_ai_fund_usage_feature', core)
        for marker in ("provider_refund", "member_refund", "Provider 退款予基金", "退款予委員"):
            self.assertIn(marker, html)
            self.assertIn(marker, core)
        self.assertIn("WHEN transaction_type='member_refund' THEN -amount_hkd", core)

    def test_ai_training_reviews_preserve_server_page(self):
        app = (ROOT / "frontend" / "ai_training" / "app.js").read_text(encoding="utf-8")
        html = (ROOT / "frontend" / "ai_training" / "index.html").read_text(encoding="utf-8")
        for marker in ("preservePage=false", "target._voteServerSpec?.page", "loadRecordings(resetPage=false)", '"adminLlm"'):
            self.assertIn(marker, app)
        self.assertIn('loadRecordings(true)', app)
        self.assertIn('/ai-training/app.js?v=__APP_VERSION__', html)
        proxy = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn('html.replace("__APP_VERSION__", APP_VERSION)', proxy)

    def test_home_displays_release_version(self):
        home = (ROOT / "frontend" / "home" / "index.html").read_text(encoding="utf-8")
        logic = (ROOT / "core" / "home_logic.py").read_text(encoding="utf-8")
        self.assertIn('id="systemVersion"', home)
        self.assertIn('data.version || "未知"', home)
        self.assertIn('from version import APP_VERSION', logic)
        self.assertIn('"version": APP_VERSION', logic)

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
        self.assertIn('_reserve_solo_live_slot', proxy)
        self.assertIn('The quota is consumed only after', proxy)
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
        self.assertIn("duration > limits.audio_max_seconds", browser)
        self.assertIn("blob.size > limits.audio_max_bytes", browser)
        self.assertIn("bytes.subarray(index, index + 32768)", browser)
        self.assertNotIn("String.fromCharCode(...new Uint8Array", browser)
        self.assertLess(html.index('data-pane="fact"'), html.index('data-pane="research"'))

    def test_free_debate_room_formats_are_restricted_at_both_layers(self):
        html = (ROOT / "frontend" / "shared" / "ai-parity.js").read_text(encoding="utf-8")
        server = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn('free && ["星島", "基本法盃"].includes', html)
        self.assertIn("structure == \"free\" and debate_format not in FREE_DEBATE_FORMATS", server)

    def test_vote_html_keeps_ai_markdown_separate_and_accounts_for_usage(self):
        html = (ROOT / "frontend" / "vote" / "index.html").read_text(encoding="utf-8")
        api = (ROOT / "api" / "vote_api.py").read_text(encoding="utf-8")
        core = (ROOT / "core" / "vote_ai.py").read_text(encoding="utf-8")
        funds = (ROOT / "core" / "funds_logic.py").read_text(encoding="utf-8")
        self.assertIn('/shared/markdown.js', html)
        self.assertIn('SafeMarkdown.render', html)
        self.assertIn('c.user_id === AI_COMMENT_USER ?', html)
        self.assertIn(': `<div>${esc(c.comment_text)}</div>`', html)
        for feature in ('"vote_review"', '"vote_analysis"', '"vote_discussion"'):
            self.assertIn(feature, api)
            self.assertIn(feature, funds)
        self.assertIn("較常支持：", core)
        self.assertIn("- 難度 {diff_label}", core)
        self.assertIn("status='pending' LIMIT 1", api)
        for marker in ('id="deposeSearch"', 'id="deposeCategory"', '已選 ${selected} 條', 'item.source_changed'):
            self.assertIn(marker, html)
        self.assertIn("analysis_source_signature", api)

    def test_video_chapter_navigation_seeks_existing_player(self):
        html = (ROOT / "frontend" / "video_replay" / "index.html").read_text(encoding="utf-8")
        self.assertIn("player.seekTo", html)

    def test_recording_audio_checks_owner_before_streaming(self):
        source = (ROOT / "api" / "ai_training_api.py").read_text(encoding="utf-8")
        self.assertIn("speaker_user_id,r2_key,mime_type,file_ext", source)
        self.assertIn("owner != str(user).strip() and not _is_admin", source)
        self.assertNotIn("audio_data", source)
        self.assertLess(source.index("owner != str(user).strip() and not _is_admin"),
                        source.index("r2_storage.presign_get"))

    def test_ai_training_parity_endpoints_are_registered(self):
        source = (ROOT / "api" / "ai_training_api.py").read_text(encoding="utf-8")
        for route in ("/recordings/quality-check", "/manuscripts", "/coverage", "/coverage/ai", "/inventory", "/regenerate-suggestions",
                      "/export/recordings.json", "/export/llm.jsonl"):
            self.assertIn(route, source)
        browser = (
            (ROOT / "frontend" / "ai_training" / "index.html").read_text(encoding="utf-8")
            + (ROOT / "frontend" / "ai_training" / "app.js").read_text(encoding="utf-8")
        )
        self.assertIn("跳過此句", browser)
        self.assertIn("data-active-type", browser)
        proxy = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/ai-training/app.js")', proxy)


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
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("streamlit run", start)
        self.assertNotIn("streamlit>=", requirements.lower())
        pages = re.findall(r'st\.Page\(["\']([^"\']+)', main)
        self.assertEqual(pages, ["legacy_streamlit/html_migration_notice.py"])


if __name__ == "__main__":
    unittest.main()
