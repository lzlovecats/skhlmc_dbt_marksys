"""Cross-layer regressions for the competition-result path."""

from contextlib import AbstractContextManager
from io import BytesIO
from pathlib import Path

import pandas as pd
from pypdf import PdfReader

from core.judging_logic import submit_final_scores
from core.results_logic import RANK_COLUMNS, _best_debaters, judge_ranking
from score_sheet_pdf import _build_ranks, build_score_sheet_pdf
from scoring import COHERENCE_MAX, FREE_DEBATE_CRITERIA, SPEECH_CRITERIA, free_debate_col, speech_col


def _side_data():
    return {
        "team_name": "測試隊",
        "raw_df_a": [
            {
                "辯位": role,
                "姓名": role,
                **{speech_col(item): 5 for item in SPEECH_CRITERIA},
            }
            for role in ("主辯", "一副", "二副", "結辯")
        ],
        "raw_df_b": [
            {free_debate_col(item): item["max"] for item in FREE_DEBATE_CRITERIA}
        ],
        "deduction": 0,
        "coherence": COHERENCE_MAX,
    }


class _RowResult:
    def __init__(self, values):
        self.values = values

    def fetchone(self):
        class Row:
            pass

        row = Row()
        row._mapping = self.values
        return row


class _Transaction(AbstractContextManager):
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc_value, traceback):
        self.session.exit_error = exc_value
        return False


class _FinalSession:
    def __init__(self, submitted=False):
        self.submitted = submitted
        self.calls = []
        self.exit_error = None

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params))
        if " AS submitted" in sql:
            return _RowResult({"submitted": self.submitted, "n": 1})
        return None


class _FinalDb:
    def __init__(self, submitted=False):
        self.session = _FinalSession(submitted=submitted)

    def transaction(self):
        return _Transaction(self.session)


def test_final_submission_writes_both_sides_totals_and_eight_speakers_in_one_transaction():
    db = _FinalDb()
    result = submit_final_scores("M1", " Judge ", _side_data(), _side_data(), db=db)
    assert result["ok"] is True
    assert db.session.exit_error is None
    assert len(db.session.calls) == 5
    assert "pg_advisory_xact_lock" in db.session.calls[0][0]
    assert "INSERT INTO score_drafts" in db.session.calls[2][0]
    assert len(db.session.calls[2][1]) == 2
    assert "INSERT INTO scores" in db.session.calls[3][0]
    assert "INSERT INTO debater_scores" in db.session.calls[4][0]
    assert len(db.session.calls[4][1]) == 8


def test_duplicate_final_submission_stops_before_any_score_write():
    db = _FinalDb(submitted=True)
    assert submit_final_scores("M1", "Judge", _side_data(), _side_data(), db=db) is False
    assert len(db.session.calls) == 2
    assert all("INSERT INTO" not in sql for sql, _ in db.session.calls)


class _RankingDb:
    def __init__(self, rankings):
        self.rankings = rankings

    def query(self, sql, params=None):
        if "FROM debaters" in sql:
            return pd.DataFrame(columns=["side", "position", "debater_name"])
        if "FROM best_debater_rankings" in sql:
            rows = self.rankings
            if params and params.get("judge_name"):
                rows = [row for row in rows if row["judge_name"] == params["judge_name"]]
            return pd.DataFrame(rows, columns=["judge_name", "side", "position", "rank"])
        raise AssertionError(sql)


def _score_frame():
    rows = []
    for judge in ("評判甲", "評判乙"):
        row = {"judge_name": judge}
        row.update({column: 80 - index * 5 for index, column in enumerate(RANK_COLUMNS)})
        rows.append(row)
    return pd.DataFrame(rows)


def _reverse_ranking(judge="評判甲"):
    slots = [
        (side, position)
        for side in ("con", "pro")
        for position in range(4, 0, -1)
    ]
    return [
        {"judge_name": judge, "side": side, "position": position, "rank": rank}
        for rank, (side, position) in enumerate(slots, 1)
    ]


def test_each_judge_independently_uses_submitted_or_derived_best_debater_ranking():
    rows, _ = _best_debaters("M1", _score_frame(), _RankingDb(_reverse_ranking()))
    by_role = {row["role"]: row for row in rows}
    # 甲 submitted reverse order (8), 乙 skipped and therefore derives (1).
    assert by_role["正方主辯"]["rank_sum"] == 9
    # 甲 submitted first (1), 乙 derives last (8).
    assert by_role["反方結辯"]["rank_sum"] == 9


