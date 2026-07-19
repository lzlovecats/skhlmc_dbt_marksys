import datetime as dt
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.match_topic_release import (
    TopicReleaseError,
    _final_topic,
    _sync_final_topic,
    public_payload,
    release_schedule,
    visible_topic_for_roster,
)


ROOT = Path(__file__).resolve().parents[1]


def release_row(**overrides):
    schedule = release_schedule("2026-08-16", "16:00")
    row = {
        "id": 1,
        "match_id": "決賽",
        "release_match_date": dt.date(2026, 8, 16),
        "release_match_time": dt.time(16, 0),
        "candidate_1": "第一條辯題",
        "candidate_2": "第二條辯題",
        "candidate_3": "第三條辯題",
        "pro_token": "private-pro-token",
        "con_token": "private-con-token",
        "pro_team": "甲隊",
        "con_team": "乙隊",
        "pro_veto_candidate": None,
        "pro_veto_at": None,
        "con_veto_candidate": None,
        "con_veto_at": None,
        **schedule,
    }
    row.update(overrides)
    return row


def test_release_schedule_uses_hong_kong_rules_boundaries():
    schedule = release_schedule("2026-08-16", "16:00")
    assert schedule == {
        "first_reveal_at": dt.datetime(2026, 8, 2, 17, 0),
        "first_veto_deadline": dt.datetime(2026, 8, 3, 16, 0),
        "second_reveal_at": dt.datetime(2026, 8, 3, 17, 0),
        "second_veto_deadline": dt.datetime(2026, 8, 4, 16, 0),
        "third_reveal_at": dt.datetime(2026, 8, 4, 17, 0),
        "expires_at": dt.datetime(2026, 8, 16, 16, 0),
    }
    with pytest.raises(TopicReleaseError):
        release_schedule("", "16:00")


def test_public_link_does_not_leak_unreleased_topics_or_tokens():
    payload = public_payload(
        release_row(), "pro", now=dt.datetime(2026, 8, 2, 16, 59, 59),
    )
    assert payload["phase"] == "scheduled"
    assert payload["topic_text"] == ""
    assert payload["candidate_number"] == 1
    assert payload["veto_allowed"] is False
    serialized = repr(payload)
    assert "第一條辯題" not in serialized
    assert "第二條辯題" not in serialized
    assert "第三條辯題" not in serialized
    assert "private-pro-token" not in serialized
    assert "private-con-token" not in serialized


def test_first_topic_reveals_at_five_and_becomes_final_after_deadline():
    revealed = public_payload(
        release_row(), "con", now=dt.datetime(2026, 8, 2, 17, 0),
    )
    assert revealed["phase"] == "revealed"
    assert revealed["topic_text"] == "第一條辯題"
    assert revealed["veto_allowed"] is True
    assert revealed["final"] is False

    deadline = public_payload(
        release_row(), "con", now=dt.datetime(2026, 8, 3, 16, 0),
    )
    assert deadline["phase"] == "final"
    assert deadline["topic_text"] == "第一條辯題"
    assert deadline["veto_allowed"] is False


def test_veto_advances_to_next_scheduled_topic_without_revealing_identity():
    after_first_veto = release_row(
        pro_veto_candidate=1,
        pro_veto_at=dt.datetime(2026, 8, 2, 18, 0),
    )
    other_side = public_payload(
        after_first_veto, "con", now=dt.datetime(2026, 8, 2, 18, 1),
    )
    assert other_side["phase"] == "scheduled"
    assert other_side["candidate_number"] == 2
    assert other_side["topic_text"] == ""
    assert other_side["veto_count"] == 1
    assert "vetoed_by" not in other_side
    assert "pro_veto" not in repr(other_side)

    second_reveal_for_first_side = public_payload(
        after_first_veto, "pro", now=dt.datetime(2026, 8, 3, 17, 0),
    )
    second_reveal_for_other_side = public_payload(
        after_first_veto, "con", now=dt.datetime(2026, 8, 3, 17, 0),
    )
    assert second_reveal_for_first_side["topic_text"] == "第二條辯題"
    assert second_reveal_for_first_side["my_veto_used"] is True
    assert second_reveal_for_first_side["veto_allowed"] is False
    assert second_reveal_for_other_side["veto_allowed"] is True


