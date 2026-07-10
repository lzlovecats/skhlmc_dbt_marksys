"""Committee login endpoints for the HTML page (Phase 3).

Lets the HTML vote page authenticate without Streamlit. Verifies credentials via
core.auth_logic and sets the same signed ``committee_user`` cookie that the rest
of the system (proxy _verify_committee_token, Streamlit auth) already trusts, so
a session started here works everywhere.
"""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/committee", tags=["committee"])

COOKIE_NAME = "committee_user"
COOKIE_MAX_AGE = 180 * 24 * 60 * 60   # 180 days, matching functions.return_expire_day


class LoginBody(BaseModel):
    user_id: str
    password: str


@router.post("/login")
def login(body: LoginBody, response: Response):
    from deploy.proxy import get_vote_db, _sign_committee_token
    from core.auth_logic import check_login, record_login

    uid = (body.user_id or "").strip()
    pw = (body.password or "").strip()
    if not uid or not pw:
        raise HTTPException(400, "請輸入帳號及密碼")

    db = get_vote_db()
    if not check_login(uid, pw, db=db):
        raise HTTPException(401, "用戶名稱或密碼錯誤")

    token = _sign_committee_token(uid)
    if not token:
        raise HTTPException(503, "登入服務暫時未能使用")

    record_login(uid, db=db)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=COOKIE_MAX_AGE, path="/", samesite="lax", httponly=True,
    )
    return {"status": "ok", "user_id": uid}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/me")
def me(request: Request):
    """Return the logged-in committee user, or 401. Lets the HTML page check
    login state without pulling vote data."""
    from deploy.proxy import _require_committee_user
    return {"user_id": _require_committee_user(request)}
