from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from api import community_api
from core import push


ROOT = Path(__file__).resolve().parents[1]


def test_forum_frontend_preserves_navigation_drafts_and_inline_editing():
    source = (ROOT / "frontend" / "ghost_forum" / "index.html").read_text(
        encoding="utf-8"
    )

    for expected in (
        'id="threadPager"',
        "history.pushState",
        'addEventListener("popstate"',
        "sessionStorage",
        "beforeunload",
        'data-post-edit-form="${row.id}"',
        'id="post-${row.id}"',
        'aria-pressed="${row.viewer_liked?"true":"false"}"',
        "matched_post_id",
        "post_id",
    ):
        assert expected in source
    assert 'prompt("編輯留言"' not in source


def test_forum_frontend_exposes_unread_mute_retry_and_live_status_contracts():
    source = (ROOT / "frontend" / "ghost_forum" / "index.html").read_text(
        encoding="utf-8"
    )

    for expected in (
        'id="retryNotifications"',
        'data-thread-mute',
        "unread_count",
        'aria-live="polite"',
        'role="status"',
        '"/forum/session"',
        "/read",
        "/state",
    ):
        assert expected in source


def test_forum_api_has_search_hits_post_targeting_and_durable_notification_contracts():
    source = (ROOT / "api" / "community_api.py").read_text(encoding="utf-8")

    for expected in (
        "TABLE_GHOST_FORUM_NOTIFICATIONS",
        "TABLE_GHOST_FORUM_THREAD_USER_STATE",
        "TABLE_GHOST_FORUM_USER_PROFILES",
        "matched_post_id",
        "matched_excerpt",
        "def _queue_forum_notification",
        "def _dispatch_forum_notification",
        '@router.post("/forum/session")',
        '@router.post("/forum/threads/{thread_id}/read")',
        '@router.patch("/forum/threads/{thread_id}/state")',
        '@router.post("/forum/notifications/{notification_id}/retry")',
        "forum_thread_id=thread_id",
        'url=f"/ghost-forum?thread={int(thread_id)}&post={int(post_id)}"',
    ):
        assert expected in source


def test_forum_migration_and_bootstrap_provision_read_state_and_outbox():
    up_path = ROOT / "migrations" / "20260717_0005_ghost_forum_engagement.up.sql"
    down_path = ROOT / "migrations" / "20260717_0005_ghost_forum_engagement.down.sql"
    assert up_path.exists()
    assert down_path.exists()

    up = up_path.read_text(encoding="utf-8")
    down = down_path.read_text(encoding="utf-8")
    schema = (ROOT / "schema.py").read_text(encoding="utf-8")

    for table in (
        "ghost_forum_user_profiles",
        "ghost_forum_thread_user_state",
        "ghost_forum_notifications",
    ):
        assert f"CREATE TABLE public.{table}" in up
        assert f"DROP TABLE public.{table}" in down
        assert f'TABLE_{table.upper()} = "{table}"' in schema
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in up
    assert "gin_trgm_ops" in up
    assert "REVOKE ALL PRIVILEGES" in up


def test_push_sender_can_exclude_users_who_muted_a_forum_thread():
    source = (ROOT / "core" / "push.py").read_text(encoding="utf-8")

    assert "forum_thread_id=None" in source
    assert "TABLE_GHOST_FORUM_THREAD_USER_STATE" in source
    assert "state.muted=TRUE" in source


def test_push_sender_parameterizes_muted_thread_filter():
    class CaptureDb:
        def __init__(self):
            self.sql = ""
            self.params = {}

        def query(self, sql, params):
            self.sql = sql
            self.params = params
            return pd.DataFrame()

    db = CaptureDb()
    assert push.notify_committee(
        db,
        {"private_key": "test"},
        "老鬼專區有新回覆",
        "內容",
        senior_only=True,
        forum_thread_id=73,
    ) == 0
    assert "ghost_forum_thread_user_state" in db.sql
    assert "state.thread_id=:forum_thread_id" in db.sql
    assert db.params["forum_thread_id"] == 73


def test_forum_session_establishes_first_visit_baseline_and_lists_author_retries(monkeypatch):
    calls = []

    class Result:
        pass

    class Connection:
        def execute(self, statement, params=None):
            calls.append((str(statement), params or {}))
            return Result()

    class Transaction:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    class Db:
        def transaction(self):
            return Transaction()

        def query(self, sql, params):
            calls.append((sql, params))
            return pd.DataFrame([
                {
                    "notification_id": 5,
                    "event_kind": "reply",
                    "thread_id": 8,
                    "title": "舊隊史",
                }
            ])

    monkeypatch.setattr(
        community_api, "_ghost_context", lambda _request: ("graduate01", Db())
    )
    result = community_api.forum_session(SimpleNamespace())

    assert result["user_id"] == "graduate01"
    assert result["retryable_notifications"][0]["notification_id"] == 5
    assert "INSERT INTO ghost_forum_user_profiles" in calls[0][0]
    assert "ON CONFLICT (user_id) DO NOTHING" in calls[0][0]
    assert calls[-1][1]["user"] == "graduate01"
    assert "n.state IN ('pending','retryable')" in calls[-1][0]
    assert "stale" in calls[-1][1]


