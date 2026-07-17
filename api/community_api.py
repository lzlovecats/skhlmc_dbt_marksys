"""Committee recent matches, team history and graduate discussion forum."""

from datetime import datetime, timedelta
import logging
import secrets
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from account_access import NON_MEMBER_ACCOUNT_DB_KEYS
from api.access import require_page_user, require_page_user_or_developer
from api.pagination import PAGE_SIZE, bounds, json_safe, payload, scalar_count
from core.community_logic import (
    academic_year_label,
    forum_notification_copy,
    recent_notification_copy,
    validate_history_event,
    validate_membership,
    validate_post_body,
    validate_recent_match,
    validate_thread,
)
from core.roles import is_senior_committee
from schema import (
    TABLE_ACCOUNTS,
    TABLE_COMMITTEE_MEMBERSHIPS,
    TABLE_GHOST_FORUM_POSTS,
    TABLE_GHOST_FORUM_REACTIONS,
    TABLE_GHOST_FORUM_THREAD_MATCHES,
    TABLE_GHOST_FORUM_THREAD_PHOTOS,
    TABLE_GHOST_FORUM_THREADS,
    TABLE_HISTORY_EVENT_MATCHES,
    TABLE_HISTORY_EVENT_PHOTOS,
    TABLE_HISTORY_EVENTS,
    TABLE_MATCHES,
    TABLE_MATCH_PHOTOS,
    TABLE_RECENT_MATCH_NOTIFICATIONS,
    TABLE_RECENT_MATCHES,
)
from system_limits import (
    COMMITTEE_MEMBERSHIP_INVENTORY_LIMIT,
    GHOST_FORUM_POST_LIMIT,
    GHOST_FORUM_THREAD_LIMIT,
    HISTORY_EVENT_INVENTORY_LIMIT,
    RECENT_MATCH_INVENTORY_LIMIT,
    RECENT_MATCH_NOTIFICATION_CLAIM_TTL_SECONDS,
)


router = APIRouter(prefix="/api/community", tags=["committee-community"])
logger = logging.getLogger(__name__)


class RecentMatchBody(BaseModel):
    competition_name: str = Field(max_length=300)
    opponent: str = Field(max_length=300)
    match_date: str = Field(max_length=10)
    match_time: str = Field(max_length=8)
    topic_text: str = Field(max_length=1000)
    our_side: str = Field(max_length=20)
    result: str = Field(default="unconfirmed", max_length=20)
    score_text: str = Field(default="", max_length=40)
    best_debater: str = Field(default="", max_length=300)
    notes: str = Field(default="", max_length=3000)


class RecentMatchUpdateBody(RecentMatchBody):
    revision: int = Field(ge=1)


class MembershipBody(BaseModel):
    member_user_id: str = Field(default="", max_length=200)
    display_name: str = Field(max_length=300)
    joined_academic_year: int
    ended_academic_year: int | None = None
    exit_type: str = Field(default="current", max_length=20)


class MembershipUpdateBody(MembershipBody):
    revision: int = Field(ge=1)


class HistoryEventBody(BaseModel):
    academic_year_start: int
    event_date: str = Field(default="", max_length=10)
    title: str = Field(max_length=500)
    description: str = Field(default="", max_length=5000)
    match_ids: list[str] = Field(default_factory=list, max_length=20)
    photo_ids: list[int] = Field(default_factory=list, max_length=30)


class HistoryEventUpdateBody(HistoryEventBody):
    revision: int = Field(ge=1)


class ForumThreadBody(BaseModel):
    title: str = Field(max_length=300)
    body: str = Field(max_length=8000)
    match_ids: list[str] = Field(default_factory=list, max_length=20)
    photo_ids: list[int] = Field(default_factory=list, max_length=30)


class ForumThreadUpdateBody(BaseModel):
    title: str = Field(max_length=300)
    match_ids: list[str] = Field(default_factory=list, max_length=20)
    photo_ids: list[int] = Field(default_factory=list, max_length=30)
    revision: int = Field(ge=1)


class ForumPostBody(BaseModel):
    body: str = Field(max_length=8000)
    quoted_post_id: int | None = Field(default=None, ge=1)


class ForumPostUpdateBody(BaseModel):
    body: str = Field(max_length=8000)
    revision: int = Field(ge=1)


def _db():
    from deploy.proxy import get_vote_db

    return get_vote_db()


def _member_context(request: Request, page: str):
    return require_page_user_or_developer(request, page), _db()


def _senior_context(request: Request, page: str):
    user, db = _member_context(request, page)
    if not is_senior_committee(user, db=db):
        raise HTTPException(403, "只有高級委員可以修改此項資料。")
    return user, db


def _ghost_context(request: Request):
    user = require_page_user(request, "ghost_forum")
    db = _db()
    if not is_senior_committee(user, db=db):
        raise HTTPException(403, "老鬼專區只限高級委員帳戶進入。")
    return user, db


def _now():
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)


def _records(frame):
    return json_safe(frame.to_dict("records"))