def test_second_veto_advances_to_third_final_topic_and_link_expires_at_match():
    row = release_row(
        pro_veto_candidate=1,
        pro_veto_at=dt.datetime(2026, 8, 2, 18, 0),
        con_veto_candidate=2,
        con_veto_at=dt.datetime(2026, 8, 3, 18, 0),
    )
    scheduled = public_payload(row, "pro", now=dt.datetime(2026, 8, 3, 18, 1))
    assert scheduled["candidate_number"] == 3
    assert scheduled["topic_text"] == ""
    assert scheduled["veto_count"] == 2

    final = public_payload(row, "pro", now=dt.datetime(2026, 8, 4, 17, 0))
    assert final["phase"] == "final"
    assert final["topic_text"] == "第三條辯題"
    assert final["veto_allowed"] is False

    expired = public_payload(row, "pro", now=dt.datetime(2026, 8, 16, 16, 0))
    assert expired == {
        "phase": "expired",
        "expired": True,
        "message": "此辯題連結已於比賽開始時失效。",
    }


def test_only_rules_final_topic_is_selected_for_match_record():
    no_veto = release_row()
    assert _final_topic(no_veto, now=dt.datetime(2026, 8, 3, 15, 59, 59)) == ""
    assert _final_topic(no_veto, now=dt.datetime(2026, 8, 3, 16, 0)) == "第一條辯題"

    one_veto = release_row(
        pro_veto_candidate=1,
        pro_veto_at=dt.datetime(2026, 8, 2, 18, 0),
    )
    assert _final_topic(one_veto, now=dt.datetime(2026, 8, 4, 15, 59, 59)) == ""
    assert _final_topic(one_veto, now=dt.datetime(2026, 8, 4, 16, 0)) == "第二條辯題"

    two_vetoes = release_row(
        pro_veto_candidate=1,
        pro_veto_at=dt.datetime(2026, 8, 2, 18, 0),
        con_veto_candidate=2,
        con_veto_at=dt.datetime(2026, 8, 3, 18, 0),
    )
    assert _final_topic(two_vetoes, now=dt.datetime(2026, 8, 4, 16, 59, 59)) == ""
    assert _final_topic(two_vetoes, now=dt.datetime(2026, 8, 4, 17, 0)) == "第三條辯題"


def test_match_record_is_blank_until_final_then_receives_only_final_topic():
    statements = []

    class Connection:
        def execute(self, statement, params):
            statements.append((" ".join(str(statement).split()), params))

    row = release_row()
    conn = Connection()
    assert _sync_final_topic(
        conn, row, now=dt.datetime(2026, 8, 3, 15, 59, 59),
    ) == ""
    assert "SET topic_text=''" in statements[-1][0]
    assert statements[-1][1] == {"match_id": "決賽"}

    assert _sync_final_topic(
        conn, row, now=dt.datetime(2026, 8, 3, 16, 0),
    ) == "第一條辯題"
    assert "SET topic_text=:topic" in statements[-1][0]
    assert statements[-1][1] == {"topic": "第一條辯題", "match_id": "決賽"}


