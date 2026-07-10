"""Pure business logic for the topic voting / deposition feature.

Phase 1 of the HTML migration: this module holds the non-UI vote logic that was
previously embedded in ``vote.py`` — reason parsing, ballot writes, vote-data
queries, threshold maths and auto-resolution DB effects. It is the single source
of truth shared by the current Streamlit page and (later) the HTML/JSON API.

Rules for this module:
  * NO ``import streamlit`` and NO ``st.*`` UI calls (toasts, dialogs, reruns).
  * NO ``@st.cache_data`` — caching is a UI-runtime concern and stays in vote.py.
  * DB access goes through the shared primitives in ``functions``/``db``. Those
    still ride on Streamlit's connection today; decoupling that belongs to a
    later phase and does not change this module's public surface.
"""

import json
import hashlib
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from functions import get_connection, execute_query, execute_query_count, query_params
from schema import (
    TABLE_MOTION_COMMENTS,
    TABLE_TOPICS,
    TABLE_TOPIC_VOTES,
    TABLE_TOPIC_VOTE_BALLOTS,
    TABLE_TOPIC_REMOVAL_VOTES,
    TABLE_TOPIC_REMOVAL_VOTE_BALLOTS,
)


# ─────────────────────────────────────────────────────────────
# Pure parsing / formatting helpers (no I/O)
# ─────────────────────────────────────────────────────────────
def parse_reason_map(raw_value):
    if isinstance(raw_value, dict):
        return raw_value
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def parse_reason_list(raw_value):
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except (TypeError, json.JSONDecodeError):
        pass
    return [str(raw_value).strip()] if str(raw_value).strip() else []


def dump_json(data):
    return json.dumps(data, ensure_ascii=False)


def collect_reasons(selected_reasons, other_reason):
    reasons = [reason.strip() for reason in selected_reasons if reason.strip()]
    other_reason = other_reason.strip()
    if other_reason:
        reasons.append(f"其他：{other_reason}")
    return reasons


def parse_deadline_row(row, key="deadline_date"):
    # row: the row of the vote data
    """Returns (deadline_passed: bool, deadline_str: str)."""
    deadline_val = row.get(key, "")
    deadline_passed = False
    deadline_str = ""
    if deadline_val and deadline_val != "":
        try:
            if hasattr(deadline_val, 'date'):
                deadline_date = deadline_val.date() if hasattr(deadline_val, 'hour') else deadline_val
            else:
                deadline_date = datetime.strptime(str(deadline_val)[:10], "%Y-%m-%d").date()
            today_hk = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
            deadline_passed = today_hk > deadline_date
            deadline_str = deadline_date.strftime("%Y-%m-%d")
        except Exception:
            pass
    return deadline_passed, deadline_str


def discussion_comment_key(motion_type, motion_key):
    raw = f"{motion_type}:{motion_key}".encode("utf-8")
    return f"comment_{motion_type}_{hashlib.sha1(raw).hexdigest()[:12]}"


# ─────────────────────────────────────────────────────────────
# Thresholds (pure)
# ─────────────────────────────────────────────────────────────
def entry_threshold(active_count):
    """Votes needed for a proposed topic to enter the bank."""
    return max(5, math.ceil(active_count * 0.4))


def depose_threshold(active_count):
    """Votes needed for a deposition motion to pass."""
    return max(6, math.ceil(active_count * 0.5))


def resolve_vote(agree_count, against_count, threshold):
    """Pure decision for a motion given its tallies.

    Returns "pass", "reject" or None (undecided). Encodes the shared rule
    "reach threshold AND strict majority" used by both topic and depose votes.
    """
    if agree_count >= threshold and agree_count > against_count:
        return "pass"
    if against_count >= threshold and against_count > agree_count:
        return "reject"
    return None


