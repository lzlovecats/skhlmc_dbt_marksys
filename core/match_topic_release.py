"""Private, side-scoped match-topic release and veto workflow."""

from __future__ import annotations

import datetime as dt
import secrets
from zoneinfo import ZoneInfo

from sqlalchemy import text

from core.vote_logic import _resolve_db
from schema import TABLE_MATCHES, TABLE_MATCH_TOPIC_RELEASES, TABLE_TOPICS
from system_limits import MATCH_TOPIC_RELEASE_GENERATION_LIMIT


HKT = ZoneInfo("Asia/Hong_Kong")
SIDES = ("pro", "con")


class TopicReleaseError(RuntimeError):
    """Known organiser or team workflow error safe to show to the user."""


def _clean(value) -> str:
    value = str(value or "").strip()
    return "" if value.lower() in {"nan", "nat", "none", "<na>"} else value


def _mapping(row) -> dict:
    if row is None:
        return {}
    return dict(getattr(row, "_mapping", row))


def _date(value) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(_clean(value)[:10])
    except ValueError:
        return None


def _time(value) -> dt.time | None:
    if isinstance(value, dt.datetime):
        return value.time().replace(tzinfo=None)
    if isinstance(value, dt.time):
        return value.replace(tzinfo=None)
    raw = _clean(value)[:5]
    try:
        return dt.time.fromisoformat(raw)
    except ValueError:
        return None


def _datetime(value) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            return value.astimezone(HKT).replace(tzinfo=None)
        return value
    raw = _clean(value).replace(" ", "T")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(HKT).replace(tzinfo=None)
    return parsed


def _now(value: dt.datetime | None = None) -> dt.datetime:
    value = value or dt.datetime.now(HKT)
    if value.tzinfo is not None:
        return value.astimezone(HKT).replace(tzinfo=None)
    return value


def _api_time(value) -> str:
    parsed = _datetime(value)
    return parsed.replace(tzinfo=HKT).isoformat() if parsed else ""


def release_schedule(match_date, match_time) -> dict[str, dt.datetime]:
    """Return the immutable Hong Kong schedule required by rules.md."""
    date_value = _date(match_date)
    time_value = _time(match_time)
    if date_value is None or time_value is None:
        raise TopicReleaseError("請先儲存完整的比賽日期及時間。")
    at = lambda days, hour: dt.datetime.combine(  # noqa: E731
        date_value - dt.timedelta(days=days), dt.time(hour, 0),
    )
    return {
        "first_reveal_at": at(14, 17),
        "first_veto_deadline": at(13, 16),
        "second_reveal_at": at(13, 17),
        "second_veto_deadline": at(12, 16),
        "third_reveal_at": at(12, 17),
        "expires_at": dt.datetime.combine(date_value, time_value),
    }


def _candidate(row: dict, number: int) -> str:
    return _clean(row.get(f"candidate_{number}"))


def _side_from_token(row: dict, token: str) -> str:
    for side in SIDES:
        if token and secrets.compare_digest(token, _clean(row.get(f"{side}_token"))):
            return side
    return ""


def _veto_used(row: dict, side: str) -> bool:
    return bool(row.get(f"{side}_veto_candidate"))


def _veto_count(row: dict) -> int:
    return sum(1 for side in SIDES if _veto_used(row, side))


def _current_candidate_number(row: dict) -> int:
    vetoed = {
        int(row[f"{side}_veto_candidate"])
        for side in SIDES
        if row.get(f"{side}_veto_candidate") is not None
    }
    if 1 not in vetoed:
        return 1
    if 2 not in vetoed:
        return 2
    return 3


def _final_topic(row: dict, *, now: dt.datetime | None = None) -> str:
    """Return the topic that has become final under the rules, if any."""
    values = _mapping(row)
    now_value = _now(now)
    number = _current_candidate_number(values)
    final_at = {
        1: _datetime(values.get("first_veto_deadline")),
        2: _datetime(values.get("second_veto_deadline")),
        3: _datetime(values.get("third_reveal_at")),
    }[number]
    if final_at is None or now_value < final_at:
        return ""
    return _candidate(values, number)


