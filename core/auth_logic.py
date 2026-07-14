"""Committee credentials, account lifecycle and login audit logic.

Cookie signing stays in the HTTP runtime; this module verifies passwords and
persists account/login side effects.
"""

import datetime
import math
import threading
import time
from collections import deque
from zoneinfo import ZoneInfo

import bcrypt

from account_access import is_non_member_account, normalize_account_id
from core.config_store import get_config
from schema import TABLE_ACCOUNTS, TABLE_LOGIN_RECORDS, VIEW_COMMITTEE_VOTE_ACTIVITY
from core.vote_logic import _resolve_db
from system_limits import (
    LOGIN_RATE_MAX_GLOBAL,
    LOGIN_RATE_MAX_PER_CLIENT,
    LOGIN_RATE_MAX_PER_CLIENT_ACCOUNT,
    LOGIN_RATE_WINDOW_SECONDS,
    LOGIN_RECORD_RETENTION_DAYS,
    MAINTENANCE_PRUNE_INTERVAL_SECONDS,
)

_login_prune_lock = threading.Lock()
_login_last_prune = 0.0
_login_attempt_lock = threading.Lock()
_login_global_attempts: deque[float] = deque()
_login_client_attempts: dict[str, deque[float]] = {}
_login_client_account_attempts: dict[tuple[str, str], deque[float]] = {}


def _login_client_key(request) -> str:
    """Return the server-observed client address, never a caller-supplied header."""
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "").strip()
    return host[:200] or "unknown"


def _prune_attempt_bucket(bucket: deque[float], cutoff: float) -> None:
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()


def _prune_attempt_map(mapping: dict, cutoff: float) -> None:
    for key, bucket in list(mapping.items()):
        _prune_attempt_bucket(bucket, cutoff)
        if not bucket:
            mapping.pop(key, None)


def login_rate_limit_retry_after(request, account_id: object, *, now: float | None = None) -> int | None:
    """Record one pre-password-check attempt or return seconds until allowed.

    The accepted-attempt global deque bounds both bcrypt work and the number of
    client/account buckets an attacker can create. Rejected attempts do not
    extend the window, so a legitimate operator always gets a deterministic
    retry time instead of an attacker being able to keep the lock alive.
    """
    moment = time.monotonic() if now is None else float(now)
    cutoff = moment - LOGIN_RATE_WINDOW_SECONDS
    client_key = _login_client_key(request)
    account_key = normalize_account_id(account_id) or "<empty>"
    pair_key = (client_key, account_key)

    with _login_attempt_lock:
        _prune_attempt_bucket(_login_global_attempts, cutoff)
        _prune_attempt_map(_login_client_attempts, cutoff)
        _prune_attempt_map(_login_client_account_attempts, cutoff)
        client_bucket = _login_client_attempts.get(client_key)
        pair_bucket = _login_client_account_attempts.get(pair_key)

        blocked_buckets = []
        if len(_login_global_attempts) >= LOGIN_RATE_MAX_GLOBAL:
            blocked_buckets.append(_login_global_attempts)
        if client_bucket is not None and len(client_bucket) >= LOGIN_RATE_MAX_PER_CLIENT:
            blocked_buckets.append(client_bucket)
        if pair_bucket is not None and len(pair_bucket) >= LOGIN_RATE_MAX_PER_CLIENT_ACCOUNT:
            blocked_buckets.append(pair_bucket)
        if blocked_buckets:
            retry = max(bucket[0] + LOGIN_RATE_WINDOW_SECONDS - moment for bucket in blocked_buckets)
            return max(1, int(math.ceil(retry)))

        _login_global_attempts.append(moment)
        _login_client_attempts.setdefault(client_key, deque()).append(moment)
        _login_client_account_attempts.setdefault(pair_key, deque()).append(moment)
    return None


def _reset_login_rate_limit_state() -> None:
    """Clear process-local limiter state for deterministic unit tests."""
    with _login_attempt_lock:
        _login_global_attempts.clear()
        _login_client_attempts.clear()
        _login_client_account_attempts.clear()


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


def authenticate_login(user_id: str, password: str, db=None) -> str | None:
    """Return the exact verified credential hash, or ``None`` on failure.

    Passing that hash into the session signer closes the small reset race
    between bcrypt verification and token minting.
    """
    db = _resolve_db(db)
    if is_login_disabled(user_id, db=db):
        return None
    df = db.query(
        f"SELECT password_hash FROM {TABLE_ACCOUNTS} WHERE user_id = :uid",
        {"uid": user_id},
    )
    if df.empty:
        return None
    stored = str(df.iloc[0]["password_hash"])
    if not verify_password(password, stored):
        return None
    # Transparently retire the legacy plaintext account format after the first
    # successful verification; failed attempts never mutate credentials.
    if not stored.startswith(("$2a$", "$2b$", "$2y$")):
        stored = hash_password(password)
        db.execute(
            f"UPDATE {TABLE_ACCOUNTS} SET password_hash=:password_hash WHERE user_id=:uid",
            {"password_hash": stored, "uid": user_id},
        )
    return stored


def check_login(user_id: str, password: str, db=None) -> bool:
    """True if user_id exists and the password matches its stored hash."""
    return authenticate_login(user_id, password, db=db) is not None


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
        if not is_non_member_account(user_id):
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