# ─────────────────────────────────────────────────────────────
# Ballot writes
# ─────────────────────────────────────────────────────────────
def ballot_delete(table, topic, user_id):
    params = {"user_id": user_id, "topic_text": topic}
    if table == TABLE_TOPIC_VOTES:
        execute_query(f"DELETE FROM {TABLE_TOPIC_VOTE_BALLOTS} WHERE topic_text = :topic_text AND user_id = :user_id", params)
    else:
        execute_query(f"DELETE FROM {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} WHERE topic_text = :topic_text AND user_id = :user_id", params)


def ballot_upsert(table, topic, user_id, vote, reasons=None):
    params = {"user_id": user_id, "topic_text": topic}
    if table == TABLE_TOPIC_VOTES:
        if vote == "agree":
            execute_query(
                f"INSERT INTO {TABLE_TOPIC_VOTE_BALLOTS} (topic_text, user_id, vote_choice) VALUES (:topic_text, :user_id, 'agree')"
                " ON CONFLICT (topic_text, user_id) DO UPDATE SET vote_choice = 'agree'",
                params,
            )
        else:
            execute_query(
                f"INSERT INTO {TABLE_TOPIC_VOTE_BALLOTS} (topic_text, user_id, vote_choice, against_reasons) VALUES (:topic_text, :user_id, 'against', :reasons)"
                " ON CONFLICT (topic_text, user_id) DO UPDATE SET vote_choice = 'against', against_reasons = EXCLUDED.against_reasons",
                {**params, "reasons": reasons or "[]"},
            )
    else:
        execute_query(
            f"INSERT INTO {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} (topic_text, user_id, vote_choice) VALUES (:topic_text, :user_id, :vote)"
            " ON CONFLICT (topic_text, user_id) DO UPDATE SET vote_choice = :vote",
            {**params, "vote": vote},
        )


def ballot_switch_agree(table, topic, user_id):
    params = {"user_id": user_id, "topic_text": topic}
    if table == TABLE_TOPIC_VOTES:
        execute_query(
            f"UPDATE {TABLE_TOPIC_VOTE_BALLOTS} SET vote_choice = 'agree', against_reasons = '[]' WHERE topic_text = :topic_text AND user_id = :user_id",
            params,
        )
    else:
        execute_query(
            f"UPDATE {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} SET vote_choice = 'agree' WHERE topic_text = :topic_text AND user_id = :user_id",
            params,
        )


def check_category_would_exceed(category):
    """Check if adding one more topic of this category would push it past 20% of the bank."""
    conn = get_connection()
    all_topics_df = conn.query(f"SELECT category FROM {TABLE_TOPICS}", ttl=5)
    if all_topics_df.empty:
        return False, 0.0, 0, 0
    total = len(all_topics_df)
    cat_count = int((all_topics_df["category"] == category).sum())
    new_ratio = (cat_count + 1) / (total + 1)
    return new_ratio > 0.2, new_ratio, cat_count, total


# ─────────────────────────────────────────────────────────────
# Vote-data queries
# ─────────────────────────────────────────────────────────────
def get_comment_counts(motion_type):
    df = query_params(
        f"SELECT motion_key, COUNT(*) AS cnt FROM {TABLE_MOTION_COMMENTS} "
        "WHERE motion_type = :type GROUP BY motion_key",
        {"type": motion_type},
    )
    if df.empty:
        return {}
    return dict(zip(df["motion_key"], df["cnt"].astype(int)))


