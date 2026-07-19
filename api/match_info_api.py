"""Organiser-authenticated match, password, topic, and roster-link API."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from api.access import require_competition_staff

from debate_timing import DEBATE_FORMATS

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
    debate_format: str = Field(default=DEBATE_FORMATS[0], max_length=20)
    free_debate_minutes: float | None = Field(default=None, ge=2, le=10)
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

    @field_validator("debate_format")
    @classmethod
    def validate_debate_format(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if normalized not in DEBATE_FORMATS:
            raise ValueError("請選擇有效的賽制")
        return normalized

    @model_validator(mode="after")
    def validate_free_debate_minutes(self):
        if self.debate_format != "聯中" and self.free_debate_minutes is not None:
            raise ValueError("只有聯中賽制可設定自由辯論時間")
        return self


class DrawTopicBody(BaseModel):
    difficulty: int | None = None


class DrawSidesBody(BaseModel):
    team1: str = Field(default="", max_length=100)
    team2: str = Field(default="", max_length=100)


class SideBody(BaseModel):
    side: str = Field(max_length=10)


class TopicReleaseBody(BaseModel):
    difficulty: int | None = Field(default=None, ge=1, le=3)


def _db():
    from deploy.proxy import get_vote_db

    return get_vote_db()


def _admin(request: Request):
    return require_competition_staff(request)


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


@router.post("/{match_id}/score-confirmation/open")
def open_score_confirmation(match_id: str, request: Request):
    from core.score_confirmation import open_confirmation

    _admin(request)
    try:
        result = open_confirmation(match_id, db=_db())
    except Exception as exc:
        raise HTTPException(503, "核對分紙功能尚未完成資料庫準備。") from exc
    if not result.get("ok"):
        raise HTTPException(409, result.get("message") or "未能開放核對分紙。")
    return result


def _topic_release_action(action):
    from core.match_topic_release import TopicReleaseError

    try:
        return action()
    except TopicReleaseError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "辯題公布功能尚未完成資料庫準備。") from exc


@router.post("/{match_id}/topic-release/open")
def open_topic_release(match_id: str, body: TopicReleaseBody, request: Request):
    from core.match_topic_release import open_release

    _admin(request)
    return _topic_release_action(
        lambda: open_release(match_id, body.difficulty, db=_db())
    )


@router.post("/{match_id}/topic-release/rotate-links")
def rotate_topic_release_links(match_id: str, request: Request):
    from core.match_topic_release import rotate_links

    _admin(request)
    return _topic_release_action(lambda: rotate_links(match_id, db=_db()))


@router.post("/{match_id}/topic-release/cancel")
def cancel_topic_release(match_id: str, request: Request):
    from core.match_topic_release import cancel_release

    _admin(request)
    return _topic_release_action(lambda: cancel_release(match_id, db=_db()))


@router.delete("/{match_id}")
def delete(match_id: str, request: Request):
    from core import match_logic as logic

    _admin(request)
    return logic.delete_match(match_id, db=_db())
