"""Bearer-link API for teams to review and acknowledge final score sheets."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field


router = APIRouter(
    prefix="/api/score-sheet-confirmation", tags=["score-sheet-confirmation"]
)


class ResponseBody(BaseModel):
    status: Literal["confirmed", "disputed"]
    reason: str = Field(default="", max_length=2000)


def _db():
    from deploy.proxy import get_vote_db

    return get_vote_db()


@router.get("/data")
def data(
    response: Response,
    token: str = Query(default="", max_length=128),
    judge_name: str | None = Query(default=None, max_length=200),
):
    from core.score_confirmation import confirmation_data

    payload = confirmation_data(token, judge_name, db=_db())
    if payload is None:
        raise HTTPException(
            404,
            "核對連結無效或已被重新生成，請向賽會索取最新連結。",
            headers={"Cache-Control": "no-store"},
        )
    response.headers["Cache-Control"] = "no-store"
    return payload


@router.post("/respond")
def submit_response(
    body: ResponseBody,
    response: Response,
    token: str = Query(default="", max_length=128),
):
    from core.score_confirmation import respond

    result = respond(token, body.status, body.reason, db=_db())
    if not result.get("ok"):
        status_code = {
            "invalid": 404,
            "validation": 400,
            "responded": 409,
            "stale": 409,
        }.get(result.get("reason"), 400)
        raise HTTPException(
            status_code,
            result.get("message") or "未能提交核對結果。",
            headers={"Cache-Control": "no-store"},
        )
    response.headers["Cache-Control"] = "no-store"
    return result
