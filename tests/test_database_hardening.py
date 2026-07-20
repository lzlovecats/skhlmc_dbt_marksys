from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from core import vote_logic
from schema import (
    CREATE_ACCOUNTS,
    CREATE_AI_COACH_LIVE_BRIEFS,
    CREATE_COMMITTEE_VOTE_ACTIVITY_VIEW,
    CREATE_R2_UPLOAD_INTENTS,
    CREATE_SCORE_DRAFTS,
    CREATE_SCORES,
    CREATE_TOPIC_REMOVAL_VOTES,
    LOCK_APPLICATION_PRIVILEGES,
)
from tools.compare_db_catalogs import compare_snapshots
from tools.cleanup_r2_orphans import _issued_intents


ROOT = Path(__file__).resolve().parents[1]


class _MotionSession:
    def __init__(self, owner):
        self.owner = owner

    def execute_count(self, sql, params=None):
        self.owner.session_calls.append((sql, params or {}))
        topic = str((params or {}).get("topic_text") or "")
        if "UPDATE topic_removal_votes" in sql:
            if self.owner.state["removals"].get(topic) == "pending":
                self.owner.state["removals"][topic] = "passed"
                return 1
            return 0
        if "UPDATE topic_votes" in sql:
            if self.owner.fail_on_topic_update:
                raise RuntimeError("topic update failed")
            if self.owner.state["motions"].get(topic) == "pending":
                self.owner.state["motions"][topic] = "passed"
                return 1
            return 0
        return 0

    def query(self, _sql, _params=None):
        raise AssertionError("motion transitions do not issue row queries")

    def execute(self, sql, params=None):
        self.owner.session_calls.append((sql, params or {}))
        topic = str((params or {}).get("topic_text") or "")
        if "pg_advisory_xact_lock" in sql:
            return None
        if "INSERT INTO topics" in sql:
            self.owner.state["topics"].add(topic)
            return None
        if "DELETE FROM topics" in sql:
            if self.owner.fail_on_topic_delete:
                raise RuntimeError("topic delete failed")
            self.owner.state["topics"].discard(topic)
            return None
        raise AssertionError(sql)


class _AtomicMotionDb:
    def __init__(self):
        self.state = {
            "topics": {"motion"},
            "motions": {"new motion": "pending"},
            "removals": {"motion": "pending"},
            "removal_ballots": {("motion", "alice"), ("motion", "bob")},
        }
        self.direct_calls = []
        self.session_calls = []
        self.transaction_count = 0
        self.fail_on_topic_delete = False
        self.fail_on_topic_update = False

    def execute(self, sql, params=None):
        self.direct_calls.append((sql, params or {}))

    def execute_count(self, sql, params=None):
        self.direct_calls.append((sql, params or {}))
        return 1

    @contextmanager
    def transaction(self):
        self.transaction_count += 1
        before = deepcopy(self.state)
        try:
            yield _MotionSession(self)
        except Exception:
            self.state = before
            raise


def test_passed_removal_is_atomic_and_keeps_motion_ballots():
    db = _AtomicMotionDb()

    changed = vote_logic.apply_depose_pass("motion", db=db)

    assert changed == 1
    assert db.transaction_count == 1
    assert db.direct_calls == []
    assert db.state["removals"]["motion"] == "passed"
    assert "motion" not in db.state["topics"]
    assert db.state["removal_ballots"] == {
        ("motion", "alice"), ("motion", "bob"),
    }


def test_passed_removal_rolls_back_status_when_topic_delete_fails():
    db = _AtomicMotionDb()
    db.fail_on_topic_delete = True

    with pytest.raises(RuntimeError, match="topic delete failed"):
        vote_logic.apply_depose_pass("motion", db=db)

    assert db.state["removals"]["motion"] == "pending"
    assert "motion" in db.state["topics"]


def test_passed_topic_rolls_back_bank_insert_when_status_update_fails():
    db = _AtomicMotionDb()
    db.fail_on_topic_update = True

    with pytest.raises(RuntimeError, match="topic update failed"):
        vote_logic.apply_topic_pass("new motion", db=db)

    assert "new motion" not in db.state["topics"]
    assert db.state["motions"]["new motion"] == "pending"


def _migration(version, direction="up"):
    path = next((ROOT / "migrations").glob(f"{version}_*.{direction}.sql"))
    return path.read_text(encoding="utf-8")


