"""Focused tests for read-only R2 and shared upload-intent quota helpers."""

import datetime as dt
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from core import r2_storage


class _ScalarResult:
    def __init__(self, value=0):
        self.value = value

    def scalar(self):
        return self.value


class _Session:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((" ".join(str(statement).split()), params or {}))
        return _ScalarResult()


class _Db:
    def __init__(self):
        self.session = _Session()

    @contextmanager
    def transaction(self):
        yield self.session


def test_connection_ready_performs_only_one_minimal_read(monkeypatch):
    calls = []

    class _Client:
        def list_objects_v2(self, **kwargs):
            calls.append(kwargs)
            return {"Contents": []}

    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(r2_storage, "settings", lambda: {"bucket": "private-media"})
    monkeypatch.setattr(r2_storage, "client", lambda: _Client())

    assert r2_storage.connection_ready() is True
    assert calls == [{"Bucket": "private-media", "MaxKeys": 1}]


def test_connection_ready_is_false_without_configuration_or_on_sdk_error(monkeypatch):
    monkeypatch.setattr(r2_storage, "configured", lambda: False)
    monkeypatch.setattr(
        r2_storage,
        "client",
        lambda: (_ for _ in ()).throw(AssertionError("client must not run")),
    )
    assert r2_storage.connection_ready() is False

    class _FailingClient:
        def list_objects_v2(self, **_kwargs):
            raise RuntimeError("credential and endpoint details must stay private")

    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(r2_storage, "settings", lambda: {"bucket": "private-media"})
    monkeypatch.setattr(r2_storage, "client", lambda: _FailingClient())
    assert r2_storage.connection_ready() is False

    monkeypatch.setattr(
        r2_storage,
        "configured",
        lambda: (_ for _ in ()).throw(RuntimeError("secret lookup failed")),
    )
    assert r2_storage.connection_ready() is False


