from pathlib import Path
from types import SimpleNamespace
import asyncio

import pandas as pd
import pytest

from api import access, community_api
from core import community_logic, push, roles


ROOT = Path(__file__).resolve().parents[1]


class _MembershipDb:
    def __init__(self, graduated=()):
        self.graduated = set(graduated)

    def query(self, _sql, params=None):
        user = str((params or {}).get("user") or "")
        return pd.DataFrame([{"found": 1}]) if user in self.graduated else pd.DataFrame()


def test_academic_year_is_september_to_august_and_exit_type_controls_ghost_status():
    assert community_logic.academic_year_label(2025) == "2025/26"
    event = community_logic.validate_history_event(
        {
            "academic_year_start": 2025,
            "event_date": "2026-08-31",
            "title": "畢業",
            "description": "",
            "match_ids": [],
            "photo_ids": [],
        }
    )
    assert event["academic_year_start"] == 2025
    with pytest.raises(ValueError, match="9 月至翌年 8 月"):
        community_logic.validate_history_event(
            {
                "academic_year_start": 2025,
                "event_date": "2026-09-01",
                "title": "錯誤學年",
                "match_ids": [],
                "photo_ids": [],
            }
        )

    db = _MembershipDb(graduated={"graduate"})
    assert roles.is_graduate("graduate", db=db)
    assert not roles.is_graduate("left", db=db)
    assert roles.is_senior_committee("graduate", db=db)
    assert not roles.is_senior_committee("left", db=db)


def test_membership_validation_requires_an_end_year_only_for_left_or_graduated():
    current = community_logic.validate_membership(
        {
            "display_name": "現役委員",
            "joined_academic_year": 2025,
            "ended_academic_year": None,
            "exit_type": "current",
        }
    )
    assert current["ended_academic_year"] is None

    graduate = community_logic.validate_membership(
        {
            "member_user_id": "old01",
            "display_name": "畢業委員",
            "joined_academic_year": 2022,
            "ended_academic_year": 2025,
            "exit_type": "graduated",
        }
    )
    assert graduate["exit_type"] == "graduated"

    with pytest.raises(ValueError, match="離隊／畢業學年"):
        community_logic.validate_membership(
            {
                "display_name": "離隊委員",
                "joined_academic_year": 2025,
                "ended_academic_year": None,
                "exit_type": "left",
            }
        )


def test_recent_match_contract_and_first_result_notification_copy():
    match = community_logic.validate_recent_match(
        {
            "competition_name": "聯中第一回合",
            "opponent": "友校",
            "match_date": "2026-09-12",
            "match_time": "14:30",
            "topic_text": "測試辯題",
            "our_side": "pro",
            "result": "win",
            "score_text": "3 : 0",
            "best_debater": "甲同學",
            "notes": "禮堂",
        }
    )
    assert match["score_text"] == "3:0"
    title, body = community_logic.recent_notification_copy(match, "result")
    assert title == "賽果：聯中第一回合－勝"
    assert "3:0" in body

    with pytest.raises(ValueError, match="比分格式"):
        community_logic.validate_recent_match({**match, "score_text": "正方勝"})


def test_forum_notification_copy_shows_author_and_title_but_not_post_body():
    title, body = community_logic.forum_notification_copy(
        "graduate01", "昔日聯中回憶", "thread",
    )
    assert title == "老鬼專區有新帖文"
    assert body == "graduate01 發表「昔日聯中回憶」"

    title, body = community_logic.forum_notification_copy(
        "graduate02", "昔日聯中回憶", "reply",
    )
    assert title == "老鬼專區有新回覆"
    assert body == "graduate02 回覆「昔日聯中回憶」"
    assert "留言內容" not in body


def test_forum_push_targets_senior_members_and_excludes_the_author(monkeypatch):
    captured = {}

    def fake_notify(db, vapid, title, body, **kwargs):
        captured.update(
            db=db, vapid=vapid, title=title, body=body, kwargs=kwargs,
        )
        return 3

    from deploy import proxy

    monkeypatch.setattr(push, "notify_committee", fake_notify)
    monkeypatch.setattr(proxy, "_get_vapid", lambda: {"private_key": "test"})
    db = object()
    result = community_api._fire_forum_push(
        db, "graduate01", 42, "昔日聯中回憶", "reply", post_id=99,
    )

    assert result == {"sent_count": 3}
    assert captured["db"] is db
    assert captured["title"] == "老鬼專區有新回覆"
    assert captured["kwargs"] == {
        "exclude_user": "graduate01",
        "senior_only": True,
        "tag": "ghost-forum-thread-42-post-99",
        "url": "/ghost-forum?thread=42",
    }


