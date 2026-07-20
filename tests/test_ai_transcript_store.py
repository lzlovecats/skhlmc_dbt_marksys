"""Atomic store contracts for full-transcript structure processing."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from core import ai_transcript_store as store
from core.ai_factory_store import FactoryStoreError, sha256_text


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

    def scalar(self):
        return None if self._scalar is _MISSING else self._scalar


class _Connection:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        values = params or {}
        self.calls.append((sql, values))
        if not self.steps:
            raise AssertionError(f"unexpected SQL: {sql}")
        expected, result = self.steps.pop(0)
        expected_parts = (expected,) if isinstance(expected, str) else expected
        for part in expected_parts:
            assert part in sql, f"expected {part!r} in {sql}"
        return result


class _Db:
    def __init__(self, steps):
        self.conn = _Connection(steps)
        self.rollback_count = 0

    @contextmanager
    def transaction(self):
        try:
            yield self.conn
        except Exception:
            self.rollback_count += 1
            raise


def _segment_row(text="司儀開場。正方發言。"):
    return {
        "id": "segment-1",
        "run_id": "run-1",
        "transcript_id": "transcript-1",
        "review_status": "pending",
        "run_status": "awaiting_review",
        "invalidated_at": None,
        "title": "完整比賽",
        "topic_text": "測試辯題",
        "source_note": "已取得許可",
        "language_code": "yue-Hant-HK",
        "rights_basis": "permission",
        "rights_confirmed_by": "manager",
        "rights_confirmed_at": datetime.now(timezone.utc),
        "content_text": text,
        "original_json": _payload(text),
        "withdrawn_at": None,
    }


def _payload(text="司儀開場。正方發言。"):
    return {
        "sequence_no": 1,
        "start_offset": 0,
        "end_offset": 5,
        "quote": text[:5],
        "speaker_label": "司儀",
        "side": "neutral",
        "stage": "general",
        "full_text": text[:5],
        "confidence": 90,
        "review_items": [],
    }


def _review_lock_steps(text="司儀開場。正方發言。"):
    combined = _segment_row(text)
    return [
        (
            "SELECT run_id,transcript_id FROM ai_factory_transcript_segments",
            _Result(row={
                "run_id": combined["run_id"],
                "transcript_id": combined["transcript_id"],
            }),
        ),
        (
            ("SELECT * FROM ai_factory_transcripts", "FOR UPDATE"),
            _Result(row={
                "id": combined["transcript_id"],
                "title": combined["title"],
                "topic_text": combined["topic_text"],
                "source_note": combined["source_note"],
                "language_code": combined["language_code"],
                "rights_basis": combined["rights_basis"],
                "rights_confirmed_by": combined["rights_confirmed_by"],
                "rights_confirmed_at": combined["rights_confirmed_at"],
                "content_text": combined["content_text"],
                "withdrawn_at": combined["withdrawn_at"],
            }),
        ),
        (
            ("SELECT * FROM ai_factory_transcript_runs", "FOR UPDATE"),
            _Result(row={
                "id": combined["run_id"],
                "transcript_id": combined["transcript_id"],
                "status": combined["run_status"],
                "invalidated_at": combined["invalidated_at"],
            }),
        ),
        (
            ("SELECT * FROM ai_factory_transcript_segments", "FOR UPDATE"),
            _Result(row={
                "id": combined["id"],
                "run_id": combined["run_id"],
                "transcript_id": combined["transcript_id"],
                "review_status": combined["review_status"],
                "original_json": combined["original_json"],
            }),
        ),
    ]


def test_preview_creation_is_one_transaction_with_private_lineage(monkeypatch):
    steps = [
        ("pg_advisory_xact_lock", _Result()),
        ("COUNT(*) FROM ai_factory_transcripts", _Result(scalar=0)),
        ("COUNT(*) FROM ai_factory_transcript_runs", _Result(scalar=0)),
        ("INSERT INTO ai_factory_transcripts", _Result()),
        ("INSERT INTO ai_factory_transcript_runs", _Result()),
        ("INSERT INTO ai_factory_transcript_windows", _Result()),
        ("INSERT INTO ai_training_audit", _Result()),
    ]
    db = _Db(steps)
    monkeypatch.setattr(store, "new_id", lambda _prefix: "window-1")

    result = store.create_transcript_preview(
        db,
        "manager",
        transcript_id="transcript-1",
        run_id="run-1",
        title="完整比賽",
        topic_text="測試辯題",
        source_note="已取得許可",
        language_code="yue-Hant-HK",
        rights_basis="permission",
        content_text="司儀開場。",
        content_sha256=sha256_text("司儀開場。"),
        model_label="Gemini Test",
        provider="gemini",
        provider_model="gemini-test",
        prompt_version="v1",
        prompt_template_sha256="a" * 64,
        instruction_text="",
        manifest_sha256="b" * 64,
        preview_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        estimated_cost_hkd=0,
        window_previews=[{
            "ordinal": 1,
            "context_start": 0,
            "context_end": 5,
            "core_start": 0,
            "core_end": 5,
            "prompt_sha256": "c" * 64,
            "input_sha256": "d" * 64,
            "preview_sha256": "e" * 64,
        }],
    )

    assert result["transcript_id"] == "transcript-1"
    assert result["windows"][0]["id"] == "window-1"
    assert db.conn.steps == []


def test_confirmation_flags_fail_before_opening_a_transaction():
    db = _Db([])

    with pytest.raises(FactoryStoreError, match="確認匿名化"):
        store.confirm_transcript_run(
            db,
            "manager",
            "run-1",
            manifest_sha256="a" * 64,
            confirmation_version="v1",
            anonymization_confirmed=False,
            rights_confirmed=True,
            third_party_confirmed=True,
            pii_warning_count=0,
            pii_override_reason="",
        )

    assert db.conn.calls == []


def test_run_lineage_locks_transcript_before_run():
    db = _Db([
        (
            "SELECT transcript_id FROM ai_factory_transcript_runs",
            _Result(row={"transcript_id": "transcript-1"}),
        ),
        (
            ("SELECT * FROM ai_factory_transcripts", "FOR UPDATE"),
            _Result(row={"id": "transcript-1"}),
        ),
        (
            ("SELECT * FROM ai_factory_transcript_runs", "FOR UPDATE"),
            _Result(row={"id": "run-1", "transcript_id": "transcript-1"}),
        ),
    ])

    transcript, run = store._lock_transcript_run_lineage(db.conn, "run-1")

    assert transcript["id"] == "transcript-1"
    assert run["id"] == "run-1"
    assert db.conn.steps == []


def test_rejected_segment_requires_an_audit_note_before_transaction():
    db = _Db([])

    with pytest.raises(FactoryStoreError, match="填寫原因"):
        store.review_transcript_segment(
            db,
            "manager",
            "segment-1",
            decision="rejected",
            reviewed_payload=None,
            note="   ",
        )

    assert db.conn.calls == []


def test_transcript_withdrawal_requires_a_reason_before_transaction():
    db = _Db([])

    with pytest.raises(FactoryStoreError, match="撤回原因"):
        store.withdraw_transcript(db, "manager", "transcript-1", "   ")

    assert db.conn.calls == []


def test_transcript_withdrawal_atomically_invalidates_lineage_and_sources(monkeypatch):
    db = _Db([
        (
            ("FROM ai_factory_transcripts", "FOR UPDATE"),
            _Result(row={"id": "transcript-1", "withdrawn_at": None}),
        ),
        (
            ("FROM ai_factory_transcript_runs", "ORDER BY id FOR UPDATE"),
            _Result(rows=[
                {"id": "run-draft", "status": "draft"},
                {"id": "run-processing", "status": "processing"},
            ]),
        ),
        ("UPDATE ai_factory_transcripts", _Result()),
        (("UPDATE ai_factory_transcript_runs", "status='invalidated'"), _Result()),
        (
            (
                "FROM ai_factory_transcript_attempts",
                "status='claimed'",
                "FOR UPDATE",
            ),
            _Result(rows=[{"id": "attempt-1", "window_id": "window-1"}]),
        ),
        (("UPDATE ai_factory_transcript_attempts", "status='discarded'"), _Result()),
        (("UPDATE ai_factory_transcript_windows", "status='discarded'"), _Result()),
        (
            ("FROM ai_factory_transcript_segments", "approved_source_id IS NOT NULL"),
            _Result(rows=[{"approved_source_id": "source-1"}]),
        ),
        ("INSERT INTO ai_training_audit", _Result()),
    ])
    cascades = []

    def withdraw_sources(conn, actor, source_ids, reason, now):
        cascades.append((conn, actor, source_ids, reason, now))
        return {
            "sources": ["source-1"],
            "items": ["item-1"],
            "releases": ["rag-v000001"],
        }

    monkeypatch.setattr(store, "_withdraw_sources_in_transaction", withdraw_sources)

    result = store.withdraw_transcript(
        db,
        "manager",
        "transcript-1",
        "來源授權已撤回",
    )

    assert result["changed"] is True
    assert result["runs"] == ["run-draft", "run-processing"]
    assert result["claimed_attempts"] == ["attempt-1"]
    assert result["sources"] == ["source-1"]
    assert result["items"] == ["item-1"]
    assert result["releases"] == ["rag-v000001"]
    assert cascades[0][1:4] == (
        "manager",
        ["source-1"],
        "來源授權已撤回",
    )
    audit_sql, audit_params = db.conn.calls[-1]
    assert "INSERT INTO ai_training_audit" in audit_sql
    assert audit_params["action"] == "factory_transcript_withdrawn"
    assert db.conn.steps == []


def test_stale_running_window_is_reaped_in_lineage_order_and_accounted():
    now = datetime.now(timezone.utc)
    stale_at = now - timedelta(seconds=store.AI_FACTORY_PREVIEW_TTL_SECONDS + 1)
    db = _Db([
        (
            ("FROM ai_factory_transcript_attempts a", "ORDER BY a.id"),
            _Result(rows=[{"id": "attempt-1"}]),
        ),
        (
            ("FROM ai_factory_transcript_attempts a", "JOIN ai_factory_transcript_runs"),
            _Result(row={
                "run_id": "run-1",
                "window_id": "window-1",
                "transcript_id": "transcript-1",
            }),
        ),
        (
            ("SELECT * FROM ai_factory_transcripts", "FOR UPDATE"),
            _Result(row={
                "id": "transcript-1",
                "withdrawn_at": None,
                "content_text": "司儀開場。",
            }),
        ),
        (
            ("SELECT * FROM ai_factory_transcript_runs", "FOR UPDATE"),
            _Result(row={"id": "run-1", "invalidated_at": None}),
        ),
        (
            ("SELECT * FROM ai_factory_transcript_windows", "FOR UPDATE"),
            _Result(row={"id": "window-1", "ordinal": 1, "status": "processing"}),
        ),
        (
            ("SELECT * FROM ai_factory_transcript_attempts", "FOR UPDATE"),
            _Result(row={
                "id": "attempt-1",
                "run_id": "run-1",
                "window_id": "window-1",
                "attempt_no": 2,
                "status": "running",
                "provider_attempted_at": stale_at,
                "created_at": stale_at,
                "estimated_cost_hkd": 0.78,
                "confirmed_by": "original-manager",
                "model_label": "Gemini Test",
                "provider": "gemini",
            }),
        ),
        (("UPDATE ai_factory_transcript_attempts", "error_code='orphaned_attempt'"), _Result()),
        (("UPDATE ai_factory_transcript_windows", "error_code='orphaned_attempt'"), _Result()),
        (("UPDATE ai_factory_transcript_runs", "status='failed'"), _Result()),
        ("INSERT INTO ai_fund_usage_logs", _Result()),
    ])

    reaped = store._reap_stale_transcript_attempts(db.conn, now)

    assert reaped == 1
    locked_tables = [
        sql.split(" FROM ", 1)[1].split(" WHERE ", 1)[0].split()[0]
        for sql, _params in db.conn.calls
        if "SELECT * FROM ai_factory_" in sql and "FOR UPDATE" in sql
    ]
    assert locked_tables == [
        "ai_factory_transcripts",
        "ai_factory_transcript_runs",
        "ai_factory_transcript_windows",
        "ai_factory_transcript_attempts",
    ]
    ledger = db.conn.calls[-1][1]
    assert ledger["user"] == "original-manager"
    assert ledger["status"] == "failed"
    assert ledger["hkd"] == pytest.approx(0.78)
    assert ledger["operation_id"] == "run-1"
    assert ledger["operation_stage"] == "window_1_attempt_2"
    assert ledger["source"] == "factory_preview_estimate_orphaned_running"
    assert db.conn.steps == []


def test_discarded_completion_preserves_provider_lineage(monkeypatch):
    now = datetime.now(timezone.utc)
    db = _Db([
        ("ai_factory_transcript_segment_capacity", _Result()),
        (
            ("FROM ai_factory_transcript_attempts a", "JOIN ai_factory_transcript_runs"),
            _Result(row={
                "run_id": "run-1",
                "window_id": "window-1",
                "transcript_id": "transcript-1",
            }),
        ),
        (
            ("SELECT * FROM ai_factory_transcripts", "FOR UPDATE"),
            _Result(row={
                "id": "transcript-1",
                "withdrawn_at": now,
                "content_text": "司儀開場。",
            }),
        ),
        (
            ("SELECT * FROM ai_factory_transcript_runs", "FOR UPDATE"),
            _Result(row={"id": "run-1", "invalidated_at": now}),
        ),
        (
            ("SELECT * FROM ai_factory_transcript_windows", "FOR UPDATE"),
            _Result(row={"id": "window-1", "ordinal": 1, "status": "processing"}),
        ),
        (
            ("SELECT * FROM ai_factory_transcript_attempts", "FOR UPDATE"),
            _Result(row={
                "id": "attempt-1",
                "run_id": "run-1",
                "status": "running",
                "attempt_no": 1,
                "model_label": "Gemini Test",
                "provider": "gemini",
            }),
        ),
        (("UPDATE ai_factory_transcript_attempts", "status='discarded'"), _Result()),
        (("UPDATE ai_factory_transcript_windows", "status='discarded'"), _Result()),
    ])
    settled = []
    monkeypatch.setattr(store, "_settle_usage", lambda *args, **kwargs: settled.append((args, kwargs)))
    usage = {
        "provider_request_id": "provider-request-123",
        "resolved_provider_model": "gemini-resolved-202607",
    }

    result = store.complete_transcript_attempt(
        db,
        "manager",
        "attempt-1",
        boundaries=[],
        response_sha256="response-sha",
        response_bytes=123,
        usage=usage,
    )

    assert result == {"discarded": True, "done": False}
    attempt_sql, attempt_params = next(
        call for call in db.conn.calls
        if "UPDATE ai_factory_transcript_attempts" in call[0]
    )
    assert "provider_request_id=:request_id" in attempt_sql
    assert "resolved_provider_model=:resolved_model" in attempt_sql
    assert attempt_params["request_id"] == "provider-request-123"
    assert attempt_params["resolved_model"] == "gemini-resolved-202607"
    assert len(settled) == 1
    assert db.conn.steps == []


def test_invalid_review_quote_rolls_back_before_creating_a_child_source():
    db = _Db(_review_lock_steps())
    payload = _payload()
    payload["quote"] = "改寫內容"

    with pytest.raises(FactoryStoreError, match="does not match"):
        store.review_transcript_segment(
            db,
            "manager",
            "segment-1",
            decision="approved",
            reviewed_payload=payload,
        )

    assert db.rollback_count == 1
    assert not any("INSERT INTO ai_factory_sources" in sql for sql, _ in db.conn.calls)


def test_review_cannot_change_server_derived_speaking_order():
    db = _Db(_review_lock_steps())
    payload = _payload()
    payload["sequence_no"] = 2

    with pytest.raises(FactoryStoreError, match="發言次序"):
        store.review_transcript_segment(
            db,
            "manager",
            "segment-1",
            decision="approved",
            reviewed_payload=payload,
        )

    assert db.rollback_count == 1
    assert not any("INSERT INTO ai_factory_sources" in sql for sql, _ in db.conn.calls)


def test_review_cannot_change_server_derived_offsets():
    text = "司儀開場。正方發言。"
    db = _Db(_review_lock_steps(text))
    payload = _payload(text)
    payload.update({
        "start_offset": 1,
        "end_offset": 5,
        "quote": text[1:5],
        "full_text": text[1:5],
    })

    with pytest.raises(FactoryStoreError, match="原文邊界"):
        store.review_transcript_segment(
            db,
            "manager",
            "segment-1",
            decision="approved",
            reviewed_payload=payload,
        )

    assert db.rollback_count == 1
    assert not any("INSERT INTO ai_factory_sources" in sql for sql, _ in db.conn.calls)


def test_approval_creates_exact_child_source_and_finishes_review_run(monkeypatch):
    db = _Db([
        *_review_lock_steps(),
        ("ai_factory_source_capacity", _Result()),
        ("COUNT(*) FROM ai_factory_sources", _Result(scalar=0)),
        ("INSERT INTO ai_factory_sources", _Result()),
        ("UPDATE ai_factory_transcript_segments", _Result()),
        ("INSERT INTO ai_training_audit", _Result()),
        (("COUNT(*) FROM ai_factory_transcript_segments", "review_status='pending'"), _Result(scalar=0)),
        ("UPDATE ai_factory_transcript_runs", _Result()),
        ("INSERT INTO ai_training_audit", _Result()),
    ])
    ids = iter(("source-1", "source-group-1"))
    monkeypatch.setattr(store, "new_id", lambda _prefix: next(ids))

    result = store.review_transcript_segment(
        db,
        "manager",
        "segment-1",
        decision="approved",
        reviewed_payload=_payload(),
        note="已核對司儀身份",
    )

    source_insert = next(
        params for sql, params in db.conn.calls
        if "INSERT INTO ai_factory_sources" in sql
    )
    assert result["approved_source_id"] == "source-1"
    assert source_insert["content"] == _payload()["full_text"]
    assert source_insert["content_sha"] == sha256_text(_payload()["full_text"])
    source_lock = next(
        sql for sql, _params in db.conn.calls
        if "ai_factory_source_capacity" in sql
    )
    assert "pg_advisory_xact_lock" in source_lock
    segment_update = next(
        sql for sql, _params in db.conn.calls
        if "UPDATE ai_factory_transcript_segments" in sql
    )
    assert "start_offset=" not in segment_update
    assert "end_offset=" not in segment_update
    review_locks = [
        sql.split(" FROM ", 1)[1].split(" WHERE ", 1)[0].strip()
        for sql, _params in db.conn.calls
        if "SELECT * FROM ai_factory_transcript" in sql and "FOR UPDATE" in sql
    ]
    assert review_locks == [
        "ai_factory_transcripts",
        "ai_factory_transcript_runs",
        "ai_factory_transcript_segments",
    ]
    assert db.conn.steps == []