def _validation(function, values):
    try:
        return function(values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _connection_rows(connection, sql, params=None):
    result = connection.execute(text(sql), params or {})
    return [dict(row) for row in result.mappings().all()]


def _check_links(connection, match_ids, photo_ids):
    if match_ids:
        found = connection.execute(
            text(
                f"SELECT COUNT(*) FROM {TABLE_MATCHES} "
                "WHERE match_id=ANY(CAST(:ids AS text[]))"
            ),
            {"ids": match_ids},
        ).scalar()
        if int(found or 0) != len(match_ids):
            raise HTTPException(400, "其中一個比賽連結已不存在。")
    if photo_ids:
        found = connection.execute(
            text(
                f"SELECT COUNT(*) FROM {TABLE_MATCH_PHOTOS} "
                "WHERE id=ANY(CAST(:ids AS integer[]))"
            ),
            {"ids": photo_ids},
        ).scalar()
        if int(found or 0) != len(photo_ids):
            raise HTTPException(400, "其中一張圖片連結已不存在。")


def _check_membership_account(connection, user_id):
    if user_id is None:
        return
    found = connection.execute(
        text(
            f"""SELECT 1 FROM {TABLE_ACCOUNTS}
                WHERE user_id=:user AND COALESCE(account_disabled,FALSE)=FALSE
                  AND LOWER(user_id) <> ALL(:excluded)"""
        ),
        {"user": user_id, "excluded": list(NON_MEMBER_ACCOUNT_DB_KEYS)},
    ).scalar()
    if not found:
        raise HTTPException(400, "任期只可連結未停用的內部委員帳戶。")


def _replace_links(connection, owner_id, match_ids, photo_ids, *, forum=False):
    if forum:
        match_table, photo_table, owner_column = (
            TABLE_GHOST_FORUM_THREAD_MATCHES,
            TABLE_GHOST_FORUM_THREAD_PHOTOS,
            "thread_id",
        )
    else:
        match_table, photo_table, owner_column = (
            TABLE_HISTORY_EVENT_MATCHES,
            TABLE_HISTORY_EVENT_PHOTOS,
            "event_id",
        )
    connection.execute(
        text(f"DELETE FROM {match_table} WHERE {owner_column}=:owner"),
        {"owner": owner_id},
    )
    connection.execute(
        text(f"DELETE FROM {photo_table} WHERE {owner_column}=:owner"),
        {"owner": owner_id},
    )
    for match_id in match_ids:
        connection.execute(
            text(
                f"INSERT INTO {match_table}({owner_column},match_id) "
                "VALUES(:owner,:match)"
            ),
            {"owner": owner_id, "match": match_id},
        )
    for photo_id in photo_ids:
        connection.execute(
            text(
                f"INSERT INTO {photo_table}({owner_column},photo_id) "
                "VALUES(:owner,:photo)"
            ),
            {"owner": owner_id, "photo": photo_id},
        )


def _resource_links(db, owner_ids, *, forum=False):
    links = {int(owner): {"matches": [], "photos": []} for owner in owner_ids}
    if not links:
        return links
    match_table, photo_table, owner_column = (
        (TABLE_GHOST_FORUM_THREAD_MATCHES, TABLE_GHOST_FORUM_THREAD_PHOTOS, "thread_id")
        if forum
        else (TABLE_HISTORY_EVENT_MATCHES, TABLE_HISTORY_EVENT_PHOTOS, "event_id")
    )
    params = {"ids": list(links)}
    matches = db.query(
        f"""SELECT l.{owner_column} owner_id,m.match_id,m.match_date,m.match_time,
                   m.topic_text,m.pro_team,m.con_team,m.debate_format
            FROM {match_table} l JOIN {TABLE_MATCHES} m ON m.match_id=l.match_id
            WHERE l.{owner_column}=ANY(CAST(:ids AS bigint[]))
            ORDER BY m.match_date DESC NULLS LAST,m.match_id""",
        params,
    )
    photos = db.query(
        f"""SELECT l.{owner_column} owner_id,p.id,p.album_label,p.photo_date,
                   p.photo_title,p.caption
            FROM {photo_table} l JOIN {TABLE_MATCH_PHOTOS} p ON p.id=l.photo_id
            WHERE l.{owner_column}=ANY(CAST(:ids AS bigint[]))
            ORDER BY p.photo_date DESC NULLS LAST,p.id DESC""",
        params,
    )
    for row in _records(matches):
        links[int(row.pop("owner_id"))]["matches"].append(row)
    for row in _records(photos):
        links[int(row.pop("owner_id"))]["photos"].append(row)
    return links


# Recent matches -----------------------------------------------------------


@router.get("/recent-matches/data")
def recent_match_data(request: Request):
    user, db = _member_context(request, "recent_matches")
    return {
        "user_id": user,
        "can_manage": is_senior_committee(user, db=db),
        "side_options": ["pro", "con", "unconfirmed"],
        "result_options": ["win", "loss", "draw", "unconfirmed"],
    }


@router.get("/recent-matches")
def recent_matches(request: Request, page: int = 1):
    _user, db = _member_context(request, "recent_matches")
    page, _, offset = bounds(page)
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_RECENT_MATCHES}")
    rows = db.query(
        f"""SELECT id,competition_name,opponent,match_date,match_time,topic_text,
                   our_side,result,score_text,best_debater,notes,revision,
                   created_by,updated_by,created_at,updated_at
            FROM {TABLE_RECENT_MATCHES}
            ORDER BY match_date DESC,match_time DESC,id DESC
            LIMIT :limit OFFSET :offset""",
        {"limit": PAGE_SIZE, "offset": offset},
    )
    return payload(_records(rows), page, total)