def _sync_final_topic(conn, row: dict, *, now: dt.datetime | None = None) -> str:
    """Persist only a rules-final topic in the ordinary match record."""
    topic = _final_topic(row, now=now)
    if not topic:
        conn.execute(text(f"""
            UPDATE {TABLE_MATCHES}
            SET topic_text=''
            WHERE match_id=:match_id
              AND COALESCE(topic_text, '')<>''
        """), {"match_id": _clean(row.get("match_id"))})
        return ""
    conn.execute(text(f"""
        UPDATE {TABLE_MATCHES}
        SET topic_text=:topic
        WHERE match_id=:match_id
          AND COALESCE(topic_text, '')<>:topic
    """), {"topic": topic, "match_id": _clean(row.get("match_id"))})
    return topic


def public_payload(row, side: str, *, now: dt.datetime | None = None) -> dict:
    """Map one active release row to the exact data visible to one team."""
    values = _mapping(row)
    now_value = _now(now)
    expires_at = _datetime(values.get("expires_at"))
    if side not in SIDES or expires_at is None:
        raise TopicReleaseError("辯題連結資料不完整。")
    if now_value >= expires_at:
        return {
            "phase": "expired",
            "expired": True,
            "message": "此辯題連結已於比賽開始時失效。",
        }

    number = _current_candidate_number(values)
    reveal_key = ("first_reveal_at", "second_reveal_at", "third_reveal_at")[number - 1]
    deadline_key = ("first_veto_deadline", "second_veto_deadline", None)[number - 1]
    reveal_at = _datetime(values.get(reveal_key))
    deadline = _datetime(values.get(deadline_key)) if deadline_key else None
    if reveal_at is None:
        raise TopicReleaseError("辯題公布時間資料不完整。")

    revealed = now_value >= reveal_at
    within_veto_window = bool(deadline and revealed and now_value < deadline)
    veto_allowed = within_veto_window and not _veto_used(values, side)
    final = bool(revealed and (number == 3 or (deadline and now_value >= deadline)))
    phase = "scheduled" if not revealed else "final" if final else "revealed"
    team_name = _clean(values.get("pro_team" if side == "pro" else "con_team"))
    opponent = _clean(values.get("con_team" if side == "pro" else "pro_team"))
    payload = {
        "phase": phase,
        "expired": False,
        "match_id": _clean(values.get("match_id")),
        "match_date": _clean(values.get("release_match_date"))[:10],
        "match_time": _clean(values.get("release_match_time"))[:5],
        "side": side,
        "side_label": "正方" if side == "pro" else "反方",
        "team_name": team_name,
        "opponent": opponent,
        "candidate_number": number,
        "topic_text": _candidate(values, number) if revealed else "",
        "reveal_at": _api_time(reveal_at),
        "veto_deadline": _api_time(deadline),
        "veto_allowed": veto_allowed,
        "my_veto_used": _veto_used(values, side),
        "veto_count": _veto_count(values),
        "final": final,
    }
    if not revealed:
        payload["message"] = "辯題尚未到公布時間。"
    elif final:
        payload["message"] = "此為本場比賽的最終辯題。"
    elif veto_allowed:
        payload["message"] = "你方仍可在截止時間前行使一次辯題否決權。"
    else:
        payload["message"] = "你方已行使否決權；仍可查看目前辯題及公布狀態。"
    return payload


