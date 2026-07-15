from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api import match_photos_api
from core import media_logic


ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CURRENT = object()


class _PhotoDb:
    def __init__(
        self, *, albums=(), photos=(), changed=1, current=_DEFAULT_CURRENT,
    ):
        self.albums = list(albums)
        self.photos = list(photos)
        self.changed = changed
        if current is _DEFAULT_CURRENT:
            current = (
                {
                    "match_video_id": self.albums[0]["match_video_id"],
                    "album_label": self.albums[0]["album_label"],
                }
                if self.albums
                else {
                    "match_video_id": None,
                    "album_label": media_logic.OTHER_ALBUM,
                }
            )
        self.current = current
        self.executed = []
        self.queries = []

    def query(self, sql, _params=None):
        self.queries.append(sql)
        if "SELECT album_label,match_video_id" in sql:
            rows = [] if self.current is None else [self.current]
            return pd.DataFrame(rows, columns=["album_label", "match_video_id"])
        if "SELECT DISTINCT ON (album_label)" in sql:
            return pd.DataFrame(
                self.albums,
                columns=["match_video_id", "album_label"],
            )
        if "SELECT COUNT(*) total" in sql:
            return pd.DataFrame([{"total": len(self.photos)}])
        if "SELECT id,album_label,match_video_id" in sql:
            return pd.DataFrame(self.photos)
        raise AssertionError(f"unexpected query: {sql}")

    def execute_count(self, sql, params):
        self.executed.append((sql, params))
        return self.changed


def _body(**overrides):
    values = {
        "album_label": "決賽",
        "match_video_id": 9,
        "photo_date": "2026-07-14",
        "photo_title": "最佳瞬間",
        "caption": "正方主辯發言",
    }
    values.update(overrides)
    return match_photos_api.PhotoMetadataBody(**values)


def test_update_photo_metadata_is_owner_scoped_and_storage_immutable():
    db = _PhotoDb(
        albums=[{"match_video_id": 9, "album_label": "決賽"}],
    )

    changed = media_logic.update_photo_metadata(
        "alice", 17, "決賽", 9, "2026-07-14", " 最佳瞬間 ",
        " 正方主辯發言 ", db=db,
    )

    assert changed is True
    sql, params = db.executed[0]
    set_clause, where_clause = sql.split("WHERE", 1)
    assert "uploaded_by=:uploaded_by" in where_clause
    assert "id=:id" in where_clause
    assert "match_video_id IS NOT DISTINCT FROM :old_match_video_id" in where_clause
    assert params == {
        "id": 17,
        "uploaded_by": "alice",
        "old_album_label": "決賽",
        "old_match_video_id": 9,
        "match_video_id": 9,
        "album_label": "決賽",
        "photo_date": date(2026, 7, 14),
        "photo_title": "最佳瞬間",
        "caption": "正方主辯發言",
    }
    for protected in (
        "r2_key", "thumbnail_r2_key", "file_name", "mime_type", "byte_size",
        "sha256", "width", "height", "uploaded_by", "created_at",
    ):
        assert protected not in set_clause


def test_update_photo_metadata_accepts_other_album_and_clears_optional_fields():
    db = _PhotoDb()

    changed = media_logic.update_photo_metadata(
        "alice", 18, media_logic.OTHER_ALBUM, None, "", "  ", " ", db=db,
    )

    assert changed is True
    params = db.executed[0][1]
    assert params["match_video_id"] is None
    assert params["photo_date"] is None
    assert params["photo_title"] is None
    assert params["caption"] is None


@pytest.mark.parametrize(
    ("current", "live_albums"),
    (
        (
            {"album_label": "已隱藏賽事", "match_video_id": 3},
            [{"album_label": "其他可見賽事", "match_video_id": 9}],
        ),
        (
            {"album_label": "舊名稱", "match_video_id": 4},
            [{"album_label": "新名稱", "match_video_id": 4}],
        ),
        (
            {"album_label": "同名賽事", "match_video_id": 5},
            [{"album_label": "同名賽事", "match_video_id": 9}],
        ),
        (
            {"album_label": "已刪除賽事", "match_video_id": None},
            [{"album_label": "其他可見賽事", "match_video_id": 9}],
        ),
    ),
)
def test_obsolete_current_album_pair_can_be_preserved(current, live_albums):
    db = _PhotoDb(albums=live_albums, current=current)

    changed = media_logic.update_photo_metadata(
        "alice", 19, current["album_label"], current["match_video_id"],
        "2026-07-14", "只改標題", "只改說明", db=db,
    )

    assert changed is True
    assert not [sql for sql in db.queries if "SELECT DISTINCT ON" in sql]
    params = db.executed[0][1]
    assert params["old_album_label"] == current["album_label"]
    assert params["old_match_video_id"] == current["match_video_id"]
    assert params["album_label"] == current["album_label"]
    assert params["match_video_id"] == current["match_video_id"]


def test_obsolete_current_album_pair_cannot_change_to_forged_pair():
    current = {"album_label": "已隱藏賽事", "match_video_id": 3}
    db = _PhotoDb(
        current=current,
        albums=[{"album_label": "可見賽事", "match_video_id": 9}],
    )

    with pytest.raises(ValueError, match="不相符"):
        media_logic.update_photo_metadata(
            "alice", 19, "偽造賽事", 10, "", "標題", "", db=db,
        )

    assert db.executed == []


