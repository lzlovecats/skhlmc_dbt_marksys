"""Organiser-only read API for submitted debate results."""

from fastapi import APIRouter, HTTPException, Request
from api.access import require_competition_staff

router = APIRouter(prefix="/api/management", tags=["management"])


def _require_admin(request: Request):
    return require_competition_staff(request)


@router.get("/data")
def data(request: Request, match_id: str | None = None):
    from core.results_logic import results_data
    from deploy.proxy import get_vote_db
    _require_admin(request)
    try:
        return results_data(match_id, db=get_vote_db())
    except Exception as exc:
        raise HTTPException(503, f"讀取評分失敗：{exc}") from exc
