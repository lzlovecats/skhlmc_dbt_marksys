"""Offline contracts for replay best-debater markers and video rosters."""

from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from api.video_admin_api import (
    ChaptersBody as AdminChaptersBody,
    RosterBody,
    router as video_admin_router,
)
from core import media_logic
from core.db_migrations import discover_migrations
import schema


ROOT = Path(__file__).resolve().parents[1]


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))


class _TransactionDb:
    def __init__(self, *, existing_best=None, accounts=(), existing_roster=()):
        self.connection = _Connection()
        self.existing_best = existing_best
        self.accounts = list(accounts)
        self.existing_roster = list(existing_roster)

    def query(self, sql, params=None):
        if "is_best_debater=TRUE" in sql:
            existing = (
                [self.existing_best]
                if isinstance(self.existing_best, str)
                else list(self.existing_best or ())
            )
            rows = [{"chapter_label": role} for role in existing]
            return pd.DataFrame(rows, columns=["chapter_label"])
        if "SELECT 1 AS found" in sql:
            return pd.DataFrame([{"found": 1}])
        if "SELECT role_label, member_user_id FROM video_roster" in sql:
            return pd.DataFrame(
                self.existing_roster,
                columns=["role_label", "member_user_id"],
            )
        if "SELECT user_id FROM accounts" in sql:
            return pd.DataFrame({"user_id": self.accounts})
        raise AssertionError(sql)

    @contextmanager
    def transaction(self):
        yield self.connection


def _chapter(label, time_text="1:23", enabled=True, is_best_debater=None):
    chapter = {"chapter_label": label, "enabled": enabled, "time_text": time_text}
    if is_best_debater is not None:
        chapter["is_best_debater"] = is_best_debater
    return chapter


def test_individual_speech_contract_excludes_interactive_chapters():
    assert media_logic.INDIVIDUAL_SPEECH_LABELS == [
        "正主", "反主", "正一", "反一", "正二",
        "反二", "正三", "反三", "反結", "正結",
    ]
    assert not set(("攻辯", "台下", "交互", "自由辯論")) & set(
        media_logic.INDIVIDUAL_SPEECH_LABELS
    )


def test_save_chapters_marks_multiple_enabled_individual_roles():
    db = _TransactionDb()
    result = media_logic.save_chapters(
        7,
        [
            _chapter("正主", is_best_debater=True),
            _chapter("反一", is_best_debater=True),
            _chapter("自由辯論", "9:00", is_best_debater=False),
        ],
        db=db,
    )
    assert result["ok"] is True
    assert result["best_debater_roles"] == ["正主", "反一"]
    insert = next(params for sql, params in db.connection.calls if "INSERT INTO video_chapters" in sql)
    assert [row["chapter_label"] for row in insert if row["is_best_debater"]] == ["正主", "反一"]


def test_explicit_best_role_must_be_an_enabled_individual_chapter():
    db = _TransactionDb()
    invalid_section = media_logic.save_chapters(
        7, [_chapter("自由辯論")], best_debater_role="自由辯論", db=db,
    )
    disabled = media_logic.save_chapters(
        7, [_chapter("正主", enabled=False)], best_debater_role="正主", db=db,
    )
    assert invalid_section == {"ok": False, "message": "最佳辯論員必須選擇個人發言辯位。"}
    assert disabled == {"ok": False, "message": "最佳辯論員必須同時啟用該辯位的章節時間。"}
    assert db.connection.calls == []


def test_chapter_best_markers_must_be_enabled_individual_chapters():
    db = _TransactionDb()
    invalid_section = media_logic.save_chapters(
        7, [_chapter("自由辯論", is_best_debater=True)], db=db,
    )
    disabled = media_logic.save_chapters(
        7, [_chapter("正主", enabled=False, is_best_debater=True)], db=db,
    )
    assert invalid_section == {"ok": False, "message": "最佳辯論員必須選擇個人發言辯位。"}
    assert disabled == {"ok": False, "message": "最佳辯論員必須同時啟用該辯位的章節時間。"}
    assert db.connection.calls == []


def test_omitted_admin_best_preserves_it_or_clears_when_chapter_is_disabled():
    body = AdminChaptersBody(chapters=[])
    assert "best_debater_role" not in body.model_fields_set

    preserved_db = _TransactionDb(existing_best="反二")
    preserved = media_logic.save_chapters(
        8, [_chapter("反二")], db=preserved_db,
    )
    preserved_insert = next(
        params for sql, params in preserved_db.connection.calls
        if "INSERT INTO video_chapters" in sql
    )
    assert preserved["best_debater_role"] == "反二"
    assert preserved_insert[0]["is_best_debater"] is True

    cleared_db = _TransactionDb(existing_best="反二")
    cleared = media_logic.save_chapters(8, [_chapter("正主")], db=cleared_db)
    assert cleared["ok"] is True
    assert cleared["best_debater_role"] is None

    explicit_clear_db = _TransactionDb(existing_best="反二")
    explicit_clear = media_logic.save_chapters(
        8, [_chapter("反二")], best_debater_role=None, db=explicit_clear_db,
    )
    explicit_insert = next(
        params for sql, params in explicit_clear_db.connection.calls
        if "INSERT INTO video_chapters" in sql
    )
    assert explicit_clear["best_debater_role"] is None
    assert not any(row["is_best_debater"] for row in explicit_insert)


def test_legacy_chapter_save_preserves_all_existing_best_markers():
    db = _TransactionDb(existing_best=("正主", "反二"))
    result = media_logic.save_chapters(
        8, [_chapter("正主"), _chapter("反二")], db=db,
    )
    insert = next(
        params for sql, params in db.connection.calls
        if "INSERT INTO video_chapters" in sql
    )
    assert result["best_debater_roles"] == ["正主", "反二"]
    assert [
        row["chapter_label"] for row in insert if row["is_best_debater"]
    ] == ["正主", "反二"]


