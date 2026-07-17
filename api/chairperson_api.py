"""Organiser-authenticated API for the direct HTML chairperson console."""

from fastapi import APIRouter, HTTPException, Request
from api.access import require_competition_staff

router = APIRouter(prefix="/api/chairperson", tags=["chairperson"])


def _require_admin(request: Request):
    return require_competition_staff(request)


@router.get("/data")
def data(request: Request, match_id: str | None = None):
    from core.chairperson_logic import chairperson_data
    from deploy.proxy import get_vote_db
    _require_admin(request)
    return chairperson_data(match_id, db=get_vote_db())
