"""Focused contracts for monthly budgets, Render sync, and R2 audio analysis."""

import asyncio
import datetime as dt
import json
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException, Request

from api import ai_coach_api, auth_api
from core import ai_provider, funds_logic, google_files, r2_storage
import deploy.proxy as proxy


ROOT = Path(__file__).resolve().parents[1]


class _Result:
    def __init__(self, *, scalar=None, one=None, all_rows=None):
        self._scalar = scalar
        self._one = one
        self._all = list(all_rows or [])

    def scalar(self):
        return self._scalar

    def mappings(self):
        return self

    def one(self):
        return self._one

    def one_or_none(self):
        return self._one

    def all(self):
        return self._all


def test_budget_save_snapshots_donations_and_applies_google_ten_percent_buffer(monkeypatch):
    statements = []

    class Session:
        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            statements.append((sql, params or {}))
            if "SELECT COALESCE(SUM(amount_hkd),0)" in sql:
                return _Result(scalar=100)
            if "SELECT notified_at,notification_audit" in sql:
                return _Result(one=None)
            return _Result()

    class Db:
        @contextmanager
        def transaction(self):
            yield Session()

    monkeypatch.setattr(funds_logic, "is_ai_manager", lambda *_a, **_k: True)
    monkeypatch.setattr(funds_logic, "ai_budget_data", lambda *_a, **_k: {"saved": True})
    now = dt.datetime(2026, 7, 25, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    result = funds_logic.save_ai_budget("manager", {
        "budget_month": "2026-08-01", "fx_hkd_per_usd": 7.8,
        "allocations": {
            "google": {"allocated_hkd": 78, "external_cap_confirmed": True},
            "openrouter": {"allocated_hkd": 22, "external_cap_confirmed": True},
            "azure": {"allocated_hkd": 0, "external_cap_confirmed": False},
            "other": {"allocated_hkd": 0, "external_cap_confirmed": False},
        },
    }, db=Db(), now=now)
    assert result == {"saved": True}
    inserts = [params for sql, params in statements
               if sql.startswith("INSERT INTO monthly_resource_limits")]
    fund = next(item for item in inserts if item.get("available") == 100)
    google = next(item for item in inserts if item.get("key") == "provider:google")
    openrouter = next(item for item in inserts if item.get("key") == "provider:openrouter")
    assert fund["start"].isoformat() == "2026-06-25T00:00:00+08:00"
    assert fund["end"].isoformat() == "2026-07-25T00:00:00+08:00"
    assert google["cap"] == 9.0
    assert openrouter["cap"] == round(22 / 7.8, 4)


def test_positive_provider_allocation_requires_external_cap_confirmation(monkeypatch):
    monkeypatch.setattr(funds_logic, "is_ai_manager", lambda *_a, **_k: True)
    with pytest.raises(ValueError, match="Google|google"):
        funds_logic.save_ai_budget("manager", {
            "budget_month": "2026-08-01", "fx_hkd_per_usd": 7.8,
            "allocations": {
                "google": {"allocated_hkd": 1, "external_cap_confirmed": False},
            },
        }, db=object(), now=dt.datetime(2026, 7, 25, 0, 0))


def test_budget_permissions_and_allocation_total_are_enforced(monkeypatch):
    with pytest.raises(PermissionError):
        funds_logic.save_ai_budget(
            "member", {}, db=object(),
            now=dt.datetime(2026, 7, 25, 0, 0),
        )

    class Session:
        def execute(self, statement, _params=None):
            sql = " ".join(str(statement).split())
            if "SELECT COALESCE(SUM(amount_hkd),0)" in sql:
                return _Result(scalar=100)
            if "SELECT notified_at,notification_audit" in sql:
                return _Result(one=None)
            return _Result()

    class Db:
        @contextmanager
        def transaction(self):
            yield Session()

    monkeypatch.setattr(funds_logic, "is_ai_manager", lambda *_a, **_k: True)
    with pytest.raises(ValueError, match="不能高於"):
        funds_logic.save_ai_budget("manager", {
            "budget_month": "2026-08-01", "fx_hkd_per_usd": 7.8,
            "allocations": {
                "google": {"allocated_hkd": 101, "external_cap_confirmed": True},
            },
        }, db=Db(), now=dt.datetime(2026, 7, 25, 0, 0))


def test_zero_push_delivery_remains_retryable_but_creates_login_announcement(monkeypatch):
    audit_updates = []
    providers = [
        {"limit_key": f"provider:{name}", "allocated_hkd": 25,
         "hard_value": 3, "external_cap_confirmed": True}
        for name in funds_logic.AI_BUDGET_PROVIDERS
    ]
    fund = {
        "allocated_hkd": 100, "fx_hkd_per_usd": 7.8,
        "funding_window_start": dt.datetime(2026, 6, 25),
        "funding_window_end": dt.datetime(2026, 7, 25),
        "notified_at": None, "notification_audit": {},
    }

    class Session:
        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            if "FOR UPDATE" in sql:
                return _Result(one=fund)
            if "SELECT limit_key,allocated_hkd" in sql:
                return _Result(all_rows=providers)
            if sql.startswith("UPDATE monthly_resource_limits"):
                audit_updates.append(json.loads(params["audit"]))
            return _Result()

    class AnnouncementRows:
        def to_dict(self, _orient):
            return [{
                "period_month": "2026-08-01", "allocated_hkd": 100,
                "notification_audit": audit_updates[-1],
            }]

    class Db:
        @contextmanager
        def transaction(self):
            yield Session()

        def execute_count(self, _sql, params):
            audit_updates.append(json.loads(params["audit"]))
            return 1

        def query(self, _sql):
            return AnnouncementRows()

    from core import push

    monkeypatch.setattr(funds_logic, "is_ai_manager", lambda *_a, **_k: True)
    monkeypatch.setattr(push, "notify_committee", lambda *_a, **_k: 0)
    db = Db()
    with pytest.raises(RuntimeError, match="安全重試"):
        funds_logic.notify_ai_budget(
            "manager", db, {"private_key": "x"},
            now=dt.datetime(2026, 7, 25, 0, 1, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        )
    retryable = audit_updates[-1]
    assert retryable["state"] == "retryable"
    assert retryable["announcement_at"]
    assert "HKD 100.00" in retryable["body"]
    notices = auth_api._fund_notifications(db)
    assert notices[0]["id"] == -202608
    assert notices[0]["title"] == retryable["title"]


def test_partial_push_success_finalizes_exactly_once(monkeypatch):
    providers = [
        {"limit_key": f"provider:{name}", "allocated_hkd": 0,
         "hard_value": 0, "external_cap_confirmed": False}
        for name in funds_logic.AI_BUDGET_PROVIDERS
    ]
    fund = {
        "allocated_hkd": 0, "fx_hkd_per_usd": 7.8,
        "funding_window_start": dt.datetime(2026, 6, 25),
        "funding_window_end": dt.datetime(2026, 7, 25),
        "notified_at": None, "notification_audit": {},
    }

    class Session:
        def execute(self, statement, _params=None):
            sql = " ".join(str(statement).split())
            if "FOR UPDATE" in sql:
                return _Result(one=fund)
            if "SELECT limit_key,allocated_hkd" in sql:
                return _Result(all_rows=providers)
            return _Result()

    class Db:
        @contextmanager
        def transaction(self):
            yield Session()

        def execute_count(self, sql, params):
            if "SET notified_by" in sql:
                fund["notified_at"] = params["now"]
            return 1

    from core import push

    monkeypatch.setattr(funds_logic, "is_ai_manager", lambda *_a, **_k: True)
    monkeypatch.setattr(funds_logic, "ai_budget_data", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(push, "notify_committee", lambda *_a, **_k: 1)
    now = dt.datetime(2026, 7, 25, 0, 1, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    result = funds_logic.notify_ai_budget("manager", Db(), {"key": "x"}, now=now)
    assert result == {"sent": 1, "budget": {"ok": True}}
    with pytest.raises(ValueError, match="已經通知"):
        funds_logic.notify_ai_budget("manager", Db(), {"key": "x"}, now=now)


def test_render_bucket_parser_is_category_hour_idempotent():
    start = int(dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc).timestamp())
    payload = {"series": [
        {"labels": {"traffic": "service"}, "values": [[start, 100]]},
        {"labels": {"traffic": "service"}, "values": [[start, 120]]},
        {"labels": {"traffic": "cdn"}, "values": [[start, 30]]},
    ]}
    buckets = proxy._render_bandwidth_buckets(payload, "srv-1")
    assert len(buckets) == 2
    assert {(item["category"], item["bytes"]) for item in buckets} == {
        ("service", 120), ("cdn", 30),
    }
    assert all(len(item["id"]) == 64 for item in buckets)


def test_render_bucket_parser_converts_official_units_and_label_arrays():
    stamp = "2026-07-01T00:00:00Z"
    payload = [{
        "labels": [
            {"field": "service", "value": "srv-1"},
            {"field": "trafficSource", "value": "total"},
        ],
        "unit": "GB",
        "values": [{"timestamp": stamp, "value": 1.25}],
    }]
    buckets = proxy._render_bandwidth_buckets(payload, "srv-1")
    assert len(buckets) == 1
    assert buckets[0]["category"] == "total"
    assert buckets[0]["bytes"] == 1_250_000_000


def test_render_source_breakdown_default_unit_is_gb():
    start = int(dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc).timestamp())
    payload = {"data": [{
        "labels": {"resource": "srv-1", "trafficSource": "websocket"},
        "values": [{"timestamp": start, "value": 0.5}],
    }]}
    buckets = proxy._render_bandwidth_buckets(payload, "srv-1", default_unit="GB")
    assert buckets[0]["category"] == "websocket"
    assert buckets[0]["bytes"] == 500_000_000


def test_bandwidth_status_uses_official_complete_plus_only_later_local(monkeypatch):
    start = dt.datetime(2026, 6, 30, 16)
    through = start + dt.timedelta(hours=1)
    seen = []

    class Connection:
        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            seen.append((sql, params or {}))
            if "MIN(bucket_start)" in sql:
                return _Result(one={
                    "bytes": 100, "first_bucket": start, "through": through,
                })
            return _Result(scalar=20)

    class Engine:
        @contextmanager
        def begin(self):
            yield Connection()

    monkeypatch.setattr(proxy, "_get_db_engine", lambda: Engine())
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_a, **_k: "")
    from core import resource_limits
    monkeypatch.setattr(resource_limits, "get_monthly_limit", lambda *_a, **_k: {
        "warning_value": 300, "stop_value": 350, "hard_value": 400,
    })
    status = proxy.bandwidth_budget_status()
    assert status["official_bytes"] == 100
    assert status["tracked_bytes"] == 20
    assert status["total_bytes"] == 120
    local_query = next(item for item in seen if "official_bucket_id IS NULL" in item[0])
    assert local_query[1]["start"] == through
    official_query = next(item for item in seen if "MIN(bucket_start)" in item[0])
    assert "traffic_category" in official_query[0]


def test_bandwidth_reservation_has_margin_and_settles_to_raw_streamed_bytes(monkeypatch):
    executions = []

    class Connection:
        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            executions.append((sql, params or {}))
            return _Result(scalar=42)

    class Engine:
        @contextmanager
        def begin(self):
            yield Connection()

    monkeypatch.setattr(proxy, "_get_db_engine", lambda: Engine())
    monkeypatch.setattr(proxy, "bandwidth_budget_status", lambda **_k: {
        "total_bytes": 0, "stop_live_bytes": 2_000_000,
    })
    reservation = proxy.reserve_bandwidth_transfer("audio-1", 1_000_000)
    assert reservation == 42
    insert = next(params for sql, params in executions
                  if sql.startswith("INSERT INTO bandwidth_usage_logs"))
    assert insert["bytes"] == 1_000_000 + 65_536
    proxy.settle_bandwidth_transfer(42, 765_432, success=False)
    update = executions[-1][1]
    assert update["bytes"] == 765_432
    assert update["source"] == "ai_coach_audio_provider_failed"


def test_concurrent_bandwidth_reservations_cannot_cross_stop_gate(monkeypatch):
    import threading

    class Engine:
        def __init__(self):
            self.lock = threading.Lock()
            self.total = 0
            self.next_id = 1

        @contextmanager
        def begin(self):
            with self.lock:
                yield Connection(self)

    class Connection:
        def __init__(self, engine):
            self.engine = engine

        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            if sql.startswith("INSERT INTO bandwidth_usage_logs"):
                self.engine.total += int(params["bytes"])
                value = self.engine.next_id
                self.engine.next_id += 1
                return _Result(scalar=value)
            return _Result()

    engine = Engine()
    monkeypatch.setattr(proxy, "_get_db_engine", lambda: engine)
    monkeypatch.setattr(proxy, "bandwidth_budget_status", lambda **_k: {
        "total_bytes": engine.total, "stop_live_bytes": 1_200_000,
    })
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda index: proxy.reserve_bandwidth_transfer(f"audio-{index}", 600_000),
            range(2),
        ))
    assert sum(value is not None for value in results) == 1
    assert sum(value is None for value in results) == 1