def test_chapter_api_distinguishes_omitted_markers_from_explicit_false():
    omitted = AdminChaptersBody(chapters=[_chapter("正主")])
    explicit = AdminChaptersBody(
        chapters=[_chapter("正主", is_best_debater=False)]
    )
    assert "is_best_debater" not in omitted.chapters[0].model_fields_set
    assert explicit.chapters[0].is_best_debater is False


def test_admin_roster_api_uses_post_assignments_and_nullable_user_ids():
    body = RosterBody(assignments=[{"role_label": "正主", "user_id": None}])
    assert body.model_dump() == {
        "assignments": [{"role_label": "正主", "user_id": None}],
    }
    route = next(
        item for item in video_admin_router.routes
        if item.path == "/api/video-admin/videos/{video_id}/roster"
    )
    assert route.methods == {"POST"}


def test_video_roster_replacement_accepts_only_normal_member_accounts():
    db = _TransactionDb(accounts=("alice", "bob"))
    result = media_logic.save_video_roster(
        9,
        [
            {"role_label": "正主", "user_id": "alice"},
            {"role_label": "反主", "user_id": "bob"},
            {"role_label": "正三", "user_id": None},
        ],
        db=db,
    )
    assert result["ok"] is True
    insert = next(params for sql, params in db.connection.calls if "INSERT INTO video_roster" in sql)
    assert [(row["role_label"], row["member_user_id"]) for row in insert] == [
        ("正主", "alice"), ("反主", "bob"),
    ]

    rejected_db = _TransactionDb(accounts=("alice",))
    rejected = media_logic.save_video_roster(
        9, [{"role_label": "正主", "user_id": "admin"}], db=rejected_db,
    )
    assert rejected["ok"] is False
    assert rejected_db.connection.calls == []


def test_disabled_member_can_only_preserve_the_same_historical_role_assignment():
    existing = [{"role_label": "反主", "member_user_id": "retired_member"}]
    preserved_db = _TransactionDb(accounts=("alice",), existing_roster=existing)
    preserved = media_logic.save_video_roster(
        9,
        [
            {"role_label": "正主", "user_id": "alice"},
            {"role_label": "反主", "user_id": "retired_member"},
        ],
        db=preserved_db,
    )
    assert preserved["ok"] is True

    moved_db = _TransactionDb(accounts=("alice",), existing_roster=existing)
    moved = media_logic.save_video_roster(
        9,
        [{"role_label": "正一", "user_id": "retired_member"}],
        db=moved_db,
    )
    assert moved["ok"] is False
    assert moved_db.connection.calls == []

    system_db = _TransactionDb(
        accounts=("alice",),
        existing_roster=[{"role_label": "反主", "member_user_id": "admin"}],
    )
    system = media_logic.save_video_roster(
        9, [{"role_label": "反主", "user_id": "admin"}], db=system_db,
    )
    assert system["ok"] is False


def test_replay_record_exposes_frontend_participation_arrays_and_search_text():
    record = media_logic._replay_record({
        "id": 1,
        "participated_by_me": True,
        "my_roles_text": "正主,正結",
        "roster_user_ids": ["alice", "bob"],
        "roster_search_text": "alice bob",
    })
    assert record["participated_by_me"] is True
    assert record["my_roles"] == ["正主", "正結"]
    assert record["roster_user_ids"] == ["alice", "bob"]
    assert record["roster_search_text"] == "alice bob"


def test_mine_only_filter_is_applied_before_the_replay_limit():
    class CaptureDb:
        def query(self, sql, params):
            self.sql = sql
            self.params = params
            return pd.DataFrame()

    db = CaptureDb()
    media_logic._replay_rows("alice", db, mine_only=True)
    assert "EXISTS" in db.sql and "mine.member_user_id=:user_id" in db.sql
    assert db.sql.index("EXISTS") < db.sql.index("LIMIT :limit")
    assert db.params["mine_only"] is True


def test_bootstrap_and_versioned_migration_allow_multiple_best_and_enforce_roster_roles():
    assert "is_best_debater BOOLEAN" in schema.CREATE_VIDEO_CHAPTERS
    assert "video_chapters_best_role_check" in schema.CREATE_VIDEO_CHAPTERS
    assert "PRIMARY KEY (video_id, role_label)" in schema.CREATE_VIDEO_ROSTER
    assert "'正三'" in schema.CREATE_VIDEO_ROSTER
    assert "FROM PUBLIC" in schema.LOCK_VIDEO_ROSTER_PRIVILEGES
    assert "'anon', 'authenticated'" in schema.LOCK_VIDEO_ROSTER_PRIVILEGES
    roster_index = schema.ALL_SCHEMAS.index(schema.CREATE_VIDEO_ROSTER)
    assert schema.ALL_SCHEMAS[roster_index + 1] == schema.LOCK_VIDEO_ROSTER_PRIVILEGES
    migrations = discover_migrations(ROOT / "migrations")
    roster_migration = next(item for item in migrations if item.version == "20260714_0006")
    multi_best_migration = next(item for item in migrations if item.version == "20260715_0002")
    assert "video_chapters_best_role_check" in roster_migration.up_sql
    assert "REVOKE ALL PRIVILEGES ON TABLE video_roster" in roster_migration.up_sql
    assert "DROP INDEX IF EXISTS idx_video_chapters_one_best_debater" in multi_best_migration.up_sql
    assert "idx_video_chapters_one_best_debater" not in schema.CREATE_INDICES