def test_privilege_migration_closes_browser_roles_and_provisions_runtime_role():
    up = _migration("20260720_0004")
    down = _migration("20260720_0004", "down")

    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC" in up
    assert "WHERE rolname IN ('anon', 'authenticated')" in up
    assert "CREATE ROLE app_backend" in up
    assert "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE" in up
    assert (
        "ALTER ROLE app_backend\n"
        "    NOLOGIN NOCREATEDB NOCREATEROLE NOINHERIT;"
    ) in up
    assert "REVOKE ALL PRIVILEGES ON TABLE public.schema_migrations FROM app_backend" in up
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public" in up
    assert "Break-glass rollback" in down
    assert "public.app_config" in down


def test_access_audit_scopes_default_privileges_to_public_schema():
    audit_source = (ROOT / "tools" / "audit_db_access.py").read_text(
        encoding="utf-8"
    )

    assert audit_source.count("defaults.defaclnamespace=(") == 2
    assert audit_source.count("nspname='public'") >= 2


def test_constraint_repair_removes_history_cascade_and_exact_duplicates():
    up = _migration("20260720_0005")

    assert "DROP CONSTRAINT IF EXISTS fk_topic_removal_votes_topic" in up
    assert "DROP CONSTRAINT IF EXISTS topic_removal_votes_proposer_user_fkey" in up
    assert "DROP CONSTRAINT IF EXISTS score_drafts_match_id_judge_name_side_key" in up
    assert "DROP INDEX IF EXISTS public.idx_competition_prep_manuscripts_project" in up
    assert "CREATE UNIQUE INDEX idx_match_photos_r2_key" in up


def test_activity_view_migration_is_set_based_and_keeps_bootstrap_in_sync():
    up = _migration("20260720_0006")

    for sql in (up, CREATE_COMMITTEE_VOTE_ACTIVITY_VIEW):
        assert "eligible_activity AS" in sql
        assert "ROW_NUMBER() OVER" in sql
        assert "event_recency" in sql
        assert "SELECT COUNT(*) FROM all_events" not in sql


def test_verified_contracts_are_in_migration_and_fresh_bootstrap():
    up = _migration("20260720_0007")

    assert "ALTER COLUMN object_keys TYPE JSONB" in up
    assert "ALTER COLUMN expires_at TYPE TIMESTAMPTZ" in up
    assert "topic_votes_pending_deadline_check" in up
    assert "topic_removal_votes_reasons_array_check" in up
    assert "r2_upload_intents_completion_check" in up
    assert "llm_training_submissions_ai_review_status_check" in up

    assert "object_keys     JSONB NOT NULL" in CREATE_R2_UPLOAD_INTENTS
    assert "expires_at TIMESTAMPTZ NOT NULL" in CREATE_AI_COACH_LIVE_BRIEFS
    assert "PRIMARY KEY (match_id, judge_name)" in CREATE_SCORES
    assert "PRIMARY KEY (match_id, judge_name, side)" in CREATE_SCORE_DRAFTS
    assert "topic_removal_votes_reasons_array_check" in CREATE_TOPIC_REMOVAL_VOTES
    assert "accounts_status_check" in CREATE_ACCOUNTS
    assert "CREATE ROLE app_backend" in LOCK_APPLICATION_PRIVILEGES


def test_semantic_catalog_comparison_ignores_order_but_detects_type_drift():
    snapshot = {
        "schema": {
            "tables": [{
                "table_name": "example", "partitioned": False,
                "rls_enabled": False, "rls_forced": False,
            }],
            "columns": [{
                "table_name": "example", "column_name": "value",
                "ordinal_position": 1, "data_type": "text",
                "nullable": True, "default_expression": None,
                "identity_kind": None, "generated_kind": None,
            }],
            "constraints": [],
            "indexes": [],
            "views": [],
            "sequences": [],
        },
    }
    reordered = deepcopy(snapshot)
    reordered["schema"]["columns"][0]["ordinal_position"] = 9

    assert compare_snapshots(snapshot, reordered)["semantic_catalog_match"]

    reordered["schema"]["columns"][0]["data_type"] = "jsonb"
    report = compare_snapshots(snapshot, reordered)
    assert report["semantic_catalog_match"] is False
    assert report["sections"]["columns"]["difference_count"] == 1


def test_orphan_audit_accepts_jsonb_lists_and_legacy_json_text():
    class _Db:
        def query(self, _sql):
            return pd.DataFrame([
                {
                    "intent_id": "jsonb", "object_keys": ["pending/a"],
                    "created_at": "2026-07-20 00:00:00",
                },
                {
                    "intent_id": "text", "object_keys": '["pending/b"]',
                    "created_at": "2026-07-20 00:00:00",
                },
            ])

    intents = _issued_intents(_Db())

    assert intents["jsonb"]["keys"] == ["pending/a"]
    assert intents["text"]["keys"] == ["pending/b"]
