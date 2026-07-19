from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import community_api
from core import sticker_catalog


ROOT = Path(__file__).resolve().parents[1]


def test_repository_sticker_catalog_is_bounded_to_webp_directory(monkeypatch, tmp_path):
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    (sticker_dir / "agree.webp").write_bytes(b"RIFF-test-WEBP")
    (sticker_dir / "開心-sticker.WEBP").write_bytes(b"RIFF-test-WEBP")
    (sticker_dir / "ignored.png").write_bytes(b"png")
    outside = tmp_path / "outside.webp"
    outside.write_bytes(b"RIFF-outside-WEBP")
    (sticker_dir / "escaped.webp").symlink_to(outside)
    (sticker_dir / (("x" * 201) + ".webp")).write_bytes(b"RIFF-long-WEBP")

    monkeypatch.setattr(sticker_catalog, "STICKER_DIRECTORY", sticker_dir)
    sticker_catalog.clear_catalog_cache()
    try:
        items = sticker_catalog.list_stickers()
        assert {item.sticker_id for item in items} == {"agree", "開心-sticker"}
        assert sticker_catalog.get_sticker("agree").path == (
            sticker_dir / "agree.webp"
        ).resolve()
        assert sticker_catalog.get_sticker("ignored") is None
        assert sticker_catalog.get_sticker("escaped") is None
    finally:
        sticker_catalog.clear_catalog_cache()


def test_forum_sticker_catalog_and_binary_routes_recheck_ghost_access(
    monkeypatch, tmp_path
):
    sticker_path = tmp_path / "開心 sticker.webp"
    sticker_path.write_bytes(b"RIFF-test-WEBP")
    item = sticker_catalog.Sticker("開心 sticker", "開心 sticker", sticker_path)
    contexts = []

    def context(request):
        contexts.append(request)
        return "graduate", object()

    monkeypatch.setattr(community_api, "_ghost_context", context)
    monkeypatch.setattr(community_api, "list_stickers", lambda: [item])
    monkeypatch.setattr(community_api, "get_sticker", lambda value: item if value == item.sticker_id else None)

    request = SimpleNamespace()
    catalog = community_api.forum_stickers(request)
    response = community_api.forum_sticker_image(item.sticker_id, request)

    assert len(contexts) == 2
    assert catalog["items"] == [
        {
            "id": "開心 sticker",
            "label": "開心 sticker",
            "url": (
                "/api/community/forum/stickers/"
                "%E9%96%8B%E5%BF%83%20sticker?v=" + community_api.APP_VERSION
            ),
        }
    ]
    assert response.media_type == "image/webp"
    assert response.headers["cache-control"].startswith("private, max-age=")
    assert response.headers["vary"] == "Cookie"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_forum_reply_requires_exactly_text_or_one_catalog_sticker(monkeypatch):
    sticker = SimpleNamespace(sticker_id="agree")
    monkeypatch.setattr(
        community_api,
        "get_sticker",
        lambda sticker_id: sticker if sticker_id == "agree" else None,
    )

    assert community_api._forum_reply_content(" 文字回覆 ", None) == (
        "文字回覆",
        None,
    )
    assert community_api._forum_reply_content("", "agree") == ("", "agree")

    for body, sticker_id in (("", None), ("文字", "agree"), ("", "missing")):
        with pytest.raises(HTTPException) as raised:
            community_api._forum_reply_content(body, sticker_id)
        assert raised.value.status_code == 400


def test_create_forum_sticker_post_writes_stable_id_and_empty_body(monkeypatch):
    calls = []

    class Result:
        def __init__(self, *, row=None, scalar_value=None):
            self.row = row
            self.scalar_value = scalar_value

        def mappings(self):
            return self

        def one_or_none(self):
            return self.row

        def one(self):
            return self.row

        def scalar(self):
            return self.scalar_value

    class Connection:
        def execute(self, statement, params=None):
            sql = str(statement)
            calls.append((sql, params or {}))
            if "SELECT title FROM ghost_forum_threads" in sql:
                return Result(row={"title": "舊事"})
            if "SELECT COUNT(*) FROM ghost_forum_posts" in sql:
                return Result(scalar_value=0)
            if "INSERT INTO ghost_forum_posts" in sql:
                return Result(row={"id": 73, "revision": 1})
            if "INSERT INTO ghost_forum_notifications" in sql:
                return Result(row={"id": 91})
            return Result()

    class Transaction:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    class Db:
        def transaction(self):
            return Transaction()

    db = Db()
    monkeypatch.setattr(
        community_api, "_ghost_context", lambda _request: ("graduate", db)
    )
    monkeypatch.setattr(
        community_api,
        "get_sticker",
        lambda sticker_id: SimpleNamespace(sticker_id=sticker_id),
    )
    monkeypatch.setattr(
        community_api,
        "_dispatch_forum_notification",
        lambda _db, notification_id: {
            "id": notification_id,
            "state": "sent",
            "sent_count": 1,
        },
    )

    result = community_api.create_forum_post(
        8,
        community_api.ForumPostBody(sticker_id="agree"),
        SimpleNamespace(),
    )

    insert_sql, insert_params = next(
        (sql, params) for sql, params in calls if "INSERT INTO ghost_forum_posts" in sql
    )
    assert "body,sticker_id,quoted_post_id" in insert_sql
    assert insert_params["body"] == ""
    assert insert_params["sticker"] == "agree"
    assert result["id"] == 73
    assert result["notification"]["state"] == "sent"


def test_discussion_sticker_migration_prepares_all_three_stores_and_bootstrap():
    up = (
        ROOT / "migrations" / "20260717_0007_discussion_sticker_columns.up.sql"
    ).read_text(encoding="utf-8")
    down = (
        ROOT / "migrations" / "20260717_0007_discussion_sticker_columns.down.sql"
    ).read_text(encoding="utf-8")
    schema = (ROOT / "schema.py").read_text(encoding="utf-8")

    for table in ("ghost_forum_posts", "motion_comments", "video_comments"):
        assert f"ALTER TABLE public.{table}" in up
        assert f"ALTER TABLE public.{table}" in down
    assert up.count("ADD COLUMN sticker_id TEXT") == 3
    assert down.count("DROP COLUMN sticker_id") == 3
    assert schema.count("sticker_id") >= 6
    assert "body = ''" in up


def test_ghost_forum_sticker_ui_requires_preview_confirmation_and_degrades_safely():
    source = (ROOT / "frontend" / "ghost_forum" / "index.html").read_text(
        encoding="utf-8"
    )
    api_source = (ROOT / "api" / "community_api.py").read_text(encoding="utf-8")

    for expected in (
        'id="stickerToggle"',
        'id="stickerPreview"',
        'id="sendSticker"',
        "先選擇一張 Sticker",
        "await loadStickers()",
        "sticker_id:sticker.id",
        "Sticker 暫時不可用",
        "quoted_sticker_id",
    ):
        assert expected in source
    assert '$("sendSticker").addEventListener("click",sendSelectedSticker)' in source
    assert 'data-sticker-id' in source
    assert "AND sticker_id IS NULL" in api_source
    assert "SET body='',sticker_id=NULL,deleted_at" in api_source
