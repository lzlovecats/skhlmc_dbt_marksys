from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/match-info", tags=["match-info"])

class CreateBody(BaseModel): match_id: str
class SaveBody(BaseModel):
    match_id: str; match_date: str = ""; match_time: str = ""; topic_text: str = ""; pro_team: str = ""; con_team: str = ""
    pro_1: str = ""; pro_2: str = ""; pro_3: str = ""; pro_4: str = ""; con_1: str = ""; con_2: str = ""; con_3: str = ""; con_4: str = ""
    access_code: str = ""; review_password: str = ""; clear_access_code: bool = False; clear_review_password: bool = False
class DrawTopicBody(BaseModel): difficulty: int | None = None
class DrawSidesBody(BaseModel): team1: str = ""; team2: str = ""
class SideBody(BaseModel): side: str

def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()
def _admin(request: Request):
    from deploy.proxy import _verify_registration_admin_token
    if not _verify_registration_admin_token(request.cookies.get("registration_admin") or ""): raise HTTPException(401, "未登入")

@router.get("/data")
def data(request: Request, match_id: str | None = None):
    from core import match_logic as logic
    _admin(request); return logic.match_admin_data(match_id, db=_db())
@router.post("/create")
def create(body: CreateBody, request: Request):
    from core import match_logic as logic
    _admin(request); return logic.create_match(body.match_id, db=_db())
@router.post("/save")
def save(body: SaveBody, request: Request):
    from core import match_logic as logic
    _admin(request); return logic.save_match(body.model_dump(), db=_db())
@router.post("/draw-topic")
def draw_topic(body: DrawTopicBody, request: Request):
    from core import match_logic as logic
    _admin(request); return logic.draw_topic(body.difficulty, db=_db())
@router.post("/draw-sides")
def draw_sides(body: DrawSidesBody, request: Request):
    from core import match_logic as logic
    _admin(request); return logic.draw_sides(body.team1, body.team2)
@router.post("/{match_id}/reopen")
def reopen(match_id: str, body: SideBody, request: Request):
    from core import match_logic as logic
    _admin(request); return logic.reopen_link(match_id, body.side, db=_db())
@router.post("/{match_id}/regenerate")
def regenerate(match_id: str, body: SideBody, request: Request):
    from core import match_logic as logic
    _admin(request); return logic.regenerate_link(match_id, body.side, db=_db())
@router.delete("/{match_id}")
def delete(match_id: str, request: Request):
    from core import match_logic as logic
    _admin(request); return logic.delete_match(match_id, db=_db())