def test_senior_only_push_uses_manual_role_or_graduated_membership_filter():
    class CaptureDb:
        def __init__(self):
            self.sql = ""
            self.params = {}

        def query(self, sql, params):
            self.sql = sql
            self.params = params
            return pd.DataFrame()

    db = CaptureDb()
    sent = push.notify_committee(
        db,
        {"private_key": "test"},
        "老鬼專區有新帖文",
        "graduate01 發表「標題」",
        exclude_user="graduate01",
        senior_only=True,
    )
    assert sent == 0
    assert "committee_memberships" in db.sql
    assert "cm.member_user_id=p.user_id" in db.sql
    assert "cm.exit_type='graduated'" in db.sql
    assert "app_config" in db.sql
    assert "c.key=:senior_key" in db.sql
    assert "c.value ? p.user_id" in db.sql
    assert "p.user_id != :exclude_user" in db.sql
    assert db.params["exclude_user"] == "graduate01"
    assert db.params["senior_key"] == roles.SENIOR_COMMITTEE_MEMBERS_KEY


def test_developer_identity_has_every_delegated_management_gate(monkeypatch):
    assert roles.is_ai_manager("developer")
    assert roles.is_senior_committee("developer")

    monkeypatch.setattr(access, "has_developer_session", lambda _request: True)
    request = SimpleNamespace(cookies={})
    assert access.require_competition_staff(request) == "developer"
    assert access.require_page_user_or_developer(request, "recent_matches") == "developer"

    from deploy import proxy

    def no_committee_account(_request):
        raise access.HTTPException(401, "committee login required")

    monkeypatch.setattr(proxy, "_require_committee_user", no_committee_account)
    with pytest.raises(access.HTTPException) as caught:
        access.require_page_user(request, "ghost_forum")
    assert caught.value.status_code == 401


def test_ghost_forum_gate_accepts_every_senior_account_and_rejects_ordinary_members(monkeypatch):
    current_user = {"value": "manual-senior"}
    db = object()
    monkeypatch.setattr(
        community_api, "require_page_user",
        lambda _request, _page: current_user["value"],
    )
    monkeypatch.setattr(community_api, "_db", lambda: db)
    monkeypatch.setattr(
        community_api,
        "is_senior_committee",
        lambda user, db=None: user in {"manual-senior", "graduate"},
    )
    assert community_api._ghost_context(SimpleNamespace())[0] == "manual-senior"

    current_user["value"] = "graduate"
    assert community_api._ghost_context(SimpleNamespace())[0] == "graduate"

    current_user["value"] = "ordinary"
    with pytest.raises(community_api.HTTPException) as caught:
        community_api._ghost_context(SimpleNamespace())
    assert caught.value.status_code == 403


def test_sql_console_is_removed_and_role_migration_never_restores_its_secret():
    assert not (ROOT / "frontend" / "db_mgmt" / "index.html").exists()
    proxy_source = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
    assert '"/db-mgmt"' not in proxy_source
    assert '"/api/db-management' not in proxy_source

    up = (ROOT / "migrations" / "20260717_0003_community_roles_and_history.up.sql").read_text(encoding="utf-8")
    down = (ROOT / "migrations" / "20260717_0003_community_roles_and_history.down.sql").read_text(encoding="utf-8")
    assert "'sql_password'" in up
    assert "'sql_password'" not in down
    assert "'ai_managers'" in up
    assert "'senior_committee_members'" in up

    from deploy import proxy

    route_paths = {getattr(route, "path", "") for route in proxy.app.routes}
    assert "/db-mgmt" not in route_paths
    assert not any(path.startswith("/api/db-management") for path in route_paths)
    assert {"/recent-matches", "/team-history", "/ghost-forum"} <= route_paths


def test_new_pages_are_member_only_in_central_policy():
    from account_access import account_can_access

    for page in ("recent_matches", "team_history", "ghost_forum"):
        assert account_can_access("member01", page)
        assert not account_can_access("developer", page)


