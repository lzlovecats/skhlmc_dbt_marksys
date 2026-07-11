"""Organiser-authenticated JSON endpoints for registration management."""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/registration-admin", tags=["registration-admin"])
COOKIE_NAME = "registration_admin"


class LoginBody(BaseModel):
    password: str


class SettingsBody(BaseModel):
    competition_edition: int
    registration_start: str
    registration_end: str


class StatusBody(BaseModel):
    registration_id: int
    status: str


def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()


def _require_admin(request: Request):
    from deploy.proxy import _verify_registration_admin_token
    if not _verify_registration_admin_token(request.cookies.get(COOKIE_NAME) or ""):
        raise HTTPException(401, "未登入")


@router.post("/login")
def login(body: LoginBody, response: Response):
    from core import registration_logic as logic
    from deploy.proxy import _sign_registration_admin_token
    try:
        result = logic.check_admin_password(body.password, db=_db())
    except Exception as exc:
        raise HTTPException(503, f"登入失敗：{exc}") from exc
    if not result["ok"]:
        raise HTTPException(401, result["message"])
    token = _sign_registration_admin_token()
    if not token:
        raise HTTPException(503, "登入服務暫時未能使用")
    # Streamlit's admin gate is session-scoped, so this deliberately has no max_age.
    response.set_cookie(COOKIE_NAME, token, path="/", samesite="lax", httponly=True)
    return {"ok": True}


@router.get("/data")
def data(request: Request, edition: int | None = None, status: str = "全部"):
    from core import registration_logic as logic
    _require_admin(request)
    try:
        return logic.registration_admin_data(edition, status, db=_db())
    except Exception as exc:
        raise HTTPException(503, f"連線錯誤: {exc}") from exc


@router.post("/settings")
def save_settings(body: SettingsBody, request: Request):
    from core import registration_logic as logic
    _require_admin(request)
    try:
        return logic.save_registration_settings(
            body.competition_edition, body.registration_start, body.registration_end, db=_db()
        )
    except Exception as exc:
        raise HTTPException(503, f"儲存報名設定失敗：{exc}") from exc


@router.post("/status")
def update_status(body: StatusBody, request: Request):
    from core import registration_logic as logic
    _require_admin(request)
    try:
        return logic.update_registration_status(body.registration_id, body.status, db=_db())
    except Exception as exc:
        raise HTTPException(503, f"更新報名狀態失敗：{exc}") from exc
