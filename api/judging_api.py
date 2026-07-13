"""JSON API for the HTML judge score sheet.

The judge cookie is deliberately scoped to one match.  Every write compares the
requested match with that signed scope before passing data to the domain layer.
"""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/judging", tags=["judging"])
COOKIE_NAME = "judging_match"


class LoginBody(BaseModel):
    match_id: str = Field(max_length=200)
    password: str = Field(max_length=512)


class DraftBody(BaseModel):
    judge_name: str = Field(max_length=100)
    side: str = Field(max_length=10)
    score_data: dict


class FinalBody(BaseModel):
    judge_name: str = Field(max_length=100)
    pro_data: dict
    con_data: dict


class RankingsBody(BaseModel):
    judge_name: str = Field(max_length=100)
    rankings: list[dict] = Field(max_length=8)


def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()


def _match_scope(request: Request):
    from deploy.proxy import _verify_judging_token
    match_id = _verify_judging_token(request.cookies.get(COOKIE_NAME) or "")
    if not match_id:
        raise HTTPException(401, "請先驗證評判入場密碼。")
    return match_id


@router.get("/matches")
def matches():
    from core.judging_logic import matches_for_judging
    return {"matches": matches_for_judging(db=_db(), summaries=True)}


@router.get("/config")
def scoring_config():
    """Single browser-facing source of truth for score-sheet criteria and totals."""
    from scoring import (
        COHERENCE_MAX, FREE_DEBATE_CRITERIA, FREE_DEBATE_MAX, GRAND_TOTAL,
        SPEECH_CRITERIA, SPEECH_TOTAL_MAX, free_debate_col, speech_col,
    )
    return {
        "speech": [{**item, "column": speech_col(item)} for item in SPEECH_CRITERIA],
        "free_debate": [{**item, "column": free_debate_col(item)} for item in FREE_DEBATE_CRITERIA],
        "coherence_max": COHERENCE_MAX,
        "speech_total_max": SPEECH_TOTAL_MAX,
        "free_debate_max": FREE_DEBATE_MAX,
        "grand_total": GRAND_TOTAL,
    }


@router.post("/login")
def login(body: LoginBody, response: Response):
    from core.judging_logic import verify_match_access
    from deploy.proxy import _sign_judging_token
    result = verify_match_access(body.match_id, body.password, db=_db())
    if not result["ok"]:
        raise HTTPException(401, result["message"])
    token = _sign_judging_token(body.match_id)
    if not token:
        raise HTTPException(503, "登入服務暫時未能使用。")
    response.set_cookie(COOKIE_NAME, token, path="/", samesite="lax", httponly=True)
    return {"ok": True, "match_id": body.match_id}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/state")
def state(request: Request, judge_name: str = ""):
    from core.judging_logic import has_final_submission, load_drafts, matches_for_judging, normalize_judge_name
    match_id = _match_scope(request)
    database = _db()
    matches = matches_for_judging(db=database, match_id=match_id)
    match = matches[0] if matches else None
    if not match:
        raise HTTPException(404, "場次不存在。")
    judge = normalize_judge_name(judge_name)
    return {
        "match": match,
        "judge_name": judge,
        "submitted": has_final_submission(match_id, judge, db=database) if judge else False,
        "drafts": load_drafts(match_id, judge, db=database) if judge else {"正方": None, "反方": None},
    }


@router.post("/draft")
def save_draft(body: DraftBody, request: Request):
    from core.judging_logic import save_draft
    try:
        score_data = save_draft(_match_scope(request), body.judge_name, body.side, body.score_data, db=_db())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "score_data": score_data}


@router.post("/submit")
def submit(body: FinalBody, request: Request):
    from core.judging_logic import load_drafts, submit_final_scores
    match_id = _match_scope(request)
    database = _db()
    drafts = load_drafts(match_id, body.judge_name, db=database)
    if not drafts.get("正方") or not drafts.get("反方"):
        raise HTTPException(409, "請先分別完成正方及反方評分，並各自暫存。")
    try:
        result = submit_final_scores(match_id, body.judge_name, body.pro_data, body.con_data, db=database)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result:
        raise HTTPException(409, "你已提交過評分！無法再次提交！")
    return result


@router.post("/rankings")
def rankings(body: RankingsBody, request: Request):
    from core.judging_logic import submit_best_debater_rankings
    try:
        submit_best_debater_rankings(_match_scope(request), body.judge_name, body.rankings, db=_db())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}
