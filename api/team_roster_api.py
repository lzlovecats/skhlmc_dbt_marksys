from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/team-roster", tags=["team-roster"])

class RosterBody(BaseModel):
    team_name: str = Field(default="", max_length=100)
    debater_1: str = Field(default="", max_length=80)
    debater_2: str = Field(default="", max_length=80)
    debater_3: str = Field(default="", max_length=80)
    debater_4: str = Field(default="", max_length=80)

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
