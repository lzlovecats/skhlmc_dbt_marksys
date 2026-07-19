"""Team score-sheet confirmation workflow regressions."""

from contextlib import contextmanager
from pathlib import Path

from core import score_confirmation


ROOT = Path(__file__).resolve().parents[1]


class _Result:
    def __init__(self, *, row=None, scalar=None, rowcount=1):
        self._row = row
        self._scalar = scalar
        self.rowcount = rowcount

    def fetchone(self):
        if self._row is None:
            return None
        return type("Row", (), {"_mapping": self._row})()

    def scalar(self):
        return self._scalar


class _OpenConnection:
    def __init__(self, score_count=2, incomplete_count=0):
        self.score_count = score_count
        self.incomplete_count = incomplete_count
        self.insert_params = None

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        if "pg_advisory_xact_lock" in sql:
            return _Result()
        if "select match_id,pro_team,con_team" in sql:
            return _Result(row={"match_id": "M1", "pro_team": "甲", "con_team": "乙"})
        if "as score_count" in sql:
            return _Result(scalar=self.score_count)
        if "as incomplete_count" in sql:
            return _Result(scalar=self.incomplete_count)
        if sql.startswith("insert into score_sheet_confirmations"):
            self.insert_params = params
            return _Result()
        raise AssertionError(sql)


class _OpenDb:
    def __init__(self, score_count=2, incomplete_count=0):
        self.connection = _OpenConnection(score_count, incomplete_count)

    @contextmanager
    def transaction(self):
        yield self.connection


class _RespondConnection:
    def __init__(self, *, opened_count=2, current_count=2, status="pending"):
        self.opened_count = opened_count
        self.current_count = current_count
        self.status = status
        self.update_params = None

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        if sql.startswith("select match_id from score_sheet_confirmations"):
            return _Result(row={"match_id": "M1"})
        if "for update" in sql:
            return _Result(row={
                "match_id": "M1",
                "status": self.status,
                "opened_score_count": self.opened_count,
            })
        if "pg_advisory_xact_lock" in sql:
            return _Result()
        if sql.startswith("select count(*) from scores"):
            return _Result(scalar=self.current_count)
        if sql.startswith("update score_sheet_confirmations"):
            self.update_params = params
            return _Result(rowcount=1)
        raise AssertionError(sql)


class _RespondDb:
    def __init__(self, **kwargs):
        self.connection = _RespondConnection(**kwargs)

    @contextmanager
    def transaction(self):
        yield self.connection


def test_opening_confirmation_rotates_two_side_links_and_binds_score_count():
    db = _OpenDb(score_count=3)

    result = score_confirmation.open_confirmation("M1", db=db)

    assert result["ok"] is True
    assert result["score_count"] == 3
    assert set(result["links"]) == {"pro", "con"}
    assert result["links"]["pro"]["confirmation_token"] != result["links"]["con"]["confirmation_token"]
    assert db.connection.insert_params["score_count"] == 3
    assert db.connection.insert_params["pro_token"] == result["links"]["pro"]["confirmation_token"]


def test_opening_confirmation_requires_complete_submitted_score_sheets():
    assert "未有評判" in score_confirmation.open_confirmation(
        "M1", db=_OpenDb(score_count=0)
    )["message"]


def test_team_response_is_single_state_transition_bound_to_opened_score_count():
    current = _RespondDb(opened_count=2, current_count=2)
    result = score_confirmation.respond("token", "confirmed", db=current)
    assert result == {
        "ok": True,
        "status": "confirmed",
        "message": "已確認分紙無誤。",
    }
    assert current.connection.update_params["status"] == "confirmed"
    assert current.connection.update_params["reason"] == ""

    stale = score_confirmation.respond(
        "token", "disputed", "總分不符",
        db=_RespondDb(opened_count=2, current_count=3),
    )
    assert stale["ok"] is False and stale["reason"] == "stale"

    repeated = score_confirmation.respond(
        "token", "confirmed", db=_RespondDb(status="confirmed")
    )
    assert repeated["ok"] is False and repeated["reason"] == "responded"
    assert "細項" in score_confirmation.open_confirmation(
        "M1", db=_OpenDb(score_count=2, incomplete_count=1)
    )["message"]


def test_schema_migration_and_ui_cover_side_status_and_stale_score_count():
    schema = (ROOT / "schema.py").read_text(encoding="utf-8")
    up = (
        ROOT
        / "migrations"
        / "20260718_0001_score_sheet_confirmations.up.sql"
    ).read_text(encoding="utf-8")
    down = (
        ROOT
        / "migrations"
        / "20260718_0001_score_sheet_confirmations.down.sql"
    ).read_text(encoding="utf-8")
    page = (
        ROOT / "frontend" / "score_sheet_confirmation" / "index.html"
    ).read_text(encoding="utf-8")
    home = (ROOT / "frontend" / "home" / "index.html").read_text(
        encoding="utf-8"
    )

    for source in (schema, up):
        assert "score_sheet_confirmations" in source
        assert "opened_score_count" in source
        assert "pending" in source and "confirmed" in source and "disputed" in source
        assert "REVOKE ALL PRIVILEGES" in source
    assert "DROP TABLE IF EXISTS public.score_sheet_confirmations" in down
    assert 'id="confirmCorrect"' in page
    assert 'id="submitDispute"' in page
    assert "/api/score-sheet-confirmation/respond" in page
    assert 'href="/score-sheet-confirmation">✅ 核對比賽分紙' not in home


def test_match_management_can_refresh_score_submission_and_confirmation_state():
    page = (ROOT / "frontend" / "match_info" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="refreshScoreConfirmation"' in page
    assert '$("refreshScoreConfirmation").addEventListener("click"' in page
    assert "await refreshScoreConfirmation()" in page
    assert "renderScoreConfirmation()" in page


def test_rules_limit_fact_check_authority_to_factual_verification():
    rules = (ROOT / "assets" / "rules.md").read_text(encoding="utf-8")

    assert "Fact Check易" in rules
    assert "資料真確性核查" in rules
    assert "不影響評判分數、主席裁決或正式賽果" in rules