def _admin_round(row, *, now: dt.datetime | None = None) -> dict:
    values = _mapping(row)
    phase = public_payload(values, "pro", now=now)
    candidates = []
    for number, reveal_key, deadline_key in (
        (1, "first_reveal_at", "first_veto_deadline"),
        (2, "second_reveal_at", "second_veto_deadline"),
        (3, "third_reveal_at", None),
    ):
        veto_side = next((
            side for side in SIDES
            if int(values.get(f"{side}_veto_candidate") or 0) == number
        ), "")
        candidates.append({
            "number": number,
            "topic_text": _candidate(values, number),
            "reveal_at": _api_time(values.get(reveal_key)),
            "veto_deadline": _api_time(values.get(deadline_key)) if deadline_key else "",
            "vetoed_by": veto_side,
            "vetoed_at": _api_time(values.get(f"{veto_side}_veto_at")) if veto_side else "",
        })
    return {
        "id": int(values.get("id") or 0),
        "generation": int(values.get("generation") or 0),
        "match_date": _clean(values.get("release_match_date"))[:10],
        "match_time": _clean(values.get("release_match_time"))[:5],
        "created_at": _api_time(values.get("created_at")),
        "tokens_rotated_at": _api_time(values.get("tokens_rotated_at")),
        "revoked_at": _api_time(values.get("revoked_at")),
        "expires_at": _api_time(values.get("expires_at")),
        "phase": phase.get("phase"),
        "candidate_number": phase.get("candidate_number"),
        "candidates": candidates,
    }


def _release_select(where: str) -> str:
    return f"""
        SELECT r.*,m.pro_team,m.con_team
        FROM {TABLE_MATCH_TOPIC_RELEASES} r
        JOIN {TABLE_MATCHES} m ON m.match_id=r.match_id
        WHERE {where}
    """


def admin_state(match_id: str, db=None, *, now: dt.datetime | None = None) -> dict:
    db = _resolve_db(db)
    match_id = _clean(match_id)
    with db.transaction() as conn:
        conn.execute(text(f"""
            SELECT match_id FROM {TABLE_MATCHES}
            WHERE match_id=:match_id FOR UPDATE
        """), {"match_id": match_id}).fetchone()
        rows = [
            _mapping(row)
            for row in conn.execute(text(
                _release_select("r.match_id=:match_id")
                + " ORDER BY r.generation DESC LIMIT :limit FOR UPDATE OF r"
            ), {
                "match_id": match_id,
                "limit": MATCH_TOPIC_RELEASE_GENERATION_LIMIT,
            }).fetchall()
        ]
        active_row = next((row for row in rows if not _clean(row.get("revoked_at"))), None)
        if active_row is not None:
            _sync_final_topic(conn, active_row, now=now)
    history = [_admin_round(row, now=now) for row in rows]
    if active_row is None:
        return {"schema_ready": True, "active": False, "history": history}
    values = dict(active_row)
    current = _admin_round(values, now=now)
    current.update({
        "pro_token": _clean(values.get("pro_token")),
        "con_token": _clean(values.get("con_token")),
        "can_redraw": _now(now) < _datetime(values.get("first_reveal_at")),
        "can_cancel": _now(now) < _datetime(values.get("first_reveal_at")),
        "can_rotate": _now(now) < _datetime(values.get("expires_at")),
    })
    return {"schema_ready": True, "active": True, "current": current, "history": history}


