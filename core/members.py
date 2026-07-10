"""Committee membership / activity queries (streamlit-free).

Mirrors the active-member count that ``functions.get_active_user_count`` derives
from the ``committee_vote_activity_view`` DB view, but through the injected DB
executor so the proxy can compute dynamic vote thresholds without importing
Streamlit. The heavy lifting (participation rate, last-10 rule) lives in the SQL
view defined in schema.py — this only reads its ``is_active`` flag.
"""

from schema import VIEW_COMMITTEE_VOTE_ACTIVITY
from core.vote_logic import _resolve_db


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "t", "1", "yes"}


def count_active_members(db=None) -> int:
    """Number of currently-active committee members (same definition as the
    Streamlit ``get_active_user_count``)."""
    db = _resolve_db(db)
    df = db.query(f"SELECT is_active FROM {VIEW_COMMITTEE_VOTE_ACTIVITY}")
    if df.empty:
        return 0
    return int(df["is_active"].apply(_coerce_bool).sum())


def active_member_ids(db=None) -> list:
    """user_ids of currently-active committee members."""
    db = _resolve_db(db)
    df = db.query(f"SELECT user_id, is_active FROM {VIEW_COMMITTEE_VOTE_ACTIVITY}")
    if df.empty:
        return []
    return [str(r["user_id"]).strip() for _, r in df.iterrows() if _coerce_bool(r["is_active"])]


def is_active_member(user_id, db=None) -> bool:
    """Whether ``user_id`` may propose topics / deposition motions.

    Matches vote.py's ``_naturally_active`` (admin or in the active list). NOTE:
    the admin "bypass_active_check" override (is_bypass_active_check) is not yet
    ported here — a temporarily-bypassed non-active member would be blocked on the
    HTML page. Acceptable until the bypass logic moves into core.
    """
    return user_id == "admin" or user_id in active_member_ids(db=db)
