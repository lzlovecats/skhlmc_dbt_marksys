"""JSON endpoints backing topic proposals, ballots and removals.

Mounted by ``deploy/proxy.py`` via ``app.include_router(router)``. Auth and the
DB executor live in the proxy; they are pulled in lazily inside the handlers so
this module and the proxy don't form an import cycle at load time.

The read path and state transitions reuse ``core.vote_logic`` so HTTP and
domain code stay on one source of truth.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from account_access import AI_COMMENT_ACCOUNT_ID
from ai_model_config import NON_MANUAL_DEFAULT_AI_MODEL
from api.access import require_interactive_features_available, require_page_user
from system_limits import VOTE_PENDING_MOTION_LIMIT
from ai_name import LMC_AI_MODEL_LABEL

router = APIRouter(prefix="/api/vote", tags=["vote"])

VOTE_GEMINI_MODEL_LABEL = NON_MANUAL_DEFAULT_AI_MODEL


def _vote_ai_model(choice: str) -> str:
    return (
        LMC_AI_MODEL_LABEL
        if choice == "local"
        else VOTE_GEMINI_MODEL_LABEL
    )


def _labelled_ai_output(text: str, model_label: str) -> str:
    return f"**AI 模型：{model_label}**\n\n{text}"


def _require_vote_ai_available(choice: str, db) -> None:
    if choice != "local":
        return
    from core.lmc_ai_client import local_ai_availability
    from core.vote_ai import VOTE_LOCAL_AI_MODE

    status = _run_vote_ai(local_ai_availability(db))
    mode_status = next(
        (
            item for item in status.get("modes", [])
            if item.get("id") == VOTE_LOCAL_AI_MODE
        ),
        None,
    )
    if not status.get("available") or not mode_status or not mode_status.get("available"):
        raise HTTPException(
            503,
            (mode_status or {}).get("message")
            or status.get("message")
            or "自家 AI 暫時未準備好。",
        )


# ── dependencies (resolved from the proxy at request time) ────────────────────
def _committee_user(request: Request) -> str:
    """401 unless the request carries a valid committee cookie / bearer token."""
    user_id = require_page_user(request, "vote")
    require_interactive_features_available(request)
    return user_id


def _activity_payload(user_id: str, db) -> dict:
    from core.members import member_activity
    return member_activity(user_id, db=db)


def _vote_db():
    from deploy.proxy import get_vote_db
    return get_vote_db()


def _ai_secrets():
    from deploy.proxy import _get_proxy_secret
    return {
        "GEMINI_API_KEY": _get_proxy_secret("GEMINI_API_KEY"),
        "OPENROUTER_API_KEY": _get_proxy_secret("OPENROUTER_API_KEY"),
    }


def _run_vote_ai(awaitable):
    """Run async provider transport inside FastAPI's sync-route worker.

    Vote handlers deliberately remain synchronous because their DB and push
    dependencies are synchronous. FastAPI runs these handlers in its bounded
    worker pool, so this bridge avoids blocking the application's event loop or
    silently changing the existing request-concurrency boundary.
    """
    return asyncio.run(awaitable)


def _log_vote_ai(user_id, feature, text, usage, db):
    """Usage accounting must never turn a completed AI response into an error."""
    try:
        from core.funds_logic import log_ai_usage
        from core.vote_ai import is_successful_ai_result

        success = is_successful_ai_result(text)
        log_ai_usage(
            user_id, feature, success, usage=usage,
            error_message="" if success else str(text), db=db,
        )
    except Exception:
        pass


def _require_pending_motion(motion_type, motion_key, db):
    from schema import TABLE_TOPIC_REMOVAL_VOTES, TABLE_TOPIC_VOTES

    table = TABLE_TOPIC_VOTES if motion_type == "topic_vote" else TABLE_TOPIC_REMOVAL_VOTES
    rows = db.query(
        f"SELECT status FROM {table} WHERE topic_text=:topic AND status='pending' LIMIT 1",
        {"topic": motion_key},
    )
    if not rows.empty:
        return
    existing = db.query(f"SELECT 1 FROM {table} WHERE topic_text=:topic LIMIT 1", {"topic": motion_key})
    if existing.empty:
        raise HTTPException(404, "motion not found")
    raise HTTPException(409, "motion already resolved")


def _validate_ai_selection(category, difficulty):
    from core.vote_ai import CATEGORIES, DIFFICULTY_OPTIONS

    if category not in CATEGORIES:
        raise HTTPException(400, "不支援的辯題類別")
    if difficulty not in DIFFICULTY_OPTIONS:
        raise HTTPException(400, "辯題難度必須為 1、2 或 3")


@router.get("/ai-status")
async def ai_status(request: Request):
    _committee_user(request)
    from core.lmc_ai_client import local_ai_availability
    from core.vote_ai import VOTE_LOCAL_AI_MODE

    status = await local_ai_availability(_vote_db())
    status["required_mode"] = VOTE_LOCAL_AI_MODE
    return JSONResponse(status, headers={"Cache-Control": "no-store"})


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
        "comment_count": int(row.get("comment_count") or 0),
        # Per-topic stored threshold. The dynamic active-member threshold is
        # surfaced in a later slice once get_active_user_count moves into core.
        "approval_threshold": threshold,
    }


@router.get("/data")
def vote_data(user_id: str = Depends(_committee_user)):
    """Read-only vote board: pending motions plus resolved-topic names."""
    from core.vote_logic import fetch_vote_data, count_pending_deposes, get_comment_counts, fetch_vote_history
    from core.vote_logic import expire_pending_topic_votes
    from core.vote_logic import entry_threshold, depose_threshold
    from core.members import count_active_members

    db = _vote_db()
    expired = expire_pending_topic_votes(db=db)
    for e in expired:
        _fire_push(db, "辯題投票逾期", f"「{e['topic']}」未達入庫標準，已自動否決。",
                   f"topic-vote-expired-{e['topic']}")
    pending, passed, rejected = fetch_vote_data(db=db, resolved_limit=0)
    comment_counts = get_comment_counts("topic_vote", db=db)
    for row in pending:
        row["comment_count"] = comment_counts.get(row.get("topic_text"), 0)
    active_count = count_active_members(db=db)
    return {
        "user_id": user_id,
        "pending": [_jsonify_pending(row) for row in pending],
        "passed": passed,
        "rejected": rejected,
        "history": fetch_vote_history(limit=20, db=db),
        "pending_vote_count": len(pending),
        "pending_depose_count": count_pending_deposes(db=db),
        "active_count": active_count,
        "entry_threshold": entry_threshold(active_count),
        "depose_threshold": depose_threshold(active_count),
        "expired": expired,
        **_activity_payload(user_id, db),
    }


# ── cast a vote ───────────────────────────────────────────────────────────────
class CastBody(BaseModel):
    mode: str = Field(max_length=20)                       # "topic" | "depose"
    topic: str = Field(max_length=500)
    action: str = Field(max_length=20)                     # "agree" | "against" | "withdraw"
    reasons: list[str] | None = Field(default=None, max_length=10)  # required for a topic "against" vote
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


def _apply_auto_resolution(
    vl, mode, topic, motion, agree_count, against_count, threshold, db
):
    """Apply a resolution and return ``(status, changed)`` without network I/O."""
    outcome = vl.resolve_vote(agree_count, against_count, threshold)
    if outcome is None:
        return None, False
    if mode == "topic":
        if outcome == "pass":
            changed = vl.apply_topic_pass(
                topic,
                author=motion.get("proposer_user_id"),
                category=motion.get("category"),
                difficulty=motion.get("difficulty"),
                db=db,
            )
            resolved = "passed"
        else:
            changed = vl.apply_topic_reject(topic, db=db)
            resolved = "rejected"
    else:
        # depose: "pass" == the removal motion carries (topic deleted)
        if outcome == "pass":
            changed = vl.apply_depose_pass(topic, db=db)
            resolved = "passed"
        else:
            changed = vl.apply_depose_reject(topic, db=db)
            resolved = "rejected"
    return resolved, bool(changed)


def _auto_resolve(vl, mode, topic, motion, agree_count, against_count, threshold, db):
    """Compatibility helper: resolve and send a best-effort notification."""
    resolved, changed = _apply_auto_resolution(
        vl, mode, topic, motion, agree_count, against_count, threshold, db
    )
    if changed:
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
    reasons = None
    if body.action == "against" and body.mode == "topic":
        reasons = [
            str(reason).strip()
            for reason in (body.reasons or [])
            if str(reason).strip()
        ]
        if not reasons:
            raise HTTPException(
                400, "a topic against-vote requires at least one reason"
            )
        if any(len(reason) > 500 for reason in reasons):
            raise HTTPException(400, "每個反對原因最多 500 個字")

    table = TABLE_TOPIC_VOTES if body.mode == "topic" else TABLE_TOPIC_REMOVAL_VOTES
    db = _vote_db()
    resolution_changed = False
    with vl.motion_transaction(db, table, topic) as transaction_db:
        motion = vl.get_motion(table, topic, db=transaction_db)
        if motion is None:
            raise HTTPException(404, "motion not found")
        if motion.get("status") != "pending":
            raise HTTPException(409, "motion already resolved")

        current = vl.get_user_ballot(table, topic, user_id, db=transaction_db)

        if body.action == "withdraw":
            vl.ballot_delete(table, topic, user_id, db=transaction_db)
        elif body.action == "agree":
            # The category guard and ballot transition are protected by the same
            # motion lock, so two voters cannot resolve from stale tallies.
            if body.mode == "topic" and current != "agree":
                exceeds, ratio, cat_count, total = vl.check_category_would_exceed(
                    motion.get("category"), db=transaction_db
                )
                if exceeds and not body.confirm_category:
                    return {
                        "status": "confirm_category",
                        "category": motion.get("category"),
                        "ratio": ratio,
                        "cat_count": cat_count,
                        "total": total,
                    }
            if current == "against":
                vl.ballot_switch_agree(table, topic, user_id, db=transaction_db)
            else:
                vl.ballot_upsert(
                    table, topic, user_id, "agree", db=transaction_db
                )
        else:
            vl.ballot_upsert(
                table,
                topic,
                user_id,
                "against",
                reasons=vl.dump_json(reasons) if reasons is not None else None,
                db=transaction_db,
            )

        agree_count, against_count = vl.count_ballots(
            table, topic, db=transaction_db
        )
        threshold = _motion_threshold(motion, body.mode, transaction_db)
        resolved, resolution_changed = _apply_auto_resolution(
            vl,
            body.mode,
            topic,
            motion,
            agree_count,
            against_count,
            threshold,
            transaction_db,
        )

    # Do external notification work after commit. A push failure must not roll
    # back a valid ballot or hold the per-motion database lock open.
    if resolution_changed:
        _fire_resolution_push(db, body.mode, topic, resolved)

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
        "comment_count": int(row.get("comment_count") or 0),
        "approval_threshold": threshold,
    }


@router.get("/depose-data")
def depose_data(user_id: str = Depends(_committee_user)):
    """Deposition board: pending removal motions + the bank topics that can be
    proposed for removal."""
    from core.vote_logic import fetch_depose_data, list_bank_topics, depose_threshold, get_comment_counts
    from core.vote_logic import expire_pending_depose_votes
    from core.members import count_active_members

    db = _vote_db()
    expired = expire_pending_depose_votes(db=db)
    for e in expired:
        _fire_push(db, "罷免動議逾期", f"「{e['topic']}」的罷免動議未達標準，已自動取消。",
                   f"topic-removal-expired-{e['topic']}")
    pending = fetch_depose_data(db=db)
    comment_counts = get_comment_counts("topic_removal", db=db)
    for row in pending:
        row["comment_count"] = comment_counts.get(row.get("topic_text"), 0)
    pending_topics = {r["topic_text"] for r in pending}
    bank = [t for t in list_bank_topics(db=db) if t["topic_text"] not in pending_topics]
    return {
        "user_id": user_id,
        "pending": [_jsonify_depose(row) for row in pending],
        "pending_depose_count": len(pending),
        "depose_threshold": depose_threshold(count_active_members(db=db)),
        "expired": expired,
        **_activity_payload(user_id, db),
        "bank_topics": bank,
    }


@router.get("/member-stats")
def member_stats(user_id: str = Depends(_committee_user)):
    """Committee-only participation table for the HTML member-stats tab."""
    from core.members import get_member_participation_stats, count_active_members

    db = _vote_db()
    stats, total_vote_count = get_member_participation_stats(db=db)
    current = next(
        (s for s in stats if str(s.get("用戶", "")).strip() == str(user_id).strip()),
        None,
    )
    return {
        "user_id": user_id,
        "stats": stats,
        "current_user_stats": current,
        "total_vote_count": total_vote_count,
        "active_count": count_active_members(db=db),
    }


class CommentBody(BaseModel):
    motion_type: str = Field(max_length=30)
    motion_key: str = Field(max_length=500)
    text: str | None = Field(default=None, max_length=2000)
    tag_ai: bool = False
    ai_model: Literal["local", "gemini"] = "local"


def _validate_motion_type(value: str) -> str:
    if value not in ("topic_vote", "topic_removal"):
        raise HTTPException(400, "invalid motion_type")
    return value


@router.get("/comments")
def comments(motion_type: str, motion_key: str, user_id: str = Depends(_committee_user)):
    from core.vote_logic import fetch_comments

    db = _vote_db()
    motion_type = _validate_motion_type(motion_type)
    motion_key = (motion_key or "").strip()
    if not motion_key:
        raise HTTPException(400, "motion_key is required")
    _require_pending_motion(motion_type, motion_key, db)
    return {
        "user_id": user_id,
        "comments": fetch_comments(motion_type, motion_key, db=db),
    }


@router.post("/comments")
def post_comment(body: CommentBody, user_id: str = Depends(_committee_user)):
    from core import vote_logic as vl
    from core import vote_ai

    motion_type = _validate_motion_type(body.motion_type)
    motion_key = (body.motion_key or "").strip()
    if not motion_key:
        raise HTTPException(400, "motion_key is required")

    db = _vote_db()
    _require_pending_motion(motion_type, motion_key, db)
    text = (body.text or "").strip()
    ai_text = ""
    should_ai = bool(body.tag_ai)
    question = vote_ai.extract_gemini_question(text) if text else None
    if question is not None:
        should_ai = True
    if should_ai:
        _require_vote_ai_available(body.ai_model, db)
    if text:
        try:
            vl.insert_comment(motion_type, motion_key, user_id, text, db=db)
        except ValueError as exc:
            raise HTTPException(429, str(exc)) from exc
        snippet = text if len(text) <= 40 else text[:40] + "⋯"
        topic_label = motion_key if len(motion_key) <= 20 else motion_key[:20] + "⋯"
        _fire_push(db, "💬 新留言", f"{user_id} 在「{topic_label}」發表意見：{snippet}",
                   f"comment-{motion_type}-{motion_key}", exclude_user=user_id)
    elif not body.tag_ai:
        raise HTTPException(400, "請輸入內容")

    if should_ai:
        vl.ensure_ai_comment_account(db=db)
        existing = vl.fetch_comments(motion_type, motion_key, db=db)
        model_label = _vote_ai_model(body.ai_model)
        ai_text, usage = _run_vote_ai(vote_ai.discussion_reply(
            motion_type, motion_key, existing, db, _ai_secrets(), question=question,
            model_label=model_label,
        ))
        _log_vote_ai(user_id, "vote_discussion", ai_text, usage, db)
        if vote_ai.is_successful_ai_result(ai_text):
            vl.insert_comment(
                motion_type, motion_key, AI_COMMENT_ACCOUNT_ID,
                _labelled_ai_output(ai_text, model_label), db=db
            )
        else:
            return {
                "status": "ai_failed" if text else "failed",
                "message": ai_text,
                "comments": vl.fetch_comments(motion_type, motion_key, db=db),
            }

    return {
        "status": "ok",
        "ai_replied": should_ai and bool(ai_text),
        "comments": vl.fetch_comments(motion_type, motion_key, db=db),
    }


class AiReviewBody(BaseModel):
    topic: str = Field(max_length=500)
    category: str = Field(max_length=80)
    difficulty: int
    ai_model: Literal["local", "gemini"] = "local"


@router.post("/ai-review")
def ai_review(body: AiReviewBody, user_id: str = Depends(_committee_user)):
    from core.vote_ai import review_topic

    topic = (body.topic or "").strip()
    if not topic:
        raise HTTPException(400, "請先輸入完整辯題")
    _validate_ai_selection(body.category, body.difficulty)
    db = _vote_db()
    _require_vote_ai_available(body.ai_model, db)
    model_label = _vote_ai_model(body.ai_model)
    text, usage = _run_vote_ai(
        review_topic(
            topic, body.category, body.difficulty, db, _ai_secrets(), model_label
        )
    )
    _log_vote_ai(user_id, "vote_review", text, usage, db)
    return {"status": "ok", "review": text, "model_label": model_label}


@router.get("/analysis")
def analysis(user_id: str = Depends(_committee_user)):
    from core.vote_logic import (
        find_stale_removed_topics,
        fetch_vote_history_analysis_data,
        analysis_source_signature,
        load_saved_analysis,
        vote_history_chart_data,
    )

    db = _vote_db()
    history_df = fetch_vote_history_analysis_data(db=db)
    bank_saved = load_saved_analysis("bank", db=db)
    history_saved = load_saved_analysis("history", db=db)
    bank_signature = analysis_source_signature("bank", db=db)
    history_signature = analysis_source_signature("history", db=db, vote_df=history_df)
    bank_saved["source_changed"] = bool(bank_saved["analysis"]) and bank_saved.get("source_signature") != bank_signature
    history_saved["source_changed"] = bool(history_saved["analysis"]) and history_saved.get("source_signature") != history_signature
    return {
        "user_id": user_id,
        "stale_topics": find_stale_removed_topics(db=db),
        "bank": bank_saved,
        "history": history_saved,
        "history_visuals": vote_history_chart_data(history_df),
    }


class AiAnalysisBody(BaseModel):
    kind: str = Field(max_length=20)
    ai_model: Literal["local", "gemini"] = "local"


@router.post("/analysis/ai")
def run_analysis(body: AiAnalysisBody, user_id: str = Depends(_committee_user)):
    from core import vote_logic as vl
    from core import vote_ai

    db = _vote_db()
    _require_vote_ai_available(body.ai_model, db)
    model_label = _vote_ai_model(body.ai_model)
    if body.kind == "bank":
        source_signature = vl.analysis_source_signature("bank", db=db)
        text, usage = _run_vote_ai(vote_ai.analyze_topic_bank(
            db, _ai_secrets(), model_label
        ))
        if vote_ai.is_successful_ai_result(text):
            vl.save_analysis(
                "bank", _labelled_ai_output(text, model_label), user_id,
                source_signature=source_signature, db=db,
            )
    elif body.kind == "history":
        history_df = vl.fetch_vote_history_analysis_data(db=db)
        source_signature = vl.analysis_source_signature("history", db=db, vote_df=history_df)
        text, usage = _run_vote_ai(
            vote_ai.analyze_vote_history(
                history_df, db, _ai_secrets(), model_label
            )
        )
        if vote_ai.is_successful_ai_result(text):
            vl.save_analysis(
                "history", _labelled_ai_output(text, model_label), user_id,
                source_signature=source_signature, db=db,
            )
    else:
        raise HTTPException(400, "kind must be 'bank' or 'history'")
    _log_vote_ai(user_id, "vote_analysis", text, usage, db)
    if not vote_ai.is_successful_ai_result(text):
        raise HTTPException(502, text)
    return {"status": "ok", "analysis": text, "model_label": model_label}


class ProposeBody(BaseModel):
    topic: str = Field(max_length=500)
    category: str = Field(max_length=80)
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
    _validate_ai_selection(body.category, body.difficulty)

    db = _vote_db()
    if not is_active_member(user_id, db=db):
        raise HTTPException(403, "非活躍成員不能提出新辯題")
    if vl.count_pending_votes(db=db) >= VOTE_PENDING_MOTION_LIMIT:
        raise HTTPException(409, f"目前已有 {VOTE_PENDING_MOTION_LIMIT} 個待表決辯題，請先完成投票再提交")
    if vl.topic_vote_or_bank_exists(topic, db=db):
        raise HTTPException(409, "此辯題已存在")

    ratio, cat_count, total = vl.category_current_ratio(body.category, db=db)
    if total >= vl.TOPIC_BANK_MAX:
        raise HTTPException(409, "辯題庫已達保護上限，請先整理或罷免舊辯題")
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
    topic: str | None = Field(default=None, max_length=500)
    topics: list[str] | None = Field(default=None, max_length=10)
    reasons: list[str] = Field(max_length=10)


@router.post("/depose")
def depose(body: DeposeBody, user_id: str = Depends(_committee_user)):
    """Propose a deposition (removal) motion against a bank topic — mirrors vote.py."""
    from core import vote_logic as vl
    from core.members import is_active_member, count_active_members

    raw_topics = [str(t).strip() for t in (body.topics or []) if str(t).strip()]
    if any(len(topic) > 500 for topic in raw_topics):
        raise HTTPException(400, "每個辯題最多 500 個字")
    topics = list(dict.fromkeys(raw_topics))
    if body.topic:
        single = str(body.topic).strip()
        if single and single not in topics:
            topics.insert(0, single)
    if not topics:
        raise HTTPException(400, "請選擇要罷免的辯題")
    reasons = [str(r).strip() for r in (body.reasons or []) if str(r).strip()]
    if not reasons:
        raise HTTPException(400, "請至少交代一個罷免原因")
    if any(len(reason) > 500 for reason in reasons):
        raise HTTPException(400, "每個罷免原因最多 500 個字")

    db = _vote_db()
    if not is_active_member(user_id, db=db):
        raise HTTPException(403, "非活躍成員不能提出罷免動議")
    if vl.count_pending_deposes(db=db) >= VOTE_PENDING_MOTION_LIMIT:
        raise HTTPException(409, f"目前已有 {VOTE_PENDING_MOTION_LIMIT} 個罷免動議，請先完成投票再提交")

    missing = [topic for topic in topics if not vl.topic_in_bank(topic, db=db)]
    if missing:
        raise HTTPException(404, f"「{missing[0]}」不在辯題庫中")

    threshold = vl.depose_threshold(count_active_members(db=db))
    created = []
    skipped = []
    deadline = None
    pending_now = vl.count_pending_deposes(db=db)
    for topic in topics:
        if pending_now >= VOTE_PENDING_MOTION_LIMIT:
            skipped.append(topic)
            continue
        if vl.depose_pending_exists(topic, db=db):
            skipped.append(topic)
            continue
        deadline = vl.insert_depose_vote(topic, user_id, vl.dump_json(reasons), threshold, db=db)
        pending_now += 1
        created.append(topic)
        _fire_push(db, "新罷免動議待投票",
                   f"「{topic}」已提出罷免動議，截止日期為 {deadline}。",
                   f"topic-removal-new-{topic}", exclude_user=user_id)
    if not created:
        raise HTTPException(409, "所選辯題已有待處理的罷免動議，或待處理動議已達上限")
    return {"status": "ok", "deadline": deadline, "threshold": threshold, "created": created, "skipped": skipped}