def open_release(
    match_id: str,
    difficulty: int | None = None,
    db=None,
    *,
    now: dt.datetime | None = None,
) -> dict:
    """Draw three distinct topics in one action and create both team links."""
    match_id = _clean(match_id)
    if difficulty not in (None, 1, 2, 3):
        raise TopicReleaseError("請選擇有效的辯題難度。")
    db = _resolve_db(db)
    now_value = _now(now)
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"), {
            "lock_key": f"match-topic-release:{match_id}",
        })
        match = _mapping(conn.execute(text(f"""
            SELECT match_id,match_date,match_time,pro_team,con_team
            FROM {TABLE_MATCHES} WHERE match_id=:match_id FOR UPDATE
        """), {"match_id": match_id}).fetchone())
        if not match:
            raise TopicReleaseError("場次不存在。")
        if not _clean(match.get("pro_team")) or not _clean(match.get("con_team")):
            raise TopicReleaseError("請先儲存正反方隊名。")
        schedule = release_schedule(match.get("match_date"), match.get("match_time"))
        if now_value >= schedule["first_reveal_at"]:
            raise TopicReleaseError("已到首條辯題公布時間，不能再建立或重新抽取辯題組合。")

        difficulty_clause = " WHERE difficulty=:difficulty" if difficulty else ""
        topic_rows = conn.execute(text(f"""
            SELECT topic_text FROM {TABLE_TOPICS}{difficulty_clause}
            ORDER BY RANDOM() LIMIT 3
        """), {"difficulty": difficulty} if difficulty else {}).fetchall()
        topics = [_clean(_mapping(row).get("topic_text") or row[0]) for row in topic_rows]
        if len(topics) != 3 or len(set(topics)) != 3 or not all(topics):
            raise TopicReleaseError("辯題庫沒有足夠三條符合條件的不同辯題。")

        existing = _mapping(conn.execute(text(f"""
            SELECT id,generation,first_reveal_at
            FROM {TABLE_MATCH_TOPIC_RELEASES}
            WHERE match_id=:match_id AND revoked_at IS NULL
            FOR UPDATE
        """), {"match_id": match_id}).fetchone())
        if existing and now_value >= _datetime(existing.get("first_reveal_at")):
            raise TopicReleaseError("首條辯題已公布，不能重新抽取辯題組合。")
        generation = int(conn.execute(text(f"""
            SELECT COALESCE(MAX(generation),0)
            FROM {TABLE_MATCH_TOPIC_RELEASES} WHERE match_id=:match_id
        """), {"match_id": match_id}).scalar() or 0) + 1
        if generation > MATCH_TOPIC_RELEASE_GENERATION_LIMIT:
            raise TopicReleaseError("此場次重新建立辯題分享的次數已達安全上限。")
        if existing:
            conn.execute(text(f"""
                UPDATE {TABLE_MATCH_TOPIC_RELEASES}
                SET revoked_at=:now WHERE id=:id AND revoked_at IS NULL
            """), {"now": now_value, "id": int(existing["id"])})

        # Candidate topics must not appear in the ordinary match record.  That
        # field is populated only when one candidate becomes final by rule.
        conn.execute(text(f"""
            UPDATE {TABLE_MATCHES} SET topic_text='' WHERE match_id=:match_id
        """), {"match_id": match_id})

        pro_token = secrets.token_urlsafe(32)
        con_token = secrets.token_urlsafe(32)
        conn.execute(text(f"""
            INSERT INTO {TABLE_MATCH_TOPIC_RELEASES}
                (match_id,generation,release_match_date,release_match_time,
                 candidate_1,candidate_2,candidate_3,pro_token,con_token,
                 first_reveal_at,first_veto_deadline,second_reveal_at,
                 second_veto_deadline,third_reveal_at,expires_at,created_at)
            VALUES
                (:match_id,:generation,:match_date,:match_time,
                 :candidate_1,:candidate_2,:candidate_3,:pro_token,:con_token,
                 :first_reveal_at,:first_veto_deadline,:second_reveal_at,
                 :second_veto_deadline,:third_reveal_at,:expires_at,:now)
        """), {
            "match_id": match_id, "generation": generation,
            "match_date": _date(match.get("match_date")),
            "match_time": _time(match.get("match_time")),
            "candidate_1": topics[0], "candidate_2": topics[1],
            "candidate_3": topics[2], "pro_token": pro_token,
            "con_token": con_token, "now": now_value, **schedule,
        })
    result = admin_state(match_id, db=db, now=now_value)
    result.update({"ok": True, "message": "已抽取三條辯題並產生正反方查閱連結。"})
    return result


def rotate_links(match_id: str, db=None, *, now: dt.datetime | None = None) -> dict:
    db = _resolve_db(db)
    match_id = _clean(match_id)
    now_value = _now(now)
    with db.transaction() as conn:
        row = _mapping(conn.execute(text(_release_select(
            "r.match_id=:match_id AND r.revoked_at IS NULL"
        ) + " FOR UPDATE"), {"match_id": match_id}).fetchone())
        if not row:
            raise TopicReleaseError("此場次尚未建立辯題分享連結。")
        if now_value >= _datetime(row.get("expires_at")):
            raise TopicReleaseError("比賽已開始，辯題連結已失效。")
        pro_token = secrets.token_urlsafe(32)
        con_token = secrets.token_urlsafe(32)
        conn.execute(text(f"""
            UPDATE {TABLE_MATCH_TOPIC_RELEASES}
            SET pro_token=:pro_token,con_token=:con_token,tokens_rotated_at=:now
            WHERE id=:id AND revoked_at IS NULL
        """), {
            "pro_token": pro_token, "con_token": con_token,
            "now": now_value, "id": int(row["id"]),
        })
    result = admin_state(match_id, db=db, now=now_value)
    result.update({"ok": True, "message": "已重新產生連結；舊連結即時失效。"})
    return result