def test_exact_best_debater_tie_is_left_for_the_judging_panel():
    scores = _score_frame()
    scores.loc[:, list(RANK_COLUMNS)] = 50
    scores.loc[:, "pro1_m"] = 80
    scores.loc[:, "con1_m"] = 80
    slots = [
        ("pro", 1), ("con", 1), ("pro", 2), ("pro", 3),
        ("pro", 4), ("con", 2), ("con", 3), ("con", 4),
    ]
    rankings = []
    for judge, first_two in (("評判甲", (1, 2)), ("評判乙", (2, 1))):
        assigned = [*first_two, 3, 4, 5, 6, 7, 8]
        rankings.extend(
            {"judge_name": judge, "side": side, "position": position, "rank": rank}
            for (side, position), rank in zip(slots, assigned)
        )

    _, best = _best_debaters("M1", scores, _RankingDb(rankings))
    assert best["is_tie"] is True
    assert best["tied_roles"] == ["正方主辯", "反方主辯"]
    assert best["rank_sum"] == 3
    assert best["average_score"] == 80


def test_non_finite_debater_scores_fail_closed_without_querying_rankings():
    scores = _score_frame().iloc[[0]].copy()
    scores[list(RANK_COLUMNS)] = scores[list(RANK_COLUMNS)].astype(float)
    scores.loc[:, "pro1_m"] = float("inf")

    class NoQueryDb:
        def query(self, *_args, **_kwargs):
            raise AssertionError("corrupt scores must fail before ranking queries")

    assert _best_debaters("M1", scores, NoQueryDb()) == (None, None)
    assert judge_ranking("M1", "評判甲", scores.iloc[0], NoQueryDb()) is None


def test_review_and_pdf_preserve_the_selected_judges_submitted_ranking():
    frame = _score_frame()
    db = _RankingDb(_reverse_ranking())
    ranking = judge_ranking("M1", "評判甲", frame.iloc[0], db)
    assert ranking["source"] == "submitted"
    assert ranking["正方"] == [8, 7, 6, 5]
    assert ranking["反方"] == [4, 3, 2, 1]

    pdf_ranks = _build_ranks(_side_data(), _side_data(), rankings=ranking)
    assert pdf_ranks == {"正方": [8, 7, 6, 5], "反方": [4, 3, 2, 1]}

    pdf = build_score_sheet_pdf(
        {
            "match_id": "M1",
            "match_date": "2026-07-14",
            "match_time": "14:30",
            "topic_text": "測試辯題",
        },
        {
            "judge_name": "評判甲",
            "pro_team": "正方測試隊",
            "con_team": "反方測試隊",
            "pro_total_score": 300,
            "con_total_score": 290,
        },
        _side_data(),
        _side_data(),
        rankings=ranking,
    )
    pages = PdfReader(BytesIO(pdf)).pages
    assert len(pages) == 2
    assert "正方測試隊" in (pages[0].extract_text() or "")
    assert "反方測試隊" in (pages[1].extract_text() or "")


def test_judge_state_requests_have_a_stale_response_guard():
    html = Path(__file__).resolve().parents[1] / "frontend" / "judging" / "index.html"
    source = html.read_text(encoding="utf-8")
    assert "stateRequestId" in source
    assert "requestId !== stateRequestId || expectedJudge !== S.judge" in source


def test_judge_save_responses_have_a_stale_response_guard():
    html = Path(__file__).resolve().parents[1] / "frontend" / "judging" / "index.html"
    source = html.read_text(encoding="utf-8")
    assert "expectedStateRequestId = stateRequestId" in source
    assert "expectedJudge !== S.judge" in source
    assert "expectedStateRequestId !== stateRequestId" in source


def test_judge_change_resets_dirty_state_and_old_draft_response_cannot_clear_new_state():
    script = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "shared"
        / "judging-ux.js"
    ).read_text(encoding="utf-8")
    assert "clearTimeout(judgeNameTimer);\n        judgeNameTimer = null;" in script
    assert 'dirtySides.clear();\n        manualSaveSide = "";' in script
    assert 'requestJudge = String(payload.judge_name || "").trim();' in script
    assert "response.ok && requestJudge === currentJudge" in script
