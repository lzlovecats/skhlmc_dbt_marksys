"""Responsive layout contracts for the developer settings page."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dev_settings_split_items_can_shrink_around_scrollable_tables():
    source = (ROOT / "frontend" / "dev_settings" / "index.html").read_text(
        encoding="utf-8"
    )

    rule = re.search(r"\.split\s*>\s*\*\s*\{([^}]*)\}", source)

    assert rule, "dev-settings split children need an explicit shrink contract"
    assert re.search(r"\bmin-width\s*:\s*0(?:px)?\s*;", rule.group(1))