def _queue_notification(connection, match_id, event_kind):
    connection.execute(
        text(
            f"""INSERT INTO {TABLE_RECENT_MATCH_NOTIFICATIONS}
                    (recent_match_id,event_kind,state)
                VALUES(:match,:kind,'pending')
                ON CONFLICT(recent_match_id,event_kind) DO NOTHING"""
        ),
        {"match": match_id, "kind": event_kind},
    )


def _dispatch_recent_notification(db, match_id=None, event_kind=None):
    claim = secrets.token_urlsafe(18)
    now = _now()
    cutoff = now - timedelta(seconds=RECENT_MATCH_NOTIFICATION_CLAIM_TTL_SECONDS)
    with db.transaction() as connection:
        filters = [
            "(n.state IN ('pending','retryable') OR "
            "(n.state='sending' AND n.attempted_at<:cutoff))"
        ]
        params = {"cutoff": cutoff, "claim": claim, "now": now}
        if match_id is not None:
            filters.append("n.recent_match_id=:match")
            params["match"] = int(match_id)
        if event_kind is not None:
            filters.append("n.event_kind=:kind")
            params["kind"] = event_kind
        where = " AND ".join(filters)
        row = connection.execute(
            text(
                f"""WITH candidate AS (
                        SELECT n.id FROM {TABLE_RECENT_MATCH_NOTIFICATIONS} n
                        WHERE {where}
                        ORDER BY n.id FOR UPDATE SKIP LOCKED LIMIT 1
                    )
                    UPDATE {TABLE_RECENT_MATCH_NOTIFICATIONS} n
                    SET state='sending',claim_token=:claim,attempted_at=:now,last_error=''
                    FROM candidate WHERE n.id=candidate.id
                    RETURNING n.id,n.recent_match_id,n.event_kind"""
            ),
            params,
        ).mappings().one_or_none()
        if row is None:
            return {"state": "none", "sent_count": 0}
        match = connection.execute(
            text(
                f"""SELECT competition_name,opponent,match_date,match_time,result,
                           score_text,best_debater
                    FROM {TABLE_RECENT_MATCHES} WHERE id=:id"""
            ),
            {"id": row["recent_match_id"]},
        ).mappings().one()
        notification_id = int(row["id"])
        event = str(row["event_kind"])

    title, body = recent_notification_copy(dict(match), event)
    error = ""
    try:
        from core.push import notify_committee
        from deploy.proxy import _get_vapid

        sent = notify_committee(
            db,
            _get_vapid(),
            title,
            body,
            tag=f"recent-match-{row['recent_match_id']}-{event}",
            url="/recent-matches",
        )
        if sent < 1:
            error = "目前沒有成功送達的委員裝置。"
    except Exception as exc:  # Provider error is retained for explicit retry.
        sent = 0
        error = str(exc)[:1000]
    state = "sent" if sent > 0 else "retryable"
    db.execute(
        f"""UPDATE {TABLE_RECENT_MATCH_NOTIFICATIONS}
            SET state=:state,sent_at=CASE WHEN :sent THEN :now ELSE NULL END,
                sent_count=:count,last_error=:error
            WHERE id=:id AND claim_token=:claim""",
        {
            "state": state,
            "sent": sent > 0,
            "now": now,
            "count": sent,
            "error": error,
            "id": notification_id,
            "claim": claim,
        },
    )
    return {"state": state, "sent_count": sent, "error": error}


def _fire_forum_push(db, author_user_id, thread_id, thread_title, event_kind, post_id=None):
    title, body = forum_notification_copy(author_user_id, thread_title, event_kind)
    try:
        from core.push import notify_committee
        from deploy.proxy import _get_vapid

        suffix = f"-post-{int(post_id)}" if post_id is not None else ""
        sent = notify_committee(
            db,
            _get_vapid(),
            title,
            body,
            exclude_user=author_user_id,
            senior_only=True,
            tag=f"ghost-forum-thread-{int(thread_id)}{suffix}",
            url=f"/ghost-forum?thread={int(thread_id)}",
        )
    except Exception:
        logger.exception(
            "Ghost forum push failed for thread_id=%s event_kind=%s",
            thread_id,
            event_kind,
        )
        sent = 0
    return {"sent_count": int(sent)}


@router.post("/recent-matches")
def create_recent_match(body: RecentMatchBody, request: Request):
    user, db = _senior_context(request, "recent_matches")
    values = _validation(validate_recent_match, body.model_dump())
    now = _now()
    with db.transaction() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('recent_match_inventory'))"))
        count = connection.execute(text(f"SELECT COUNT(*) FROM {TABLE_RECENT_MATCHES}")).scalar()
        if int(count or 0) >= RECENT_MATCH_INVENTORY_LIMIT:
            raise HTTPException(409, "近期比賽資訊已達系統上限。")
        row = connection.execute(
            text(
                f"""INSERT INTO {TABLE_RECENT_MATCHES}
                    (competition_name,opponent,match_date,match_time,topic_text,
                     our_side,result,score_text,best_debater,notes,
                     created_by,updated_by,created_at,updated_at)
                    VALUES(:competition_name,:opponent,:match_date,:match_time,:topic_text,
                           :our_side,:result,:score_text,:best_debater,:notes,
                           :user,:user,:now,:now) RETURNING id,revision"""
            ),
            {**values, "user": user, "now": now},
        ).mappings().one()
        match_id = int(row["id"])
        _queue_notification(connection, match_id, "new_match")
        if values["result"] != "unconfirmed":
            _queue_notification(connection, match_id, "result")
    notifications = [_dispatch_recent_notification(db, match_id, "new_match")]
    if values["result"] != "unconfirmed":
        notifications.append(_dispatch_recent_notification(db, match_id, "result"))
    return {"ok": True, "id": match_id, "revision": int(row["revision"]), "notifications": notifications}


