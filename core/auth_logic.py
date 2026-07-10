"""Streamlit-free committee login logic (Phase 3).

Faithful port of the credential check + login side-effects in
``auth.check_committee_login`` / ``functions`` so the proxy can authenticate the
HTML page without importing Streamlit. Cookie signing itself stays in the proxy
(it already owns the cookie_secret reader); this module only verifies the
password and records the login.
"""

import datetime
from zoneinfo import ZoneInfo

import bcrypt

from schema import TABLE_ACCOUNTS, TABLE_LOGIN_RECORDS, VIEW_COMMITTEE_VOTE_ACTIVITY
from core.vote_logic import _resolve_db


def verify_password(plain: str, stored: str) -> bool:
    """bcrypt hash or legacy plaintext — mirrors functions._verify_config_password."""
    stored = str(stored)
    if stored.startswith("$2b$") or stored.startswith("$2a$"):
        try:
            return bcrypt.checkpw(plain.encode(), stored.encode())
        except Exception:
            return False
    return plain == stored


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def check_login(user_id: str, password: str, db=None) -> bool:
    """True if user_id exists and the password matches its stored hash."""
    db = _resolve_db(db)
    df = db.query(
        f"SELECT password_hash FROM {TABLE_ACCOUNTS} WHERE user_id = :uid",
        {"uid": user_id},
    )
    if df.empty:
        return False
    return verify_password(password, str(df.iloc[0]["password_hash"]))


def change_password(user_id: str, current_password: str, new_password: str, db=None) -> bool:
    db = _resolve_db(db)
    df = db.query(
        f"SELECT password_hash FROM {TABLE_ACCOUNTS} WHERE user_id = :uid",
        {"uid": user_id},
    )
    if df.empty or not verify_password(current_password, str(df.iloc[0]["password_hash"])):
        return False
    db.execute(
        f"UPDATE {TABLE_ACCOUNTS} SET password_hash = :password_hash WHERE user_id = :uid",
        {"password_hash": hash_password(new_password), "uid": user_id},
    )
    return True


def record_login(user_id: str, db=None) -> None:
    """Stamp the login and write an audit record — mirrors
    update_committee_login_time + _log_login. Best-effort: wrapped so a missing
    lifecycle column never blocks an otherwise-valid login."""
    db = _resolve_db(db)
    now = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
    try:
        account = db.query(
            f"SELECT account_status FROM {TABLE_ACCOUNTS} WHERE user_id = :uid",
            {"uid": user_id},
        )
        current = str(account.iloc[0]["account_status"]).strip() if not account.empty else ""
        if current not in {"admin", "developer"}:
            activity = db.query(
                f"SELECT is_active FROM {VIEW_COMMITTEE_VOTE_ACTIVITY} WHERE user_id = :uid",
                {"uid": user_id},
            )
            is_active = False if activity.empty else str(activity.iloc[0]["is_active"]).strip().lower() in {
                "true", "t", "1", "yes"
            }
            db.execute(
                f"UPDATE {TABLE_ACCOUNTS} SET account_status = :status WHERE user_id = :uid",
                {"status": "active" if is_active else "inactive", "uid": user_id},
            )
    except Exception:
        pass
    try:
        # Also clears account_disabled, re-enabling a dormant account on login.
        db.execute(
            f"UPDATE {TABLE_ACCOUNTS} SET last_login_at = :now, account_disabled = FALSE WHERE user_id = :uid",
            {"uid": user_id, "now": now},
        )
    except Exception:
        pass
    try:
        db.execute(
            f"INSERT INTO {TABLE_LOGIN_RECORDS} (user_id, login_type, logged_in_at) "
            "VALUES (:uid, 'committee', :t)",
            {"uid": user_id, "t": now.strftime("%Y-%m-%d %H:%M:%S")},
        )
    except Exception:
        pass
