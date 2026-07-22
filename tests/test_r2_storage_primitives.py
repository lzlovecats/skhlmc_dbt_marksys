"""Focused tests for R2 ownership and system-wide storage reservations."""

import json
from contextlib import contextmanager
import datetime as dt
import pandas as pd

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


def test_local_practice_media_retention_claims_exact_ttls_before_delete(monkeypatch):
    now = dt.datetime(2026, 7, 22, 12, 0, 0)
    rows = pd.DataFrame([
        {
            "intent_id": "failed-input",
            "media_kind": "local_practice_input",
            "object_keys": ["pending/local-practice/input/a.webm"],
            "intent_metadata": {},
            "status": "completed",
            "created_at": now - dt.timedelta(minutes=20),
            "completed_at": now - dt.timedelta(minutes=15),
        },
        {
            "intent_id": "output",
            "media_kind": "local_practice_tts_output",
            "object_keys": ["pending/local-practice/output/a.wav"],
            "intent_metadata": {},
            "status": "completed",
            "created_at": now - dt.timedelta(hours=2),
            "completed_at": now - dt.timedelta(hours=1),
        },
        {
            "intent_id": "retry-open",
            "media_kind": "local_practice_input",
            "object_keys": ["pending/local-practice/input/recent.webm"],
            "intent_metadata": {},
            "status": "completed",
            "created_at": now - dt.timedelta(minutes=14),
            "completed_at": now - dt.timedelta(minutes=14),
        },
        {
            "intent_id": "non-sliding-retry",
            "media_kind": "local_practice_input",
            "object_keys": ["pending/local-practice/input/retry.webm"],
            "intent_metadata": {
                "retry_started_at": (now - dt.timedelta(minutes=15)).isoformat(),
            },
            "status": "completed",
            "created_at": now - dt.timedelta(minutes=20),
            "completed_at": now - dt.timedelta(minutes=1),
        },
        {
            "intent_id": "expired-issued",
            "media_kind": "local_practice_input",
            "object_keys": ["pending/local-practice/input/issued.webm"],
            "intent_metadata": {},
            "status": "issued",
            "created_at": now - dt.timedelta(minutes=15),
            "completed_at": None,
        },
        {
            "intent_id": "cleanup-pending",
            "media_kind": "local_practice_input",
            "object_keys": ["pending/local-practice/input/cleanup.webm"],
            "intent_metadata": {
                "processing_started_at": now.isoformat(),
                "cleanup_pending_at": now.isoformat(),
            },
            "status": "processing",
            "created_at": now - dt.timedelta(minutes=1),
            "completed_at": None,
        },
    ])

    class Db:
        def __init__(self):
            self.claims = []

        def query(self, statement, params):
            assert "local_practice_input" in statement
            assert params["limit"] >= 3
            return rows

        def execute_count(self, statement, params):
            self.claims.append((" ".join(statement.split()), params))
            return 1

    deleted = []
    monkeypatch.setattr(
        r2_storage,
        "delete_intent_objects",
        lambda _db, intent_id, keys: deleted.append((intent_id, keys)) or True,
    )
    db = Db()
    result = r2_storage.prune_local_practice_media(db, now=now)
    assert result == {"examined": 5, "deleted": 5, "failed": 0}
    assert [item[0] for item in deleted] == [
        "failed-input", "output", "non-sliding-retry",
        "expired-issued", "cleanup-pending",
    ]
    assert all("status='completed'" in sql for sql, _params in db.claims)
    assert all("status='processing'" in sql for sql, _params in db.claims)


def test_failed_processing_retry_anchor_is_write_once():
    captured = []

    class Db:
        def execute_count(self, statement, params):
            captured.append((" ".join(statement.split()), params))
            return 1

    assert r2_storage.release_processing_upload_intent(
        Db(), "intent", user_id="alice", media_kind="local_practice_input",
    )
    sql, params = captured[0]
    assert "? 'retry_started_at'" in sql
    assert "{retry_started_at}" in sql
    assert params["started"]


def test_workstation_health_probe_is_single_per_node_and_deleted_r2_first(monkeypatch):
    executions = []

    class Db:
        def execute_count(self, statement, params):
            executions.append((" ".join(statement.split()), params))
            return 1

    db = Db()
    assert r2_storage.reserve_workstation_r2_health_probe(
        db,
        intent_id="a" * 32,
        node_id="node-1",
        object_key="pending/workstation-health/node-1/" + "a" * 32 + ".bin",
        sha256="b" * 64,
        byte_size=4096,
    )
    assert "ON CONFLICT (node_id) DO NOTHING" in executions[0][0]

    calls = []
    monkeypatch.setattr(r2_storage, "delete", lambda key: calls.append(("r2", key)))

    class DeleteDb:
        def execute_count(self, statement, params):
            calls.append(("db", " ".join(statement.split()), params))
            return 1

    assert r2_storage.delete_workstation_r2_health_probe(
        DeleteDb(),
        intent_id="a" * 32,
        node_id="node-1",
        object_key="pending/workstation-health/node-1/" + "a" * 32 + ".bin",
    )
    assert [call[0] for call in calls] == ["r2", "db"]


def test_workstation_health_probe_retention_retries_failed_deletes(monkeypatch):
    now = dt.datetime(2026, 7, 22, 12, 0, 0)
    rows = pd.DataFrame([
        {
            "intent_id": "a" * 32,
            "node_id": "node-1",
            "object_key": "pending/workstation-health/node-1/a.bin",
        },
        {
            "intent_id": "b" * 32,
            "node_id": "node-2",
            "object_key": "pending/workstation-health/node-2/b.bin",
        },
    ])

    class Db:
        def query(self, statement, params):
            assert "workstation_r2_health_probes" in statement
            assert params["cutoff"] == now - dt.timedelta(minutes=15)
            assert params["limit"] == 100
            return rows

    attempts = []
    monkeypatch.setattr(
        r2_storage,
        "delete_workstation_r2_health_probe",
        lambda _db, **values: attempts.append(values) is None,
    )
    # Make only the first delete succeed; the second remains durably retryable.
    monkeypatch.setattr(
        r2_storage,
        "delete_workstation_r2_health_probe",
        lambda _db, **values: attempts.append(values) or len(attempts) == 1,
    )
    result = r2_storage.prune_workstation_r2_health_probes(Db(), now=now)
    assert result == {"examined": 2, "deleted": 1, "failed": 1}
    assert [item["node_id"] for item in attempts] == ["node-1", "node-2"]