def test_roster_visibility_promotes_final_topic_in_one_locked_transaction():
    statements = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Row:
        def __init__(self, values):
            self._mapping = values

    class Connection:
        def execute(self, statement, params):
            sql = " ".join(str(statement).split())
            statements.append((sql, params))
            if "FROM matches" in sql and "FOR UPDATE" in sql:
                return Result(("決賽",))
            if "FROM match_topic_releases r" in sql:
                return Result(Row(release_row()))
            return Result()

    class Db:
        @contextmanager
        def transaction(self):
            yield Connection()

        def query(self, *_args, **_kwargs):
            raise AssertionError("roster visibility must not read outside its transaction")

        def execute(self, *_args, **_kwargs):
            raise AssertionError("roster visibility must not write outside its transaction")

    result = visible_topic_for_roster(
        "決賽", "舊辯題", Db(), now=dt.datetime(2026, 8, 3, 16, 0),
    )

    assert result == {
        "topic_text": "第一條辯題",
        "topic_locked": False,
        "topic_reveal_at": "2026-08-02T17:00:00+08:00",
    }
    assert "FROM matches" in statements[0][0]
    assert "FOR UPDATE" in statements[0][0]
    assert "FROM match_topic_releases r" in statements[1][0]
    assert "FOR UPDATE OF r" in statements[1][0]
    assert "UPDATE matches SET topic_text=:topic" in statements[2][0]


def test_topic_release_schema_api_and_frontend_contracts_are_wired():
    migration = (ROOT / "migrations/20260719_0001_match_topic_releases.up.sql").read_text()
    proxy = (ROOT / "deploy/proxy.py").read_text()
    admin_page = (ROOT / "frontend/match_info/index.html").read_text()
    team_page = (ROOT / "frontend/match_topic/index.html").read_text()
    roster_page = (ROOT / "frontend/team_roster/index.html").read_text()
    manual = (ROOT / "assets/user_manual.md").read_text()
    topic_logic = (ROOT / "core/match_topic_release.py").read_text()
    match_logic = (ROOT / "core/match_logic.py").read_text()

    assert "CREATE TABLE public.match_topic_releases" in migration
    assert "idx_match_topic_releases_active_match" in migration
    assert "REVOKE ALL PRIVILEGES ON TABLE public.match_topic_releases FROM PUBLIC" in migration
    assert "app.include_router(match_topic_release_router)" in proxy
    assert '@app.get("/match-topic")' in proxy
    assert "topic-release/open" in admin_page
    assert "產生辯題公佈連結" in admin_page
    assert "產生正反方查閱辯題連結" in admin_page
    assert 'id="refreshTopicRelease"' not in admin_page
    assert 'id="rotateTopicReleaseLinks"' not in admin_page
    assert 'id="cancelTopicRelease"' in admin_page
    assert 'id="topic" readonly' in admin_page
    assert "data-copy-topic" in admin_page
    assert 'difficulty_clause = " WHERE difficulty=:difficulty" if difficulty else ""' in topic_logic
    assert "ORDER BY RANDOM() LIMIT 3" in topic_logic
    assert "next_topic =" not in topic_logic
    assert "Persist only a rules-final topic" in topic_logic
    assert '"topic": _clean(exists._mapping.get("topic_text"))' in match_logic
    assert "產生辯題公布連結" in manual
    assert "抽取三條辯題並產生正反方查閱連結" in manual
    assert "預抽三題並建立私人分享連結" not in manual
    assert "任何時候重新產生私人連結" not in manual
    assert "查閱完整抽題及否決紀錄" not in manual
    assert "/api/match-topic-release/data" in team_page
    assert "/api/match-topic-release/veto" in team_page
    assert "不會顯示由哪一方提出否決" in team_page
    assert "專用辯題連結" in roster_page


def test_competition_prep_lists_use_grouped_filters_and_collapsible_categories():
    page = (ROOT / "frontend/ai_coach/index.html").read_text()
    script = (ROOT / "frontend/shared/competition-prep.js").read_text()
    for control in (
        "prepManuscriptSearch", "prepManuscriptFilterStatus",
        "prepStrategySearch", "prepStrategyFilterSlot",
        "prepEvidenceSearch", "prepEvidenceFilterType",
        "prepWeaknessSearch", "prepWeaknessFilterStatus",
    ):
        assert f'id="{control}"' in page
    assert 'details.className = "prep-group"' in script
    assert "renderGrouped(host" in script
    assert "沒有符合目前搜尋或篩選條件的內容" in script