def _request():
    return Request({
        "type": "http", "method": "POST", "path": "/api/ai-coach/run",
        "headers": [], "query_string": b"", "scheme": "https",
        "server": ("testserver", 443),
    })


def test_recording_intent_accepts_more_than_two_mib_and_is_operation_bound(monkeypatch):
    captured = {}
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(r2_storage, "storage_budget_status", lambda *_a, **_k: {
        "blocked": False,
    })
    monkeypatch.setattr(r2_storage, "presign_put", lambda *args: captured.update(
        presign=args,
    ) or "https://r2.invalid/put")
    monkeypatch.setattr(r2_storage, "reserve_upload_intent", lambda _db, **kwargs: (
        captured.update(reserve=kwargs) or (True, "")
    ))
    monkeypatch.setattr(proxy, "get_vote_db", lambda: object())
    body = ai_coach_api.AudioIntentBody(
        mime_type="audio/webm", byte_size=3 * 1024 * 1024, sha256="a" * 64,
    )
    payload = ai_coach_api.recording_intent(body, _request())
    assert payload["upload"]["url"] == "https://r2.invalid/put"
    assert captured["reserve"]["user_id"] == "alice"
    assert captured["reserve"]["media_kind"] == "ai_coach_audio"
    assert captured["reserve"]["declared_bytes"] == 3 * 1024 * 1024
    assert captured["reserve"]["metadata"] == {
        "sha256": "a" * 64, "mime_type": "audio/webm",
    }


