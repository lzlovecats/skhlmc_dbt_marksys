"""Pure business logic for the topic voting / deposition feature.

Phase 1 of the HTML migration: this module holds the non-UI vote logic that was
previously embedded in ``vote.py`` — reason parsing, ballot writes, vote-data
queries, threshold maths and auto-resolution DB effects. It is the single source
of truth shared by the current Streamlit page and the HTML/JSON API.

Rules for this module:
  * NO ``import streamlit`` and NO ``st.*`` UI calls (toasts, dialogs, reruns).
  * NO ``@st.cache_data`` — caching is a UI-runtime concern and stays in vote.py.
  * NO top-level import of ``functions``/``db`` — those pull in Streamlit and
    would bloat the streamlit-free proxy (uvicorn) process. DB access is done
    through an injected ``db`` executor (see the contract below); callers that
    omit it fall back to the Streamlit-backed default via a lazy import.

DB executor contract (duck-typed) — the ``db`` object must provide:
    query(sql: str, params: dict | None = None)         -> pandas.DataFrame
    execute(sql: str, params: dict | None = None)       -> None
    execute_count(sql: str, params: dict | None = None) -> int   # rows affected
Two implementations exist: ``db.StreamlitDb`` (Streamlit runtime) and a wrapper
around the proxy's own SQLAlchemy engine for the streamlit-free API process.
"""

import json
import hashlib
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from schema import (
    TABLE_ACCOUNTS,
    TABLE_MOTION_COMMENTS,
    TABLE_TOPICS,
    TABLE_TOPIC_VOTES,
    TABLE_TOPIC_VOTE_BALLOTS,
    TABLE_TOPIC_REMOVAL_VOTES,
    TABLE_TOPIC_REMOVAL_VOTE_BALLOTS,
)


def _resolve_db(db):
    """Return the injected executor, or lazily build the Streamlit-backed default.

    The import is deferred so a streamlit-free process (the proxy) that always
    passes its own executor never imports ``db``/streamlit.
    """
    if db is not None:
        return db
    from db import default_db
    return default_db()


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
def ballot_delete(table, topic, user_id, db=None):
    db = _resolve_db(db)
    params = {"user_id": user_id, "topic_text": topic}
    if table == TABLE_TOPIC_VOTES:
        db.execute(f"DELETE FROM {TABLE_TOPIC_VOTE_BALLOTS} WHERE topic_text = :topic_text AND user_id = :user_id", params)
    else:
        db.execute(f"DELETE FROM {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} WHERE topic_text = :topic_text AND user_id = :user_id", params)


def ballot_upsert(table, topic, user_id, vote, reasons=None, db=None):
    db = _resolve_db(db)
    params = {"user_id": user_id, "topic_text": topic}
    if table == TABLE_TOPIC_VOTES:
        if vote == "agree":
            db.execute(
                f"INSERT INTO {TABLE_TOPIC_VOTE_BALLOTS} (topic_text, user_id, vote_choice) VALUES (:topic_text, :user_id, 'agree')"
                " ON CONFLICT (topic_text, user_id) DO UPDATE SET vote_choice = 'agree'",
                params,
            )
        else:
            db.execute(
                f"INSERT INTO {TABLE_TOPIC_VOTE_BALLOTS} (topic_text, user_id, vote_choice, against_reasons) VALUES (:topic_text, :user_id, 'against', :reasons)"
                " ON CONFLICT (topic_text, user_id) DO UPDATE SET vote_choice = 'against', against_reasons = EXCLUDED.against_reasons",
                {**params, "reasons": reasons or "[]"},
            )
    else:
        db.execute(
            f"INSERT INTO {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} (topic_text, user_id, vote_choice) VALUES (:topic_text, :user_id, :vote)"
            " ON CONFLICT (topic_text, user_id) DO UPDATE SET vote_choice = :vote",
            {**params, "vote": vote},
        )


