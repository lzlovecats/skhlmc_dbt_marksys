"""Transaction and fail-closed contracts for the AI data-factory store.

These tests deliberately use a scripted connection instead of a live database.
They exercise the ordering of state checks and writes while keeping the suite
offline and proving that a raised exception leaves the enclosing transaction
with no committed partial writes.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core import ai_factory_store as store


_MISSING = object()


class _Result:
    def __init__(self, *, row=_MISSING, rows=None, scalar=_MISSING):
        self._row = row
        self._rows = list(rows or [])
        self._scalar = scalar

    def mappings(self):
        return self

    def first(self):
        if self._row is not _MISSING:
            return self._row
        return self._rows[0] if self._rows else None

    def all(self):
        if self._rows:
            return list(self._rows)
        return [] if self._row is _MISSING else [self._row]

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        if self._scalar is _MISSING:
            return None
        return self._scalar


class _ScriptedConnection:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = params or {}
        self.calls.append((sql, params))
        if not self.steps:
            raise AssertionError(f"unexpected SQL: {sql}")
        expected, outcome = self.steps.pop(0)
        expected_parts = (expected,) if isinstance(expected, str) else expected
        for part in expected_parts:
            assert part in sql, f"expected {part!r} in SQL: {sql}"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def assert_done(self):
        assert self.steps == []


class _ScriptedDb:
    def __init__(self, steps=(), query_frames=()):
        self.conn = _ScriptedConnection(steps)
        self.query_frames = list(query_frames)
        self.query_calls = []
        self.transaction_count = 0
        self.rollback_count = 0
        self.committed_calls = []

    @contextmanager
    def transaction(self):
        self.transaction_count += 1
        start = len(self.conn.calls)
        try:
            yield self.conn
        except Exception:
            self.rollback_count += 1
            raise
        else:
            self.committed_calls.extend(self.conn.calls[start:])

    def query(self, sql, params=None):
        self.query_calls.append((" ".join(str(sql).split()), params or {}))
        if not self.query_frames:
            raise AssertionError(f"unexpected query: {sql}")
        return self.query_frames.pop(0)


def _job_row(**overrides):
    row = {
        "id": "job-1",
        "source_id": "source-1",
        "recipe_key": "rag_knowledge_card_v1",
        "requested_count": 2,
        "instruction_text": "",
        "status": "draft",
        "created_by": "manager",
        "invalidated_at": None,
        "source_withdrawn_at": None,
        "current_source_sha": "source-sha",
        "preview_sha256": "preview-sha",
        "preview_model_label": "Gemini 3.5 Flash",
        "preview_provider": "gemini",
        "preview_provider_model": "gemini-3.5-flash",
        "preview_prompt_sha256": "prompt-sha",
        "preview_input_sha256": "input-sha",
        "preview_expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "updated_at": datetime.now(timezone.utc),
    }
    row.update(overrides)
    return row


def _job_lineage_steps(job=None):
    job = dict(job or _job_row())
    source_id = str(job["source_id"])
    return [
        (
            ("SELECT source_id FROM ai_factory_jobs", "WHERE id=:id"),
            _Result(row={"source_id": source_id}),
        ),
        (
            ("FROM ai_factory_sources", "FOR UPDATE"),
            _Result(
                row={
                    "id": source_id,
                    "withdrawn_at": job.get("source_withdrawn_at"),
                    "content_sha256": job.get("current_source_sha", "source-sha"),
                }
            ),
        ),
        (("SELECT * FROM ai_factory_jobs", "FOR UPDATE"), _Result(row=job)),
    ]


def _claim(db, **overrides):
    values = {
        "job_id": "job-1",
        "preview_sha256": "preview-sha",
        "model_label": "Gemini 3.5 Flash",
        "provider": "gemini",
        "provider_model": "gemini-3.5-flash",
        "recipe_version": "2026-07-20.v1",
        "source_sha256": "source-sha",
        "prompt_sha256": "prompt-sha",
        "input_sha256": "input-sha",
        "candidate_count": 2,
        "confirmation_version": "factory-preview-v1",
        "anonymization_confirmed": True,
        "rights_confirmed": True,
        "third_party_confirmed": True,
    }
    values.update(overrides)
    return store.claim_attempt(db, "manager", **values)


def _no_stale_attempts():
    return (
        ("FROM ai_factory_attempts a", "a.created_at<:cutoff", "ORDER BY"),
        _Result(rows=[]),
    )


def _running_attempt_row(**overrides):
    row = {
        "id": "attempt-1",
        "job_id": "job-1",
        "attempt_no": 1,
        "model_label": "Gemini 3.5 Flash",
        "provider": "gemini",
        "status": "running",
        "candidate_count": 2,
        "job_invalidated_at": None,
        "source_withdrawn_at": None,
    }
    row.update(overrides)
    return row


def _attempt_lineage_steps(attempt=None):
    attempt = dict(attempt or _running_attempt_row())
    job_id = str(attempt["job_id"])
    source_id = str(attempt.get("source_id", "source-1"))
    return [
        (
            ("FROM ai_factory_attempts a", "JOIN ai_factory_jobs j", "WHERE a.id=:id"),
            _Result(row={"job_id": job_id, "source_id": source_id}),
        ),
        (
            ("FROM ai_factory_sources", "FOR UPDATE"),
            _Result(
                row={
                    "id": source_id,
                    "withdrawn_at": attempt.get("source_withdrawn_at"),
                }
            ),
        ),
        (
            ("FROM ai_factory_jobs", "FOR UPDATE"),
            _Result(
                row={
                    "id": job_id,
                    "source_id": source_id,
                    "invalidated_at": attempt.get("job_invalidated_at"),
                }
            ),
        ),
        (("SELECT * FROM ai_factory_attempts", "FOR UPDATE"), _Result(row=attempt)),
    ]


def _item_lineage_steps(item):
    item = dict(item)
    job_id = str(item["job_id"])
    source_id = str(item.get("source_id", "source-1"))
    return [
        (
            ("FROM ai_factory_items i", "JOIN ai_factory_jobs j", "WHERE i.id=:id"),
            _Result(row={"job_id": job_id, "source_id": source_id}),
        ),
        (
            ("FROM ai_factory_sources", "FOR UPDATE"),
            _Result(
                row={
                    "id": source_id,
                    "withdrawn_at": item.get("source_withdrawn_at"),
                }
            ),
        ),
        (
            ("FROM ai_factory_jobs", "FOR UPDATE"),
            _Result(
                row={
                    "id": job_id,
                    "source_id": source_id,
                    "recipe_key": item.get("recipe_key", "rag_knowledge_card_v1"),
                    "created_by": item.get("created_by", "manager"),
                    "invalidated_at": item.get("job_invalidated_at"),
                }
            ),
        ),
        (("SELECT * FROM ai_factory_items", "FOR UPDATE"), _Result(row=item)),
    ]


def _stale_attempt_lock_steps(attempt_rows):
    attempt_rows = list(attempt_rows)
    job_ids = sorted({str(row[1]) for row in attempt_rows})
    source_by_job = {job_id: f"source-for-{job_id}" for job_id in job_ids}
    candidate_rows = [
        (str(row[0]), str(row[1]), source_by_job[str(row[1])])
        for row in attempt_rows
    ]
    source_ids = sorted(set(source_by_job.values()))
    return [
        (
            ("FROM ai_factory_attempts a", "a.created_at<:cutoff", "ORDER BY"),
            _Result(rows=candidate_rows),
        ),
        (
            ("FROM ai_factory_sources", "ORDER BY id FOR UPDATE"),
            _Result(rows=[(source_id,) for source_id in source_ids]),
        ),
        (
            ("FROM ai_factory_jobs", "ORDER BY id FOR UPDATE"),
            _Result(rows=[(job_id, source_by_job[job_id]) for job_id in job_ids]),
        ),
        (
            ("FROM ai_factory_attempts", "id=ANY(:ids)", "ORDER BY id FOR UPDATE"),
            _Result(rows=attempt_rows),
        ),
    ]


def _locked_lineage_tables(calls):
    hierarchy = (
        "ai_factory_sources",
        "ai_factory_jobs",
        "ai_factory_attempts",
        "ai_factory_items",
        "ai_factory_releases",
    )
    return [
        table
        for sql, _ in calls
        if "FOR UPDATE" in sql
        for table in hierarchy
        if f"FROM {table}" in sql
    ]


def _actual_usage():
    return {
        "model_label": "client-must-not-authorize-model",
        "provider": "client-must-not-authorize-provider",
        "estimated_cost_usd": 0.10,
        "estimated_cost_hkd": 0.78,
        "input_tokens": 120,
        "output_tokens": 45,
        "provider_request_id": "provider-request-123",
        "resolved_provider_model": "gemini-resolved-202607",
        "cost_source": "provider_actual",
    }


@pytest.mark.parametrize(
    "missing_confirmation",
    ("anonymization_confirmed", "rights_confirmed", "third_party_confirmed"),
)
def test_claim_requires_all_three_confirmations_before_opening_a_transaction(
    missing_confirmation,
):
    db = _ScriptedDb()

    with pytest.raises(store.FactoryStoreError) as error:
        _claim(db, **{missing_confirmation: False})

    assert error.value.status_code == 400
    assert db.transaction_count == 0
    assert db.conn.calls == []


def test_claim_requires_a_reason_when_preview_detected_possible_pii():
    db = _ScriptedDb()

    with pytest.raises(store.FactoryStoreError) as error:
        _claim(db, pii_warning_count=1, pii_override_reason="   ")

    assert error.value.status_code == 400
    assert db.transaction_count == 0


@pytest.mark.parametrize(
    "changed_field",
    (
        "preview_sha256",
        "preview_model_label",
        "preview_provider",
        "preview_provider_model",
        "preview_prompt_sha256",
        "preview_input_sha256",
        "current_source_sha",
    ),
)
def test_claim_pins_every_exact_preview_and_source_identity(changed_field):
    job = _job_row(**{changed_field: "changed-after-preview"})
    db = _ScriptedDb(
        [
            ("ai_factory_provider_capacity", _Result()),
            _no_stale_attempts(),
            *_job_lineage_steps(job),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        _claim(db)

    assert error.value.status_code == 409
    assert "重新預覽" in str(error.value)
    assert not any("INSERT INTO ai_factory_attempts" in sql for sql, _ in db.conn.calls)
    assert db.rollback_count == 1
    db.conn.assert_done()


def test_claim_rejects_an_expired_exact_preview_before_attempt_counting():
    db = _ScriptedDb(
        [
            ("ai_factory_provider_capacity", _Result()),
            _no_stale_attempts(),
            *_job_lineage_steps(
                _job_row(
                    preview_expires_at=datetime.now(timezone.utc)
                    - timedelta(seconds=1)
                )
            ),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        _claim(db)

    assert error.value.status_code == 409
    assert "預覽已過期" in str(error.value)
    assert not any("INSERT INTO ai_factory_attempts" in sql for sql, _ in db.conn.calls)
    db.conn.assert_done()


@pytest.mark.parametrize(
    ("global_active", "manager_active", "expected_message"),
    (
        (store.AI_FACTORY_CONCURRENCY, None, "其他生成工作"),
        (0, store.AI_FACTORY_MANAGER_CONCURRENCY, "已有一個生成工作"),
    ),
)
def test_claim_enforces_database_locked_global_and_per_manager_concurrency(
    global_active, manager_active, expected_message
):
    steps = [
        ("ai_factory_provider_capacity", _Result()),
        _no_stale_attempts(),
        *_job_lineage_steps(),
        ("FROM ai_factory_attempts WHERE job_id=:id", _Result(scalar=0)),
        ("status IN ('claimed','running')", _Result(scalar=global_active)),
        (
            "j.created_by=:actor",
            _Result(scalar=manager_active if manager_active is not None else 0),
        ),
    ]
    db = _ScriptedDb(steps)

    with pytest.raises(store.FactoryStoreError) as error:
        _claim(db)

    assert error.value.status_code == 429
    assert expected_message in str(error.value)
    assert not any("INSERT INTO ai_factory_attempts" in sql for sql, _ in db.conn.calls)
    global_sql = next(
        sql for sql, _params in db.conn.calls
        if "SELECT COUNT(*) FROM ai_factory_transcript_attempts" in sql
        and "created_by" not in sql
    )
    manager_sql = next(
        sql for sql, _params in db.conn.calls
        if "j.created_by=:actor" in sql
    )
    assert "FROM ai_factory_transcript_attempts" in global_sql
    assert "FROM ai_factory_transcript_attempts" in manager_sql
    assert "JOIN ai_factory_transcript_runs" in manager_sql
    db.conn.assert_done()


def test_claim_enforces_manual_retry_cap_before_capacity_or_provider_state():
    db = _ScriptedDb(
        [
            ("ai_factory_provider_capacity", _Result()),
            _no_stale_attempts(),
            *_job_lineage_steps(_job_row(status="draft")),
            (
                "FROM ai_factory_attempts WHERE job_id=:id",
                _Result(scalar=store.AI_FACTORY_ATTEMPT_MAX),
            ),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        _claim(db)

    assert error.value.status_code == 409
    assert "重試上限" in str(error.value)
    assert not any("INSERT INTO ai_factory_attempts" in sql for sql, _ in db.conn.calls)
    db.conn.assert_done()


def test_paid_claim_records_estimate_without_budget_configuration_or_limit_lookup():
    """Paid factory work must not depend on the separately managed AI Fund."""
    db = _ScriptedDb(
        [
            ("ai_factory_provider_capacity", _Result()),
            _no_stale_attempts(),
            *_job_lineage_steps(),
            ("FROM ai_factory_attempts WHERE job_id=:id", _Result(scalar=0)),
            ("status IN ('claimed','running')", _Result(scalar=0)),
            ("j.created_by=:actor", _Result(scalar=0)),
            ("INSERT INTO ai_factory_attempts", _Result()),
            (("UPDATE ai_factory_jobs", "status='processing'"), _Result()),
            ("INSERT INTO ai_training_audit", _Result()),
        ]
    )

    result = _claim(db, estimated_cost_hkd=20)

    assert result["attempt_no"] == 1
    insert_sql, insert_params = next(
        call for call in db.conn.calls if "INSERT INTO ai_factory_attempts" in call[0]
    )
    assert insert_params["estimated_cost_hkd"] == 20
    assert "budget_provider_name" not in insert_sql
    assert "budget_period_month" not in insert_sql
    assert "budget_window_start" not in insert_sql
    assert "monthly_resource_limits" not in " ".join(
        sql for sql, _params in db.conn.calls
    )
    assert "ai_factory_budget:" not in " ".join(
        str(params) for _sql, params in db.conn.calls
    )
    db.conn.assert_done()


def test_failed_job_cannot_reuse_its_old_preview_without_refresh():
    db = _ScriptedDb(
        [
            ("ai_factory_provider_capacity", _Result()),
            _no_stale_attempts(),
            *_job_lineage_steps(_job_row(status="failed")),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        _claim(db)

    assert error.value.status_code == 409
    assert not any("INSERT INTO ai_factory_attempts" in sql for sql, _ in db.conn.calls)
    assert db.rollback_count == 1
    db.conn.assert_done()


def test_successful_refreshed_retry_claims_one_durable_attempt_and_audits_confirmation():
    db = _ScriptedDb(
        [
            ("ai_factory_provider_capacity", _Result()),
            _no_stale_attempts(),
            *_job_lineage_steps(_job_row(status="draft")),
            ("FROM ai_factory_attempts WHERE job_id=:id", _Result(scalar=1)),
            ("status IN ('claimed','running')", _Result(scalar=0)),
            ("j.created_by=:actor", _Result(scalar=0)),
            ("INSERT INTO ai_factory_attempts", _Result()),
            (("UPDATE ai_factory_jobs", "status='processing'"), _Result()),
            ("INSERT INTO ai_training_audit", _Result()),
        ]
    )

    result = _claim(db, pii_warning_count=2, pii_override_reason="已人工刪除姓名")

    assert result["job_id"] == "job-1"
    assert result["attempt_no"] == 2
    insert_sql, insert_params = next(
        call for call in db.conn.calls if "INSERT INTO ai_factory_attempts" in call[0]
    )
    assert ":job_id,:attempt_no,:job_id" in insert_sql
    assert insert_params["attempt_no"] == 2
    assert insert_params["confirmation"] == "factory-preview-v1"
    audit_params = db.conn.calls[-1][1]
    assert audit_params["action"] == "factory_generation_confirmed"
    assert '"third_party_confirmed":true' in audit_params["details"]
    assert '"pii_warning_count":2' in audit_params["details"]
    assert _locked_lineage_tables(db.conn.calls) == [
        "ai_factory_sources",
        "ai_factory_jobs",
    ]
    assert db.rollback_count == 0
    db.conn.assert_done()


def test_claim_reclaims_stale_attempts_and_processing_jobs_before_counting_capacity():
    stale_rows = [
        (
            "stale-attempt-1", "stale-job", "claimed", 1,
            "Gemini 3.5 Flash", "gemini", 0, "manager",
        ),
        (
            "stale-attempt-2", "stale-job", "claimed", 2,
            "Gemini 3.5 Flash", "gemini", 0, "manager",
        ),
    ]
    db = _ScriptedDb(
        [
            ("ai_factory_provider_capacity", _Result()),
            *_stale_attempt_lock_steps(stale_rows),
            (
                ("UPDATE ai_factory_attempts", "status='failed'", "error_code='orphaned_attempt'"),
                _Result(),
            ),
            (
                ("UPDATE ai_factory_jobs", "status='failed'", "status='processing'"),
                _Result(),
            ),
            *_job_lineage_steps(),
            ("FROM ai_factory_attempts WHERE job_id=:id", _Result(scalar=0)),
            ("status IN ('claimed','running')", _Result(scalar=0)),
            ("j.created_by=:actor", _Result(scalar=0)),
            ("INSERT INTO ai_factory_attempts", _Result()),
            (("UPDATE ai_factory_jobs", "status='processing'"), _Result()),
            ("INSERT INTO ai_training_audit", _Result()),
        ]
    )

    result = _claim(db)

    assert result["attempt_no"] == 1
    stale_select_params = db.conn.calls[1][1]
    cutoff_age = datetime.now(timezone.utc) - stale_select_params["cutoff"]
    assert store.AI_FACTORY_PREVIEW_TTL_SECONDS <= cutoff_age.total_seconds() < (
        store.AI_FACTORY_PREVIEW_TTL_SECONDS + 5
    )
    attempt_update = next(
        call for call in db.conn.calls if "UPDATE ai_factory_attempts" in call[0]
    )
    assert attempt_update[1]["ids"] == ["stale-attempt-1", "stale-attempt-2"]
    job_update = next(
        call for call in db.conn.calls if "UPDATE ai_factory_jobs SET status='failed'" in call[0]
    )
    assert job_update[1]["ids"] == ["stale-job"]
    assert "invalidated_at IS NULL" in job_update[0]
    row_locks = [
        sql
        for sql, _ in db.conn.calls
        if "FOR UPDATE" in sql and "ai_factory_" in sql
    ][:3]
    assert "FROM ai_factory_sources" in row_locks[0]
    assert "FROM ai_factory_jobs" in row_locks[1]
    assert "FROM ai_factory_attempts" in row_locks[2]
    assert db.rollback_count == 0
    db.conn.assert_done()


def test_reap_stale_running_attempt_accounts_reserved_estimate_atomically():
    stale_rows = [(
        "stale-running", "stale-job", "running", 2,
        "Gemini 3.5 Flash", "gemini", 0.78, "original-manager",
    )]
    db = _ScriptedDb(
        [
            ("ai_factory_provider_capacity", _Result()),
            *_stale_attempt_lock_steps(stale_rows),
            (("UPDATE ai_factory_attempts", "error_code='orphaned_attempt'"), _Result()),
            (("UPDATE ai_factory_jobs", "status='failed'"), _Result()),
            ("INSERT INTO ai_fund_usage_logs", _Result()),
        ]
    )

    result = store.reap_stale_attempts(db)

    assert result == {"reaped": 1}
    ledger_params = db.conn.calls[-1][1]
    assert ledger_params["user"] == "original-manager"
    assert ledger_params["status"] == "failed"
    assert ledger_params["hkd"] == pytest.approx(0.78)
    assert ledger_params["source"] == "factory_preview_estimate_orphaned_running"
    assert ledger_params["operation_id"] == "stale-job"
    assert ledger_params["operation_stage"] == "attempt_2"
    assert db.rollback_count == 0
    db.conn.assert_done()


def test_wrong_provider_candidate_count_fails_before_any_item_is_inserted():
    db = _ScriptedDb(
        [
            ("ai_factory_item_capacity", _Result()),
            *_attempt_lineage_steps(
                {
                    "id": "attempt-1",
                    "job_id": "job-1",
                    "status": "running",
                    "candidate_count": 2,
                    "job_invalidated_at": None,
                    "source_withdrawn_at": None,
                }
            ),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        store.complete_attempt(
            db,
            "manager",
            "attempt-1",
            payloads=[{"candidate": 1}],
            response_sha256="response-sha",
            response_bytes=100,
        )

    assert error.value.status_code == 400
    assert "數量不符" in str(error.value)
    assert not any("INSERT INTO ai_factory_items" in sql for sql, _ in db.conn.calls)
    assert db.committed_calls == []
    assert db.rollback_count == 1
    db.conn.assert_done()


def test_candidate_serialization_failure_rolls_back_earlier_items_in_same_batch():
    db = _ScriptedDb(
        [
            ("ai_factory_item_capacity", _Result()),
            *_attempt_lineage_steps(
                {
                    "id": "attempt-1",
                    "job_id": "job-1",
                    "status": "running",
                    "candidate_count": 2,
                    "job_invalidated_at": None,
                    "source_withdrawn_at": None,
                }
            ),
            ("SELECT COUNT(*) FROM ai_factory_items", _Result(scalar=0)),
            ("INSERT INTO ai_factory_items", _Result()),
        ]
    )

    with pytest.raises(TypeError):
        store.complete_attempt(
            db,
            "manager",
            "attempt-1",
            payloads=[{"candidate": 1}, {"not_json": object()}],
            response_sha256="response-sha",
            response_bytes=100,
        )

    assert any("INSERT INTO ai_factory_items" in sql for sql, _ in db.conn.calls)
    assert db.committed_calls == []
    assert db.rollback_count == 1
    db.conn.assert_done()


def test_complete_success_commits_items_states_audit_and_actual_usage_ledger_together():
    db = _ScriptedDb(
        [
            ("ai_factory_item_capacity", _Result()),
            *_attempt_lineage_steps(),
            ("SELECT COUNT(*) FROM ai_factory_items", _Result(scalar=0)),
            ("INSERT INTO ai_factory_items", _Result()),
            ("INSERT INTO ai_factory_items", _Result()),
            (("UPDATE ai_factory_attempts", "status='succeeded'"), _Result()),
            (("UPDATE ai_factory_jobs", "status='awaiting_review'"), _Result()),
            ("INSERT INTO ai_training_audit", _Result()),
            ("INSERT INTO ai_fund_usage_logs", _Result()),
        ]
    )

    result = store.complete_attempt(
        db,
        "manager",
        "attempt-1",
        payloads=[{"candidate": 1}, {"candidate": 2}],
        response_sha256="response-sha",
        response_bytes=321,
        usage=_actual_usage(),
    )

    assert result["discarded"] is False
    assert len(result["items"]) == 2
    state_sql = "\n".join(sql for sql, _ in db.committed_calls)
    assert "status='succeeded'" in state_sql
    assert "status='awaiting_review'" in state_sql
    attempt_sql, attempt_params = next(
        call
        for call in db.committed_calls
        if "UPDATE ai_factory_attempts" in call[0] and "status='succeeded'" in call[0]
    )
    assert "resolved_provider_model=:resolved_provider_model" in attempt_sql
    assert attempt_params["provider_request_id"] == "provider-request-123"
    assert attempt_params["resolved_provider_model"] == "gemini-resolved-202607"
    ledger_sql, ledger_params = db.committed_calls[-1]
    assert "INSERT INTO ai_fund_usage_logs" in ledger_sql
    assert ledger_params["user"] == "manager"
    assert ledger_params["feature"] == "data_factory_generation"
    assert ledger_params["status"] == "success"
    assert ledger_params["model"] == "Gemini 3.5 Flash"
    assert ledger_params["provider"] == "gemini"
    assert ledger_params["operation_id"] == "job-1"
    assert ledger_params["operation_stage"] == "attempt_1"
    assert ledger_params["hkd"] == pytest.approx(0.78)
    assert ledger_params["source"] == "provider_actual"
    assert _locked_lineage_tables(db.conn.calls) == [
        "ai_factory_sources",
        "ai_factory_jobs",
        "ai_factory_attempts",
    ]
    assert db.transaction_count == 1
    assert db.rollback_count == 0
    assert db.committed_calls == db.conn.calls
    db.conn.assert_done()


def test_complete_ledger_insert_failure_rolls_back_items_and_success_states():
    db = _ScriptedDb(
        [
            ("ai_factory_item_capacity", _Result()),
            *_attempt_lineage_steps(),
            ("SELECT COUNT(*) FROM ai_factory_items", _Result(scalar=0)),
            ("INSERT INTO ai_factory_items", _Result()),
            ("INSERT INTO ai_factory_items", _Result()),
            (("UPDATE ai_factory_attempts", "status='succeeded'"), _Result()),
            (("UPDATE ai_factory_jobs", "status='awaiting_review'"), _Result()),
            ("INSERT INTO ai_training_audit", _Result()),
            ("INSERT INTO ai_fund_usage_logs", RuntimeError("ledger unavailable")),
        ]
    )

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        store.complete_attempt(
            db,
            "manager",
            "attempt-1",
            payloads=[{"candidate": 1}, {"candidate": 2}],
            response_sha256="response-sha",
            response_bytes=321,
            usage=_actual_usage(),
        )

    attempted_sql = "\n".join(sql for sql, _ in db.conn.calls)
    assert "INSERT INTO ai_factory_items" in attempted_sql
    assert "status='succeeded'" in attempted_sql
    assert "status='awaiting_review'" in attempted_sql
    assert db.committed_calls == []
    assert db.rollback_count == 1
    db.conn.assert_done()


def test_failed_provider_call_commits_failed_states_and_actual_usage_ledger_together():
    db = _ScriptedDb(
        [
            *_attempt_lineage_steps(),
            (("UPDATE ai_factory_attempts", "status='failed'"), _Result()),
            ("UPDATE ai_factory_jobs", _Result()),
            ("INSERT INTO ai_fund_usage_logs", _Result()),
        ]
    )

    store.fail_attempt(
        db,
        "manager",
        "attempt-1",
        error_code="provider_timeout",
        response_sha256="partial-response-sha",
        response_bytes=99,
        provider_called=True,
        usage=_actual_usage(),
    )

    attempt_update_sql, attempt_update_params = next(
        call for call in db.committed_calls if "UPDATE ai_factory_attempts" in call[0]
    )
    assert "status='failed'" in attempt_update_sql
    assert attempt_update_params["error"] == "provider_timeout"
    assert attempt_update_params["bytes"] == 99
    assert attempt_update_params["provider_request_id"] == "provider-request-123"
    assert attempt_update_params["resolved_provider_model"] == (
        "gemini-resolved-202607"
    )
    job_update_sql = next(
        sql for sql, _ in db.committed_calls if "UPDATE ai_factory_jobs" in sql
    )
    assert "THEN 'failed' ELSE 'invalidated'" in job_update_sql
    ledger_sql, ledger_params = db.committed_calls[-1]
    assert "INSERT INTO ai_fund_usage_logs" in ledger_sql
    assert ledger_params["feature"] == "data_factory_generation"
    assert ledger_params["status"] == "failed"
    assert ledger_params["error"] == "provider_timeout"
    assert ledger_params["model"] == "Gemini 3.5 Flash"
    assert ledger_params["provider"] == "gemini"
    assert ledger_params["operation_id"] == "job-1"
    assert ledger_params["operation_stage"] == "attempt_1"
    assert ledger_params["hkd"] == pytest.approx(0.78)
    assert _locked_lineage_tables(db.conn.calls) == [
        "ai_factory_sources",
        "ai_factory_jobs",
        "ai_factory_attempts",
    ]
    assert db.transaction_count == 1
    assert db.rollback_count == 0
    assert db.committed_calls == db.conn.calls
    db.conn.assert_done()


def test_failed_provider_ledger_insert_failure_rolls_back_failed_states():
    db = _ScriptedDb(
        [
            *_attempt_lineage_steps(),
            (("UPDATE ai_factory_attempts", "status='failed'"), _Result()),
            ("UPDATE ai_factory_jobs", _Result()),
            ("INSERT INTO ai_fund_usage_logs", RuntimeError("ledger unavailable")),
        ]
    )

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        store.fail_attempt(
            db,
            "manager",
            "attempt-1",
            error_code="provider_timeout",
            provider_called=True,
            usage=_actual_usage(),
        )

    attempted_sql = "\n".join(sql for sql, _ in db.conn.calls)
    assert "status='failed'" in attempted_sql
    assert "UPDATE ai_factory_jobs" in attempted_sql
    assert db.committed_calls == []
    assert db.rollback_count == 1
    db.conn.assert_done()


def test_submission_withdrawal_soft_invalidates_the_complete_lineage_and_audits():
    conn = _ScriptedConnection(
        [
            ("origin_submission_id=:submission_id", _Result(rows=[("source-1",)])),
            (("FROM ai_factory_sources", "ORDER BY id FOR UPDATE"), _Result(rows=[("source-1",)])),
            ("UPDATE ai_factory_sources SET withdrawn_by", _Result()),
            (("SELECT id FROM ai_factory_jobs", "ORDER BY source_id,id FOR UPDATE"), _Result(rows=[("job-1",)])),
            (("UPDATE ai_factory_jobs", "status='invalidated'"), _Result()),
            (("SELECT id FROM ai_factory_attempts", "ORDER BY job_id,id FOR UPDATE"), _Result(rows=[("attempt-1",)])),
            (("UPDATE ai_factory_attempts", "status='discarded'"), _Result()),
            (("SELECT id FROM ai_factory_items", "ORDER BY job_id,id FOR UPDATE"), _Result(rows=[("item-1",)])),
            ("UPDATE ai_factory_items SET invalidated_by", _Result()),
            (("SELECT r.id FROM ai_factory_releases", "ORDER BY r.id FOR UPDATE OF r"), _Result(rows=[("rag-v000001",)])),
            ("UPDATE ai_factory_releases SET invalidated_by", _Result()),
            ("INSERT INTO ai_training_audit", _Result()),
            ("INSERT INTO ai_training_audit", _Result()),
        ]
    )
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)

    result = store.withdraw_submission_sources_in_transaction(
        conn, "manager", 91, "投稿人撤回", now=now
    )

    assert result == {
        "sources": ["source-1"],
        "items": ["item-1"],
        "releases": ["rag-v000001"],
    }
    sql_text = "\n".join(sql for sql, _ in conn.calls)
    assert "DELETE FROM" not in sql_text.upper()
    assert "content_text=" not in sql_text
    assert "original_json=" not in sql_text
    assert "jsonl_text=" not in sql_text
    assert "status='discarded'" in sql_text
    assert "status='claimed'" in sql_text
    assert "status IN ('claimed','running')" not in sql_text
    assert "status='invalidated'" in sql_text
    audit_actions = [
        params["action"]
        for sql, params in conn.calls
        if "INSERT INTO ai_training_audit" in sql
    ]
    assert audit_actions == [
        "factory_release_invalidated",
        "factory_source_withdrawn",
    ]
    assert _locked_lineage_tables(conn.calls) == [
        "ai_factory_sources",
        "ai_factory_jobs",
        "ai_factory_attempts",
        "ai_factory_items",
        "ai_factory_releases",
    ]
    conn.assert_done()


@pytest.mark.parametrize("review_status", ("pending", "rejected"))
def test_withdraw_item_rejects_non_approved_review_states(review_status):
    db = _ScriptedDb(
        [
            (
                ("SELECT id,review_status,invalidated_at", "FOR UPDATE"),
                _Result(
                    row={
                        "id": "item-1",
                        "review_status": review_status,
                        "invalidated_at": None,
                    }
                ),
            )
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        store.withdraw_item(db, "manager", "item-1", "資料內容有誤")

    assert error.value.status_code == 409
    assert "只可撤回已批准資料" in str(error.value)
    assert not any("UPDATE ai_factory_items" in sql for sql, _ in db.conn.calls)
    assert db.rollback_count == 1
    db.conn.assert_done()


def test_explicitly_invalidated_release_is_never_returned_for_download():
    release = pd.DataFrame(
        [{"id": "rag-v000001", "invalidated_at": datetime.now(timezone.utc)}]
    )
    db = _ScriptedDb(query_frames=[release])

    with pytest.raises(store.FactoryStoreError) as error:
        store.get_release_for_download(db, "rag-v000001")

    assert error.value.status_code == 410
    assert len(db.query_calls) == 1


def test_release_download_rechecks_withdrawn_descendants_even_if_release_row_is_live():
    release = pd.DataFrame([{"id": "rag-v000001", "invalidated_at": None}])
    invalid_lineage = pd.DataFrame([{"invalid": 1}])
    db = _ScriptedDb(query_frames=[release, invalid_lineage])

    with pytest.raises(store.FactoryStoreError) as error:
        store.get_release_for_download(db, "rag-v000001")

    assert error.value.status_code == 410
    lineage_sql = db.query_calls[1][0]
    assert "i.invalidated_at IS NOT NULL" in lineage_sql
    assert "j.invalidated_at IS NOT NULL" in lineage_sql
    assert "s.withdrawn_at IS NOT NULL" in lineage_sql


def test_source_inventory_cap_is_locked_and_fails_before_insert():
    db = _ScriptedDb(
        [
            ("ai_factory_source_capacity", _Result()),
            (
                "SELECT COUNT(*) FROM ai_factory_sources",
                _Result(scalar=store.AI_FACTORY_SOURCE_MAX_TOTAL),
            ),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        store.create_pasted_source(
            db,
            "manager",
            title="來源",
            content_text="內容",
            source_note="自行撰寫",
            rights_basis="own_work",
            language_code="yue-Hant-HK",
        )

    assert error.value.status_code == 409
    assert not any("INSERT INTO ai_factory_sources" in sql for sql, _ in db.conn.calls)
    assert db.committed_calls == []
    db.conn.assert_done()


def test_submission_snapshot_preserves_the_submitters_confirmation_timestamp(
    monkeypatch,
):
    submitted_at = datetime(2026, 6, 3, 9, 15)
    submission = {
        "id": 91,
        "submitted_by": "member-1",
        "data_type": "speech",
        "title": "演辭",
        "topic_text": "辯題",
        "side": "pro",
        "content_text": "呢段係已確認可用嘅投稿內容。",
        "source_note": "本人原創",
        "status": "accepted",
        "anonymized": True,
        "permission_confirmed": True,
        "created_at": submitted_at,
    }
    db = _ScriptedDb(
        [
            ("ai_factory_source_capacity", _Result()),
            (("FROM llm_training_submissions", "created_at"), _Result(row=submission)),
            (("FROM ai_factory_sources", "origin_submission_id=:id"), _Result(row=None)),
            ("SELECT COUNT(*) FROM ai_factory_sources", _Result(scalar=0)),
            ("INSERT INTO ai_factory_sources", _Result()),
            ("INSERT INTO ai_training_audit", _Result()),
        ],
        query_frames=[pd.DataFrame([{"id": "source-1"}])],
    )
    monkeypatch.setattr(store, "new_id", lambda _prefix: "source-1")

    result = store.snapshot_submission_source(db, "manager", 91)

    assert result["id"] == "source-1"
    _sql, params = next(
        call for call in db.committed_calls if "INSERT INTO ai_factory_sources" in call[0]
    )
    assert params["rights_actor"] == "member-1"
    assert params["rights_confirmed_at"] == submitted_at
    assert params["now"] != submitted_at
    db.conn.assert_done()


def test_job_inventory_cap_is_locked_and_fails_before_insert():
    db = _ScriptedDb(
        [
            ("FROM ai_factory_sources", _Result(row={"id": "source-1", "withdrawn_at": None})),
            ("ai_factory_job_capacity", _Result()),
            (
                "SELECT COUNT(*) FROM ai_factory_jobs",
                _Result(scalar=store.AI_FACTORY_JOB_MAX_TOTAL),
            ),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        store.create_or_refresh_job_preview(
            db,
            "manager",
            source_id="source-1",
            recipe_key="rag_knowledge_card_v1",
            requested_count=2,
            instruction_text="",
            preview_model_label="Gemini 3.5 Flash",
            preview_provider="gemini",
            preview_provider_model="gemini-3.5-flash",
            preview_prompt_sha256="prompt-sha",
            preview_input_sha256="input-sha",
            preview_sha256="preview-sha",
            preview_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    assert error.value.status_code == 409
    assert not any("INSERT INTO ai_factory_jobs" in sql for sql, _ in db.conn.calls)
    assert db.committed_calls == []
    db.conn.assert_done()


def test_retry_preview_transitions_failed_job_back_to_draft():
    db = _ScriptedDb(
        [
            ("FROM ai_factory_sources", _Result(row={"id": "source-1", "withdrawn_at": None})),
            ("FROM ai_factory_jobs", _Result(row=_job_row(status="failed"))),
            (("UPDATE ai_factory_jobs SET", "status='draft'"), _Result()),
        ],
        query_frames=[pd.DataFrame([{"id": "job-1", "status": "draft"}])],
    )

    result = store.create_or_refresh_job_preview(
        db,
        "manager",
        source_id="source-1",
        recipe_key="rag_knowledge_card_v1",
        requested_count=2,
        instruction_text="",
        preview_model_label="Gemini 3.5 Flash",
        preview_provider="gemini",
        preview_provider_model="gemini-3.5-flash",
        preview_prompt_sha256="new-prompt-sha",
        preview_input_sha256="new-input-sha",
        preview_sha256="new-preview-sha",
        preview_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        job_id="job-1",
    )

    assert result["status"] == "draft"
    assert db.rollback_count == 0
    db.conn.assert_done()


def test_provider_start_rechecks_source_job_and_attempt_under_matching_locks():
    db = _ScriptedDb(
        [
            ("FROM ai_factory_attempts a", _Result(row={"job_id": "job-1", "source_id": "source-1"})),
            (("FROM ai_factory_sources", "FOR UPDATE"), _Result(row={"id": "source-1", "withdrawn_at": None})),
            (("FROM ai_factory_jobs", "FOR UPDATE"), _Result(row={"id": "job-1", "source_id": "source-1", "invalidated_at": None})),
            (("FROM ai_factory_attempts", "FOR UPDATE"), _Result(row={"id": "attempt-1", "job_id": "job-1", "status": "claimed"})),
            (("UPDATE ai_factory_attempts", "status='running'"), _Result()),
        ]
    )

    store.mark_provider_started(db, "attempt-1")

    assert db.rollback_count == 0
    assert "FROM ai_factory_sources" in db.conn.calls[1][0]
    assert "FROM ai_factory_jobs" in db.conn.calls[2][0]
    assert "FROM ai_factory_attempts" in db.conn.calls[3][0]
    db.conn.assert_done()


def test_provider_start_stops_before_send_when_source_withdrawal_won_the_lock():
    db = _ScriptedDb(
        [
            ("FROM ai_factory_attempts a", _Result(row={"job_id": "job-1", "source_id": "source-1"})),
            (("FROM ai_factory_sources", "FOR UPDATE"), _Result(row={"id": "source-1", "withdrawn_at": datetime.now(timezone.utc)})),
            (("FROM ai_factory_jobs", "FOR UPDATE"), _Result(row={"id": "job-1", "source_id": "source-1", "invalidated_at": datetime.now(timezone.utc)})),
            (("FROM ai_factory_attempts", "FOR UPDATE"), _Result(row={"id": "attempt-1", "job_id": "job-1", "status": "discarded"})),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        store.mark_provider_started(db, "attempt-1")

    assert error.value.status_code == 410
    assert "沒有呼叫 AI provider" in str(error.value)
    assert not any("UPDATE ai_factory_attempts" in sql for sql, _ in db.conn.calls)
    assert db.rollback_count == 1
    db.conn.assert_done()


def test_item_inventory_cap_rejects_the_whole_batch_before_insert():
    db = _ScriptedDb(
        [
            ("ai_factory_item_capacity", _Result()),
            *_attempt_lineage_steps(
                {
                    "id": "attempt-1",
                    "job_id": "job-1",
                    "status": "running",
                    "candidate_count": 2,
                    "job_invalidated_at": None,
                    "source_withdrawn_at": None,
                }
            ),
            (
                "SELECT COUNT(*) FROM ai_factory_items",
                _Result(scalar=store.AI_FACTORY_ITEM_MAX_TOTAL - 1),
            ),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        store.complete_attempt(
            db,
            "manager",
            "attempt-1",
            payloads=[{"candidate": 1}, {"candidate": 2}],
            response_sha256="response-sha",
            response_bytes=100,
        )

    assert error.value.status_code == 409
    assert not any("INSERT INTO ai_factory_items" in sql for sql, _ in db.conn.calls)
    assert db.committed_calls == []
    db.conn.assert_done()


def test_topic_tag_normalisation_rejects_casefold_expansion_past_limit():
    label = "ß" * store.AI_FACTORY_TOPIC_TAG_MAX_CHARS

    with pytest.raises(store.FactoryStoreError) as error:
        store._normalise_tag(label)

    assert error.value.status_code == 400
    assert "正規化後長度" in str(error.value)


def test_topic_tag_inventory_cap_rolls_back_the_item_approval():
    payload = {"title": "已人工核對"}
    payload_sha = store.sha256_text(store.canonical_json(payload))
    db = _ScriptedDb(
        [
            *_item_lineage_steps(
                {
                    "id": "item-1",
                    "job_id": "job-1",
                    "review_status": "pending",
                    "invalidated_at": None,
                    "job_invalidated_at": None,
                    "source_withdrawn_at": None,
                }
            ),
            ("SELECT pg_advisory_xact_lock(hashtext(:key))", _Result()),
            ("COALESCE(reviewed_sha256,original_sha256)=:sha", _Result()),
            ("UPDATE ai_factory_items SET", _Result()),
            ("ai_factory_topic_tag_capacity", _Result()),
            ("WHERE normalized_label=:normalized", _Result()),
            (
                "SELECT COUNT(*) FROM ai_factory_topic_tags",
                _Result(scalar=store.AI_FACTORY_TOPIC_TAG_MAX_TOTAL),
            ),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        store.review_item(
            db,
            "manager",
            "item-1",
            decision="approved",
            reviewed_payload=payload,
            reviewed_sha256=payload_sha,
            note="",
            topic_tags=["交通"],
        )

    assert error.value.status_code == 409
    approved_hash_lock = next(
        params
        for sql, params in db.conn.calls
        if params.get("key", "").startswith("ai_factory_approved_hash:")
    )
    assert approved_hash_lock["key"] == f"ai_factory_approved_hash:{payload_sha}"
    duplicate_sql = next(
        sql
        for sql, _ in db.conn.calls
        if "COALESCE(reviewed_sha256,original_sha256)=:sha" in sql
    )
    assert "invalidated_at" not in duplicate_sql
    assert _locked_lineage_tables(db.conn.calls)[:3] == [
        "ai_factory_sources",
        "ai_factory_jobs",
        "ai_factory_items",
    ]
    assert db.committed_calls == []
    assert db.rollback_count == 1
    db.conn.assert_done()


def test_release_inventory_cap_is_locked_before_reading_or_publishing_items():
    db = _ScriptedDb(
        [
            ("SELECT pg_advisory_xact_lock(hashtext(:key))", _Result()),
            (
                "SELECT COUNT(*) FROM ai_factory_releases",
                _Result(scalar=store.AI_FACTORY_RELEASE_MAX_TOTAL),
            ),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        store.create_release(
            db,
            "manager",
            release_kind="rag",
            item_ids=["item-1"],
            schema_version="rag-v1",
            jsonl_text='{"title":"資料"}\n',
            manifest={},
            jsonl_line_hashes=["line-sha"],
            item_hashes=["item-sha"],
        )

    assert error.value.status_code == 409
    assert db.conn.calls[0][1]["key"] == "ai_factory_release_capacity"
    assert not any("INSERT INTO ai_factory_releases" in sql for sql, _ in db.conn.calls)
    assert db.committed_calls == []
    db.conn.assert_done()


def test_create_release_locks_all_lineages_in_canonical_deterministic_order():
    db = _ScriptedDb(
        [
            ("SELECT pg_advisory_xact_lock(hashtext(:key))", _Result()),
            ("SELECT COUNT(*) FROM ai_factory_releases", _Result(scalar=0)),
            (
                ("FROM ai_factory_items i", "JOIN ai_factory_jobs j", "ORDER BY i.id"),
                _Result(
                    rows=[
                        {"id": "item-1", "job_id": "job-1", "source_id": "source-1"}
                    ]
                ),
            ),
            (
                ("FROM ai_factory_sources", "ORDER BY id FOR UPDATE"),
                _Result(rows=[{"id": "source-1", "withdrawn_at": None}]),
            ),
            (
                ("FROM ai_factory_jobs", "ORDER BY id FOR UPDATE"),
                _Result(
                    rows=[
                        {
                            "id": "job-1",
                            "source_id": "source-1",
                            "recipe_key": "rag_knowledge_card_v1",
                            "invalidated_at": None,
                        }
                    ]
                ),
            ),
            (
                ("FROM ai_factory_items", "ORDER BY id FOR UPDATE"),
                _Result(
                    rows=[
                        {
                            "id": "item-1",
                            "job_id": "job-1",
                            "item_sha": "item-sha",
                            "review_status": "pending",
                            "invalidated_at": None,
                        }
                    ]
                ),
            ),
        ]
    )

    with pytest.raises(store.FactoryStoreError) as error:
        store.create_release(
            db,
            "manager",
            release_kind="rag",
            item_ids=["item-1"],
            schema_version="rag-v1",
            jsonl_text='{"title":"資料"}\n',
            manifest={},
            jsonl_line_hashes=["line-sha"],
            item_hashes=["item-sha"],
        )

    assert error.value.status_code == 409
    assert _locked_lineage_tables(db.conn.calls) == [
        "ai_factory_sources",
        "ai_factory_jobs",
        "ai_factory_items",
    ]
    assert db.rollback_count == 1
    db.conn.assert_done()