def test_recording_intent_is_rejected_before_r2_at_render_stop(monkeypatch):
    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(proxy, "bandwidth_budget_status", lambda **_kwargs: {
        "total_bytes": 350, "stop_live_bytes": 350,
    })
    monkeypatch.setattr(
        r2_storage, "presign_put",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("3.5GB stop gate reached R2 presign"),
        ),
    )
    with pytest.raises(HTTPException, match="3.5GB"):
        ai_coach_api.recording_intent(ai_coach_api.AudioIntentBody(
            mime_type="audio/webm", byte_size=3 * 1024 * 1024,
            sha256="a" * 64,
        ), _request())


def test_verified_audio_intent_rechecks_owner_size_sha_and_mime(monkeypatch):
    intent = {
        "status": "completed", "object_keys": ["pending/audio.webm"],
        "declared_bytes": 123, "intent_metadata": {
            "sha256": "a" * 64, "mime_type": "audio/webm",
        },
    }
    monkeypatch.setattr(proxy, "get_vote_db", lambda: object())
    monkeypatch.setattr(r2_storage, "get_upload_intent", lambda *_a: intent)
    monkeypatch.setattr(r2_storage, "head", lambda _key: {
        "ContentLength": 123, "ContentType": "audio/webm",
        "Metadata": {"sha256": "a" * 64},
    })
    verified = ai_coach_api._verified_intent("alice", "intent")
    assert verified[2:] == ("pending/audio.webm", "audio/webm", "a" * 64, 123)

    monkeypatch.setattr(r2_storage, "head", lambda _key: {
        "ContentLength": 123, "ContentType": "audio/webm",
        "Metadata": {"sha256": "b" * 64},
    })
    with pytest.raises(HTTPException, match="SHA256"):
        ai_coach_api._verified_intent("alice", "intent")


