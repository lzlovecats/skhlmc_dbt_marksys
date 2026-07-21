"""Transactional lifecycle regressions for fixed local-AI evaluations."""

from contextlib import contextmanager

import pytest

from core import lmc_ai_eval_store as store
from core.lmc_ai_eval import REVIEW_DIMENSIONS


_MISSING = object()


class _Result:
    def __init__(self, *, row=_MISSING, scalar=_MISSING, rowcount=0):
        self._row = row
        self._scalar = scalar
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return None if self._row is _MISSING else self._row

    def one(self):
        if self._row is _MISSING:
            raise AssertionError("expected one row")
        return self._row

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
        parts = (expected,) if isinstance(expected, str) else expected
        for part in parts:
            assert part in sql, f"expected {part!r} in SQL: {sql}"
        return result

    def assert_done(self):
        assert not self.steps


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


def test_manager_release_is_atomic_and_audited_without_reviewer_identity():
    db = _Db([
        ("SELECT status FROM ai_eval_campaigns", _Result(scalar="reviewing")),
        ("SELECT case_id,pair_key FROM ai_eval_reviews", _Result(row={
            "case_id": "case-1", "pair_key": "daily_complex",
        })),
        (("UPDATE ai_eval_reviews", "submitted_at IS NULL", "released_at IS NULL"), _Result(rowcount=1)),
        ("INSERT INTO ai_training_audit", _Result()),
    ])

    store.release_assignment(db, "campaign-1", "review-1", "manager", "stalled")

    audit_params = db.conn.calls[-1][1]
    assert "reviewer_user_id" not in str(audit_params)
    assert "review-1" in audit_params["details"]
    db.conn.assert_done()


def test_expired_assignment_submission_rolls_back_instead_of_counting_vote():
    row = {
        "campaign_id": "campaign-1", "submitted_at": None, "note": "",
        **{dimension: None for dimension in REVIEW_DIMENSIONS},
    }
    db = _Db([
        ("SELECT * FROM ai_eval_reviews", _Result(row=row)),
        ("SELECT status FROM ai_eval_campaigns", _Result(scalar="reviewing")),
        (("UPDATE ai_eval_reviews", "expires_at>NOW()", "released_at IS NULL"), _Result(rowcount=0)),
    ])

    with pytest.raises(ValueError, match="過期"):
        store.submit_review(
            db, "review-1", "reviewer",
            {dimension: "tie" for dimension in REVIEW_DIMENSIONS}, "",
        )

    assert db.rollback_count == 1
    db.conn.assert_done()


def _campaign(exported_at, *, status="closed"):
    return {
        "campaign_id": "campaign-1", "status": status, "suite_id": "suite",
        "suite_version": 1, "suite_hash": "a" * 64, "prompt_hash": "b" * 64,
        "persona_hash": "c" * 64, "model_profile_version": 2,
        "summary_hash": "d" * 64, "exported_at": exported_at,
        "exported_by": "manager" if exported_at else None,
    }


def test_closed_campaign_purge_fails_until_export_has_been_recorded():
    db = _Db([
        ("FROM ai_eval_campaigns", _Result(row=_campaign(None))),
    ])

    with pytest.raises(ValueError, match="先下載"):
        store.purge_campaign(
            db, "campaign-1", "manager", "campaign-1", "retention limit",
        )

    assert not any("DELETE FROM" in sql for sql, _params in db.conn.calls)
    assert db.rollback_count == 1
    db.conn.assert_done()


def test_invalidated_campaign_purges_without_a_prior_export_and_keeps_audit_first():
    db = _Db([
        ("FROM ai_eval_campaigns", _Result(row=_campaign(None, status="invalidated"))),
        ("SELECT (SELECT COUNT(*) FROM ai_eval_outputs", _Result(row={"outputs": 90, "reviews": 0})),
        ("INSERT INTO ai_training_audit", _Result()),
        ("DELETE FROM ai_eval_reviews", _Result()),
        ("DELETE FROM ai_eval_outputs", _Result()),
        ("DELETE FROM ai_eval_campaigns", _Result()),
    ])

    result = store.purge_campaign(
        db, "campaign-1", "manager", "campaign-1", "invalidated run is no longer needed",
    )

    sql = [statement for statement, _params in db.conn.calls]
    assert sql.index(next(item for item in sql if "INSERT INTO ai_training_audit" in item)) < sql.index(
        next(item for item in sql if "DELETE FROM ai_eval_reviews" in item)
    )
    assert result == {"campaign_id": "campaign-1", "outputs": 90, "reviews": 0}
    db.conn.assert_done()


def test_exported_terminal_campaign_purges_children_and_keeps_audit_first():
    db = _Db([
        ("FROM ai_eval_campaigns", _Result(row=_campaign("2026-07-21T10:00:00+00:00"))),
        ("SELECT (SELECT COUNT(*) FROM ai_eval_outputs", _Result(row={"outputs": 90, "reviews": 270})),
        ("INSERT INTO ai_training_audit", _Result()),
        ("DELETE FROM ai_eval_reviews", _Result()),
        ("DELETE FROM ai_eval_outputs", _Result()),
        ("DELETE FROM ai_eval_campaigns", _Result()),
    ])

    result = store.purge_campaign(
        db, "campaign-1", "manager", "campaign-1", "retention limit",
    )

    sql = [statement for statement, _params in db.conn.calls]
    assert sql.index(next(item for item in sql if "INSERT INTO ai_training_audit" in item)) < sql.index(
        next(item for item in sql if "DELETE FROM ai_eval_reviews" in item)
    )
    assert result == {"campaign_id": "campaign-1", "outputs": 90, "reviews": 270}
    db.conn.assert_done()
