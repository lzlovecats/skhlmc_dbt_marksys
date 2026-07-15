"""Focused tests for R2 ownership and system-wide storage reservations."""

import json
from contextlib import contextmanager

from core import r2_storage


class _ScalarResult:
    def __init__(self, value=0):
        self.value = value

    def scalar(self):
        return self.value


class _Session:
    def __init__(self, current_declared=0):
        self.current_declared = current_declared
        self.statements = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.statements.append((sql, params or {}))
        if "SELECT COALESCE(SUM(declared_bytes),0)" in sql:
            return _ScalarResult(self.current_declared)
        return _ScalarResult()


class _Db:
    def __init__(self, current_declared=0):
        self.session = _Session(current_declared)

    @contextmanager
    def transaction(self):
        yield self.session


def test_connection_ready_performs_only_one_minimal_read(monkeypatch):
    calls = []

    class Client:
        def list_objects_v2(self, **kwargs):
            calls.append(kwargs)
            return {"Contents": []}

    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(r2_storage, "settings", lambda: {"bucket": "private-media"})
    monkeypatch.setattr(r2_storage, "client", lambda: Client())
    assert r2_storage.connection_ready() is True
    assert calls == [{"Bucket": "private-media", "MaxKeys": 1}]


def test_connection_ready_fails_closed_without_leaking_sdk_errors(monkeypatch):
    monkeypatch.setattr(r2_storage, "configured", lambda: False)
    monkeypatch.setattr(
        r2_storage, "client",
        lambda: (_ for _ in ()).throw(AssertionError("client must not run")),
    )
    assert r2_storage.connection_ready() is False

    class Client:
        def list_objects_v2(self, **_kwargs):
            raise RuntimeError("credential details")

    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(r2_storage, "settings", lambda: {"bucket": "private-media"})
    monkeypatch.setattr(r2_storage, "client", lambda: Client())
    assert r2_storage.connection_ready() is False


def test_reservation_has_no_member_or_calendar_quota_and_is_owner_bound(monkeypatch):
    db = _Db(current_declared=100)
    monkeypatch.setattr(
        r2_storage, "get_configs_from_connection",
        lambda *_args: {"r2_storage_usage_snapshot": {
            "bytes": 800, "intent_bytes_snapshot": 50,
        }},
    )
    accepted = r2_storage.reserve_upload_intent(
        db,
        intent_id="intent-1", user_id="alice", media_kind="ai_coach_audio",
        object_keys=["pending/ai-coach/alice/intent-1.webm"],
        declared_bytes=100, storage_stop_bytes=1_000,
        metadata={"sha256": "a" * 64, "mime_type": "audio/webm"},
    )
    assert accepted == (True, "")
    sqls = [sql for sql, _params in db.session.statements]
    assert any("pg_advisory_xact_lock" in sql for sql in sqls)
    assert not any("usage_date" in sql or "month_start" in sql for sql in sqls)
    insert_sql, insert = next(
        item for item in db.session.statements
        if item[0].startswith("INSERT INTO r2_upload_intents")
    )
    assert insert["user"] == "alice" and insert["kind"] == "ai_coach_audio"
    assert json.loads(insert["metadata"])["sha256"] == "a" * 64
    assert "intent_metadata" in insert_sql


def test_system_wide_storage_projection_blocks_atomically(monkeypatch):
    db = _Db(current_declared=100)
    monkeypatch.setattr(
        r2_storage, "get_configs_from_connection",
        lambda *_args: {"r2_storage_usage_snapshot": {
            "bytes": 800, "intent_bytes_snapshot": 50,
        }},
    )
    blocked = r2_storage.reserve_upload_intent(
        db,
        intent_id="intent-2", user_id="alice", media_kind="match_photo",
        object_keys=["pending/photo"], declared_bytes=200,
        storage_stop_bytes=1_000,
    )
    assert blocked == (False, "storage_global")
    assert not any(
        sql.startswith("INSERT INTO r2_upload_intents")
        for sql, _params in db.session.statements
    )


def test_provider_processing_still_reserves_storage_until_object_is_deleted():
    captured = []

    class Rows:
        empty = False
        iloc = [{"total": 1234}]

    class Db:
        def query(self, statement):
            captured.append(" ".join(statement.split()))
            return Rows()

    assert r2_storage._intent_declared_bytes(Db()) == 1234
    assert "status NOT IN ('orphan_deleted','consumed')" in captured[0]
    assert "provider_processing" not in captured[0]


def test_complete_and_claim_are_owner_kind_scoped_and_single_use():
    executions = []

    class Db:
        def execute_count(self, statement, params):
            executions.append((" ".join(statement.split()), params))
            return 1

    db = Db()
    assert r2_storage.complete_upload_intent(
        db, "intent", user_id="alice", media_kind="ai_coach_audio",
    )
    assert r2_storage.claim_completed_upload_intent(
        db, "intent", user_id="alice", media_kind="ai_coach_audio",
    )
    assert "status='issued'" in executions[0][0]
    assert "status='completed'" in executions[1][0]
    assert all(item[1]["user"] == "alice" for item in executions)


def test_discard_releases_completed_temp_but_not_provider_started_or_consumed_object():
    executions = []

    class Db:
        def execute(self, statement, params):
            executions.append((" ".join(statement.split()), params))

    r2_storage.mark_upload_intent_deleted(Db(), "intent-1")
    sql, params = executions[0]
    assert "status IN ('issued','completed','processing','orphan_deleted')" in sql
    assert "provider_processing" not in sql and "consumed" not in sql
    assert params["id"] == "intent-1"
