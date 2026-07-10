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

from schema import TABLE_ACCOUNTS, TABLE_LOGIN_RECORDS
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


def record_login(user_id: str, db=None) -> None:
    """Stamp the login and write an audit record — mirrors
    update_committee_login_time + _log_login. Best-effort: wrapped so a missing
    lifecycle column never blocks an otherwise-valid login. NOTE: does not call
    refresh_acc_type (account_status column); vote logic reads the activity view,
    not that column, so it isn't needed for the vote feature."""
    db = _resolve_db(db)
    now = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
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
