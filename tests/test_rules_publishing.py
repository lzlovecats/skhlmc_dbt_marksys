"""Official competition rules are published as one complete support document."""

import re
from pathlib import Path

from core.home_logic import rules_for_role


ROOT = Path(__file__).resolve().parents[1]


def test_rules_asset_contains_full_sections_and_confirmed_event_roles():
    rules = (ROOT / "assets" / "rules.md").read_text(encoding="utf-8")

    for section in "零一二三四五六七":
        assert f"## {section}、" in rules
    assert "主席兼任官方計時員" in rules
    assert "使用「叮叮易」操作官方碼表" in rules
    assert "IT 顧問" in rules
    assert "Kiosk" in rules
    assert "標準競賽排名" in rules
    assert "`1、1、3`" in rules
    assert "由過半數評判決定" in rules
    assert "原定評判數目為雙數" in rules
    assert "必須在所有原定評判正式提交電子分紙後加入一名正式 AI 評判" in rules
    assert "所有評判及正式 AI 評判（如有）給予同一辯員的名次" in rules
    assert "所有評判及正式 AI 評判（如有）給予該等辯員的平均得分" in rules
    assert "系統的核心評分或計分功能故障" in rules
    assert "如只有正式 AI 評判或其他 AI 功能無法運作" in rules
    assert "主席不得在宣讀賽果前手動計分" in rules
    assert "主席宣讀正式賽果及評判完成評語後" in rules
    assert "專用核對連結傳送予該方代表" in rules
    assert "所有評判分紙，以及適用時的正式 AI 評判分紙" in rules


def test_rules_markdown_bold_delimiters_are_unambiguous():
    rules = (ROOT / "assets" / "rules.md").read_text(encoding="utf-8")

    assert re.search(r"\*\*[^*\n]+\*\*\S", rules) is None
    assert all(line.count("**") % 2 == 0 for line in rules.splitlines())


def test_new_rules_provisions_do_not_use_chinese_semicolons():
    rules = (ROOT / "assets" / "rules.md").read_text(encoding="utf-8")
    new_provisions = [
        line for line in rules.splitlines()
        if any(marker in line for marker in (
            "由過半數評判決定",
            "正式 AI 評判",
            "電子系統故障及後備分紙",
            "核分流程",
        ))
    ]

    assert new_provisions
    assert all("；" not in line for line in new_provisions)


def test_rules_are_not_filtered_by_viewer_role():
    full_rules = rules_for_role("評判")

    assert rules_for_role("賽會人員") == full_rules
    assert rules_for_role("參賽隊伍") == full_rules
    assert "## 七、賽會人員職責分工" in full_rules


def test_home_points_to_complete_rules_in_support_section():
    html = (ROOT / "frontend" / "home" / "index.html").read_text(encoding="utf-8")

    assert "如需查閱賽規，請至最底「📚 支援資料」查閱。" in html
    assert '<button type="button" data-doc="rules">📋 查看賽規</button>' in html
    assert "rules: []" in html
    assert 'activeRole = kind === "rules" ? ""' in html