def ballot_switch_agree(table, topic, user_id, db=None):
    db = _resolve_db(db)
    params = {"user_id": user_id, "topic_text": topic}
    if table == TABLE_TOPIC_VOTES:
        db.execute(
            f"UPDATE {TABLE_TOPIC_VOTE_BALLOTS} SET vote_choice = 'agree', against_reasons = '[]' WHERE topic_text = :topic_text AND user_id = :user_id",
            params,
        )
    else:
        db.execute(
            f"UPDATE {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} SET vote_choice = 'agree' WHERE topic_text = :topic_text AND user_id = :user_id",
            params,
        )


def check_category_would_exceed(category, db=None):
    """Check if adding one more topic of this category would push it past 20% of the bank."""
    db = _resolve_db(db)
    all_topics_df = db.query(f"SELECT category FROM {TABLE_TOPICS}")
    if all_topics_df.empty:
        return False, 0.0, 0, 0
    total = len(all_topics_df)
    cat_count = int((all_topics_df["category"] == category).sum())
    new_ratio = (cat_count + 1) / (total + 1)
    return new_ratio > 0.2, new_ratio, cat_count, total


# ─────────────────────────────────────────────────────────────
# Vote-data queries
# ─────────────────────────────────────────────────────────────
def get_comment_counts(motion_type, db=None):
    db = _resolve_db(db)
    df = db.query(
        f"SELECT motion_key, COUNT(*) AS cnt FROM {TABLE_MOTION_COMMENTS} "
        "WHERE motion_type = :type GROUP BY motion_key",
        {"type": motion_type},
    )
    if df.empty:
        return {}
    return dict(zip(df["motion_key"], df["cnt"].astype(int)))


def fetch_comments(motion_type, motion_key, db=None):
    """Discussion comments for one motion, oldest first."""
    db = _resolve_db(db)
    df = db.query(
        f"SELECT user_id, comment_text, created_at FROM {TABLE_MOTION_COMMENTS} "
        "WHERE motion_type = :type AND motion_key = :key ORDER BY created_at ASC",
        {"type": motion_type, "key": motion_key},
    )
    if df.empty:
        return []
    out = []
    for _, row in df.iterrows():
        created = row.get("created_at")
        out.append({
            "user_id": str(row.get("user_id", "") or ""),
            "comment_text": str(row.get("comment_text", "") or ""),
            "created_at": created.strftime("%m-%d %H:%M") if hasattr(created, "strftime") else str(created)[:16],
        })
    return out


def insert_comment(motion_type, motion_key, user_id, text, db=None):
    db = _resolve_db(db)
    hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        f"INSERT INTO {TABLE_MOTION_COMMENTS} (motion_type, motion_key, user_id, comment_text, created_at) "
        "VALUES (:type, :key, :uid, :text, :now)",
        {"type": motion_type, "key": motion_key, "uid": user_id, "text": text, "now": hk_now},
    )
    return hk_now


def ensure_ai_comment_account(db=None):
    db = _resolve_db(db)
    db.execute(
        f"""
        INSERT INTO {TABLE_ACCOUNTS} (user_id, password_hash, account_status, account_disabled)
        VALUES (:uid, '', 'inactive', TRUE)
        ON CONFLICT (user_id) DO UPDATE SET account_disabled = TRUE
        """,
        {"uid": "Gemini"},
    )


