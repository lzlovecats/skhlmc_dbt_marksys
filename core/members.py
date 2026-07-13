"""Committee membership and activity queries.

The participation rate and last-ten rule live in the database view defined in
``schema.py``; this module supplies bounded read models and bypass handling.
"""

import datetime
from zoneinfo import ZoneInfo

from core.config_store import get_config
from schema import VIEW_COMMITTEE_VOTE_ACTIVITY
from core.vote_logic import _resolve_db
from system_limits import ACCOUNT_LIST_LIMIT

_ACTIVITY_VIEW_SQL = f"""
SELECT
    user_id,
    account_status,
    total_votes,
    participated_votes,
    last10_participated,
    total_ballots,
    agree_ballots,
    overall_rate_pct,
    agree_rate_pct,
    is_active
FROM {VIEW_COMMITTEE_VOTE_ACTIVITY}
ORDER BY user_id
LIMIT {ACCOUNT_LIST_LIMIT}
"""


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "t", "1", "yes"}


def _to_int(value, default=0) -> int:
    try:
        if value is None or str(value) == "nan":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _all_user_stats(db=None):
    db = _resolve_db(db)
    return db.query(_ACTIVITY_VIEW_SQL)


def count_active_members(db=None) -> int:
    """Number of committee members currently meeting the activity rule."""
    db = _resolve_db(db)
    frame = db.query(
        f"SELECT COUNT(*) AS active_count FROM {VIEW_COMMITTEE_VOTE_ACTIVITY} "
        "WHERE COALESCE(is_active, FALSE) = TRUE"
    )
    return 0 if frame.empty else _to_int(frame.iloc[0].get("active_count"))


def active_member_ids(db=None) -> list:
    """user_ids of currently-active committee members."""
    db = _resolve_db(db)
    frame = db.query(
        f"SELECT user_id FROM {VIEW_COMMITTEE_VOTE_ACTIVITY} "
        "WHERE COALESCE(is_active, FALSE) = TRUE ORDER BY user_id LIMIT :limit",
        {"limit": ACCOUNT_LIST_LIMIT},
    )
    return [str(value).strip() for value in frame.get("user_id", []) if str(value).strip()]


def get_bypass_active_until(user_id: str, db=None):
    """Return an unexpired proposal-limit bypass for one member."""
    db = _resolve_db(db)
    raw = get_config(db, "bypass_active_check_until", {})
    if not isinstance(raw, dict):
        return None
    try:
        value = raw.get(user_id)
        if not value:
            return None
        deadline = datetime.datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo("Asia/Hong_Kong")
        )
        return deadline if datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")) < deadline else None
    except (TypeError, ValueError):
        return None


def member_activity(user_id, db=None) -> dict:
    """Activity and bypass state used by the voting API."""
    user_id = str(user_id or "").strip()
    if user_id == "admin":
        naturally_active = True
    else:
        db = _resolve_db(db)
        frame = db.query(
            f"SELECT is_active FROM {VIEW_COMMITTEE_VOTE_ACTIVITY} "
            "WHERE user_id = :user_id LIMIT 1",
            {"user_id": user_id},
        )
        naturally_active = not frame.empty and _coerce_bool(frame.iloc[0].get("is_active"))
    bypass_until = None if naturally_active else get_bypass_active_until(user_id, db=db)
    return {
        "naturally_active": naturally_active,
        "bypass_until": bypass_until.strftime("%Y-%m-%d %H:%M") if bypass_until else None,
        "is_active": naturally_active or bypass_until is not None,
    }


def is_active_member(user_id, db=None) -> bool:
    """Whether ``user_id`` may propose topics / deposition motions.

    Includes the temporary proposal-limit bypass used by vote.py.
    """
    return member_activity(user_id, db=db)["is_active"]


def get_member_participation_stats(db=None):
    """Per-member participation stats matching functions.get_member_participation_stats.

    Returns ``(stats_list, total_votes)`` with stable Chinese display labels.
    """
    df = _all_user_stats(db=db)
    if df.empty:
        return [], 0
    total_votes = _to_int(df["total_votes"].max()) if "total_votes" in df else 0

    stats = []
    for _, row in df.iterrows():
        member_total_votes = _to_int(row.get("total_votes"))
        participated = _to_int(row.get("participated_votes"))
        overall_rate = participated / member_total_votes if member_total_votes > 0 else 0
        last10 = _to_int(row.get("last10_participated"))
        total_ballots = _to_int(row.get("total_ballots"))
        agree_ballots = _to_int(row.get("agree_ballots"))
        agree_rate = agree_ballots / total_ballots if total_ballots > 0 else None
        is_active = _coerce_bool(row.get("is_active"))

        stats.append({
            "用戶": str(row.get("user_id", "")).strip(),
            "整體投票次數": f"{participated} / {member_total_votes}",
            "整體投票率": f"{overall_rate:.1%}",
            "最近10次參與": last10,
            "同意票數": f"{agree_ballots} / {total_ballots}",
            "投票同意率": f"{agree_rate:.1%}" if agree_rate is not None else "—",
            "活躍狀態": "✅ 活躍" if is_active else "❌ 非活躍",
        })

    stats.sort(key=lambda s: (not str(s["活躍狀態"]).startswith("✅"), str(s["用戶"])))
    return stats, total_votes