def test_bandwidth_race_rejection_deletes_completed_audio_intent(monkeypatch):
    db = object()
    events = []
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_a, **_k: "google-key")
    monkeypatch.setattr(proxy, "reserve_bandwidth_transfer", lambda *_a: None)
    monkeypatch.setattr(ai_coach_api, "_verified_intent", lambda *_a, **_k: (
        db, {"status": "completed"}, "pending/audio.webm",
        "audio/webm", "a" * 64, 123,
    ))
    monkeypatch.setattr(
        ai_coach_api, "_discard_audio_intent",
        lambda *args: events.append(args),
    )
    body = ai_coach_api.CoachRequest(
        feature="speech_review", audio_intent_id="a" * 32,
        audio_duration_seconds=10,
    )
    with pytest.raises(HTTPException, match="3.5GB"):
        asyncio.run(ai_coach_api._generate({
            "provider": "gemini", "supports_audio": True,
            "api_key": "GEMINI_API_KEY", "model": "gemini-test",
        }, "system", "user", body, "alice"))
    assert events == [("alice", "a" * 32)]


@pytest.mark.parametrize("provider_fails", [False, True])
def test_google_files_analysis_always_cleans_google_r2_and_bandwidth(
    monkeypatch, tmp_path, provider_fails,
):
    staged = tmp_path / "audio.webm"
    staged.write_bytes(b"raw-opus")
    db = object()
    events = []
    size = staged.stat().st_size
    sha = "a" * 64
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_a, **_k: "google-key")
    monkeypatch.setattr(proxy, "reserve_bandwidth_transfer", lambda *_a: 77)
    monkeypatch.setattr(proxy, "settle_bandwidth_transfer", lambda *args, **kwargs: events.append(
        ("settle", args, kwargs),
    ))
    monkeypatch.setattr(ai_coach_api, "_verified_intent", lambda *_a, **_k: (
        db, {"status": "completed"}, "pending/audio.webm", "audio/webm", sha, size,
    ))
    monkeypatch.setattr(r2_storage, "claim_completed_upload_intent", lambda *_a, **_k: True)
    monkeypatch.setattr(ai_coach_api, "_stage_r2_audio", lambda *_a: str(staged))
    monkeypatch.setattr(ai_coach_api, "probe_audio_file", lambda *_a, **_k: {
        "sha256": sha, "duration": 500,
    })

    async def upload(_path, _mime, _key, on_chunk=None):
        on_chunk(size)
        return {"name": "files/temporary", "uri": "gs://temporary"}

    async def active(file_data, _key):
        return file_data

    async def delete_google(file_data, _key):
        events.append(("google_delete", file_data["name"]))

    async def generate(*_args, **kwargs):
        assert kwargs["audio_file_uri"] == "gs://temporary"
        if provider_fails:
            raise RuntimeError("provider failure")
        return "分析完成", {"input_tokens": 1}

    monkeypatch.setattr(google_files, "upload_audio_file", upload)
    monkeypatch.setattr(google_files, "wait_until_active", active)
    monkeypatch.setattr(google_files, "delete_file", delete_google)
    monkeypatch.setattr(r2_storage, "delete_intent_objects", lambda *_a, **_k: events.append(
        ("r2_delete",),
    ))
    monkeypatch.setattr(ai_provider, "generate_text", generate)
    body = ai_coach_api.CoachRequest(
        feature="speech_review", audio_intent_id="a" * 32,
        audio_duration_seconds=500,
    )
    config = {
        "provider": "gemini", "supports_audio": True,
        "api_key": "GEMINI_API_KEY", "model": "gemini-test",
    }
    if provider_fails:
        with pytest.raises(HTTPException, match="AI 服務"):
            asyncio.run(ai_coach_api._generate(config, "system", "user", body, "alice"))
    else:
        assert asyncio.run(
            ai_coach_api._generate(config, "system", "user", body, "alice"),
        )[0] == "分析完成"
    assert not staged.exists()
    assert any(item[0] == "google_delete" for item in events)
    assert any(item[0] == "r2_delete" for item in events)
    settlement = next(item for item in events if item[0] == "settle")
    assert settlement[1] == (77, size)
    assert settlement[2]["success"] is True


def test_migration_removes_quota_tables_locks_monthly_limits_and_keeps_62_days():
    up = (ROOT / "migrations/20260715_0001_monthly_resource_limits_and_remove_quotas.up.sql").read_text()
    down = (ROOT / "migrations/20260715_0001_monthly_resource_limits_and_remove_quotas.down.sql").read_text()
    assert "CREATE TABLE public.monthly_resource_limits" in up
    assert "PRIMARY KEY (period_month, limit_key)" in up
    assert "INTERVAL '62 days'" in up
    assert "DROP TABLE IF EXISTS public.practice_daily_usage" in up
    assert "DROP TABLE IF EXISTS public.ai_coach_prepare_usage" in up
    assert "FROM PUBLIC" in up
    assert "rolname IN ('anon', 'authenticated')" in up
    assert "idx_r2_upload_intents_lifecycle" in up
    assert "CREATE TABLE public.practice_daily_usage" in down
    assert "CREATE TABLE public.ai_coach_prepare_usage" in down
    assert "DROP TABLE public.monthly_resource_limits" in down
