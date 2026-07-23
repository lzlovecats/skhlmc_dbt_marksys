from pathlib import Path
from types import SimpleNamespace
import asyncio
import json

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
            "video_ids": [],
            "photo_ids": [],
        }
    )
    assert event["academic_year_start"] == 2025
    linked = community_logic.validate_history_event(
        {
            "academic_year_start": 2025,
            "event_date": "",
            "title": "多影片事件",
            "video_ids": [73, 73, 74],
            "photo_ids": [],
        }
    )
    assert linked["video_ids"] == [73, 74]
    with pytest.raises(ValueError, match="9 月至翌年 8 月"):
        community_logic.validate_history_event(
            {
                "academic_year_start": 2025,
                "event_date": "2026-09-01",
                "title": "錯誤學年",
                "video_ids": [],
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


def test_history_memberships_default_oldest_and_allow_newest_server_sort(monkeypatch):
    class CaptureDb:
        def __init__(self):
            self.calls = []

        def query(self, sql, params=None):
            self.calls.append((sql, dict(params or {})))
            if "COUNT(*) total" in sql:
                return pd.DataFrame([{"total": 0}])
            return pd.DataFrame()

    db = CaptureDb()
    monkeypatch.setattr(
        community_api,
        "_member_context",
        lambda _request, _page: ("member", db),
    )

    community_api.history_memberships(SimpleNamespace())
    default_sql = next(sql for sql, _params in db.calls if "SELECT id,member_user_id" in sql)
    assert "ORDER BY joined_academic_year ASC,display_name ASC,id ASC" in default_sql

    db.calls.clear()
    community_api.history_memberships(SimpleNamespace(), order="newest")
    newest_sql = next(sql for sql, _params in db.calls if "SELECT id,member_user_id" in sql)
    assert "ORDER BY joined_academic_year DESC,display_name ASC,id DESC" in newest_sql

    with pytest.raises(community_api.HTTPException) as caught:
        community_api.history_memberships(SimpleNamespace(), order="sideways")
    assert caught.value.status_code == 400


def test_history_events_default_newest_and_allow_oldest_server_sort(monkeypatch):
    class CaptureDb:
        def __init__(self):
            self.calls = []

        def query(self, sql, params=None):
            self.calls.append((sql, dict(params or {})))
            if "COUNT(*) total" in sql:
                return pd.DataFrame([{"total": 0}])
            return pd.DataFrame()

    db = CaptureDb()
    monkeypatch.setattr(
        community_api,
        "_member_context",
        lambda _request, _page: ("member", db),
    )

    community_api.history_events(SimpleNamespace())
    default_sql = next(sql for sql, _params in db.calls if "SELECT id,academic_year_start" in sql)
    assert "ORDER BY academic_year_start DESC,event_date DESC NULLS LAST,id DESC" in default_sql

    db.calls.clear()
    community_api.history_events(SimpleNamespace(), order="oldest")
    oldest_sql = next(sql for sql, _params in db.calls if "SELECT id,academic_year_start" in sql)
    assert "ORDER BY academic_year_start ASC,event_date ASC NULLS LAST,id ASC" in oldest_sql

    with pytest.raises(community_api.HTTPException) as caught:
        community_api.history_events(SimpleNamespace(), order="sideways")
    assert caught.value.status_code == 400


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
        "forum_thread_id": 42,
        "tag": "ghost-forum-thread-42",
        "url": "/ghost-forum?thread=42&post=99",
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

    assert "mediaHref(`/video-replay?video_id=${encodeURIComponent(row.id)}`" in ghost
    assert "mediaHref(`/video-replay?video_id=${encodeURIComponent(row.id)}`" in history
    for page in (ghost, history):
        assert 'href="/team-history?match_id=${encodeURIComponent(row.match_id)}"' not in page
        assert "mediaHref(`/match-photos?photo_id=${encodeURIComponent(row.id)}`" in page
        assert 'searchParams.set("return_to"' in page
    assert "new URLSearchParams(location.search).get(\"photo_id\")" in gallery
    assert "&photo_id=${encodeURIComponent(linkedPhotoId)}" in gallery


def test_all_general_frontend_pages_offer_a_home_link():
    # The home page is the destination.  match_topic is a private bearer-link
    # view whose deliberately narrow navigation contract is tested separately.
    excluded = {"home", "match_topic"}
    missing = []
    for page in (ROOT / "frontend").glob("*/index.html"):
        if page.parent.name in excluded:
            continue
        if 'href="/"' not in page.read_text(encoding="utf-8"):
            missing.append(page.parent.name)

    assert missing == []


def test_match_photo_review_uses_the_school_gallery_name_everywhere():
    home = (ROOT / "frontend" / "home" / "index.html").read_text(
        encoding="utf-8"
    )
    photos = (ROOT / "frontend" / "match_photos" / "index.html").read_text(
        encoding="utf-8"
    )
    bug_labels = (ROOT / "core" / "bug_report_logic.py").read_text(
        encoding="utf-8"
    )

    for source in (home, photos, bug_labels):
        assert "聖呂中辯圖片回顧" in source
        assert "比賽圖片回顧" not in source


def test_linked_media_pages_offer_only_validated_source_return_links():
    ghost = (ROOT / "frontend" / "ghost_forum" / "index.html").read_text(encoding="utf-8")
    history = (ROOT / "frontend" / "team_history" / "index.html").read_text(encoding="utf-8")
    photos = (ROOT / "frontend" / "match_photos" / "index.html").read_text(encoding="utf-8")
    replay = (ROOT / "frontend" / "video_replay" / "index.html").read_text(encoding="utf-8")
    shared = (ROOT / "frontend" / "shared" / "server-tables.js").read_text(encoding="utf-8")

    assert "linkHtml(activeThread.links,returnTo)" in ghost
    assert 'url.searchParams.set("return_to", `${location.pathname}${location.search}`)' in history
    for page in (photos, replay):
        assert 'id="sourceReturn"' in page
        assert "← 返回主頁" in page
    assert '"/ghost-forum": "← 返回剛才帖文"' in shared
    assert '"/team-history": "← 返回隊史 Timeline"' in shared
    assert "target.origin === location.origin && labels[target.pathname]" in shared
    assert "history.back()" in shared
    assert 'shareUrl.searchParams.delete("return_to")' in replay


def test_linked_history_event_returns_to_the_exact_forum_thread():
    ghost = (ROOT / "frontend" / "ghost_forum" / "index.html").read_text(
        encoding="utf-8"
    )
    history = (ROOT / "frontend" / "team_history" / "index.html").read_text(
        encoding="utf-8"
    )
    shared = (ROOT / "frontend" / "shared" / "server-tables.js").read_text(
        encoding="utf-8"
    )

    assert "linkedHistoryEventHtml(row,returnTo)" in ghost
    assert (
        "mediaHref(`/team-history?event_id=${encodeURIComponent(row.id)}`,returnTo)"
        in ghost
    )
    assert 'id="sourceReturn"' in history
    assert 'src="/shared/server-tables.js?v=__APP_VERSION__"' in history
    assert '"/team-history": {' in shared
    assert '"/ghost-forum": "← 返回剛才帖文"' in shared
    assert "target.origin === location.origin && labels[target.pathname]" in shared


def test_history_membership_sort_controls_share_url_backed_server_order():
    history = (ROOT / "frontend" / "team_history" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="memberSort"' in history
    assert 'id="memberManagementSort"' in history
    assert 'id="memberSort"><option value="oldest">由舊至新</option><option value="newest">由新至舊</option>' in history
    assert 'id="memberManagementSort"><option value="oldest">由舊至新</option><option value="newest">由新至舊</option>' in history
    assert 'pageParams.get("membership_order")' in history
    assert 'searchParams.set("membership_order", membershipOrder)' in history
    assert "history.replaceState(null, \"\", url)" in history
    assert "memberships?order=${membershipOrder}" in history


def test_history_timeline_sort_controls_share_url_backed_server_order():
    history = (ROOT / "frontend" / "team_history" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="eventSort"><option value="newest">由新至舊</option><option value="oldest">由舊至新</option>' in history
    assert 'id="eventManagementSort"><option value="newest">由新至舊</option><option value="oldest">由舊至新</option>' in history
    assert 'pageParams.get("timeline_order")' in history
    assert 'url.searchParams.set("timeline_order", eventOrder)' in history
    assert "history/events?order=${eventOrder}" in history
    assert '["eventSort", "eventManagementSort"]' in history
    assert "連結現有影片／圖片" in history


def test_installed_web_app_allows_device_orientation_changes():
    manifest = json.loads(
        (ROOT / "static" / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["display"] == "standalone"
    assert manifest["orientation"] == "any"


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
        'data-resource-kind="videos"',
        'data-resource-kind="photos"',
        'data-resource-kind="history_events"',
        'id="resourcePager"',
        'params.set("latest","true")',
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
        "_forum_resource_links",
        lambda _db, owner_ids: {
            int(owner): {"videos": [], "photos": [], "history_events": []}
            for owner in owner_ids
        },
    )

    result = community_api.forum_thread(7, SimpleNamespace(), latest=True)

    assert result["posts"]["page"] == 3
    assert captured["offset"] == 40


def test_forum_links_preserve_the_exact_video_and_history_event():
    calls = []

    class ResourceDb:
        def query(self, sql, params):
            calls.append(sql)
            if "ghost_forum_thread_videos" in sql:
                return pd.DataFrame(
                    [
                        {
                            "owner_id": 4,
                            "id": 73,
                            "video_title": "指定影片",
                            "match_display": "M1",
                            "topic_text": "測試辯題",
                            "pro_team": "甲隊",
                            "con_team": "乙隊",
                        }
                    ]
                )
            if "ghost_forum_thread_history_events" in sql:
                return pd.DataFrame(
                    [{
                        "owner_id": 4,
                        "id": 9,
                        "academic_year_start": 2025,
                        "event_date": "2026-07-17",
                        "title": "隊史事件",
                        "description": "事件內容",
                    }]
                )
            return pd.DataFrame()

    links = community_api._forum_resource_links(ResourceDb(), [4])

    assert links[4]["videos"][0]["id"] == 73
    assert links[4]["videos"][0]["video_title"] == "指定影片"
    assert links[4]["history_events"][0]["id"] == 9
    assert links[4]["history_events"][0]["academic_year_label"] == "2025/26"
    video_sql = calls[0]
    assert "ghost_forum_thread_videos" in video_sql
    assert "JOIN match_videos v ON v.id=l.video_id" in video_sql
    assert "COALESCE(v.is_visible,TRUE)=TRUE" in video_sql


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
    (("videos", "match_videos", "id"), ("photos", "match_photos", "id")),
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
            if kind == "videos":
                return pd.DataFrame(
                    [{"id": index + 1, "video_title": f"影片 {index + 1}"} for index in range(20)]
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


def test_team_history_preserves_multiple_exact_videos_from_the_same_match():
    calls = []

    class ResourceDb:
        def query(self, sql, params):
            calls.append(sql)
            if "history_event_videos" in sql:
                return pd.DataFrame(
                    [
                        {
                            "owner_id": 4,
                            "id": 73,
                            "match_id": "M1",
                            "video_title": "第一條片",
                            "match_display": "同一場比賽",
                            "topic_text": "測試辯題",
                            "pro_team": "甲隊",
                            "con_team": "乙隊",
                        },
                        {
                            "owner_id": 4,
                            "id": 74,
                            "match_id": "M1",
                            "video_title": "第二條片",
                            "match_display": "同一場比賽",
                            "topic_text": "測試辯題",
                            "pro_team": "甲隊",
                            "con_team": "乙隊",
                        },
                    ]
                )
            return pd.DataFrame()

    links = community_api._history_resource_links(ResourceDb(), [4])

    assert [row["id"] for row in links[4]["videos"]] == [73, 74]
    assert links[4]["photos"] == []
    video_sql = calls[0]
    assert "JOIN match_videos v ON v.id=l.video_id" in video_sql
    assert "COALESCE(v.is_visible,TRUE)=TRUE" in video_sql
    assert "LIMIT 1" not in video_sql


def test_team_history_replaces_every_selected_exact_video_link():
    calls = []

    class Connection:
        def execute(self, statement, params):
            calls.append((str(statement), dict(params)))

    community_api._replace_history_links(Connection(), 4, [73, 74], [5])

    assert "DELETE FROM history_event_videos" in calls[0][0]
    assert "DELETE FROM history_event_photos" in calls[1][0]
    video_inserts = [
        params for sql, params in calls if "INSERT INTO history_event_videos" in sql
    ]
    assert video_inserts == [
        {"owner": 4, "video": 73},
        {"owner": 4, "video": 74},
    ]
    assert any(
        "INSERT INTO history_event_photos" in sql
        and params == {"owner": 4, "photo": 5}
        for sql, params in calls
    )


def test_team_history_and_forum_frontends_keep_multiple_exact_video_ids():
    history = (ROOT / "frontend" / "team_history" / "index.html").read_text(
        encoding="utf-8"
    )
    forum = (ROOT / "frontend" / "ghost_forum" / "index.html").read_text(
        encoding="utf-8"
    )

    for source in (history, forum):
        assert "selectedVideos=newMap()" in source.replace(" ", "")
        assert "video_ids:[...selectedVideos.keys()].map(Number)" in source.replace(" ", "")
        assert "data-add-video" in source
        assert "data-remove-video" in source
    assert "data-add-match" not in history
    assert "match_ids:" not in history


def test_history_resource_picker_rejects_unknown_resource_kind(monkeypatch):
    monkeypatch.setattr(
        community_api, "_member_context", lambda _request, _page: ("member", object()),
    )
    with pytest.raises(community_api.HTTPException) as caught:
        community_api.resource_options(SimpleNamespace(), kind="everything")
    assert caught.value.status_code == 400


def test_forum_video_picker_searches_visible_video_inventory_and_titles(monkeypatch):
    class ResourceDb:
        def __init__(self):
            self.calls = []

        def query(self, sql, params):
            self.calls.append((sql, dict(params)))
            if "COUNT(*)" in sql:
                return pd.DataFrame([{"total": 1}])
            return pd.DataFrame(
                [
                    {
                        "id": 32,
                        "match_id": None,
                        "match_display": "2024 聯經複賽",
                        "video_title": "零工經濟的生態對本港勞工市場利大於弊（反）",
                        "topic_text": "零工經濟的生態對本港勞工市場利大於弊",
                        "pro_team": "甲隊",
                        "con_team": "乙隊",
                    }
                ]
            )

    db = ResourceDb()
    monkeypatch.setattr(community_api, "_ghost_context", lambda _request: ("senior", db))

    result = community_api.forum_resource_options(
        SimpleNamespace(), search="零工經濟", kind="videos", page=1,
    )

    assert result["total"] == 1
    assert result["items"][0]["id"] == 32
    count_sql, count_params = db.calls[0]
    item_sql, item_params = db.calls[1]
    assert "FROM match_videos" in count_sql
    assert "video_title" in count_sql
    assert "COALESCE(v.is_visible,TRUE)=TRUE" in count_sql
    assert count_params["search"] == "%零工經濟%"
    assert "FROM match_videos" in item_sql
    assert item_params["limit"] == 20
    assert item_params["offset"] == 0


def test_forum_thread_validation_uses_exact_video_and_history_event_ids():
    values = community_logic.validate_thread(
        {
            "title": "討論",
            "body": "內容",
            "video_ids": [73, 73, 32],
            "photo_ids": [5],
            "history_event_ids": [9, 9, 10],
        }
    )

    assert values["video_ids"] == [73, 32]
    assert values["photo_ids"] == [5]
    assert values["history_event_ids"] == [9, 10]
    assert "match_ids" not in values

    with pytest.raises(ValueError, match="最多連結 20 個隊史事件"):
        community_logic.validate_thread(
            {
                "title": "太多事件",
                "body": "內容",
                "video_ids": [],
                "photo_ids": [],
                "history_event_ids": list(range(1, 22)),
            }
        )


def test_forum_event_picker_returns_live_history_event_cards(monkeypatch):
    class ResourceDb:
        def query(self, sql, params):
            if "COUNT(*)" in sql:
                return pd.DataFrame([{"total": 1}])
            return pd.DataFrame(
                [{
                    "id": 9,
                    "academic_year_start": 2025,
                    "event_date": "2026-07-17",
                    "title": "校際賽奪冠",
                    "description": "事件內容",
                }]
            )

    monkeypatch.setattr(
        community_api, "_ghost_context", lambda _request: ("senior", ResourceDb()),
    )

    result = community_api.forum_resource_options(
        SimpleNamespace(), kind="history_events", event_id=9,
    )

    assert result["items"] == [{
        "id": 9,
        "academic_year_start": 2025,
        "event_date": "2026-07-17",
        "title": "校際賽奪冠",
        "description": "事件內容",
        "academic_year_label": "2025/26",
    }]


def test_history_event_deep_link_resolves_the_target_page(monkeypatch):
    captured = {}

    class HistoryDb:
        def query(self, sql, params=None):
            if "ROW_NUMBER()" in sql:
                return pd.DataFrame([{"position": 21}])
            if "COUNT(*) total" in sql:
                return pd.DataFrame([{"total": 41}])
            captured.update(params or {})
            return pd.DataFrame(
                [{
                    "id": 9,
                    "academic_year_start": 2025,
                    "event_date": "2026-07-17",
                    "title": "校際賽奪冠",
                    "description": "事件內容",
                }]
            )

    monkeypatch.setattr(
        community_api,
        "_member_context",
        lambda _request, _page: ("member", HistoryDb()),
    )
    monkeypatch.setattr(
        community_api,
        "_history_resource_links",
        lambda _db, owner_ids: {
            int(owner): {"videos": [], "photos": []} for owner in owner_ids
        },
    )

    result = community_api.history_events(
        SimpleNamespace(), order="newest", event_id=9,
    )

    assert result["page"] == 2
    assert result["target_event_id"] == 9
    assert captured["offset"] == 20


def test_team_history_discussion_link_preselects_event_without_prefilling_title():
    history = (ROOT / "frontend" / "team_history" / "index.html").read_text(
        encoding="utf-8"
    )
    forum = (ROOT / "frontend" / "ghost_forum" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "↗ 於老鬼專區出post討論" in history
    assert "/ghost-forum?compose=1&history_event=" in history
    assert 'data-resource-kind="history_events"' in forum
    assert "showEventDiscussionComposer(historyEventId)" in forum
    assert "selectedHistoryEvents.set(String(row.id),row)" in forum
    assert "video_ids:[...selectedVideos.keys()].map(Number)" in forum
    assert "history_event_ids:[...selectedHistoryEvents.keys()].map(Number)" in forum
    event_helper = forum.split(
        "async function showEventDiscussionComposer", 1
    )[1].split("function startThreadEdit", 1)[0]
    assert '$("threadTitle").value' not in event_helper


def test_forum_video_link_migration_selects_first_visible_video_and_retires_match_links():
    up = (
        ROOT / "migrations" / "20260717_0006_ghost_forum_video_event_links.up.sql"
    ).read_text(encoding="utf-8")
    down = (
        ROOT / "migrations" / "20260717_0006_ghost_forum_video_event_links.down.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE public.ghost_forum_thread_videos" in up
    assert "JOIN LATERAL" in up
    assert "COALESCE(video.is_visible, TRUE)=TRUE" in up
    assert "video.display_order ASC NULLS LAST" in up
    assert "video.created_at DESC" in up
    assert "video.id DESC" in up
    assert "LIMIT 1" in up
    assert "DROP TABLE public.ghost_forum_thread_matches" in up
    assert "CREATE TABLE public.ghost_forum_thread_history_events" in up
    assert "WHERE video.match_id IS NULL" in down
    assert "RAISE EXCEPTION" in down


def test_team_history_video_link_migration_is_exact_private_and_fail_closed():
    up = (
        ROOT / "migrations" / "20260723_0002_team_history_video_links.up.sql"
    ).read_text(encoding="utf-8")
    down = (
        ROOT / "migrations" / "20260723_0002_team_history_video_links.down.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE public.history_event_videos" in up
    assert "JOIN LATERAL" in up
    assert "COALESCE(video.is_visible, TRUE)=TRUE" in up
    assert "LIMIT 1" in up
    assert "DROP TABLE public.history_event_matches" in up
    assert "REVOKE ALL PRIVILEGES ON TABLE public.history_event_videos FROM PUBLIC" in up
    assert "WHERE video.match_id IS NULL" in down
    assert "HAVING COUNT(*) > 1" in down
    assert "RAISE EXCEPTION" in down