@router.patch("/recent-matches/{match_id}")
def update_recent_match(match_id: int, body: RecentMatchUpdateBody, request: Request):
    user, db = _senior_context(request, "recent_matches")
    values = _validation(validate_recent_match, body.model_dump())
    now = _now()
    with db.transaction() as connection:
        prior = connection.execute(
            text(f"SELECT result FROM {TABLE_RECENT_MATCHES} WHERE id=:id FOR UPDATE"),
            {"id": match_id},
        ).mappings().one_or_none()
        if prior is None:
            raise HTTPException(404, "找不到近期比賽。")
        changed = connection.execute(
            text(
                f"""UPDATE {TABLE_RECENT_MATCHES} SET
                    competition_name=:competition_name,opponent=:opponent,
                    match_date=:match_date,match_time=:match_time,topic_text=:topic_text,
                    our_side=:our_side,result=:result,score_text=:score_text,
                    best_debater=:best_debater,notes=:notes,revision=revision+1,
                    updated_by=:user,updated_at=:now
                    WHERE id=:id AND revision=:revision RETURNING revision"""
            ),
            {**values, "id": match_id, "revision": body.revision, "user": user, "now": now},
        ).mappings().one_or_none()
        if changed is None:
            raise HTTPException(409, "資料已被其他人更新，請重新載入。")
        should_notify = prior["result"] == "unconfirmed" and values["result"] != "unconfirmed"
        if should_notify:
            _queue_notification(connection, match_id, "result")
    notification = _dispatch_recent_notification(db, match_id, "result") if should_notify else None
    return {"ok": True, "revision": int(changed["revision"]), "notification": notification}


@router.post("/recent-matches/notifications/retry")
def retry_recent_notification(request: Request):
    _user, db = _senior_context(request, "recent_matches")
    return _dispatch_recent_notification(db)


# History ------------------------------------------------------------------


@router.get("/history/data")
def history_data(request: Request):
    user, db = _member_context(request, "team_history")
    accounts = db.query(
        f"""SELECT user_id FROM {TABLE_ACCOUNTS}
            WHERE COALESCE(account_disabled,FALSE)=FALSE
              AND LOWER(user_id) <> ALL(:excluded)
            ORDER BY user_id LIMIT :limit""",
        {
            "excluded": list(NON_MEMBER_ACCOUNT_DB_KEYS),
            "limit": COMMITTEE_MEMBERSHIP_INVENTORY_LIMIT,
        },
    )
    return {
        "user_id": user,
        "can_manage": is_senior_committee(user, db=db),
        "accounts": [str(value) for value in accounts.get("user_id", [])],
    }


@router.get("/history/events")
def history_events(request: Request, page: int = 1):
    _user, db = _member_context(request, "team_history")
    page, _, offset = bounds(page)
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_HISTORY_EVENTS}")
    frame = db.query(
        f"""SELECT id,academic_year_start,event_date,title,description,revision,
                   created_by,updated_by,created_at,updated_at
            FROM {TABLE_HISTORY_EVENTS}
            ORDER BY academic_year_start DESC,event_date DESC NULLS LAST,id DESC
            LIMIT :limit OFFSET :offset""",
        {"limit": PAGE_SIZE, "offset": offset},
    )
    items = _records(frame)
    links = _resource_links(db, [row["id"] for row in items])
    for item in items:
        item["academic_year_label"] = academic_year_label(item["academic_year_start"])
        item["links"] = links[int(item["id"])]
    return payload(items, page, total)


@router.post("/history/events")
def create_history_event(body: HistoryEventBody, request: Request):
    user, db = _senior_context(request, "team_history")
    values = _validation(validate_history_event, body.model_dump())
    now = _now()
    with db.transaction() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('history_event_inventory'))"))
        count = connection.execute(text(f"SELECT COUNT(*) FROM {TABLE_HISTORY_EVENTS}")).scalar()
        if int(count or 0) >= HISTORY_EVENT_INVENTORY_LIMIT:
            raise HTTPException(409, "歷史事件已達系統上限。")
        _check_links(connection, values["match_ids"], values["photo_ids"])
        row = connection.execute(
            text(
                f"""INSERT INTO {TABLE_HISTORY_EVENTS}
                    (academic_year_start,event_date,title,description,
                     created_by,updated_by,created_at,updated_at)
                    VALUES(:academic_year_start,:event_date,:title,:description,
                           :user,:user,:now,:now) RETURNING id,revision"""
            ),
            {**values, "user": user, "now": now},
        ).mappings().one()
        event_id = int(row["id"])
        _replace_links(connection, event_id, values["match_ids"], values["photo_ids"])
    return {"ok": True, "id": event_id, "revision": int(row["revision"])}