def test_new_html_routes_replace_the_shared_asset_version_placeholder():
    from deploy import proxy

    for handler in (
        proxy.recent_matches_page,
        proxy.team_history_page,
        proxy.ghost_forum_page,
    ):
        response = asyncio.run(handler())
        html = response.body.decode("utf-8")
        assert "__APP_VERSION__" not in html
        assert f"/shared/vote-ui.js?v={proxy.APP_VERSION}" in html


def test_linked_community_resources_open_their_actual_video_or_photo():
    ghost = (ROOT / "frontend" / "ghost_forum" / "index.html").read_text(
        encoding="utf-8"
    )
    history = (ROOT / "frontend" / "team_history" / "index.html").read_text(
        encoding="utf-8"
    )
    gallery = (ROOT / "frontend" / "shared" / "server-tables.js").read_text(
        encoding="utf-8"
    )

    for page in (ghost, history):
        assert 'href="/video-replay?video_id=${encodeURIComponent(row.video_id)}"' in page
        assert 'href="/team-history?match_id=${encodeURIComponent(row.match_id)}"' not in page
        assert "未有公開影片" in page
        assert 'href="/match-photos?photo_id=${row.id}"' in page
    assert "new URLSearchParams(location.search).get(\"photo_id\")" in gallery
    assert "&photo_id=${encodeURIComponent(linkedPhotoId)}" in gallery


def test_ghost_forum_uses_post_language_split_resources_and_refreshes_latest_replies():
    source = (ROOT / "frontend" / "ghost_forum" / "index.html").read_text(
        encoding="utf-8"
    )
    api_source = (ROOT / "api" / "community_api.py").read_text(encoding="utf-8")

    for expected in (
        "發表帖文",
        "最新帖文",
        "編輯標題",
        "刪除帖文",
        "🔄 重整新留言",
        'id="resourceTabs"',
        'data-resource-kind="matches"',
        'data-resource-kind="photos"',
        'id="resourcePager"',
        "latest=true",
    ):
        assert expected in source
    for retired in ("新增主題", "最新主題", "編輯主題", "刪除主題"):
        assert retired not in source
    assert ".toolbar { margin-bottom:1rem; }" in source
    assert "resourceRequest" in source
    assert "AND p.is_first_post=FALSE) post_count" in api_source


def test_refresh_new_replies_returns_the_latest_post_page(monkeypatch):
    captured = {}

    class ForumDb:
        def query(self, sql, params):
            if "FROM ghost_forum_threads" in sql:
                return pd.DataFrame(
                    [
                        {
                            "id": 7,
                            "title": "最新帖文",
                            "author_user_id": "graduate",
                            "revision": 1,
                            "can_edit": True,
                        }
                    ]
                )
            if "COUNT(*) total" in sql:
                return pd.DataFrame([{"total": 41}])
            if "FROM ghost_forum_posts" in sql:
                captured.update(params)
                return pd.DataFrame()
            raise AssertionError(sql)

    db = ForumDb()
    monkeypatch.setattr(
        community_api,
        "_ghost_context",
        lambda _request: ("graduate", db),
    )
    monkeypatch.setattr(
        community_api,
        "_resource_links",
        lambda _db, owner_ids, forum=False: {
            int(owner): {"matches": [], "photos": []} for owner in owner_ids
        },
    )

    result = community_api.forum_thread(7, SimpleNamespace(), latest=True)

    assert result["posts"]["page"] == 3
    assert captured["offset"] == 40


def test_linked_match_resolves_the_first_visible_video_by_display_order():
    calls = []

    class ResourceDb:
        def query(self, sql, params):
            calls.append(sql)
            if "ghost_forum_thread_matches" in sql:
                return pd.DataFrame(
                    [
                        {
                            "owner_id": 4,
                            "match_id": "M1",
                            "video_id": 73,
                            "match_date": "2026-07-17",
                            "match_time": "14:00",
                            "topic_text": "測試辯題",
                            "pro_team": "甲隊",
                            "con_team": "乙隊",
                            "debate_format": "聯中",
                        }
                    ]
                )
            return pd.DataFrame()

    links = community_api._resource_links(ResourceDb(), [4], forum=True)

    assert links[4]["matches"][0]["video_id"] == 73
    match_sql = calls[0]
    assert "LEFT JOIN LATERAL" in match_sql
    assert "match_videos" in match_sql
    assert "COALESCE(video.is_visible,TRUE)=TRUE" in match_sql
    assert "video.display_order ASC NULLS LAST" in match_sql
    assert "LIMIT 1" in match_sql


