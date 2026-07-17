"""Central delegated management roles for committee accounts.

Developer sessions are handled at the HTTP boundary.  This module only
answers account/data questions so business logic can be tested without the
FastAPI runtime.
"""

from account_access import DEVELOPER_ACCOUNT_ID, normalize_account_id
from core.config_store import get_config
from core.vote_logic import _resolve_db
from schema import TABLE_COMMITTEE_MEMBERSHIPS


AI_MANAGERS_KEY = "ai_managers"
SENIOR_COMMITTEE_MEMBERS_KEY = "senior_committee_members"


def _configured_accounts(db, key: str) -> set[str]:
    values = get_config(_resolve_db(db), key, [])
    if not isinstance(values, list):
        return set()
    return {
        str(value).strip()
        for value in values
        if str(value).strip()
    }


def is_ai_manager(user_id: object, db=None) -> bool:
    value = str(user_id or "").strip()
    if normalize_account_id(value) == normalize_account_id(DEVELOPER_ACCOUNT_ID):
        return True
    return value in _configured_accounts(db, AI_MANAGERS_KEY)


def is_graduate(user_id: object, db=None) -> bool:
    value = str(user_id or "").strip()
    if not value:
        return False
    db = _resolve_db(db)
    try:
        rows = db.query(
            f"""SELECT 1 FROM {TABLE_COMMITTEE_MEMBERSHIPS}
                WHERE member_user_id=:user AND exit_type='graduated'
                LIMIT 1""",
            {"user": value},
        )
    except Exception:
        # A deployment whose migration has not yet been applied must fail
        # closed instead of treating every inactive account as an alumnus.
        return False
    return not rows.empty


def is_senior_committee(user_id: object, db=None) -> bool:
    value = str(user_id or "").strip()
    if normalize_account_id(value) == normalize_account_id(DEVELOPER_ACCOUNT_ID):
        return True
    return value in _configured_accounts(db, SENIOR_COMMITTEE_MEMBERS_KEY) or is_graduate(
        value, db=db,
    )