def cancel_release(match_id: str, db=None, *, now: dt.datetime | None = None) -> dict:
    db = _resolve_db(db)
    match_id = _clean(match_id)
    now_value = _now(now)
    with db.transaction() as conn:
        conn.execute(text(f"""
            SELECT match_id FROM {TABLE_MATCHES}
            WHERE match_id=:match_id FOR UPDATE
        """), {"match_id": match_id}).fetchone()
        row = _mapping(conn.execute(text(f"""
            SELECT id,first_reveal_at FROM {TABLE_MATCH_TOPIC_RELEASES}
            WHERE match_id=:match_id AND revoked_at IS NULL FOR UPDATE
        """), {"match_id": match_id}).fetchone())
        if not row:
            raise TopicReleaseError("此場次尚未建立辯題分享連結。")
        if now_value >= _datetime(row.get("first_reveal_at")):
            raise TopicReleaseError("首條辯題已公布，不能取消辯題分享。")
        conn.execute(text(f"""
            UPDATE {TABLE_MATCH_TOPIC_RELEASES}
            SET revoked_at=:now WHERE id=:id AND revoked_at IS NULL
        """), {"now": now_value, "id": int(row["id"])})
        conn.execute(text(f"""
            UPDATE {TABLE_MATCHES} SET topic_text='' WHERE match_id=:match_id
        """), {"match_id": match_id})
    return {"ok": True, "message": "已取消辯題分享；原有連結即時失效。"}


def public_data(token: str, db=None, *, now: dt.datetime | None = None) -> dict | None:
    token = _clean(token)
    if not token or len(token) > 128:
        return None
    db = _resolve_db(db)
    with db.transaction() as conn:
        match_id = _clean(conn.execute(text(f"""
            SELECT match_id FROM {TABLE_MATCH_TOPIC_RELEASES}
            WHERE revoked_at IS NULL
              AND (pro_token=:token OR con_token=:token)
            LIMIT 1
        """), {"token": token}).scalar())
        if not match_id:
            return None
        # Match rows are always locked before release rows so redraw and final
        # promotion cannot deadlock each other.
        conn.execute(text(f"""
            SELECT match_id FROM {TABLE_MATCHES}
            WHERE match_id=:match_id FOR UPDATE
        """), {"match_id": match_id}).fetchone()
        row = _mapping(conn.execute(text(
            _release_select(
                "r.match_id=:match_id AND r.revoked_at IS NULL "
                "AND (r.pro_token=:token OR r.con_token=:token)"
            ) + " LIMIT 1 FOR UPDATE OF r"
        ), {"match_id": match_id, "token": token}).fetchone())
        if not row:
            return None
        side = _side_from_token(row, token)
        payload = public_payload(row, side, now=now)
        _sync_final_topic(conn, row, now=now)
        return payload


