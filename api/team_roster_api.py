from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/team-roster", tags=["team-roster"])

class RosterBody(BaseModel):
    team_name: str = ""
    debater_1: str = ""
    debater_2: str = ""
    debater_3: str = ""
    debater_4: str = ""

def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()

@router.get("/data")
def data(token: str = ""):
    from core import match_logic as logic
    roster = logic.roster_by_token(token, db=_db())
    if not roster: raise HTTPException(404, "此提交連結無效或已被重新生成，請向賽會人員索取最新連結。")
    return roster

@router.post("/submit")
def submit(token: str, body: RosterBody):
    from core import match_logic as logic
    result = logic.save_roster(token, body.model_dump(), db=_db())
    if result.get("reason") == "invalid": raise HTTPException(404, "此提交連結無效或已被重新生成，請向賽會人員索取最新連結。")
    return result
