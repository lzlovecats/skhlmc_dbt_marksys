"""The home support area publishes complete role-specific match-day runbooks."""

from pathlib import Path

from core.home_logic import runbook_for_role


ROOT = Path(__file__).resolve().parents[1]


def test_runbook_is_an_independent_support_document_without_forbidden_punctuation():
    manual = (ROOT / "assets" / "user_manual.md").read_text(encoding="utf-8")
    runbook = (ROOT / "assets" / "match_day_runbook.md").read_text(encoding="utf-8")

    assert runbook.startswith("# 比賽當日 Runbook")
    assert "比賽當日 Runbook" not in manual
    assert "；" not in runbook


def test_runbook_roles_are_complete_and_separate():
    chair = runbook_for_role("主席")
    it_adviser = runbook_for_role("IT 顧問")

    assert chair.startswith("## 主席")
    assert "主席主持易" in chair
    assert "叮叮易" in chair
    assert "宣讀賽果" in chair
    assert "由過半數真人評判決定" in chair
    assert "不要手動計分" in chair
    assert "正式賽果已宣讀及所有真人評判已完成評語後" in chair
    assert "疑難排解" in chair
    assert "## IT 顧問" not in chair

    assert it_adviser.startswith("## IT 顧問")
    assert "Kiosk" in it_adviser
    assert "獨立硬件錄音機" in it_adviser
    assert "正式 AI 評判" in it_adviser
    assert "原定真人評判數目為雙數時，本場必須加入一名正式 AI 評判" in it_adviser
    assert "按「開放雙方核對」" in it_adviser
    assert "專用核對連結傳送予該方代表" in it_adviser
    assert "電子賽務系統核心評分或計分功能中斷" in it_adviser
    assert "只有正式 AI 評判或其他 AI 功能失效時不要改用實體分紙" in it_adviser
    assert "疑難排解" in it_adviser
    assert "## 主席" not in it_adviser


def test_home_support_area_links_to_role_specific_runbook():
    html = (ROOT / "frontend" / "home" / "index.html").read_text(encoding="utf-8")
    api = (ROOT / "api" / "home_api.py").read_text(encoding="utf-8")

    assert 'data-doc="runbook"' in html
    assert 'runbook: ["主席", "IT 顧問"]' in html
    assert '@router.get("/runbook")' in api