@router.patch("/history/events/{event_id}")
def update_history_event(event_id: int, body: HistoryEventUpdateBody, request: Request):
    user, db = _senior_context(request, "team_history")
    values = _validation(validate_history_event, body.model_dump())
    now = _now()
    with db.transaction() as connection:
        _check_links(connection, values["match_ids"], values["photo_ids"])
        row = connection.execute(
            text(
                f"""UPDATE {TABLE_HISTORY_EVENTS} SET
                    academic_year_start=:academic_year_start,event_date=:event_date,
                    title=:title,description=:description,revision=revision+1,
                    updated_by=:user,updated_at=:now
                    WHERE id=:id AND revision=:revision RETURNING revision"""
            ),
            {**values, "id": event_id, "revision": body.revision, "user": user, "now": now},
        ).mappings().one_or_none()
        if row is None:
            exists = connection.execute(
                text(f"SELECT 1 FROM {TABLE_HISTORY_EVENTS} WHERE id=:id"), {"id": event_id}
            ).scalar()
            raise HTTPException(409 if exists else 404, "資料已更新或不存在，請重新載入。")
        _replace_links(connection, event_id, values["match_ids"], values["photo_ids"])
    return {"ok": True, "revision": int(row["revision"])}


@router.delete("/history/events/{event_id}")
def delete_history_event(event_id: int, revision: int, request: Request):
    _user, db = _senior_context(request, "team_history")
    changed = db.execute_count(
        f"DELETE FROM {TABLE_HISTORY_EVENTS} WHERE id=:id AND revision=:revision",
        {"id": event_id, "revision": revision},
    )
    if not changed:
        raise HTTPException(409, "資料已更新或不存在，請重新載入。")
    return {"ok": True}


@router.get("/history/memberships")
def history_memberships(request: Request, page: int = 1):
    _user, db = _member_context(request, "team_history")
    page, _, offset = bounds(page)
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_COMMITTEE_MEMBERSHIPS}")
    frame = db.query(
        f"""SELECT id,member_user_id,display_name,joined_academic_year,
                   ended_academic_year,exit_type,revision,created_by,updated_by,
                   created_at,updated_at
            FROM {TABLE_COMMITTEE_MEMBERSHIPS}
            ORDER BY joined_academic_year DESC,display_name,id DESC
            LIMIT :limit OFFSET :offset""",
        {"limit": PAGE_SIZE, "offset": offset},
    )
    items = _records(frame)
    for item in items:
        item["joined_academic_year_label"] = academic_year_label(item["joined_academic_year"])
        item["ended_academic_year_label"] = (
            academic_year_label(item["ended_academic_year"])
            if item.get("ended_academic_year") is not None
            else None
        )
    return payload(items, page, total)


@router.post("/history/memberships")
def create_membership(body: MembershipBody, request: Request):
    user, db = _senior_context(request, "team_history")
    values = _validation(validate_membership, body.model_dump())
    now = _now()
    with db.transaction() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('committee_membership_inventory'))"))
        count = connection.execute(text(f"SELECT COUNT(*) FROM {TABLE_COMMITTEE_MEMBERSHIPS}")).scalar()
        if int(count or 0) >= COMMITTEE_MEMBERSHIP_INVENTORY_LIMIT:
            raise HTTPException(409, "委員任期資料已達系統上限。")
        _check_membership_account(connection, values["member_user_id"])
        row = connection.execute(
            text(
                f"""INSERT INTO {TABLE_COMMITTEE_MEMBERSHIPS}
                    (member_user_id,display_name,joined_academic_year,
                     ended_academic_year,exit_type,created_by,updated_by,created_at,updated_at)
                    VALUES(:member_user_id,:display_name,:joined_academic_year,
                           :ended_academic_year,:exit_type,:user,:user,:now,:now)
                    RETURNING id,revision"""
            ),
            {**values, "user": user, "now": now},
        ).mappings().one()
    return {"ok": True, "id": int(row["id"]), "revision": int(row["revision"])}


@router.patch("/history/memberships/{membership_id}")
def update_membership(membership_id: int, body: MembershipUpdateBody, request: Request):
    user, db = _senior_context(request, "team_history")
    values = _validation(validate_membership, body.model_dump())
    with db.transaction() as connection:
        _check_membership_account(connection, values["member_user_id"])
        changed = connection.execute(
            text(
                f"""UPDATE {TABLE_COMMITTEE_MEMBERSHIPS} SET
                member_user_id=:member_user_id,display_name=:display_name,
                joined_academic_year=:joined_academic_year,
                ended_academic_year=:ended_academic_year,exit_type=:exit_type,
                revision=revision+1,updated_by=:user,updated_at=:now
                WHERE id=:id AND revision=:revision"""
            ),
            {**values, "id": membership_id, "revision": body.revision, "user": user, "now": _now()},
        ).rowcount
        if not changed:
            raise HTTPException(409, "資料已更新或不存在，請重新載入。")
    return {"ok": True, "revision": body.revision + 1}


@router.delete("/history/memberships/{membership_id}")
def delete_membership(membership_id: int, revision: int, request: Request):
    _user, db = _senior_context(request, "team_history")
    changed = db.execute_count(
        f"DELETE FROM {TABLE_COMMITTEE_MEMBERSHIPS} WHERE id=:id AND revision=:revision",
        {"id": membership_id, "revision": revision},
    )
    if not changed:
        raise HTTPException(409, "資料已更新或不存在，請重新載入。")
    return {"ok": True}


