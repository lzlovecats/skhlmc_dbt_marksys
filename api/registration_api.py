"""Public JSON endpoints for competition registration."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/registration", tags=["registration"])


class RegistrationBody(BaseModel):
    competition_edition: int
    team_name: str
    main_debater_name: str
    first_deputy_name: str
    second_deputy_name: str
    closing_debater_name: str
    contact_name: str
    contact_class: str
    contact_phone: str


def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()


@router.get("/data")
def data():
    from core import registration_logic as logic
    try:
        return logic.registration_status_payload(db=_db())
    except Exception as exc:
        raise HTTPException(503, f"連線錯誤: {exc}") from exc


@router.post("/submit")
def submit(body: RegistrationBody):
    from core import registration_logic as logic
    try:
        return logic.submit_registration(body.model_dump(), body.competition_edition, db=_db())
    except Exception as exc:
        raise HTTPException(503, f"提交報名失敗：{exc}") from exc
