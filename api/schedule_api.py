"""Organiser-only endpoint for the in-memory tournament bracket draw."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from api.access import require_competition_staff

router = APIRouter(prefix="/api/schedule", tags=["schedule"])


class DrawBody(BaseModel):
    teams_text: str = Field(default="", max_length=20_000)


def _require_admin(request: Request):
    return require_competition_staff(request)


@router.get("/auth")
def auth(request: Request):
    _require_admin(request)
    return {"ok": True}


@router.post("/draw")
def draw(body: DrawBody, request: Request):
    from core.schedule_logic import draw_schedule
    _require_admin(request)
    return draw_schedule(body.teams_text)
