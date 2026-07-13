"""Committee login endpoints for the HTML page (Phase 3).

Lets the HTML vote page authenticate without Streamlit. Verifies credentials via
core.auth_logic and sets the same signed ``committee_user`` cookie that the rest
of the system (proxy _verify_committee_token, Streamlit auth) already trusts, so
a session started here works everywhere.
"""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from pathlib import Path
from system_limits import (
    COMMITTEE_COOKIE_MAX_AGE_DAYS, NOTIFICATION_READ_RETENTION_DAYS,
)

router = APIRouter(prefix="/api/committee", tags=["committee"])

COOKIE_NAME = "committee_user"
COOKIE_MAX_AGE = COMMITTEE_COOKIE_MAX_AGE_DAYS * 24 * 60 * 60


class LoginBody(BaseModel):
    user_id: str = Field(max_length=200)
    password: str = Field(max_length=512)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=512)
    new_password: str = Field(max_length=512)
    confirm_password: str = Field(max_length=512)


def _current_notification():
    path = Path(__file__).resolve().parents[1] / "assets" / "noti.md"
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    noti_id = None
    title = None
    content_start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("NOTI_ID:"):
            try:
                noti_id = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                return None
        elif stripped.startswith("NOTI_TITLE:"):
            title = stripped.split(":", 1)[1].strip()
        elif stripped == "---":
            content_start = index + 1
            break
    if noti_id is None or not title:
        return None
    return {"id": noti_id, "title": title, "content": "\n".join(lines[content_start:]).strip()}


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
    if uid == "admin":
        raise HTTPException(403, "賽會人員帳戶不能使用此頁面，請改用內部委員會成員帳戶登入")

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


@router.get("/notification")
def notification(request: Request):
    """Return the current one-time notice when this member has not read it."""
    from deploy.proxy import _require_committee_user, get_vote_db
    from schema import CREATE_NOTIFICATION_READS, TABLE_NOTIFICATION_READS

    user_id = _require_committee_user(request)
    notice = _current_notification()
    if not notice:
        return {"notification": None}
    db = get_vote_db()
    db.execute(CREATE_NOTIFICATION_READS)
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_notification_reads_read_at ON {TABLE_NOTIFICATION_READS}(read_at)")
    seen = db.query(
        f"SELECT 1 FROM {TABLE_NOTIFICATION_READS} WHERE notification_id = :nid AND user_id = :uid",
        {"nid": notice["id"], "uid": user_id},
    )
    return {"notification": None if not seen.empty else notice}


class NotificationReadBody(BaseModel):
    notification_id: int
    notification_title: str = Field(max_length=300)


@router.post("/notification/read")
def notification_read(body: NotificationReadBody, request: Request):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from deploy.proxy import _require_committee_user, get_vote_db
    from schema import CREATE_NOTIFICATION_READS, TABLE_NOTIFICATION_READS

    user_id = _require_committee_user(request)
    current = _current_notification()
    if not current or body.notification_id != current["id"]:
        raise HTTPException(400, "通知已更新，請重新載入")
    db = get_vote_db()
    db.execute(CREATE_NOTIFICATION_READS)
    db.execute(
        f"INSERT INTO {TABLE_NOTIFICATION_READS} "
        "(notification_id, notification_title, user_id, read_at) "
        "VALUES (:nid, :title, :uid, :seen_at) "
        "ON CONFLICT (notification_id, user_id) DO NOTHING",
        {
            "nid": current["id"], "title": current["title"], "uid": user_id,
            "seen_at": datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    db.execute(f"DELETE FROM {TABLE_NOTIFICATION_READS} WHERE read_at<:cutoff", {
        "cutoff": datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
                  - timedelta(days=NOTIFICATION_READ_RETENTION_DAYS),
    })
    return {"status": "ok"}


@router.post("/password")
def password(body: PasswordBody, request: Request):
    from deploy.proxy import _require_committee_user, get_vote_db
    from core.auth_logic import change_password

    uid = _require_committee_user(request)
    current = (body.current_password or "").strip()
    new = (body.new_password or "").strip()
    confirm = (body.confirm_password or "").strip()
    if not current:
        raise HTTPException(400, "請輸入目前密碼")
    if not new:
        raise HTTPException(400, "請輸入新密碼")
    if new != confirm:
        raise HTTPException(400, "兩次輸入的新密碼不一致")
    if not change_password(uid, current, new, db=get_vote_db()):
        raise HTTPException(401, "目前密碼錯誤")
    return {"status": "ok"}