@router.get("/resources")
def resource_options(request: Request, search: str = "", kind: str = "all", page: int = 1):
    _user, db = _member_context(request, "team_history")
    query = str(search or "").strip().lower()[:100]
    pattern = f"%{query}%"
    resource_kind = str(kind or "all").strip().lower()
    if resource_kind not in {"all", "matches", "photos"}:
        raise HTTPException(400, "資源類型無效。")
    page, _, offset = bounds(page)
    common = {"empty": not bool(query), "search": pattern}

    match_where = (
        ":empty OR LOWER(COALESCE(match_id,'')||' '||COALESCE(topic_text,'')||' '"
        "||COALESCE(pro_team,'')||' '||COALESCE(con_team,'')) LIKE :search"
    )
    photo_where = (
        ":empty OR LOWER(COALESCE(album_label,'')||' '"
        "||COALESCE(photo_title,'')||' '||COALESCE(caption,'')) LIKE :search"
    )

    if resource_kind == "matches":
        total = scalar_count(
            db,
            f"SELECT COUNT(*) total FROM {TABLE_MATCHES} WHERE {match_where}",
            common,
        )
        matches = db.query(
            f"""SELECT match_id,match_date,match_time,topic_text,pro_team,con_team,debate_format
                FROM {TABLE_MATCHES} WHERE {match_where}
                ORDER BY match_date DESC NULLS LAST,match_id
                LIMIT :limit OFFSET :offset""",
            {**common, "limit": PAGE_SIZE, "offset": offset},
        )
        return {"kind": resource_kind, **payload(_records(matches), page, total)}

    if resource_kind == "photos":
        total = scalar_count(
            db,
            f"SELECT COUNT(*) total FROM {TABLE_MATCH_PHOTOS} WHERE {photo_where}",
            common,
        )
        photos = db.query(
            f"""SELECT id,album_label,photo_date,photo_title,caption
                FROM {TABLE_MATCH_PHOTOS} WHERE {photo_where}
                ORDER BY photo_date DESC NULLS LAST,id DESC
                LIMIT :limit OFFSET :offset""",
            {**common, "limit": PAGE_SIZE, "offset": offset},
        )
        return {"kind": resource_kind, **payload(_records(photos), page, total)}

    # Backward-compatible bounded response for the ghost forum picker and
    # direct linked-match lookup.  The team-history manager uses the paginated
    # kind-specific branches above.
    matches = db.query(
        f"""SELECT match_id,match_date,match_time,topic_text,pro_team,con_team,debate_format
            FROM {TABLE_MATCHES}
            WHERE {match_where}
            ORDER BY match_date DESC NULLS LAST,match_id LIMIT :limit""",
        {**common, "limit": PAGE_SIZE},
    )
    photos = db.query(
        f"""SELECT id,album_label,photo_date,photo_title,caption
            FROM {TABLE_MATCH_PHOTOS}
            WHERE {photo_where}
            ORDER BY photo_date DESC NULLS LAST,id DESC LIMIT :limit""",
        {**common, "limit": PAGE_SIZE},
    )
    return {"matches": _records(matches), "photos": _records(photos)}


# Senior committee forum --------------------------------------------------


@router.get("/forum/data")
def forum_data(request: Request):
    user, _db_value = _ghost_context(request)
    return {"user_id": user, "can_post": True}


@router.get("/forum/threads")
def forum_threads(request: Request, page: int = 1, search: str = "", sort: str = "activity"):
    user, db = _ghost_context(request)
    page, _, offset = bounds(page)
    query = str(search or "").strip().lower()[:100]
    where = "t.deleted_at IS NULL"
    params = {"user": user}
    if query:
        where += (
            " AND (LOWER(t.title) LIKE :search OR EXISTS "
            f"(SELECT 1 FROM {TABLE_GHOST_FORUM_POSTS} sp WHERE sp.thread_id=t.id "
            "AND sp.deleted_at IS NULL AND LOWER(sp.body) LIKE :search))"
        )
        params["search"] = f"%{query}%"
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_GHOST_FORUM_THREADS} t WHERE {where}", params)
    order = "t.created_at DESC,t.id DESC" if sort == "newest" else "t.last_activity_at DESC,t.id DESC"
    params.update(limit=PAGE_SIZE, offset=offset)
    frame = db.query(
        f"""SELECT t.id,t.title,t.author_user_id,t.revision,t.created_at,t.updated_at,
                   t.last_activity_at,(t.author_user_id=:user) can_edit,
                   (SELECT COUNT(*) FROM {TABLE_GHOST_FORUM_POSTS} p
                    WHERE p.thread_id=t.id AND p.deleted_at IS NULL) post_count,
                   (SELECT LEFT(p.body,300) FROM {TABLE_GHOST_FORUM_POSTS} p
                    WHERE p.thread_id=t.id AND p.is_first_post=TRUE) excerpt
            FROM {TABLE_GHOST_FORUM_THREADS} t WHERE {where}
            ORDER BY {order} LIMIT :limit OFFSET :offset""",
        params,
    )
    return payload(_records(frame), page, total)


