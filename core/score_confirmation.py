"""Side-scoped score-sheet confirmation workflow."""

from __future__ import annotations

import datetime as dt
import secrets
from zoneinfo import ZoneInfo

from sqlalchemy import text

from core.vote_logic import _resolve_db
from schema import (
    TABLE_MATCHES,
    TABLE_SCORE_DRAFTS,
    TABLE_SCORE_SHEET_CONFIRMATIONS,
    TABLE_SCORES,
)


HKT = ZoneInfo("Asia/Hong_Kong")
STATUSES = ("pending", "confirmed", "disputed")


def _now() -> dt.datetime:
    return dt.datetime.now(HKT).replace(tzinfo=None)


def _clean(value) -> str:
    text_value = str(value or "").strip()
    return "" if text_value.lower() in {"nan", "nat", "none", "<na>"} else text_value


def _mapping(row) -> dict:
    if row is None:
        return {}
    return dict(getattr(row, "_mapping", row))


def open_confirmation(match_id: str, db=None) -> dict:
    """Rotate both team links and bind them to the current judge-sheet set."""
    match_id = _clean(match_id)
    if not match_id:
        return {"ok": False, "message": "場次不存在。"}
    db = _resolve_db(db)
    pro_token = secrets.token_urlsafe(32)
    con_token = secrets.token_urlsafe(32)
    now = _now()
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"), {
            # Share the exact match-scoped lock used by final judge submission,
            # so the score count cannot change between opening and persistence.
            "lock_key": f"judge_submit:{match_id}",
        })
        match = _mapping(conn.execute(text(
            f"SELECT match_id,pro_team,con_team FROM {TABLE_MATCHES} "
            "WHERE match_id=:match_id"
        ), {"match_id": match_id}).fetchone())
        if not match:
            return {"ok": False, "message": "場次不存在。"}
        score_count = int(conn.execute(text(
            f"SELECT COUNT(*) AS score_count FROM {TABLE_SCORES} "
            "WHERE match_id=:match_id"
        ), {"match_id": match_id}).scalar() or 0)
        if score_count <= 0:
            return {"ok": False, "message": "此場次未有評判正式提交分紙。"}
        incomplete_count = int(conn.execute(text(f"""
            SELECT COUNT(*) AS incomplete_count
            FROM {TABLE_SCORES} s
            WHERE s.match_id=:match_id
              AND (
                  SELECT COUNT(DISTINCT d.side)
                  FROM {TABLE_SCORE_DRAFTS} d
                  WHERE d.match_id=s.match_id
                    AND lower(btrim(d.judge_name))=lower(btrim(s.judge_name))
                    AND COALESCE(d.is_final,FALSE)=TRUE
                    AND d.side IN ('正方','反方')
              ) < 2
        """), {"match_id": match_id}).scalar() or 0)
        if incomplete_count:
            return {
                "ok": False,
                "message": "部分評判分紙細項資料不完整，未能開放隊伍核對。",
            }
        conn.execute(text(f"""
            INSERT INTO {TABLE_SCORE_SHEET_CONFIRMATIONS}
                (match_id,side,confirmation_token,status,dispute_reason,
                 opened_score_count,opened_at,responded_at)
            VALUES
                (:match_id,'pro',:pro_token,'pending','',:score_count,:now,NULL),
                (:match_id,'con',:con_token,'pending','',:score_count,:now,NULL)
            ON CONFLICT (match_id,side) DO UPDATE SET
                confirmation_token=EXCLUDED.confirmation_token,
                status='pending',
                dispute_reason='',
                opened_score_count=EXCLUDED.opened_score_count,
                opened_at=EXCLUDED.opened_at,
                responded_at=NULL
        """), {
            "match_id": match_id,
            "pro_token": pro_token,
            "con_token": con_token,
            "score_count": score_count,
            "now": now,
        })
    return {
        "ok": True,
        "message": "已開放雙方核對分紙；舊核對連結已失效。",
        "score_count": score_count,
        "links": {
            "pro": {"confirmation_token": pro_token, "status": "pending"},
            "con": {"confirmation_token": con_token, "status": "pending"},
        },
    }


def admin_state(match_id: str, db=None) -> dict:
    """Return organiser-visible status without auto-provisioning bearer links."""
    db = _resolve_db(db)
    match_id = _clean(match_id)
    counts = db.query(
        f"SELECT COUNT(*) AS score_count FROM {TABLE_SCORES} WHERE match_id=:match_id",
        {"match_id": match_id},
    )
    score_count = int(counts.iloc[0]["score_count"] or 0) if not counts.empty else 0
    try:
        rows = db.query(f"""
            SELECT side,confirmation_token,status,dispute_reason,
                   opened_score_count,opened_at,responded_at
            FROM {TABLE_SCORE_SHEET_CONFIRMATIONS}
            WHERE match_id=:match_id ORDER BY side
        """, {"match_id": match_id})
    except Exception:
        return {"schema_ready": False, "score_count": score_count, "links": {}}
    links = {}
    for _, row in rows.iterrows():
        side = _clean(row.get("side"))
        opened_count = int(row.get("opened_score_count") or 0)
        links[side] = {
            "confirmation_token": _clean(row.get("confirmation_token")),
            "status": _clean(row.get("status")) or "pending",
            "dispute_reason": _clean(row.get("dispute_reason")),
            "opened_score_count": opened_count,
            "opened_at": _clean(row.get("opened_at")),
            "responded_at": _clean(row.get("responded_at")),
            "stale": opened_count != score_count,
        }
    return {"schema_ready": True, "score_count": score_count, "links": links}


