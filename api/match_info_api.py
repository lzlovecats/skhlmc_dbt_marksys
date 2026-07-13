"""Organiser-authenticated match, password, topic, and roster-link API."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/match-info", tags=["match-info"])


class CreateBody(BaseModel):
    match_id: str = Field(max_length=200)


class SaveBody(BaseModel):
    match_id: str = Field(max_length=200)
    match_date: str = Field(default="", max_length=10)
    match_time: str = Field(default="", max_length=5)
    topic_text: str = Field(default="", max_length=500)
    pro_team: str = Field(default="", max_length=100)
    con_team: str = Field(default="", max_length=100)
    pro_1: str = Field(default="", max_length=80)
    pro_2: str = Field(default="", max_length=80)
    pro_3: str = Field(default="", max_length=80)
    pro_4: str = Field(default="", max_length=80)
    con_1: str = Field(default="", max_length=80)
    con_2: str = Field(default="", max_length=80)
    con_3: str = Field(default="", max_length=80)
    con_4: str = Field(default="", max_length=80)
    access_code: str = Field(default="", max_length=512)
    review_password: str = Field(default="", max_length=512)
    clear_access_code: bool = False
    clear_review_password: bool = False


class DrawTopicBody(BaseModel):
    difficulty: int | None = None


class DrawSidesBody(BaseModel):
    team1: str = Field(default="", max_length=100)
    team2: str = Field(default="", max_length=100)


class SideBody(BaseModel):
    side: str = Field(max_length=10)


def _db():
    from deploy.proxy import get_vote_db

    return get_vote_db()


def _admin(request: Request):
    from deploy.proxy import _verify_registration_admin_token

    if not _verify_registration_admin_token(
        request.cookies.get("registration_admin") or ""
    ):
        raise HTTPException(401, "未登入")


@router.get("/data")
def data(request: Request, match_id: str | None = None, compact: bool = False):
    from core import match_logic as logic

    _admin(request)
    return logic.match_admin_data(match_id, db=_db(), compact=compact)


@router.post("/create")
def create(body: CreateBody, request: Request):
    from core import match_logic as logic

    _admin(request)
    return logic.create_match(body.match_id, db=_db())


@router.post("/save")
def save(body: SaveBody, request: Request):
    from core import match_logic as logic

    _admin(request)
    return logic.save_match(body.model_dump(), db=_db())


@router.post("/draw-topic")
def draw_topic(body: DrawTopicBody, request: Request):
    from core import match_logic as logic

    _admin(request)
    return logic.draw_topic(body.difficulty, db=_db())


@router.post("/draw-sides")
def draw_sides(body: DrawSidesBody, request: Request):
    from core import match_logic as logic

    _admin(request)
    return logic.draw_sides(body.team1, body.team2)


@router.post("/{match_id}/reopen")
def reopen(match_id: str, body: SideBody, request: Request):
    from core import match_logic as logic

    _admin(request)
    return logic.reopen_link(match_id, body.side, db=_db())


@router.post("/{match_id}/regenerate")
def regenerate(match_id: str, body: SideBody, request: Request):
    from core import match_logic as logic

    _admin(request)
    return logic.regenerate_link(match_id, body.side, db=_db())


@router.delete("/{match_id}")
def delete(match_id: str, request: Request):
    from core import match_logic as logic

    _admin(request)
    return logic.delete_match(match_id, db=_db())