@router.post("/forum/threads")
def create_forum_thread(body: ForumThreadBody, request: Request):
    user, db = _ghost_context(request)
    values = _validation(validate_thread, body.model_dump())
    now = _now()
    with db.transaction() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('ghost_forum_inventory'))"))
        thread_count = connection.execute(text(f"SELECT COUNT(*) FROM {TABLE_GHOST_FORUM_THREADS}")).scalar()
        post_count = connection.execute(text(f"SELECT COUNT(*) FROM {TABLE_GHOST_FORUM_POSTS}")).scalar()
        if int(thread_count or 0) >= GHOST_FORUM_THREAD_LIMIT:
            raise HTTPException(409, "老鬼專區主題已達系統上限。")
        if int(post_count or 0) >= GHOST_FORUM_POST_LIMIT:
            raise HTTPException(409, "老鬼專區留言已達系統上限。")
        _check_links(connection, values["match_ids"], values["photo_ids"])
        row = connection.execute(
            text(
                f"""INSERT INTO {TABLE_GHOST_FORUM_THREADS}
                    (title,author_user_id,created_at,updated_at,last_activity_at)
                    VALUES(:title,:user,:now,:now,:now) RETURNING id,revision"""
            ),
            {"title": values["title"], "user": user, "now": now},
        ).mappings().one()
        thread_id = int(row["id"])
        connection.execute(
            text(
                f"""INSERT INTO {TABLE_GHOST_FORUM_POSTS}
                    (thread_id,author_user_id,body,is_first_post,created_at,updated_at)
                    VALUES(:thread,:user,:body,TRUE,:now,:now)"""
            ),
            {"thread": thread_id, "user": user, "body": values["body"], "now": now},
        )
        _replace_links(connection, thread_id, values["match_ids"], values["photo_ids"], forum=True)
    notification = _fire_forum_push(
        db, user, thread_id, values["title"], "thread",
    )
    return {
        "ok": True,
        "id": thread_id,
        "revision": int(row["revision"]),
        "notification": notification,
    }


@router.get("/forum/threads/{thread_id}")
def forum_thread(thread_id: int, request: Request, page: int = 1):
    user, db = _ghost_context(request)
    thread_frame = db.query(
        f"""SELECT id,title,author_user_id,revision,created_at,updated_at,last_activity_at,
                   (author_user_id=:user) can_edit
            FROM {TABLE_GHOST_FORUM_THREADS}
            WHERE id=:id AND deleted_at IS NULL""",
        {"id": thread_id, "user": user},
    )
    if thread_frame.empty:
        raise HTTPException(404, "找不到討論主題。")
    page, _, offset = bounds(page)
    total = scalar_count(
        db,
        f"SELECT COUNT(*) total FROM {TABLE_GHOST_FORUM_POSTS} WHERE thread_id=:id",
        {"id": thread_id},
    )
    posts = db.query(
        f"""SELECT p.id,p.author_user_id,
                   CASE WHEN p.deleted_at IS NULL THEN p.body ELSE '' END body,
                   p.quoted_post_id,p.is_first_post,p.revision,p.created_at,p.updated_at,
                   p.deleted_at,(p.author_user_id=:user AND p.deleted_at IS NULL) can_edit,
                   q.author_user_id quoted_author,
                   CASE WHEN q.deleted_at IS NULL THEN LEFT(q.body,500) ELSE '' END quoted_body,
                   (SELECT COUNT(*) FROM {TABLE_GHOST_FORUM_REACTIONS} r WHERE r.post_id=p.id) like_count,
                   EXISTS(SELECT 1 FROM {TABLE_GHOST_FORUM_REACTIONS} r
                          WHERE r.post_id=p.id AND r.user_id=:user) viewer_liked
            FROM {TABLE_GHOST_FORUM_POSTS} p
            LEFT JOIN {TABLE_GHOST_FORUM_POSTS} q ON q.id=p.quoted_post_id
            WHERE p.thread_id=:id ORDER BY p.created_at,p.id
            LIMIT :limit OFFSET :offset""",
        {"id": thread_id, "user": user, "limit": PAGE_SIZE, "offset": offset},
    )
    thread_item = _records(thread_frame)[0]
    thread_item["links"] = _resource_links(db, [thread_id], forum=True)[thread_id]
    return {"thread": thread_item, "posts": payload(_records(posts), page, total)}


@router.patch("/forum/threads/{thread_id}")
def update_forum_thread(thread_id: int, body: ForumThreadUpdateBody, request: Request):
    user, db = _ghost_context(request)
    values = _validation(
        validate_thread,
        {**body.model_dump(), "body": "unchanged"},
    )
    now = _now()
    with db.transaction() as connection:
        _check_links(connection, values["match_ids"], values["photo_ids"])
        row = connection.execute(
            text(
                f"""UPDATE {TABLE_GHOST_FORUM_THREADS} SET
                    title=:title,revision=revision+1,updated_at=:now
                    WHERE id=:id AND author_user_id=:user AND revision=:revision
                      AND deleted_at IS NULL RETURNING revision"""
            ),
            {"title": values["title"], "now": now, "id": thread_id, "user": user, "revision": body.revision},
        ).mappings().one_or_none()
        if row is None:
            raise HTTPException(409, "主題已更新、不存在或不屬於你。")
        _replace_links(connection, thread_id, values["match_ids"], values["photo_ids"], forum=True)
    return {"ok": True, "revision": int(row["revision"])}


