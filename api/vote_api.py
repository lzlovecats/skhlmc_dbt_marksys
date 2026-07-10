"""JSON endpoints backing the HTML voting page (Phase 2 vertical slice).

Mounted by ``deploy/proxy.py`` via ``app.include_router(router)``. Auth and the
DB executor live in the proxy; they are pulled in lazily inside the handlers so
this module and the proxy don't form an import cycle at load time.

The read path reuses ``core.vote_logic.fetch_vote_data`` unchanged — the same
function the Streamlit page uses — so both UIs stay on one source of truth.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/vote", tags=["vote"])


# ── dependencies (resolved from the proxy at request time) ────────────────────
def _committee_user(request: Request) -> str:
    """401 unless the request carries a valid committee cookie / bearer token."""
    from deploy.proxy import _require_committee_user
    return _require_committee_user(request)


def _vote_db():
    from deploy.proxy import get_vote_db
    return get_vote_db()


# ── serialization ─────────────────────────────────────────────────────────────
def _jsonify_pending(row: dict) -> dict:
    """Shape one pending-topic dict from vote_logic into a JSON-safe payload."""
    from core.vote_logic import parse_deadline_row

    deadline_passed, deadline_str = parse_deadline_row(row)
    agree_users = row.get("agree_users", []) or []
    against_users = row.get("against_users", []) or []
    threshold_raw = row.get("approval_threshold")
    try:
        threshold = int(threshold_raw) if threshold_raw not in (None, "") else None
    except (TypeError, ValueError):
        threshold = None
    created = row.get("created_at", "")
    return {
        "topic_text": row.get("topic_text", ""),
        "proposer_user_id": row.get("proposer_user_id", ""),
        "category": row.get("category", "") or "",
        "difficulty": row.get("difficulty", "") or "",
        "created_at": str(created) if created not in (None, "") else "",
        "deadline": deadline_str,
        "deadline_passed": deadline_passed,
        "agree_users": list(agree_users),
        "against_users": list(against_users),
        "agree_count": len(agree_users),
        "against_count": len(against_users),
        "against_reasons": row.get("against_reasons", {}) or {},
        # Per-topic stored threshold. The dynamic active-member threshold is
        # surfaced in a later slice once get_active_user_count moves into core.
        "approval_threshold": threshold,
    }


@router.get("/data")
def vote_data(user_id: str = Depends(_committee_user)):
    """Read-only vote board: pending motions plus resolved-topic names."""
    from core.vote_logic import fetch_vote_data, count_pending_deposes

    from core.vote_logic import entry_threshold, depose_threshold
    from core.members import count_active_members

    db = _vote_db()
    pending, passed, rejected = fetch_vote_data(db=db)
    active_count = count_active_members(db=db)
    return {
        "user_id": user_id,
        "pending": [_jsonify_pending(row) for row in pending],
        "passed": passed,
        "rejected": rejected,
        "pending_vote_count": len(pending),
        "pending_depose_count": count_pending_deposes(db=db),
        "active_count": active_count,
        "entry_threshold": entry_threshold(active_count),
        "depose_threshold": depose_threshold(active_count),
    }


# ── cast a vote ───────────────────────────────────────────────────────────────
class CastBody(BaseModel):
    mode: str                       # "topic" | "depose"
    topic: str
    action: str                     # "agree" | "against" | "withdraw"
    reasons: list[str] | None = None  # required for a topic "against" vote
    confirm_category: bool = False    # set true to proceed past the >20% warning


def _motion_threshold(motion: dict, mode: str, db) -> int:
    """The motion's own stored approval_threshold, or the current dynamic one.

    Every app-created motion stores its threshold at proposal time, matching
    vote.py's ``int(row.approval_threshold or ENTRY_THRESHOLD)``. The dynamic
    fallback only bites on legacy rows with a null threshold.
    """
    raw = motion.get("approval_threshold")
    try:
        if raw not in (None, ""):
            return int(raw)
    except (TypeError, ValueError):
        pass
    from core.vote_logic import entry_threshold, depose_threshold
    from core.members import count_active_members

    active = count_active_members(db=db)
    return entry_threshold(active) if mode == "topic" else depose_threshold(active)


# Titles/bodies/tags identical to vote.py's notify_vote_event calls.
_RESOLUTION_PUSH = {
    ("topic", "passed"): ("辯題投票通過", "「{t}」已通過並加入辯題庫。", "topic-vote-passed-{t}"),
    ("topic", "rejected"): ("辯題投票否決", "「{t}」已被否決。", "topic-vote-rejected-{t}"),
    ("depose", "passed"): ("罷免動議通過", "「{t}」已被罷免並從辯題庫移除。", "topic-removal-passed-{t}"),
    ("depose", "rejected"): ("罷免動議否決", "「{t}」的罷免動議已被否決。", "topic-removal-rejected-{t}"),
}


def _fire_push(db, title, body, tag, exclude_user=None):
    """Best-effort committee push. Never raises (a push failure must not fail the
    vote/proposal). No-op when VAPID is unconfigured."""
    try:
        from deploy.proxy import _get_vapid
        from core.push import notify_committee

        vapid = _get_vapid()
        if not vapid:
            return
        notify_committee(db, vapid, title, body, tag=tag, exclude_user=exclude_user, url="/vote")
    except Exception:
        pass


def _fire_resolution_push(db, mode, topic, resolved):
    spec = _RESOLUTION_PUSH.get((mode, resolved))
    if not spec:
        return
    title, body_tmpl, tag_tmpl = spec
    _fire_push(db, title, body_tmpl.format(t=topic), tag_tmpl.format(t=topic))


def _auto_resolve(vl, mode, topic, motion, agree_count, against_count, threshold, db):
    """Apply the same auto-resolution the Streamlit page runs after each vote,
    then fire the matching committee push. Returns "passed", "rejected" or None."""
    outcome = vl.resolve_vote(agree_count, against_count, threshold)
    if outcome is None:
        return None
    if mode == "topic":
        if outcome == "pass":
            vl.apply_topic_pass(
                topic,
                author=motion.get("proposer_user_id"),
                category=motion.get("category"),
                difficulty=motion.get("difficulty"),
                db=db,
            )
            resolved = "passed"
        else:
            vl.apply_topic_reject(topic, db=db)
            resolved = "rejected"
    else:
        # depose: "pass" == the removal motion carries (topic deleted)
        if outcome == "pass":
            vl.apply_depose_pass(topic, db=db)
            resolved = "passed"
        else:
            vl.apply_depose_reject(topic, db=db)
            resolved = "rejected"
    _fire_resolution_push(db, mode, topic, resolved)
    return resolved


@router.post("/cast")
def vote_cast(body: CastBody, user_id: str = Depends(_committee_user)):
    """Cast / change / withdraw a vote, then auto-resolve — mirrors vote.py.

    Response ``status`` is "ok" on success, or "confirm_category" (HTTP 200, no
    write) when a topic agree-vote would push its category past 20% of the bank;
    resend with ``confirm_category: true`` to proceed.
    """
    from core import vote_logic as vl
    from schema import TABLE_TOPIC_VOTES, TABLE_TOPIC_REMOVAL_VOTES

    if body.mode not in ("topic", "depose"):
        raise HTTPException(400, "mode must be 'topic' or 'depose'")
    if body.action not in ("agree", "against", "withdraw"):
        raise HTTPException(400, "action must be 'agree', 'against' or 'withdraw'")
    topic = (body.topic or "").strip()
    if not topic:
        raise HTTPException(400, "topic is required")

    table = TABLE_TOPIC_VOTES if body.mode == "topic" else TABLE_TOPIC_REMOVAL_VOTES
    db = _vote_db()

    motion = vl.get_motion(table, topic, db=db)
    if motion is None:
        raise HTTPException(404, "motion not found")
    if motion.get("status") != "pending":
        raise HTTPException(409, "motion already resolved")

    current = vl.get_user_ballot(table, topic, user_id, db=db)

    if body.action == "withdraw":
        vl.ballot_delete(table, topic, user_id, db=db)
    elif body.action == "agree":
        # Category-balance guard only applies to topic votes transitioning to agree.
        if body.mode == "topic" and current != "agree":
            exceeds, ratio, cat_count, total = vl.check_category_would_exceed(motion.get("category"), db=db)
            if exceeds and not body.confirm_category:
                return {
                    "status": "confirm_category",
                    "category": motion.get("category"),
                    "ratio": ratio,
                    "cat_count": cat_count,
                    "total": total,
                }
        if current == "against":
            vl.ballot_switch_agree(table, topic, user_id, db=db)
        else:
            vl.ballot_upsert(table, topic, user_id, "agree", db=db)
    else:  # against
        if body.mode == "topic":
            reasons = [str(r).strip() for r in (body.reasons or []) if str(r).strip()]
            if not reasons:
                raise HTTPException(400, "a topic against-vote requires at least one reason")
            vl.ballot_upsert(table, topic, user_id, "against", reasons=vl.dump_json(reasons), db=db)
        else:
            vl.ballot_upsert(table, topic, user_id, "against", db=db)

    agree_count, against_count = vl.count_ballots(table, topic, db=db)
    threshold = _motion_threshold(motion, body.mode, db)
    resolved = _auto_resolve(vl, body.mode, topic, motion, agree_count, against_count, threshold, db)

    return {
        "status": "ok",
        "action": body.action,
        "agree_count": agree_count,
        "against_count": against_count,
        "threshold": threshold,
        "resolved": resolved,
    }


# ── deposition board + proposals ──────────────────────────────────────────────
def _jsonify_depose(row: dict) -> dict:
    from core.vote_logic import parse_deadline_row

    deadline_passed, deadline_str = parse_deadline_row(row)
    agree_users = row.get("agree_users", []) or []
    against_users = row.get("against_users", []) or []
    threshold_raw = row.get("approval_threshold")
    try:
        threshold = int(threshold_raw) if threshold_raw not in (None, "") else None
    except (TypeError, ValueError):
        threshold = None
    return {
        "topic_text": row.get("topic_text", ""),
        "proposer_user_id": row.get("proposer_user_id", ""),
        "category": row.get("category", "") or "",
        "difficulty": row.get("difficulty", "") or "",
        "reasons": row.get("removal_reasons", []) or [],
        "deadline": deadline_str,
        "deadline_passed": deadline_passed,
        "agree_users": list(agree_users),
        "against_users": list(against_users),
        "agree_count": len(agree_users),
        "against_count": len(against_users),
        "approval_threshold": threshold,
    }


@router.get("/depose-data")
def depose_data(user_id: str = Depends(_committee_user)):
    """Deposition board: pending removal motions + the bank topics that can be
    proposed for removal."""
    from core.vote_logic import fetch_depose_data, list_bank_topics, depose_threshold
    from core.members import count_active_members

    db = _vote_db()
    pending = fetch_depose_data(db=db)
    pending_topics = {r["topic_text"] for r in pending}
    bank = [t for t in list_bank_topics(db=db) if t["topic_text"] not in pending_topics]
    return {
        "user_id": user_id,
        "pending": [_jsonify_depose(row) for row in pending],
        "pending_depose_count": len(pending),
        "depose_threshold": depose_threshold(count_active_members(db=db)),
        "bank_topics": bank,
    }


class ProposeBody(BaseModel):
    topic: str
    category: str
    difficulty: int   # 1 / 2 / 3, matching vote.py's difficulty selectbox
    confirm_imbalance: bool = False


@router.post("/propose")
def propose(body: ProposeBody, user_id: str = Depends(_committee_user)):
    """Propose a new topic for voting — mirrors vote.py's proposal tab.

    Returns status "confirm_imbalance" (HTTP 200, no write) when the category is
    already >20% of the bank; resend with ``confirm_imbalance: true`` to proceed.
    """
    from core import vote_logic as vl
    from core.members import is_active_member, count_active_members

    topic = (body.topic or "").strip()
    if not topic:
        raise HTTPException(400, "請輸入辯題內容")

    db = _vote_db()
    if not is_active_member(user_id, db=db):
        raise HTTPException(403, "非活躍成員不能提出新辯題")
    if vl.count_pending_votes(db=db) >= 10:
        raise HTTPException(409, "目前已有 10 個待表決辯題，請先完成投票再提交")
    if vl.topic_vote_or_bank_exists(topic, db=db):
        raise HTTPException(409, "此辯題已存在")

    ratio, cat_count, total = vl.category_current_ratio(body.category, db=db)
    if ratio > 0.2 and not body.confirm_imbalance:
        return {
            "status": "confirm_imbalance",
            "category": body.category,
            "ratio": ratio,
            "cat_count": cat_count,
            "total": total,
        }

    threshold = vl.entry_threshold(count_active_members(db=db))
    deadline = vl.insert_topic_vote(topic, user_id, body.category, body.difficulty, threshold, db=db)
    _fire_push(db, "新辯題待投票",
               f"「{topic}」已加入投票區，截止日期為 {deadline}。",
               f"topic-vote-new-{topic}", exclude_user=user_id)
    return {"status": "ok", "deadline": deadline, "threshold": threshold}


class DeposeBody(BaseModel):
    topic: str
    reasons: list[str]


@router.post("/depose")
def depose(body: DeposeBody, user_id: str = Depends(_committee_user)):
    """Propose a deposition (removal) motion against a bank topic — mirrors vote.py."""
    from core import vote_logic as vl
    from core.members import is_active_member, count_active_members

    topic = (body.topic or "").strip()
    if not topic:
        raise HTTPException(400, "請選擇要罷免的辯題")
    reasons = [str(r).strip() for r in (body.reasons or []) if str(r).strip()]
    if not reasons:
        raise HTTPException(400, "請至少交代一個罷免原因")

    db = _vote_db()
    if not is_active_member(user_id, db=db):
        raise HTTPException(403, "非活躍成員不能提出罷免動議")
    if not vl.topic_in_bank(topic, db=db):
        raise HTTPException(404, "該辯題不在辯題庫中")
    if vl.count_pending_deposes(db=db) >= 10:
        raise HTTPException(409, "目前已有 10 個罷免動議，請先完成投票再提交")
    if vl.depose_pending_exists(topic, db=db):
        raise HTTPException(409, "該辯題已有待處理的罷免動議")

    threshold = vl.depose_threshold(count_active_members(db=db))
    deadline = vl.insert_depose_vote(topic, user_id, vl.dump_json(reasons), threshold, db=db)
    _fire_push(db, "新罷免動議待投票",
               f"「{topic}」已提出罷免動議，截止日期為 {deadline}。",
               f"topic-removal-new-{topic}", exclude_user=user_id)
    return {"status": "ok", "deadline": deadline, "threshold": threshold}