def test_durable_forum_dispatch_claims_then_settles_and_targets_exact_post(monkeypatch):
    transaction_sql = []
    settled = []

    class MappingResult:
        def mappings(self):
            return self

        def one_or_none(self):
            return {
                "id": 11,
                "event_kind": "reply",
                "post_id": 99,
                "author_user_id": "graduate01",
                "thread_id": 42,
                "title": "昔日聯中回憶",
            }

    class UpdateResult:
        pass

    class Connection:
        def execute(self, statement, params=None):
            sql = str(statement)
            transaction_sql.append((sql, params or {}))
            return MappingResult() if sql.lstrip().startswith("SELECT n.id") else UpdateResult()

    class Transaction:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    class Db:
        def transaction(self):
            return Transaction()

        def execute(self, sql, params):
            settled.append((sql, params))

    delivered = {}

    def fake_fire(db, author, thread, title, event, post_id=None):
        delivered.update(
            db=db, author=author, thread=thread, title=title,
            event=event, post_id=post_id,
        )
        return {"sent_count": 2}

    db = Db()
    monkeypatch.setattr(community_api, "_fire_forum_push", fake_fire)
    result = community_api._dispatch_forum_notification(
        db, 11, author_user_id="graduate01",
    )

    assert result == {"id": 11, "state": "sent", "sent_count": 2}
    assert delivered == {
        "db": db,
        "author": "graduate01",
        "thread": 42,
        "title": "昔日聯中回憶",
        "event": "reply",
        "post_id": 99,
    }
    assert "p.author_user_id=:author" in transaction_sql[0][0]
    assert "state='sending'" in transaction_sql[1][0]
    assert settled[0][1]["state"] == "sent"
    assert settled[0][1]["claim"]


def test_zero_delivery_keeps_forum_notification_author_retryable(monkeypatch):
    settled = []

    class MappingResult:
        def mappings(self):
            return self

        def one_or_none(self):
            return {
                "id": 11,
                "event_kind": "reply",
                "post_id": 99,
                "author_user_id": "graduate01",
                "thread_id": 42,
                "title": "昔日聯中回憶",
            }

    class Connection:
        def execute(self, statement, params=None):
            if str(statement).lstrip().startswith("SELECT n.id"):
                return MappingResult()
            return SimpleNamespace()

    class Transaction:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    class Db:
        def transaction(self):
            return Transaction()

        def execute(self, sql, params):
            settled.append((sql, params))

    monkeypatch.setattr(
        community_api, "_fire_forum_push", lambda *_args, **_kwargs: {"sent_count": 0}
    )
    result = community_api._dispatch_forum_notification(
        Db(), 11, author_user_id="graduate01"
    )

    assert result == {"id": 11, "state": "retryable", "sent_count": 0}
    assert settled[0][1]["state"] == "retryable"
    assert settled[0][1]["error"]


def test_forum_thread_post_target_selects_its_page_and_qualifies_state_columns(monkeypatch):
    captured = {}

    class Db:
        def query(self, sql, params):
            if "FROM ghost_forum_threads thread" in sql:
                captured["thread_sql"] = sql
                return pd.DataFrame([
                    {
                        "id": 7,
                        "title": "尋找舊留言",
                        "author_user_id": "graduate01",
                        "revision": 1,
                        "can_edit": True,
                        "muted": False,
                    }
                ])
            if "SELECT id,created_at FROM ghost_forum_posts" in sql:
                return pd.DataFrame([{"id": 88, "created_at": "2026-07-17 12:00:00"}])
            if "COUNT(*) total" in sql and "created_at<:created" in sql:
                return pd.DataFrame([{"total": 21}])
            if "COUNT(*) total" in sql:
                return pd.DataFrame([{"total": 41}])
            if "FROM ghost_forum_posts p" in sql:
                captured["posts_params"] = params
                return pd.DataFrame()
            raise AssertionError(sql)

    monkeypatch.setattr(
        community_api, "_ghost_context", lambda _request: ("graduate01", Db())
    )
    monkeypatch.setattr(
        community_api,
        "_resource_links",
        lambda _db, owner_ids, forum=False: {
            int(owner): {"matches": [], "photos": []} for owner in owner_ids
        },
    )

    result = community_api.forum_thread(7, SimpleNamespace(), post=88)

    assert result["posts"]["page"] == 2
    assert result["target_post_id"] == 88
    assert captured["posts_params"]["offset"] == 20
    assert "thread.updated_at" in captured["thread_sql"]


def test_forum_unread_query_keeps_every_pre_baseline_post_read(monkeypatch):
    captured = {}

    class Db:
        def query(self, sql, params):
            if "SELECT COUNT(*) total" in sql:
                return pd.DataFrame([{"total": 1}])
            captured["list_sql"] = sql
            return pd.DataFrame()

    monkeypatch.setattr(
        community_api, "_ghost_context", lambda _request: ("graduate01", Db())
    )

    community_api.forum_threads(SimpleNamespace())

    sql = captured["list_sql"]
    assert "unread.created_at>profile.unread_since" in sql
    assert "state.last_read_post_id IS NULL" in sql
    assert "OR unread.id>state.last_read_post_id" in sql
    assert "state.last_read_post_id IS NOT NULL" not in sql
