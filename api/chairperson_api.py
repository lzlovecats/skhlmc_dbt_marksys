"""Organiser-authenticated API for the direct HTML chairperson console."""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/chairperson", tags=["chairperson"])


def _require_admin(request: Request):
    from deploy.proxy import _verify_registration_admin_token
    if not _verify_registration_admin_token(request.cookies.get("registration_admin") or ""):
        raise HTTPException(401, "未登入")


@router.get("/data")
def data(request: Request, match_id: str | None = None):
    from core.chairperson_logic import chairperson_data
    from deploy.proxy import get_vote_db
    _require_admin(request)
    return chairperson_data(match_id, db=get_vote_db())