def test_upload_intent_window_uses_hong_kong_calendar_boundaries():
    now_hk = dt.datetime(2026, 8, 1, 0, 30, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    now_utc, day_utc, month_utc = r2_storage._upload_intent_quota_window(now_hk)

    assert now_utc == dt.datetime(2026, 7, 31, 16, 30)
    assert day_utc == dt.datetime(2026, 7, 31, 16, 0)
    assert month_utc == dt.datetime(2026, 7, 31, 16, 0)
    assert now_utc.tzinfo is day_utc.tzinfo is month_utc.tzinfo is None


def test_status_and_reservation_share_windows_and_count_semantics(monkeypatch):
    now_utc = dt.datetime(2026, 7, 14, 4, 0)
    day_utc = dt.datetime(2026, 7, 13, 16, 0)
    month_utc = dt.datetime(2026, 6, 30, 16, 0)
    count_calls = []

    monkeypatch.setattr(
        r2_storage,
        "_upload_intent_quota_window",
        lambda: (now_utc, day_utc, month_utc),
    )

    def counts(session, **kwargs):
        count_calls.append((session, kwargs))
        return 2, 7

    monkeypatch.setattr(r2_storage, "_upload_intent_counts", counts)
    monkeypatch.setattr(r2_storage, "get_configs_from_connection", lambda *_args: {})

    status_db = _Db()
    status = r2_storage.upload_intent_quota_status(
        status_db,
        user_id="kiosk",
        media_kind="kiosk_match_review",
        user_daily_limit=5,
        global_monthly_limit=10,
    )
    assert status == {
        "user_daily_used": 2,
        "user_daily_limit": 5,
        "user_daily_remaining": 3,
        "global_monthly_used": 7,
        "global_monthly_limit": 10,
        "global_monthly_remaining": 3,
        "allowed": True,
        "blocked_scope": "",
    }

    reserve_db = _Db()
    reserved = r2_storage.reserve_upload_intent(
        reserve_db,
        intent_id="intent-1",
        user_id="kiosk",
        media_kind="kiosk_match_review",
        object_keys=["pending/audio/kiosk-match-review/test.webm"],
        declared_bytes=1_000,
        user_daily_limit=5,
        global_monthly_limit=10,
    )
    assert reserved == (True, "")
    assert [call[1] for call in count_calls] == [
        {
            "user_id": "kiosk",
            "media_kind": "kiosk_match_review",
            "day_utc": day_utc,
            "month_utc": month_utc,
        },
        {
            "user_id": "kiosk",
            "media_kind": "kiosk_match_review",
            "day_utc": day_utc,
            "month_utc": month_utc,
        },
    ]
    insert = next(
        params for sql, params in reserve_db.session.statements
        if sql.startswith("INSERT INTO r2_upload_intents")
    )
    assert insert["now"] == now_utc
    reserve_sql = [sql for sql, _params in reserve_db.session.statements]
    assert any(
        "status='provider_processing' AND created_at<:cutoff" in sql
        for sql in reserve_sql
    )
    assert any(
        "status NOT IN ('orphan_deleted','provider_processing','consumed')" in sql
        for sql in reserve_sql
    )


def test_quota_status_clamps_remaining_and_reports_authoritative_scope(monkeypatch):
    monkeypatch.setattr(
        r2_storage,
        "_upload_intent_counts",
        lambda *_args, **_kwargs: (6, 11),
    )
    status = r2_storage.upload_intent_quota_status(
        _Db(),
        user_id="kiosk",
        media_kind="kiosk_match_review",
        user_daily_limit=5,
        global_monthly_limit=10,
    )
    assert status["user_daily_remaining"] == 0
    assert status["global_monthly_remaining"] == 0
    assert status["allowed"] is False
    assert status["blocked_scope"] == "user_daily"


def test_shared_count_query_includes_every_status_and_exact_windows():
    captured = {}

    class _MappingsResult:
        def mappings(self):
            return self

        def one(self):
            return {"user_daily_used": 4, "global_monthly_used": 9}

    class _CountSession:
        def execute(self, statement, params):
            captured["sql"] = " ".join(str(statement).split())
            captured["params"] = params
            return _MappingsResult()

    day_utc = dt.datetime(2026, 7, 13, 16)
    month_utc = dt.datetime(2026, 6, 30, 16)
    assert r2_storage._upload_intent_counts(
        _CountSession(),
        user_id="kiosk",
        media_kind="kiosk_match_review",
        day_utc=day_utc,
        month_utc=month_utc,
    ) == (4, 9)
    assert "WHERE media_kind=:kind AND created_at>=:month_start" in captured["sql"]
    assert "user_id=:user AND created_at>=:day_start" in captured["sql"]
    assert "status IN ('issued','processing','provider_processing','consumed')" in captured["sql"]
    assert captured["params"] == {
        "user": "kiosk",
        "kind": "kiosk_match_review",
        "day_start": day_utc,
        "month_start": month_utc,
    }


def test_deleted_or_provider_started_recordings_do_not_reserve_storage_bytes():
    captured = []

    class _Rows:
        empty = False

        @property
        def iloc(self):
            class _Index:
                def __getitem__(_self, _index):
                    return {"total": 1234}

            return _Index()

    class _QueryDb:
        def query(self, statement):
            captured.append(" ".join(statement.split()))
            return _Rows()

    assert r2_storage._intent_declared_bytes(_QueryDb()) == 1234
    assert "status NOT IN ('orphan_deleted','provider_processing','consumed')" in captured[0]


def test_late_discard_cannot_release_provider_started_or_consumed_quota():
    executions = []

    class _DeleteDb:
        def execute(self, statement, params):
            executions.append((" ".join(statement.split()), params))

    r2_storage.mark_upload_intent_deleted(_DeleteDb(), "intent-1")
    sql, params = executions[0]
    assert "status IN ('issued','processing','orphan_deleted')" in sql
    assert "provider_processing" not in sql
    assert "consumed" not in sql
    assert params["id"] == "intent-1"