def submit_veto(token: str, db=None, *, now: dt.datetime | None = None) -> dict:
    token = _clean(token)
    if not token or len(token) > 128:
        return {"ok": False, "reason": "invalid", "message": "辯題連結無效。"}
    db = _resolve_db(db)
    now_value = _now(now)
    with db.transaction() as conn:
        row = _mapping(conn.execute(text(_release_select(
            "r.revoked_at IS NULL AND (r.pro_token=:token OR r.con_token=:token)"
        ) + " FOR UPDATE"), {"token": token}).fetchone())
        if not row:
            return {"ok": False, "reason": "invalid", "message": "辯題連結無效或已被重新產生。"}
        side = _side_from_token(row, token)
        state = public_payload(row, side, now=now_value)
        if state.get("expired"):
            return {"ok": False, "reason": "expired", "message": state["message"]}
        if state.get("phase") == "scheduled":
            return {"ok": False, "reason": "not_revealed", "message": "辯題尚未到公布時間。"}
        if state.get("final"):
            return {"ok": False, "reason": "closed", "message": "否決期限已過，或目前已是最終辯題。"}
        if state.get("my_veto_used"):
            return {"ok": False, "reason": "used", "message": "你方已行使本場唯一一次辯題否決權。"}
        if not state.get("veto_allowed"):
            return {"ok": False, "reason": "closed", "message": "目前不能行使辯題否決權。"}
        number = int(state["candidate_number"])
        if number not in (1, 2):
            return {"ok": False, "reason": "closed", "message": "目前已是最終辯題。"}
        column_candidate = "pro_veto_candidate" if side == "pro" else "con_veto_candidate"
        column_at = "pro_veto_at" if side == "pro" else "con_veto_at"
        changed = conn.execute(text(f"""
            UPDATE {TABLE_MATCH_TOPIC_RELEASES}
            SET {column_candidate}=:candidate,{column_at}=:now
            WHERE id=:id AND {column_candidate} IS NULL AND revoked_at IS NULL
        """), {"candidate": number, "now": now_value, "id": int(row["id"])}).rowcount
        if not changed:
            return {"ok": False, "reason": "used", "message": "你方已行使本場唯一一次辯題否決權。"}
    next_reveal = row["second_reveal_at" if number == 1 else "third_reveal_at"]
    return {
        "ok": True,
        "message": "已接納你方的辯題否決；賽會不會向另一方顯示否決者身分。",
        "next_reveal_at": _api_time(next_reveal),
    }


def validate_active_release_update(
    conn,
    match_id: str,
    match_date: str,
    match_time: str,
) -> str:
    """Protect an active release schedule from ordinary match-form edits."""
    ready = conn.execute(text(
        f"SELECT to_regclass('public.{TABLE_MATCH_TOPIC_RELEASES}') IS NOT NULL"
    )).scalar()
    if not ready:
        return ""
    row = _mapping(conn.execute(text(f"""
        SELECT release_match_date,release_match_time
        FROM {TABLE_MATCH_TOPIC_RELEASES}
        WHERE match_id=:match_id AND revoked_at IS NULL FOR UPDATE
    """), {"match_id": match_id}).fetchone())
    if not row:
        return ""
    if (
        _clean(row.get("release_match_date"))[:10] != _clean(match_date)[:10]
        or _clean(row.get("release_match_time"))[:5] != _clean(match_time)[:5]
    ):
        return "此場次已有生效中的辯題分享；日期及時間已鎖定。首題公布前可先取消分享再修改。"
    return ""


def visible_topic_for_roster(
    match_id: str,
    fallback_topic: str,
    db=None,
    *,
    now: dt.datetime | None = None,
) -> dict:
    """Hide staged topics from the separate public roster bearer link."""
    db = _resolve_db(db)
    match_id = _clean(match_id)
    with db.transaction() as conn:
        # Keep the same match-then-release lock order as open/cancel/public
        # flows so a revoked generation cannot be promoted by a stale read.
        conn.execute(text(f"""
            SELECT match_id FROM {TABLE_MATCHES}
            WHERE match_id=:match_id FOR UPDATE
        """), {"match_id": match_id}).fetchone()
        row = _mapping(conn.execute(text(
            _release_select("r.match_id=:match_id AND r.revoked_at IS NULL")
            + " LIMIT 1 FOR UPDATE OF r"
        ), {"match_id": match_id}).fetchone())
        if not row:
            return {
                "topic_text": _clean(fallback_topic),
                "topic_locked": False,
                "topic_reveal_at": "",
            }
        payload = public_payload(row, "pro", now=now)
        final_topic = _sync_final_topic(conn, row, now=now)
    if payload.get("expired"):
        return {"topic_text": final_topic or _clean(fallback_topic), "topic_locked": False, "topic_reveal_at": ""}
    return {
        "topic_text": _clean(payload.get("topic_text")),
        "topic_locked": not bool(payload.get("topic_text")),
        "topic_reveal_at": _clean(payload.get("reveal_at")),
    }