def test_forward_role_cleanup_removes_only_recreated_legacy_aliases():
    up = (
        ROOT / "migrations" / "20260717_0004_remove_recreated_legacy_roles.up.sql"
    ).read_text(encoding="utf-8")
    down = (
        ROOT / "migrations" / "20260717_0004_remove_recreated_legacy_roles.down.sql"
    ).read_text(encoding="utf-8")

    for key in (
        "tts_recording_reviewers",
        "ai_fund_treasurers",
        "lateness_fund_managers",
    ):
        assert f"'{key}'" in up
        assert f"'{key}'" in down
    assert "DELETE FROM public.app_config" in up
    delete_block = up.split("DELETE FROM public.app_config", 1)[1]
    assert "'ai_managers'" not in delete_block
    assert "'senior_committee_members'" not in delete_block
    assert "'sql_password'" not in down


def test_senior_management_controls_are_on_separate_hidden_tabs():
    recent = (ROOT / "frontend" / "recent_matches" / "index.html").read_text(encoding="utf-8")
    history = (ROOT / "frontend" / "team_history" / "index.html").read_text(encoding="utf-8")
    lateness = (ROOT / "frontend" / "lateness_fund" / "index.html").read_text(encoding="utf-8")

    assert 'id="manageTab" class="hidden" data-pane="managePane"' in recent
    assert recent.index('id="managePane"') < recent.index('id="manageCard"')
    assert 'id="manageMatches"' in recent

    assert 'id="manageTab" class="hidden" data-pane="managePane"' in history
    assert history.index('id="managePane"') < history.index('id="eventManager"')
    assert history.index('id="managePane"') < history.index('id="memberManager"')
    assert 'id="eventManagementList"' in history
    assert 'id="memberManagementList"' in history

    assert 'id="seniorTab" class="hidden" data-pane="seniorManagement"' in lateness
    assert lateness.index('id="seniorManagement"') < lateness.index('id="openingCard"')
    assert lateness.index('id="seniorManagement"') < lateness.index('id="notifyCard"')
    assert lateness.index('id="seniorManagement"') < lateness.index('id="expenseForm"')
    assert 'id="managerRecords"' in lateness
    assert 'id="managerExpenses"' in lateness


@pytest.mark.parametrize(
    ("kind", "table_name", "id_key"),
    (("matches", "matches", "match_id"), ("photos", "match_photos", "id")),
)
def test_history_resource_picker_is_server_paginated_at_twenty_items(
    monkeypatch, kind, table_name, id_key,
):
    class ResourceDb:
        def __init__(self):
            self.calls = []

        def query(self, sql, params):
            self.calls.append((sql, dict(params)))
            if "COUNT(*)" in sql:
                return pd.DataFrame([{"total": 41}])
            if kind == "matches":
                return pd.DataFrame(
                    [{"match_id": f"m{index}"} for index in range(20)]
                )
            return pd.DataFrame([{"id": index + 1} for index in range(20)])

    db = ResourceDb()
    monkeypatch.setattr(
        community_api, "_member_context", lambda _request, _page: ("member", db),
    )
    result = community_api.resource_options(
        SimpleNamespace(), search="聯中", kind=kind, page=2,
    )

    assert result["kind"] == kind
    assert result["page"] == 2
    assert result["page_size"] == 20
    assert result["total"] == 41
    assert result["total_pages"] == 3
    assert len(result["items"]) == 20
    assert id_key in result["items"][0]
    assert len(db.calls) == 2
    assert f"FROM {table_name}" in db.calls[0][0]
    assert db.calls[1][1]["limit"] == 20
    assert db.calls[1][1]["offset"] == 20


def test_history_resource_picker_rejects_unknown_resource_kind(monkeypatch):
    monkeypatch.setattr(
        community_api, "_member_context", lambda _request, _page: ("member", object()),
    )
    with pytest.raises(community_api.HTTPException) as caught:
        community_api.resource_options(SimpleNamespace(), kind="everything")
    assert caught.value.status_code == 400
