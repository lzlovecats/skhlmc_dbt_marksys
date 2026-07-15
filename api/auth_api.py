"""Committee authentication, account and one-time notification endpoints."""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from pathlib import Path
import threading
import time
from account_access import account_can_access, access_denial_message
from api.access import require_page_user
from system_limits import (
    COMMITTEE_SESSION_MAX_AGE_SECONDS, MAINTENANCE_PRUNE_INTERVAL_SECONDS,
    NOTIFICATION_READ_RETENTION_DAYS,
)

router = APIRouter(prefix="/api/committee", tags=["committee"])

COOKIE_NAME = "committee_user"
COOKIE_MAX_AGE = COMMITTEE_SESSION_MAX_AGE_SECONDS
_notification_prune_lock = threading.Lock()
_notification_last_prune = 0.0


class LoginBody(BaseModel):
    user_id: str = Field(max_length=200)
    password: str = Field(max_length=512)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=512)
    new_password: str = Field(max_length=512)
    confirm_password: str = Field(max_length=512)


def _file_notification():
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


def _fund_notifications(db):
    """Return recent manual AI-fund announcements as negative notification IDs."""
    from schema import TABLE_MONTHLY_RESOURCE_LIMITS
    try:
        rows = db.query(f"""SELECT period_month,allocated_hkd,notification_audit
            FROM {TABLE_MONTHLY_RESOURCE_LIMITS}
            WHERE limit_key='ai_fund_available'
              AND (notified_at IS NOT NULL
                   OR notification_audit->>'announcement_at' IS NOT NULL)
            ORDER BY period_month DESC LIMIT 2""")
    except Exception:
        return []
    notices = []
    for row in rows.to_dict("records"):
        month = str(row.get("period_month") or "")[:7]
        if len(month) != 7:
            continue
        audit = row.get("notification_audit") or {}
        if isinstance(audit, str):
            import json
            try:
                audit = json.loads(audit)
            except ValueError:
                audit = {}
        notices.append({
            "id": -int(month.replace("-", "")),
            "title": str(audit.get("title") or f"AI基金 {month} 月度預算")[:300],
            "content": (
                str(audit.get("body") or f"{month} 可用 AI 預算 HKD {float(row.get('allocated_hkd') or 0):.2f}")
                + "\n\n[查看 AI基金詳情](/ai-fund)"
            ),
        })
    return notices


def _current_notifications(db=None):
    notices = _fund_notifications(db) if db is not None else []
    file_notice = _file_notification()
    if file_notice:
        notices.append(file_notice)
    return notices


def _prune_notification_reads(db, now):
    """Bound acknowledgement storage, with at most one DELETE per interval."""
    global _notification_last_prune
    monotonic_now = time.monotonic()
    if monotonic_now - _notification_last_prune < MAINTENANCE_PRUNE_INTERVAL_SECONDS:
        return
    with _notification_prune_lock:
        if monotonic_now - _notification_last_prune < MAINTENANCE_PRUNE_INTERVAL_SECONDS:
            return
        from datetime import timedelta
        from schema import TABLE_NOTIFICATION_READS

        db.execute(
            f"DELETE FROM {TABLE_NOTIFICATION_READS} WHERE read_at<:cutoff",
            {"cutoff": now - timedelta(days=NOTIFICATION_READ_RETENTION_DAYS)},
        )
        _notification_last_prune = monotonic_now


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    from deploy.proxy import get_vote_db, _sign_committee_token
    from core.auth_logic import (
        authenticate_login,
        login_rate_limit_retry_after,
        record_login,
    )

    uid = (body.user_id or "").strip()
    pw = (body.password or "").strip()
    if not uid or not pw:
        raise HTTPException(400, "請輸入帳號及密碼")
    # Reject service identities before password verification. Otherwise the
    # 401/403 distinction becomes a password oracle for the known kiosk id.
    if not account_can_access(uid, "committee_login"):
        raise HTTPException(403, access_denial_message("committee_login"))
    retry_after = login_rate_limit_retry_after(request, uid)
    if retry_after is not None:
        raise HTTPException(
            429,
            "登入嘗試次數過多，請稍後再試。",
            headers={"Retry-After": str(retry_after)},
        )

    db = get_vote_db()
    credential_hash = authenticate_login(uid, pw, db=db)
    if credential_hash is None:
        raise HTTPException(401, "用戶名稱或密碼錯誤")

    token = _sign_committee_token(uid, credential_hash=credential_hash)
    if not token:
        raise HTTPException(503, "登入服務暫時未能使用")

    record_login(uid, db=db)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=COOKIE_MAX_AGE, path="/", samesite="lax", httponly=True,
        secure=True,
    )
    return {"status": "ok", "user_id": uid}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(
        COOKIE_NAME, path="/", samesite="lax", httponly=True, secure=True,
    )
    return {"status": "ok"}


@router.get("/me")
def me(request: Request):
    """Return the logged-in committee user, or 401. Lets the HTML page check
    login state without pulling vote data."""
    return {"user_id": require_page_user(request, "member_profile")}


@router.get("/notification")
def notification(request: Request):
    """Return the current one-time notice when this member has not read it."""
    from deploy.proxy import get_vote_db
    from schema import TABLE_NOTIFICATION_READS

    user_id = require_page_user(request, "member_profile")
    db = get_vote_db()
    notices = _current_notifications(db)
    if not notices:
        return {"notification": None}
    ids = [int(item["id"]) for item in notices]
    seen = db.query(f"""SELECT notification_id FROM {TABLE_NOTIFICATION_READS}
        WHERE user_id=:uid AND notification_id=ANY(:ids)""", {
        "uid": user_id, "ids": ids,
    })
    seen_ids = {int(value) for value in seen.get("notification_id", [])}
    return {"notification": next(
        (item for item in notices if int(item["id"]) not in seen_ids), None,
    )}


class NotificationReadBody(BaseModel):
    notification_id: int
    notification_title: str = Field(max_length=300)


@router.post("/notification/read")
def notification_read(body: NotificationReadBody, request: Request):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from deploy.proxy import get_vote_db
    from schema import TABLE_NOTIFICATION_READS

    user_id = require_page_user(request, "member_profile")
    db = get_vote_db()
    current = next((item for item in _current_notifications(db)
                    if int(item["id"]) == body.notification_id), None)
    if not current:
        raise HTTPException(400, "通知已更新，請重新載入")
    now = datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
    db.execute(
        f"INSERT INTO {TABLE_NOTIFICATION_READS} "
        "(notification_id, notification_title, user_id, read_at) "
        "VALUES (:nid, :title, :uid, :seen_at) "
        "ON CONFLICT (notification_id, user_id) DO NOTHING",
        {
            "nid": current["id"], "title": current["title"], "uid": user_id,
            "seen_at": now,
        },
    )
    _prune_notification_reads(db, now)
    return {"status": "ok"}


@router.post("/password")
def password(body: PasswordBody, request: Request):
    from deploy.proxy import get_vote_db
    from core.auth_logic import change_password

    uid = require_page_user(request, "member_profile")
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