def test_obsolete_current_album_pair_can_change_to_live_allowed_pair():
    db = _PhotoDb(
        current={"album_label": "已隱藏賽事", "match_video_id": 3},
        albums=[{"album_label": "可見賽事", "match_video_id": 9}],
    )

    changed = media_logic.update_photo_metadata(
        "alice", 19, "可見賽事", 9, "", "標題", "", db=db,
    )

    assert changed is True
    params = db.executed[0][1]
    assert params["old_album_label"] == "已隱藏賽事"
    assert params["old_match_video_id"] == 3
    assert params["album_label"] == "可見賽事"
    assert params["match_video_id"] == 9


@pytest.mark.parametrize("photo_date", ("2026-7-04", "2026-02-30", "14/07/2026"))
def test_update_photo_metadata_rejects_non_exact_or_invalid_dates(photo_date):
    db = _PhotoDb(
        albums=[{"match_video_id": 9, "album_label": "決賽"}],
    )

    with pytest.raises(ValueError, match="相片日期格式無效"):
        media_logic.update_photo_metadata(
            "alice", 17, "決賽", 9, photo_date, "標題", "", db=db,
        )

    assert db.executed == []


def test_update_photo_metadata_rejects_album_video_mismatch():
    db = _PhotoDb(
        albums=[{"match_video_id": 9, "album_label": "決賽"}],
    )

    with pytest.raises(ValueError, match="不相符"):
        media_logic.update_photo_metadata(
            "alice", 17, "決賽", 10, "", "標題", "", db=db,
        )

    assert db.executed == []


def test_photo_update_endpoint_hides_missing_and_other_owner_rows(monkeypatch):
    db = _PhotoDb(
        albums=[{"match_video_id": 9, "album_label": "決賽"}],
        current=None,
    )
    monkeypatch.setattr(match_photos_api, "_context", lambda _request: ("alice", db))

    with pytest.raises(HTTPException) as error:
        match_photos_api.update_photo(17, _body(), object())

    assert error.value.status_code == 404
    assert error.value.detail == "找不到可編輯的圖片。"
    assert db.executed == []


def test_photo_update_endpoint_returns_404_if_album_pair_changed_concurrently(
    monkeypatch,
):
    db = _PhotoDb(
        albums=[{"match_video_id": 9, "album_label": "決賽"}],
        changed=0,
    )
    monkeypatch.setattr(match_photos_api, "_context", lambda _request: ("alice", db))

    with pytest.raises(HTTPException) as error:
        match_photos_api.update_photo(17, _body(), object())

    assert error.value.status_code == 404
    sql, params = db.executed[0]
    assert "match_video_id IS NOT DISTINCT FROM :old_match_video_id" in sql
    assert params["old_album_label"] == "決賽"
    assert params["old_match_video_id"] == 9


def test_photo_update_endpoint_maps_metadata_validation_to_400(monkeypatch):
    db = _PhotoDb(
        albums=[{"match_video_id": 9, "album_label": "決賽"}],
    )
    monkeypatch.setattr(match_photos_api, "_context", lambda _request: ("alice", db))

    with pytest.raises(HTTPException) as error:
        match_photos_api.update_photo(
            17, _body(match_video_id=10), object(),
        )

    assert error.value.status_code == 400
    assert "不相符" in error.value.detail


def test_photo_list_exposes_raw_edit_values_and_owner_capability(monkeypatch):
    rows = [
        {
            "id": 1,
            "album_label": media_logic.OTHER_ALBUM,
            "match_video_id": None,
            "photo_date": None,
            "photo_title": None,
            "caption": None,
            "file_name": "one.webp",
            "mime_type": "image/webp",
            "uploaded_by": "alice",
            "created_at": datetime(2026, 7, 14, 12, 30),
        },
        {
            "id": 2,
            "album_label": "決賽",
            "match_video_id": 9,
            "photo_date": date(2026, 7, 13),
            "photo_title": "對手圖片",
            "caption": "不可編輯",
            "file_name": "two.webp",
            "mime_type": "image/webp",
            "uploaded_by": "bob",
            "created_at": datetime(2026, 7, 14, 12, 31),
        },
    ]
    db = _PhotoDb(photos=rows)
    monkeypatch.setattr(match_photos_api, "_context", lambda _request: ("alice", db))

    result = match_photos_api.photos(object())

    own, other = result["items"]
    assert own["can_edit"] is True
    assert own["match_video_id"] is None
    assert own["photo_date"] == ""
    assert own["photo_title"] == ""
    assert own["caption"] == ""
    assert other["can_edit"] is False
    assert other["match_video_id"] == 9
    assert other["photo_date"] == "2026-07-13"


def test_photo_metadata_body_enforces_bounds_before_database_access():
    with pytest.raises(ValidationError):
        _body(photo_title="標" * 301)
    with pytest.raises(ValidationError):
        _body(match_video_id=0)


def test_gallery_only_offers_server_authorized_metadata_editor():
    source = (ROOT / "frontend/shared/server-tables.js").read_text(encoding="utf-8")

    assert "photo.can_edit" in source
    assert 'method: "PATCH"' in source
    assert "match_video_id" in source
    assert "photo-edit-form" in source
    assert "原有：" in source
    assert "原有場次已不可選" not in source
    assert "await load(1)" in source
