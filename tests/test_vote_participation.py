"""Vote participation denominators — joining before membership must not count."""

import re
from pathlib import Path

import pandas as pd

from account_access import NON_MEMBER_ACCOUNT_DB_KEYS
from core.members import get_member_participation_stats
from schema import CREATE_COMMITTEE_VOTE_ACTIVITY_VIEW


ROOT = Path(__file__).resolve().parents[1]


class _ActivityViewDb:
    def __init__(self, rows):
        self.frame = pd.DataFrame(rows)

    def query(self, _sql, _params=None):
        return self.frame


def _compact(value):
    return " ".join(str(value).split())


def test_activity_view_filters_ballots_by_each_members_start_date():
    sql = _compact(CREATE_COMMITTEE_VOTE_ACTIVITY_VIEW)

    # Eligible events are joined to each account before ballot aggregation, so
    # no pre-membership ballot can enter either denominator.
    assert (
        "JOIN all_events event ON account.active_since IS NULL OR "
        "event.created_at::DATE >= account.active_since"
        in sql
    )
    assert (
        "LEFT JOIN event_ballots ballot ON ballot.topic_text = event.topic_text"
        in sql
    )
    assert "ROW_NUMBER() OVER" in sql
    assert "SELECT COUNT(*) FROM all_events" not in sql


def test_activity_view_excludes_every_non_member_system_account():
    sql = CREATE_COMMITTEE_VOTE_ACTIVITY_VIEW
    assert "LOWER(a.user_id) NOT IN" in sql
    for account_id in NON_MEMBER_ACCOUNT_DB_KEYS:
        assert f"'{account_id}'" in sql


def test_agree_rate_uses_only_eligible_cast_ballots():
    db = _ActivityViewDb([
        {
            "user_id": "new_member",
            "account_status": "active",
            "total_votes": 70,
            "participated_votes": 70,
            "last10_participated": 10,
            # Five older ballots (including two agree votes) are intentionally
            # absent: the fixed view removes anything before active_since from
            # both numerator and denominator.
            "total_ballots": 70,
            "agree_ballots": 45,
            "overall_rate_pct": 100.0,
            "agree_rate_pct": 64.3,
            "is_active": True,
        }
    ])

    rows, total_votes = get_member_participation_stats(db=db)

    assert total_votes == 70
    assert rows[0]["整體投票次數"] == "70 / 70"
    assert rows[0]["同意票數"] == "45 / 70"
    assert rows[0]["投票同意率"] == "64.3%"


def test_pre_membership_ballots_do_not_create_a_false_agree_rate():
    db = _ActivityViewDb([
        {
            "user_id": "new_member",
            "account_status": "inactive",
            "total_votes": 70,
            "participated_votes": 0,
            "last10_participated": 0,
            "total_ballots": 0,
            "agree_ballots": 0,
            "overall_rate_pct": 0.0,
            "agree_rate_pct": None,
            "is_active": False,
        }
    ])

    rows, _ = get_member_participation_stats(db=db)

    assert rows[0]["整體投票次數"] == "0 / 70"
    assert rows[0]["同意票數"] == "0 / 0"
    assert rows[0]["投票同意率"] == "—"


def test_depose_multiselect_renders_category_and_difficulty_metadata():
    source = (ROOT / "frontend" / "vote" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'class="multi-meta"' in source
    assert "類別：${esc(t.category" in source
    assert "難度：${esc(diffLabel(t.difficulty)" in source


def test_developer_maintenance_control_never_starts_as_a_blank_button():
    source = (ROOT / "frontend" / "dev_settings" / "index.html").read_text(
        encoding="utf-8"
    )

    assert (
        '<button id="toggleMaint" class="primary" type="button">'
        "切換維護模式</button>"
    ) in source
    assert '<p id="maintState">正在讀取目前維護模式…</p>' in source


def test_other_dynamic_controls_have_non_blank_fallback_labels():
    ai_fund = (ROOT / "frontend" / "ai_fund" / "index.html").read_text(
        encoding="utf-8"
    )
    judging = (ROOT / "frontend" / "judging" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="statusSubmit" type="button" class="primary">\n            確認狀態' in ai_fund
    assert 'id="tabPro"\n            >正方</button' in judging
    assert 'id="tabCon"\n            >反方</button' in judging


def test_shared_form_labels_and_buttons_have_explicit_vertical_spacing():
    source = (ROOT / "frontend" / "shared" / "app-shell.css").read_text(
        encoding="utf-8"
    )

    assert "label > input:not([type=\"checkbox\"]):not([type=\"radio\"])" in source
    assert "form > button { margin-top: 1rem; }" in source


def test_shared_form_controls_cannot_exceed_mobile_container_width():
    source = (ROOT / "frontend" / "shared" / "app-shell.css").read_text(
        encoding="utf-8"
    )
    rule = re.search(r"input,\s*textarea,\s*select\s*\{([^}]*)\}", source)

    assert rule, "shared form controls need a common width contract"
    assert re.search(r"\bmin-width\s*:\s*0(?:px)?\s*;", rule.group(1))
    assert re.search(r"\bmax-width\s*:\s*100%\s*;", rule.group(1))


def test_ai_fund_split_cards_can_shrink_inside_the_mobile_viewport():
    source = (ROOT / "frontend" / "ai_fund" / "index.html").read_text(
        encoding="utf-8"
    )
    rule = re.search(r"\.split\s*>\s*\*\s*\{([^}]*)\}", source)

    assert rule, "AI Fund split-grid children need an explicit shrink contract"
    assert re.search(r"\bmin-width\s*:\s*0(?:px)?\s*;", rule.group(1))