@router.delete("/forum/threads/{thread_id}")
def delete_forum_thread(thread_id: int, revision: int, request: Request):
    user, db = _ghost_context(request)
    changed = db.execute_count(
        f"""UPDATE {TABLE_GHOST_FORUM_THREADS} SET deleted_at=:now,revision=revision+1
            WHERE id=:id AND author_user_id=:user AND revision=:revision AND deleted_at IS NULL""",
        {"id": thread_id, "user": user, "revision": revision, "now": _now()},
    )
    if not changed:
        raise HTTPException(409, "主題已更新、不存在或不屬於你。")
    return {"ok": True}


@router.post("/forum/threads/{thread_id}/posts")
def create_forum_post(thread_id: int, body: ForumPostBody, request: Request):
    user, db = _ghost_context(request)
    post_body = _validation(validate_post_body, body.body)
    now = _now()
    with db.transaction() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('ghost_forum_inventory'))"))
        thread = connection.execute(
            text(f"SELECT title FROM {TABLE_GHOST_FORUM_THREADS} WHERE id=:id AND deleted_at IS NULL FOR UPDATE"),
            {"id": thread_id},
        ).mappings().one_or_none()
        if thread is None:
            raise HTTPException(404, "找不到討論主題。")
        count = connection.execute(text(f"SELECT COUNT(*) FROM {TABLE_GHOST_FORUM_POSTS}")).scalar()
        if int(count or 0) >= GHOST_FORUM_POST_LIMIT:
            raise HTTPException(409, "老鬼專區留言已達系統上限。")
        if body.quoted_post_id is not None:
            quote = connection.execute(
                text(
                    f"SELECT 1 FROM {TABLE_GHOST_FORUM_POSTS} "
                    "WHERE id=:post AND thread_id=:thread"
                ),
                {"post": body.quoted_post_id, "thread": thread_id},
            ).scalar()
            if not quote:
                raise HTTPException(400, "引用留言不屬於此主題。")
        row = connection.execute(
            text(
                f"""INSERT INTO {TABLE_GHOST_FORUM_POSTS}
                    (thread_id,author_user_id,body,quoted_post_id,created_at,updated_at)
                    VALUES(:thread,:user,:body,:quote,:now,:now) RETURNING id,revision"""
            ),
            {"thread": thread_id, "user": user, "body": post_body, "quote": body.quoted_post_id, "now": now},
        ).mappings().one()
        connection.execute(
            text(f"UPDATE {TABLE_GHOST_FORUM_THREADS} SET last_activity_at=:now WHERE id=:id"),
            {"now": now, "id": thread_id},
        )
    post_id = int(row["id"])
    notification = _fire_forum_push(
        db, user, thread_id, str(thread["title"]), "reply", post_id=post_id,
    )
    return {
        "ok": True,
        "id": post_id,
        "revision": int(row["revision"]),
        "notification": notification,
    }


@router.patch("/forum/posts/{post_id}")
def update_forum_post(post_id: int, body: ForumPostUpdateBody, request: Request):
    user, db = _ghost_context(request)
    post_body = _validation(validate_post_body, body.body)
    changed = db.execute_count(
        f"""UPDATE {TABLE_GHOST_FORUM_POSTS} SET body=:body,revision=revision+1,updated_at=:now
            WHERE id=:id AND author_user_id=:user AND revision=:revision AND deleted_at IS NULL""",
        {"body": post_body, "now": _now(), "id": post_id, "user": user, "revision": body.revision},
    )
    if not changed:
        raise HTTPException(409, "留言已更新、不存在或不屬於你。")
    return {"ok": True, "revision": body.revision + 1}


@router.delete("/forum/posts/{post_id}")
def delete_forum_post(post_id: int, revision: int, request: Request):
    user, db = _ghost_context(request)
    changed = db.execute_count(
        f"""UPDATE {TABLE_GHOST_FORUM_POSTS}
            SET body='',deleted_at=:now,revision=revision+1,updated_at=:now
            WHERE id=:id AND author_user_id=:user AND revision=:revision
              AND is_first_post=FALSE AND deleted_at IS NULL""",
        {"now": _now(), "id": post_id, "user": user, "revision": revision},
    )
    if not changed:
        raise HTTPException(409, "首篇須刪除整個主題；其餘留言必須屬於你及未被更新。")
    return {"ok": True}


@router.post("/forum/posts/{post_id}/like")
def toggle_forum_like(post_id: int, request: Request):
    user, db = _ghost_context(request)
    with db.transaction() as connection:
        post = connection.execute(
            text(f"SELECT 1 FROM {TABLE_GHOST_FORUM_POSTS} WHERE id=:id AND deleted_at IS NULL"),
            {"id": post_id},
        ).scalar()
        if not post:
            raise HTTPException(404, "找不到留言。")
        removed = connection.execute(
            text(f"DELETE FROM {TABLE_GHOST_FORUM_REACTIONS} WHERE post_id=:post AND user_id=:user"),
            {"post": post_id, "user": user},
        ).rowcount
        if not removed:
            connection.execute(
                text(
                    f"""INSERT INTO {TABLE_GHOST_FORUM_REACTIONS}(post_id,user_id,created_at)
                        VALUES(:post,:user,:now)"""
                ),
                {"post": post_id, "user": user, "now": _now()},
            )
    return {"ok": True, "liked": not bool(removed)}
