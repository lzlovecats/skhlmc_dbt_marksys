"""Committee credentials, account lifecycle and login audit logic.

Cookie signing stays in the HTTP runtime; this module verifies passwords and
persists account/login side effects.
"""

import datetime
import threading
import time
from zoneinfo import ZoneInfo

import bcrypt

from core.config_store import get_config
from schema import TABLE_ACCOUNTS, TABLE_LOGIN_RECORDS, VIEW_COMMITTEE_VOTE_ACTIVITY
from core.vote_logic import _resolve_db
from system_limits import LOGIN_RECORD_RETENTION_DAYS, MAINTENANCE_PRUNE_INTERVAL_SECONDS

_login_prune_lock = threading.Lock()
_login_last_prune = 0.0


def append_login_record(user_id: str, login_type: str, logged_in_at, db=None) -> None:
    """Append a bounded audit event and prune old login rows at most daily."""
    global _login_last_prune
    db = _resolve_db(db)
    db.execute(
        f"INSERT INTO {TABLE_LOGIN_RECORDS} (user_id,login_type,logged_in_at) "
        "VALUES (:uid,:kind,:logged_in_at)",
        {"uid": user_id, "kind": str(login_type)[:40], "logged_in_at": logged_in_at},
    )
    monotonic_now = time.monotonic()
    if monotonic_now - _login_last_prune < MAINTENANCE_PRUNE_INTERVAL_SECONDS:
        return
    with _login_prune_lock:
        if monotonic_now - _login_last_prune < MAINTENANCE_PRUNE_INTERVAL_SECONDS:
            return
        cutoff = datetime.datetime.now().replace(tzinfo=None) - datetime.timedelta(days=LOGIN_RECORD_RETENTION_DAYS)
        db.execute(f"CREATE INDEX IF NOT EXISTS idx_login_records_logged_in_at ON {TABLE_LOGIN_RECORDS}(logged_in_at)")
        db.execute(f"DELETE FROM {TABLE_LOGIN_RECORDS} WHERE logged_in_at<:cutoff", {"cutoff": cutoff})
        _login_last_prune = monotonic_now


def verify_password(plain: str, stored: str) -> bool:
    """bcrypt hash or legacy plaintext — mirrors functions._verify_config_password."""
    stored = str(stored)
    if stored.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(plain.encode(), stored.encode())
        except Exception:
            return False
    return plain == stored


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def is_login_disabled(user_id: str, db=None) -> bool:
    """Persistent developer-controlled login block, separate from the
    180-day ``account_disabled`` lifecycle flag which clears on login."""
    db = _resolve_db(db)
    values = get_config(db, "login_disabled_accounts", [])
    return str(user_id or "").strip() in values if isinstance(values, list) else False


def check_login(user_id: str, password: str, db=None) -> bool:
    """True if user_id exists and the password matches its stored hash."""
    db = _resolve_db(db)
    if is_login_disabled(user_id, db=db):
        return False
    df = db.query(
        f"SELECT password_hash FROM {TABLE_ACCOUNTS} WHERE user_id = :uid",
        {"uid": user_id},
    )
    if df.empty:
        return False
    stored = str(df.iloc[0]["password_hash"])
    if not verify_password(password, stored):
        return False
    # Transparently retire the legacy plaintext account format after the first
    # successful verification; failed attempts never mutate credentials.
    if not stored.startswith(("$2a$", "$2b$", "$2y$")):
        db.execute(
            f"UPDATE {TABLE_ACCOUNTS} SET password_hash=:password_hash WHERE user_id=:uid",
            {"password_hash": hash_password(password), "uid": user_id},
        )
    return True


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
        append_login_record(user_id, "committee", now, db=db)
    except Exception:
        pass