def confirmation_data(token: str, judge_name: str | None = None, db=None):
    """Return one side-scoped link and one selected full judge sheet."""
    db = _resolve_db(db)
    token = _clean(token)
    if not token:
        return None
    rows = db.query(f"""
        SELECT c.match_id,c.side,c.status,c.dispute_reason,c.opened_score_count,
               c.opened_at,c.responded_at,m.match_date,m.match_time,m.topic_text,
               m.pro_team,m.con_team,
               (SELECT COUNT(*) FROM {TABLE_SCORES} s
                WHERE s.match_id=c.match_id) AS current_score_count
        FROM {TABLE_SCORE_SHEET_CONFIRMATIONS} c
        JOIN {TABLE_MATCHES} m ON m.match_id=c.match_id
        WHERE c.confirmation_token=:token
        LIMIT 1
    """, {"token": token})
    if rows.empty:
        return None
    row = rows.iloc[0]
    match_id = _clean(row.get("match_id"))
    side = _clean(row.get("side"))
    opened_count = int(row.get("opened_score_count") or 0)
    current_count = int(row.get("current_score_count") or 0)
    from core.review_logic import review_data

    sheet = review_data(match_id, judge_name, db)
    return {
        "match_id": match_id,
        "match_date": _clean(row.get("match_date"))[:10],
        "match_time": _clean(row.get("match_time"))[:5],
        "topic_text": _clean(row.get("topic_text")),
        "pro_team": _clean(row.get("pro_team")),
        "con_team": _clean(row.get("con_team")),
        "side": side,
        "side_label": "正方" if side == "pro" else "反方",
        "team_name": _clean(row.get("pro_team") if side == "pro" else row.get("con_team")),
        "status": _clean(row.get("status")) or "pending",
        "dispute_reason": _clean(row.get("dispute_reason")),
        "opened_at": _clean(row.get("opened_at")),
        "responded_at": _clean(row.get("responded_at")),
        "opened_score_count": opened_count,
        "current_score_count": current_count,
        "stale": opened_count != current_count,
        "sheet": sheet,
    }


def respond(token: str, status: str, reason: str = "", db=None) -> dict:
    """Atomically record the team's first response if the sheet set is current."""
    token = _clean(token)
    status = _clean(status)
    reason = _clean(reason)
    if status not in {"confirmed", "disputed"}:
        return {"ok": False, "reason": "validation", "message": "核對狀態無效。"}
    if status == "disputed" and not reason:
        return {"ok": False, "reason": "validation", "message": "請填寫異議內容。"}
    if len(reason) > 2000:
        return {"ok": False, "reason": "validation", "message": "異議內容不可超過2000字。"}
    db = _resolve_db(db)
    with db.transaction() as conn:
        identity = _mapping(conn.execute(text(f"""
            SELECT match_id
            FROM {TABLE_SCORE_SHEET_CONFIRMATIONS}
            WHERE confirmation_token=:token
        """), {"token": token}).fetchone())
        if not identity:
            return {"ok": False, "reason": "invalid", "message": "核對連結無效或已被重新生成。"}
        # Use the same lock ordering as opening and judge submission. Recheck
        # the token after the advisory lock in case staff rotated it meanwhile.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"), {
            "lock_key": f"judge_submit:{identity['match_id']}",
        })
        row = _mapping(conn.execute(text(f"""
            SELECT match_id,status,opened_score_count
            FROM {TABLE_SCORE_SHEET_CONFIRMATIONS}
            WHERE confirmation_token=:token
            FOR UPDATE
        """), {"token": token}).fetchone())
        if not row:
            return {"ok": False, "reason": "invalid", "message": "核對連結無效或已被重新生成。"}
        if _clean(row.get("status")) != "pending":
            return {"ok": False, "reason": "responded", "message": "此方已提交核對結果。"}
        score_count = int(conn.execute(text(
            f"SELECT COUNT(*) FROM {TABLE_SCORES} WHERE match_id=:match_id"
        ), {"match_id": row["match_id"]}).scalar() or 0)
        if score_count != int(row.get("opened_score_count") or 0):
            return {
                "ok": False,
                "reason": "stale",
                "message": "評判分紙數目已更新，請向賽會索取重新開放的核對連結。",
            }
        changed = conn.execute(text(f"""
            UPDATE {TABLE_SCORE_SHEET_CONFIRMATIONS}
            SET status=:status,dispute_reason=:reason,responded_at=:now
            WHERE confirmation_token=:token AND status='pending'
        """), {
            "status": status,
            "reason": reason if status == "disputed" else "",
            "now": _now(),
            "token": token,
        }).rowcount
        if not changed:
            return {"ok": False, "reason": "responded", "message": "此方已提交核對結果。"}
    return {
        "ok": True,
        "status": status,
        "message": "已確認分紙無誤。" if status == "confirmed" else "已向賽會提交異議。",
    }