def fetch_vote_data(db=None):
    db = _resolve_db(db)
    df = db.query(
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
        """
    )
    df = df.fillna("")

    # Load ballots for pending topics only — historical ballots are not needed for the UI
    ballots = db.query(
        f"SELECT b.topic_text, b.user_id, b.vote_choice, b.against_reasons"
        f" FROM {TABLE_TOPIC_VOTE_BALLOTS} b"
        f" JOIN {TABLE_TOPIC_VOTES} tv ON b.topic_text = tv.topic_text"
        " WHERE tv.status = 'pending'"
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


def count_pending_votes(db=None):
    db = _resolve_db(db)
    df = db.query(
        f"SELECT COUNT(*) AS cnt FROM {TABLE_TOPIC_VOTES} WHERE status = 'pending'"
    )
    return int(df.iloc[0]["cnt"]) if not df.empty else 0


def count_pending_deposes(db=None):
    db = _resolve_db(db)
    df = db.query(
        f"SELECT COUNT(*) AS cnt FROM {TABLE_TOPIC_REMOVAL_VOTES} WHERE status = 'pending'"
    )
    return int(df.iloc[0]["cnt"]) if not df.empty else 0


# ─────────────────────────────────────────────────────────────
# Deadline expiry (mirrors vote.py's on-render auto-reject)
#
# A motion whose deadline has passed without reaching threshold is auto-rejected.
# Topic votes and depose motions both UPDATE status='rejected' (kept in history).
# Returns [{"topic","deadline"}] of the motions newly expired this call, so the
# API can surface the warning + fire the "逾期" push. Idempotent: an already
# resolved motion is not pending, so it never re-fires.
# ─────────────────────────────────────────────────────────────
def _expire_pending(table, db):
    df = db.query(f"SELECT topic_text, deadline_date FROM {table} WHERE status = 'pending'")
    expired = []
    for _, row in df.iterrows():
        passed, deadline_str = parse_deadline_row(row.to_dict())
        if not passed:
            continue
        if db.execute_count(
            f"UPDATE {table} SET status = 'rejected' WHERE topic_text = :t AND status = 'pending'",
            {"t": row["topic_text"]},
        ):
            expired.append({"topic": row["topic_text"], "deadline": deadline_str})
    return expired


def expire_pending_topic_votes(db=None):
    return _expire_pending(TABLE_TOPIC_VOTES, _resolve_db(db))


def expire_pending_depose_votes(db=None):
    return _expire_pending(TABLE_TOPIC_REMOVAL_VOTES, _resolve_db(db))


# ─────────────────────────────────────────────────────────────
# Single-motion queries (used by the cast endpoint to read fresh state)
#
# ``table`` is the *motion* table (TABLE_TOPIC_VOTES for topic votes,
# TABLE_TOPIC_REMOVAL_VOTES for deposition votes); the matching ballot table is
# resolved from it.
# ─────────────────────────────────────────────────────────────
def _ballot_table_for(table):
    return TABLE_TOPIC_VOTE_BALLOTS if table == TABLE_TOPIC_VOTES else TABLE_TOPIC_REMOVAL_VOTE_BALLOTS


def get_motion(table, topic, db=None):
    """Return the motion row as a dict (status, proposer, threshold, and for topic
    votes category/difficulty), or None if the motion does not exist."""
    db = _resolve_db(db)
    if table == TABLE_TOPIC_VOTES:
        cols = "proposer_user_id, status, approval_threshold, category, difficulty"
    else:
        cols = "proposer_user_id, status, approval_threshold"
    # A topic_text can have several rows (re-proposed after a prior reject/expire).
    # Prefer the still-pending row so voting acts on the live motion, not a stale
    # resolved one (else cast wrongly returns "motion already resolved").
    df = db.query(
        f"SELECT {cols} FROM {table} WHERE topic_text = :t "
        "ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, created_at DESC LIMIT 1",
        {"t": topic},
    )
    if df.empty:
        return None
    motion = df.iloc[0].to_dict()
    # Legacy deployment tables use a fixed-width status column. PostgreSQL
    # ignores its padding in SQL predicates, but a pandas value retains it.
    # Normalise at this core boundary so API callers match vote.py's SQL logic.
    motion["status"] = str(motion.get("status") or "").strip()
    return motion


def get_user_ballot(table, topic, user_id, db=None):
    """Return this user's current vote_choice ('agree'/'against') or None."""
    db = _resolve_db(db)
    bt = _ballot_table_for(table)
    df = db.query(
        f"SELECT vote_choice FROM {bt} WHERE topic_text = :t AND user_id = :u",
        {"t": topic, "u": user_id},
    )
    return None if df.empty else df.iloc[0]["vote_choice"]


def count_ballots(table, topic, db=None):
    """Return (agree_count, against_count) for a single motion."""
    db = _resolve_db(db)
    bt = _ballot_table_for(table)
    df = db.query(
        f"SELECT vote_choice, COUNT(*) AS cnt FROM {bt} WHERE topic_text = :t GROUP BY vote_choice",
        {"t": topic},
    )
    agree = against = 0
    for _, r in df.iterrows():
        if r["vote_choice"] == "agree":
            agree = int(r["cnt"])
        elif r["vote_choice"] == "against":
            against = int(r["cnt"])
    return agree, against


# ─────────────────────────────────────────────────────────────
# Bank / proposal helpers (topic proposal + deposition proposal)
# ─────────────────────────────────────────────────────────────
def list_bank_topics(db=None):
    """All topics currently in the bank (topic_text, category, difficulty)."""
    db = _resolve_db(db)
    df = db.query(f"SELECT topic_text, category, difficulty FROM {TABLE_TOPICS} ORDER BY topic_text")
    return [] if df.empty else df.fillna("").to_dict("records")


def topic_in_bank(topic, db=None):
    db = _resolve_db(db)
    df = db.query(f"SELECT 1 AS x FROM {TABLE_TOPICS} WHERE topic_text = :t LIMIT 1", {"t": topic})
    return not df.empty


def topic_vote_or_bank_exists(topic, db=None):
    """True if the topic is already a pending vote or already in the bank
    (matches vote.py's duplicate guard before proposing)."""
    db = _resolve_db(db)
    v = db.query(
        f"SELECT 1 AS x FROM {TABLE_TOPIC_VOTES} WHERE topic_text = :t AND status = 'pending' LIMIT 1",
        {"t": topic},
    )
    if not v.empty:
        return True
    return topic_in_bank(topic, db=db)


def depose_pending_exists(topic, db=None):
    db = _resolve_db(db)
    df = db.query(
        f"SELECT 1 AS x FROM {TABLE_TOPIC_REMOVAL_VOTES} WHERE topic_text = :t AND status = 'pending' LIMIT 1",
        {"t": topic},
    )
    return not df.empty


def category_current_ratio(category, db=None):
    """Current share of ``category`` in the bank as (ratio, cat_count, total).

    Matches vote.py's proposal-time imbalance check (cat_count / total, not the
    +1 projection used by check_category_would_exceed during voting)."""
    db = _resolve_db(db)
    df = db.query(f"SELECT category FROM {TABLE_TOPICS}")
    total = len(df)
    if total == 0:
        return 0.0, 0, 0
    cat_count = int((df["category"] == category).sum())
    return cat_count / total, cat_count, total


def insert_topic_vote(topic, proposer, category, difficulty, threshold, db=None):
    """Insert a pending topic vote (7-day deadline). Returns the deadline string."""
    db = _resolve_db(db)
    hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    hk_time = hk_now.strftime("%Y-%m-%d %H:%M:%S")
    deadline = (hk_now.date() + timedelta(days=7)).strftime("%Y-%m-%d")
    db.execute(
        f"INSERT INTO {TABLE_TOPIC_VOTES} "
        "(topic_text, proposer_user_id, status, created_at, deadline_date, approval_threshold, category, difficulty) "
        "VALUES (:t, :u, 'pending', :c, :d, :th, :cat, :diff)",
        {"t": topic, "u": proposer, "c": hk_time, "d": deadline, "th": threshold,
         "cat": category, "diff": difficulty},
    )
    return deadline


def insert_depose_vote(topic, proposer, reasons_json, threshold, db=None):
    """Insert a pending deposition vote (7-day deadline). Returns the deadline string."""
    db = _resolve_db(db)
    hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    hk_time = hk_now.strftime("%Y-%m-%d %H:%M:%S")
    deadline = (hk_now.date() + timedelta(days=7)).strftime("%Y-%m-%d")
    db.execute(
        f"INSERT INTO {TABLE_TOPIC_REMOVAL_VOTES} "
        "(topic_text, proposer_user_id, status, created_at, removal_reasons, deadline_date, approval_threshold) "
        "VALUES (:t, :u, 'pending', :c, :r, :d, :th)",
        {"t": topic, "u": proposer, "c": hk_time, "r": reasons_json, "d": deadline, "th": threshold},
    )
    return deadline


def fetch_depose_data(db=None):
    """Pending deposition motions with tallies, reasons and topic meta.

    Mirrors vote.py's depose tab (pending only). Each row: topic_text,
    proposer_user_id, removal_reasons (parsed list), created_at, deadline_date,
    approval_threshold, agree_users, against_users, category, difficulty.
    """
    db = _resolve_db(db)
    df = db.query(
        f"SELECT topic_text, proposer_user_id, status, removal_reasons, created_at, deadline_date, approval_threshold"
        f" FROM {TABLE_TOPIC_REMOVAL_VOTES} WHERE status = 'pending' ORDER BY created_at DESC"
    )
    df = df.fillna("")
    ballots = db.query(
        f"SELECT topic_text, user_id, vote_choice FROM {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS}"
    )
    agree_map, against_map = {}, {}
    if not ballots.empty:
        for _, b in ballots.iterrows():
            t = b["topic_text"]
            (agree_map if b["vote_choice"] == "agree" else against_map).setdefault(t, []).append(b["user_id"])

    meta_df = db.query(f"SELECT topic_text, category, difficulty FROM {TABLE_TOPICS}")
    meta = {}
    if not meta_df.empty:
        for _, r in meta_df.iterrows():
            meta[r["topic_text"]] = (r.get("category"), r.get("difficulty"))

    out = []
    for _, row in df.iterrows():
        rd = row.to_dict()
        t = rd["topic_text"]
        rd["agree_users"] = agree_map.get(t, [])
        rd["against_users"] = against_map.get(t, [])
        rd["removal_reasons"] = parse_reason_list(rd.get("removal_reasons", ""))
        cat, diff = meta.get(t, ("", ""))
        rd["category"] = cat or ""
        rd["difficulty"] = diff or ""
        out.append(rd)
    return out


def fetch_vote_history(limit=20, db=None):
    """Recently resolved topic-vote motions, matching the Streamlit expander."""
    db = _resolve_db(db)
    df = db.query(
        f"""
        SELECT tv.topic_text, tv.status, tv.created_at, tv.approval_threshold, tv.category,
               (SELECT COUNT(*) FROM {TABLE_TOPIC_VOTE_BALLOTS} b WHERE b.topic_text = tv.topic_text AND b.vote_choice = 'agree') AS agree,
               (SELECT COUNT(*) FROM {TABLE_TOPIC_VOTE_BALLOTS} b WHERE b.topic_text = tv.topic_text AND b.vote_choice != 'agree') AS against
        FROM {TABLE_TOPIC_VOTES} tv
        WHERE tv.status != 'pending'
        ORDER BY tv.created_at DESC
        LIMIT :limit
        """,
        {"limit": int(limit)},
    )
    if df.empty:
        return []
    rows = []
    for _, row in df.iterrows():
        created = row.get("created_at")
        rows.append({
            "topic_text": str(row.get("topic_text", "") or ""),
            "status": str(row.get("status", "") or ""),
            "created_at": str(created)[:10] if created not in (None, "") else "",
            "approval_threshold": int(row.get("approval_threshold") or 0),
            "category": str(row.get("category", "") or ""),
            "agree": int(row.get("agree") or 0),
            "against": int(row.get("against") or 0),
        })
    return rows


def fetch_vote_history_analysis_data(db=None):
    """Raw combined motion/ballot rows used by the AI analysis visual bars."""
    db = _resolve_db(db)
    return db.query(
        f"""
        SELECT
            '辯題投票' AS motion_type,
            tv.topic_text,
            tv.status,
            tv.proposer_user_id,
            tv.category,
            tv.difficulty,
            tv.created_at,
            b.user_id,
            b.vote_choice,
            b.against_reasons
        FROM {TABLE_TOPIC_VOTES} tv
        LEFT JOIN {TABLE_TOPIC_VOTE_BALLOTS} b ON b.topic_text = tv.topic_text
        UNION ALL
        SELECT
            '罷免投票' AS motion_type,
            rv.topic_text,
            rv.status,
            rv.proposer_user_id,
            t.category,
            t.difficulty,
            rv.created_at,
            b.user_id,
            b.vote_choice,
            NULL AS against_reasons
        FROM {TABLE_TOPIC_REMOVAL_VOTES} rv
        LEFT JOIN {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} b ON b.topic_text = rv.topic_text
        LEFT JOIN {TABLE_TOPICS} t ON t.topic_text = rv.topic_text
        ORDER BY created_at DESC
        """
    )


def vote_history_chart_data(vote_df):
    if vote_df.empty:
        return {"metrics": {"motions": 0, "ballots": 0, "avg_ballots": 0}, "charts": {}}
    df = vote_df.fillna("")
    motion_cols = ["motion_type", "topic_text", "status", "created_at", "category", "difficulty"]
    motions = df[motion_cols].drop_duplicates()
    ballots = df[df["user_id"].astype(str).str.strip() != ""].copy()

    status_counts = (
        motions["status"].replace({"pending": "進行中", "passed": "通過", "rejected": "否決"})
        .value_counts().rename_axis("label").reset_index(name="value")
    )
    member_counts = (
        ballots["user_id"].value_counts().rename_axis("label").reset_index(name="value")
        if not ballots.empty else None
    )
    topic_motions = motions[motions["motion_type"] == "辯題投票"].copy()
    category_rows = []
    if not topic_motions.empty:
        for cat, cat_df in topic_motions.groupby(topic_motions["category"].replace("", "未分類")):
            n = len(cat_df)
            passed = int((cat_df["status"] == "passed").sum())
            category_rows.append({"label": cat, "value": passed / n * 100 if n else 0})
    agree_rows = []
    if not ballots.empty:
        for uid, member_df in ballots.groupby("user_id"):
            n = len(member_df)
            agree = int((member_df["vote_choice"] == "agree").sum())
            agree_rows.append({"label": uid, "value": agree / n * 100 if n else 0})
    return {
        "metrics": {
            "motions": len(motions),
            "ballots": len(ballots),
            "avg_ballots": round(len(ballots) / len(motions), 1) if len(motions) else 0,
        },
        "charts": {
            "status": status_counts.to_dict("records"),
            "member_votes": [] if member_counts is None else member_counts.to_dict("records"),
            "category_pass_rate": category_rows,
            "member_agree_rate": agree_rows,
        },
    }


def system_config_get(key, db=None):
    db = _resolve_db(db)
    df = db.query("SELECT value FROM system_config WHERE key = :key", {"key": key})
    return None if df.empty else df.iloc[0]["value"]


def system_config_set_many(values, db=None):
    db = _resolve_db(db)
    hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
    for key, val in values.items():
        db.execute(
            "INSERT INTO system_config (key, value, updated_at) VALUES (:k, :v, :u) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
            {"k": key, "v": val, "u": hk_now},
        )
    return hk_now


def load_saved_analysis(kind, db=None):
    prefix = "vote_bank_analysis" if kind == "bank" else "vote_history_analysis"
    return {
        "analysis": system_config_get(prefix, db=db) or "",
        "analysed_at": system_config_get(f"{prefix}_at", db=db) or "",
        "analysed_by": system_config_get(f"{prefix}_by", db=db) or "",
        "source_signature": system_config_get(f"{prefix}_source_signature", db=db) or "",
    }


def analysis_source_signature(kind, db=None, vote_df=None):
    """Stable fingerprint of the source data used by a saved AI analysis."""
    db = _resolve_db(db)
    if kind == "bank":
        frame = db.query(
            f"SELECT topic_text, category, difficulty FROM {TABLE_TOPICS} ORDER BY topic_text"
        )
    elif kind == "history":
        frame = vote_df if vote_df is not None else fetch_vote_history_analysis_data(db=db)
    else:
        raise ValueError("kind must be 'bank' or 'history'")
    if frame.empty:
        payload = "[]"
    else:
        normalized = frame.fillna("").astype(str)
        normalized = normalized.sort_values(list(normalized.columns), kind="stable")
        payload = json.dumps(normalized.to_dict("records"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def save_analysis(kind, analysis_text, user_id, source_signature="", db=None):
    prefix = "vote_bank_analysis" if kind == "bank" else "vote_history_analysis"
    return system_config_set_many({
        prefix: analysis_text,
        f"{prefix}_at": datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S"),
        f"{prefix}_by": user_id or "",
        f"{prefix}_source_signature": source_signature or analysis_source_signature(kind, db=db),
    }, db=db)


def find_stale_removed_topics(db=None):
    db = _resolve_db(db)
    df = db.query(
        f"SELECT t.topic_text FROM {TABLE_TOPICS} t "
        f"JOIN {TABLE_TOPIC_REMOVAL_VOTES} r ON r.topic_text = t.topic_text "
        "WHERE r.status = 'passed'"
    )
    return [] if df.empty else df["topic_text"].tolist()


# ─────────────────────────────────────────────────────────────
# Auto-resolution DB effects
#
# Each returns the number of pending status rows updated (0 when the motion was
# already resolved), so the caller can decide whether to fire notifications.
# UI feedback (success/error banners, balloons, reruns) stays in vote.py.
# ─────────────────────────────────────────────────────────────
def apply_topic_pass(topic, author=None, category=None, difficulty=None, db=None):
    db = _resolve_db(db)
    db.execute(
        f"INSERT INTO {TABLE_TOPICS} (topic_text, author, category, difficulty) VALUES (:topic_text, :author, :category, :difficulty)",
        {"topic_text": topic, "author": author, "category": category, "difficulty": difficulty},
    )
    return db.execute_count(
        f"UPDATE {TABLE_TOPIC_VOTES} SET status = 'passed' WHERE topic_text = :topic_text AND status = 'pending'",
        {"topic_text": topic},
    )


def apply_topic_reject(topic, db=None):
    db = _resolve_db(db)
    return db.execute_count(
        f"UPDATE {TABLE_TOPIC_VOTES} SET status = 'rejected' WHERE topic_text = :topic_text AND status = 'pending'",
        {"topic_text": topic},
    )


def apply_depose_pass(topic, db=None):
    db = _resolve_db(db)
    updated = db.execute_count(
        f"UPDATE {TABLE_TOPIC_REMOVAL_VOTES} SET status = 'passed' WHERE topic_text = :topic_text AND status = 'pending'",
        {"topic_text": topic},
    )
    # Always remove the topic from the bank when a removal passes.
    # Decoupled from `updated` so it self-heals even if the status row was
    # already resolved (e.g. a prior run/session), preventing stale entries.
    db.execute(f"DELETE FROM {TABLE_TOPICS} WHERE topic_text = :topic_text", {"topic_text": topic})
    return updated


def apply_depose_reject(topic, db=None):
    db = _resolve_db(db)
    return db.execute_count(
        f"UPDATE {TABLE_TOPIC_REMOVAL_VOTES} SET status = 'rejected' WHERE topic_text = :topic_text AND status = 'pending'",
        {"topic_text": topic},
    )
