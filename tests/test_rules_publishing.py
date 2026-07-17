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


def test_rules_markdown_bold_delimiters_are_unambiguous():
    rules = (ROOT / "assets" / "rules.md").read_text(encoding="utf-8")

    assert re.search(r"\*\*[^*\n]+\*\*\S", rules) is None
    assert all(line.count("**") % 2 == 0 for line in rules.splitlines())


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