def fetch_vote_data():
    conn = get_connection()
    df = conn.query(
        f"""
        SELECT
            topic_text,
            proposer_user_id,
            status,
            created_at,
            deadline_date,
            approval_threshold,
            category,
            difficulty
        FROM {TABLE_TOPIC_VOTES}
        ORDER BY created_at DESC
        """,
        ttl=5,
    )
    df = df.fillna("")

    # Load ballots for pending topics only — historical ballots are not needed for the UI
    ballots = conn.query(
        f"SELECT b.topic_text, b.user_id, b.vote_choice, b.against_reasons"
        f" FROM {TABLE_TOPIC_VOTE_BALLOTS} b"
        f" JOIN {TABLE_TOPIC_VOTES} tv ON b.topic_text = tv.topic_text"
        " WHERE tv.status = 'pending'",
        ttl=0
    )
    agree_map, against_map, reasons_map = {}, {}, {}
    if not ballots.empty:
        for _, b in ballots.iterrows():
            t, uid, v = b["topic_text"], b["user_id"], b["vote_choice"]
            if v == "agree":
                agree_map.setdefault(t, []).append(uid)
            else:
                against_map.setdefault(t, []).append(uid)
                raw = b.get("against_reasons")
                r = raw if isinstance(raw, list) else (json.loads(raw) if raw else [])
                if r:
                    reasons_map.setdefault(t, {})[uid] = r

    pending, passed, rejected = [], [], []
    for _, row in df.iterrows():
        row_dict = row.to_dict()
        t = row_dict["topic_text"]
        row_dict["agree_users"] = agree_map.get(t, [])
        row_dict["against_users"] = against_map.get(t, [])
        row_dict["against_reasons"] = reasons_map.get(t, {})
        status = row_dict.get("status", "")
        if status == "pending":
            pending.append(row_dict)
        elif status == "passed":
            passed.append(t)
        elif status == "rejected":
            rejected.append(t)

    return pending, passed, rejected


def count_pending_votes():
    df = get_connection().query(
        f"SELECT COUNT(*) AS cnt FROM {TABLE_TOPIC_VOTES} WHERE status = 'pending'",
        ttl=5,
    )
    return int(df.iloc[0]["cnt"]) if not df.empty else 0


def count_pending_deposes():
    df = get_connection().query(
        f"SELECT COUNT(*) AS cnt FROM {TABLE_TOPIC_REMOVAL_VOTES} WHERE status = 'pending'",
        ttl=5,
    )
    return int(df.iloc[0]["cnt"]) if not df.empty else 0


# ─────────────────────────────────────────────────────────────
# Auto-resolution DB effects
#
# Each returns the number of pending status rows updated (0 when the motion was
# already resolved), so the caller can decide whether to fire notifications.
# UI feedback (success/error banners, balloons, reruns) stays in vote.py.
# ─────────────────────────────────────────────────────────────
def apply_topic_pass(topic, author=None, category=None, difficulty=None):
    execute_query(
        f"INSERT INTO {TABLE_TOPICS} (topic_text, author, category, difficulty) VALUES (:topic_text, :author, :category, :difficulty)",
        {"topic_text": topic, "author": author, "category": category, "difficulty": difficulty},
    )
    return execute_query_count(
        f"UPDATE {TABLE_TOPIC_VOTES} SET status = 'passed' WHERE topic_text = :topic_text AND status = 'pending'",
        {"topic_text": topic},
    )


def apply_topic_reject(topic):
    return execute_query_count(
        f"UPDATE {TABLE_TOPIC_VOTES} SET status = 'rejected' WHERE topic_text = :topic_text AND status = 'pending'",
        {"topic_text": topic},
    )


def apply_depose_pass(topic):
    updated = execute_query_count(
        f"UPDATE {TABLE_TOPIC_REMOVAL_VOTES} SET status = 'passed' WHERE topic_text = :topic_text AND status = 'pending'",
        {"topic_text": topic},
    )
    # Always remove the topic from the bank when a removal passes.
    # Decoupled from `updated` so it self-heals even if the status row was
    # already resolved (e.g. a prior run/session), preventing stale entries.
    execute_query(f"DELETE FROM {TABLE_TOPICS} WHERE topic_text = :topic_text", {"topic_text": topic})
    return updated


def apply_depose_reject(topic):
    return execute_query_count(
        f"UPDATE {TABLE_TOPIC_REMOVAL_VOTES} SET status = 'rejected' WHERE topic_text = :topic_text AND status = 'pending'",
        {"topic_text": topic},
    )
