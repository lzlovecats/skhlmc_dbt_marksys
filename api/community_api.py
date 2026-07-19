"""Committee recent matches, team history and graduate discussion forum."""

from datetime import datetime, timedelta
import logging
import secrets
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
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
from core.sticker_catalog import get_sticker, list_stickers
from schema import (
    TABLE_ACCOUNTS,
    TABLE_COMMITTEE_MEMBERSHIPS,
    TABLE_GHOST_FORUM_POSTS,
    TABLE_GHOST_FORUM_NOTIFICATIONS,
    TABLE_GHOST_FORUM_REACTIONS,
    TABLE_GHOST_FORUM_THREAD_HISTORY_EVENTS,
    TABLE_GHOST_FORUM_THREAD_PHOTOS,
    TABLE_GHOST_FORUM_THREAD_VIDEOS,
    TABLE_GHOST_FORUM_THREAD_USER_STATE,
    TABLE_GHOST_FORUM_THREADS,
    TABLE_GHOST_FORUM_USER_PROFILES,
    TABLE_HISTORY_EVENT_MATCHES,
    TABLE_HISTORY_EVENT_PHOTOS,
    TABLE_HISTORY_EVENTS,
    TABLE_MATCHES,
    TABLE_MATCH_PHOTOS,
    TABLE_MATCH_VIDEOS,
    TABLE_RECENT_MATCH_NOTIFICATIONS,
    TABLE_RECENT_MATCHES,
)
from system_limits import (
    CACHE_STATIC_MAX_AGE_SECONDS,
    COMMITTEE_MEMBERSHIP_INVENTORY_LIMIT,
    GHOST_FORUM_NOTIFICATION_CLAIM_TTL_SECONDS,
    GHOST_FORUM_POST_LIMIT,
    GHOST_FORUM_THREAD_LIMIT,
    HISTORY_EVENT_INVENTORY_LIMIT,
    RECENT_MATCH_INVENTORY_LIMIT,
    RECENT_MATCH_NOTIFICATION_CLAIM_TTL_SECONDS,
)
from version import APP_VERSION


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
    video_ids: list[int] = Field(default_factory=list, max_length=20)
    photo_ids: list[int] = Field(default_factory=list, max_length=30)
    history_event_ids: list[int] = Field(default_factory=list, max_length=20)


class ForumThreadUpdateBody(BaseModel):
    title: str = Field(max_length=300)
    video_ids: list[int] = Field(default_factory=list, max_length=20)
    photo_ids: list[int] = Field(default_factory=list, max_length=30)
    history_event_ids: list[int] = Field(default_factory=list, max_length=20)
    revision: int = Field(ge=1)


class ForumPostBody(BaseModel):
    body: str = Field(default="", max_length=8000)
    sticker_id: str | None = Field(default=None, min_length=1, max_length=200)
    quoted_post_id: int | None = Field(default=None, ge=1)


class ForumPostUpdateBody(BaseModel):
    body: str = Field(max_length=8000)
    revision: int = Field(ge=1)


class ForumReadBody(BaseModel):
    post_id: int | None = Field(default=None, ge=1)


class ForumThreadStateBody(BaseModel):
    muted: bool


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


def _ensure_forum_profile(connection, user_id, now):
    """Start unread tracking without treating pre-existing posts as unread."""
    connection.execute(
        text(
            f"""INSERT INTO {TABLE_GHOST_FORUM_USER_PROFILES}
                (user_id,unread_since,created_at,updated_at)
                VALUES(:user,:now,:now,:now)
                ON CONFLICT (user_id) DO NOTHING"""
        ),
        {"user": user_id, "now": now},
    )


def _records(frame):
    return json_safe(frame.to_dict("records"))


def _validation(function, values):
    try:
        return function(values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _forum_reply_content(body: str, sticker_id: str | None):
    has_text = bool(str(body or "").strip())
    has_sticker = sticker_id is not None
    if has_text == has_sticker:
        raise HTTPException(400, "每則回覆必須選擇文字或一張 Sticker。")
    if has_sticker:
        sticker = get_sticker(sticker_id)
        if sticker is None:
            raise HTTPException(400, "所選 Sticker 不存在或已停止提供。")
        return "", sticker.sticker_id
    return _validation(validate_post_body, body), None


def _connection_rows(connection, sql, params=None):
    result = connection.execute(text(sql), params or {})
    return [dict(row) for row in result.mappings().all()]


def _check_history_links(connection, match_ids, photo_ids):
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


def _check_forum_links(connection, video_ids, photo_ids, history_event_ids):
    if video_ids:
        found = connection.execute(
            text(
                f"SELECT COUNT(*) FROM {TABLE_MATCH_VIDEOS} "
                "WHERE id=ANY(CAST(:ids AS integer[])) "
                "AND COALESCE(is_visible,TRUE)=TRUE"
            ),
            {"ids": video_ids},
        ).scalar()
        if int(found or 0) != len(video_ids):
            raise HTTPException(400, "其中一條影片連結已不存在或不可見。")
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
    if history_event_ids:
        found = connection.execute(
            text(
                f"SELECT COUNT(*) FROM {TABLE_HISTORY_EVENTS} "
                "WHERE id=ANY(CAST(:ids AS bigint[]))"
            ),
            {"ids": history_event_ids},
        ).scalar()
        if int(found or 0) != len(history_event_ids):
            raise HTTPException(400, "其中一個隊史事件連結已不存在。")


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


def _replace_history_links(connection, owner_id, match_ids, photo_ids):
    connection.execute(
        text(f"DELETE FROM {TABLE_HISTORY_EVENT_MATCHES} WHERE event_id=:owner"),
        {"owner": owner_id},
    )
    connection.execute(
        text(f"DELETE FROM {TABLE_HISTORY_EVENT_PHOTOS} WHERE event_id=:owner"),
        {"owner": owner_id},
    )
    for match_id in match_ids:
        connection.execute(
            text(
                f"INSERT INTO {TABLE_HISTORY_EVENT_MATCHES}(event_id,match_id) "
                "VALUES(:owner,:match)"
            ),
            {"owner": owner_id, "match": match_id},
        )
    for photo_id in photo_ids:
        connection.execute(
            text(
                f"INSERT INTO {TABLE_HISTORY_EVENT_PHOTOS}(event_id,photo_id) "
                "VALUES(:owner,:photo)"
            ),
            {"owner": owner_id, "photo": photo_id},
        )


def _replace_forum_links(connection, owner_id, video_ids, photo_ids, history_event_ids):
    for table_name in (
        TABLE_GHOST_FORUM_THREAD_VIDEOS,
        TABLE_GHOST_FORUM_THREAD_PHOTOS,
        TABLE_GHOST_FORUM_THREAD_HISTORY_EVENTS,
    ):
        connection.execute(
            text(f"DELETE FROM {table_name} WHERE thread_id=:owner"),
            {"owner": owner_id},
        )
    for video_id in video_ids:
        connection.execute(
            text(
                f"INSERT INTO {TABLE_GHOST_FORUM_THREAD_VIDEOS}(thread_id,video_id) "
                "VALUES(:owner,:video)"
            ),
            {"owner": owner_id, "video": video_id},
        )
    for photo_id in photo_ids:
        connection.execute(
            text(
                f"INSERT INTO {TABLE_GHOST_FORUM_THREAD_PHOTOS}(thread_id,photo_id) "
                "VALUES(:owner,:photo)"
            ),
            {"owner": owner_id, "photo": photo_id},
        )
    for event_id in history_event_ids:
        connection.execute(
            text(
                f"INSERT INTO {TABLE_GHOST_FORUM_THREAD_HISTORY_EVENTS}"
                "(thread_id,event_id) VALUES(:owner,:event)"
            ),
            {"owner": owner_id, "event": event_id},
        )


def _history_resource_links(db, owner_ids):
    links = {int(owner): {"matches": [], "photos": []} for owner in owner_ids}
    if not links:
        return links
    params = {"ids": list(links)}
    matches = db.query(
        f"""SELECT l.event_id owner_id,m.match_id,m.match_date,m.match_time,
                   m.topic_text,m.pro_team,m.con_team,m.debate_format,
                   selected_video.video_id
            FROM {TABLE_HISTORY_EVENT_MATCHES} l
            JOIN {TABLE_MATCHES} m ON m.match_id=l.match_id
            LEFT JOIN LATERAL (
                SELECT video.id AS video_id
                FROM {TABLE_MATCH_VIDEOS} AS video
                WHERE video.match_id=m.match_id
                  AND COALESCE(video.is_visible,TRUE)=TRUE
                ORDER BY video.display_order ASC NULLS LAST,
                         video.created_at DESC,video.id DESC
                LIMIT 1
            ) AS selected_video ON TRUE
            WHERE l.event_id=ANY(CAST(:ids AS bigint[]))
            ORDER BY m.match_date DESC NULLS LAST,m.match_id""",
        params,
    )
    photos = db.query(
        f"""SELECT l.event_id owner_id,p.id,p.album_label,p.photo_date,
                   p.photo_title,p.caption
            FROM {TABLE_HISTORY_EVENT_PHOTOS} l
            JOIN {TABLE_MATCH_PHOTOS} p ON p.id=l.photo_id
            WHERE l.event_id=ANY(CAST(:ids AS bigint[]))
            ORDER BY p.photo_date DESC NULLS LAST,p.id DESC""",
        params,
    )
    for row in _records(matches):
        links[int(row.pop("owner_id"))]["matches"].append(row)
    for row in _records(photos):
        links[int(row.pop("owner_id"))]["photos"].append(row)
    return links


def _forum_resource_links(db, thread_ids):
    links = {
        int(thread): {"videos": [], "photos": [], "history_events": []}
        for thread in thread_ids
    }
    if not links:
        return links
    params = {"ids": list(links)}
    videos = db.query(
        f"""SELECT l.thread_id owner_id,v.id,v.video_title,
                   COALESCE(NULLIF(v.match_label,''),NULLIF(v.match_id,''),v.video_title)
                       match_display,
                   COALESCE(NULLIF(m.topic_text,''),NULLIF(v.standalone_topic_text,''),'')
                       topic_text,
                   COALESCE(NULLIF(m.pro_team,''),NULLIF(v.standalone_pro_team,''),'')
                       pro_team,
                   COALESCE(NULLIF(m.con_team,''),NULLIF(v.standalone_con_team,''),'')
                       con_team
            FROM {TABLE_GHOST_FORUM_THREAD_VIDEOS} l
            JOIN {TABLE_MATCH_VIDEOS} v ON v.id=l.video_id
            LEFT JOIN {TABLE_MATCHES} m ON m.match_id=v.match_id
            WHERE l.thread_id=ANY(CAST(:ids AS bigint[]))
              AND COALESCE(v.is_visible,TRUE)=TRUE
            ORDER BY v.display_order ASC NULLS LAST,v.created_at DESC NULLS LAST,v.id DESC""",
        params,
    )
    photos = db.query(
        f"""SELECT l.thread_id owner_id,p.id,p.album_label,p.photo_date,
                   p.photo_title,p.caption
            FROM {TABLE_GHOST_FORUM_THREAD_PHOTOS} l
            JOIN {TABLE_MATCH_PHOTOS} p ON p.id=l.photo_id
            WHERE l.thread_id=ANY(CAST(:ids AS bigint[]))
            ORDER BY p.photo_date DESC NULLS LAST,p.id DESC""",
        params,
    )
    events = db.query(
        f"""SELECT l.thread_id owner_id,e.id,e.academic_year_start,e.event_date,
                   e.title,e.description
            FROM {TABLE_GHOST_FORUM_THREAD_HISTORY_EVENTS} l
            JOIN {TABLE_HISTORY_EVENTS} e ON e.id=l.event_id
            WHERE l.thread_id=ANY(CAST(:ids AS bigint[]))
            ORDER BY e.academic_year_start DESC,e.event_date DESC NULLS LAST,e.id DESC""",
        params,
    )
    for row in _records(videos):
        links[int(row.pop("owner_id"))]["videos"].append(row)
    for row in _records(photos):
        links[int(row.pop("owner_id"))]["photos"].append(row)
    for row in _records(events):
        row["academic_year_label"] = academic_year_label(row["academic_year_start"])
        links[int(row.pop("owner_id"))]["history_events"].append(row)
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

        sent = notify_committee(
            db,
            _get_vapid(),
            title,
            body,
            exclude_user=author_user_id,
            senior_only=True,
            forum_thread_id=thread_id,
            tag=f"ghost-forum-thread-{int(thread_id)}",
            url=f"/ghost-forum?thread={int(thread_id)}&post={int(post_id)}",
        )
    except Exception:
        logger.exception(
            "Ghost forum push failed for thread_id=%s event_kind=%s",
            thread_id,
            event_kind,
        )
        sent = 0
    return {"sent_count": int(sent)}


def _queue_forum_notification(connection, post_id, event_kind):
    row = connection.execute(
        text(
            f"""INSERT INTO {TABLE_GHOST_FORUM_NOTIFICATIONS}(post_id,event_kind)
                VALUES(:post,:event)
                ON CONFLICT (post_id) DO UPDATE SET post_id=EXCLUDED.post_id
                RETURNING id"""
        ),
        {"post": post_id, "event": event_kind},
    ).mappings().one_or_none()
    return int(row["id"]) if row is not None else None


def _dispatch_forum_notification(db, notification_id, *, author_user_id=None):
    """Claim, deliver, and settle one durable forum notification."""
    now = _now()
    stale_before = now - timedelta(seconds=GHOST_FORUM_NOTIFICATION_CLAIM_TTL_SECONDS)
    claim = secrets.token_urlsafe(24)
    params = {
        "id": int(notification_id),
        "now": now,
        "stale": stale_before,
        "claim": claim,
    }
    owner_sql = ""
    if author_user_id is not None:
        owner_sql = " AND p.author_user_id=:author"
        params["author"] = author_user_id
    with db.transaction() as connection:
        row = connection.execute(
            text(
                f"""SELECT n.id,n.event_kind,p.id post_id,p.author_user_id,
                           t.id thread_id,t.title
                    FROM {TABLE_GHOST_FORUM_NOTIFICATIONS} n
                    JOIN {TABLE_GHOST_FORUM_POSTS} p ON p.id=n.post_id
                    JOIN {TABLE_GHOST_FORUM_THREADS} t ON t.id=p.thread_id
                    WHERE n.id=:id AND t.deleted_at IS NULL
                      AND (n.state IN ('pending','retryable') OR
                           (n.state='sending' AND n.attempted_at<:stale))
                      {owner_sql}
                    FOR UPDATE OF n"""
            ),
            params,
        ).mappings().one_or_none()
        if row is None:
            return {"id": int(notification_id), "state": "not_retryable", "sent_count": 0}
        connection.execute(
            text(
                f"""UPDATE {TABLE_GHOST_FORUM_NOTIFICATIONS}
                    SET state='sending',claim_token=:claim,attempted_at=:now,last_error=''
                    WHERE id=:id"""
            ),
            params,
        )

    delivery = _fire_forum_push(
        db,
        str(row["author_user_id"]),
        int(row["thread_id"]),
        str(row["title"]),
        str(row["event_kind"]),
        post_id=int(row["post_id"]),
    )
    sent = int(delivery.get("sent_count") or 0)
    state = "sent" if sent > 0 else "retryable"
    error = "" if sent > 0 else "目前沒有成功送達的老鬼裝置。"
    db.execute(
        f"""UPDATE {TABLE_GHOST_FORUM_NOTIFICATIONS}
            SET state=:state,sent_at=CASE WHEN :sent THEN :now ELSE NULL END,
                sent_count=:count,last_error=:error,claim_token=NULL
            WHERE id=:id AND claim_token=:claim""",
        {
            "state": state,
            "sent": sent > 0,
            "now": _now(),
            "count": sent,
            "error": error,
            "id": int(notification_id),
            "claim": claim,
        },
    )
    return {"id": int(notification_id), "state": state, "sent_count": sent}


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
def history_events(
    request: Request,
    page: int = 1,
    order: str = "newest",
    event_id: int | None = None,
):
    _user, db = _member_context(request, "team_history")
    order_sql = {
        "newest": "academic_year_start DESC,event_date DESC NULLS LAST,id DESC",
        "oldest": "academic_year_start ASC,event_date ASC NULLS LAST,id ASC",
    }.get(str(order or "").strip().lower())
    if order_sql is None:
        raise HTTPException(400, "Timeline 排序方向無效。")
    if event_id is not None:
        if event_id < 1:
            raise HTTPException(400, "隊史事件連結無效。")
        position_frame = db.query(
            f"""SELECT position FROM (
                    SELECT id,ROW_NUMBER() OVER (ORDER BY {order_sql}) position
                    FROM {TABLE_HISTORY_EVENTS}
                ) ranked WHERE id=:event_id""",
            {"event_id": event_id},
        )
        if position_frame.empty:
            raise HTTPException(404, "找不到隊史事件。")
        position = int(position_frame.iloc[0]["position"])
        page = max(1, (position + PAGE_SIZE - 1) // PAGE_SIZE)
    page, _, offset = bounds(page)
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_HISTORY_EVENTS}")
    frame = db.query(
        f"""SELECT id,academic_year_start,event_date,title,description,revision,
                   created_by,updated_by,created_at,updated_at
            FROM {TABLE_HISTORY_EVENTS}
            ORDER BY {order_sql}
            LIMIT :limit OFFSET :offset""",
        {"limit": PAGE_SIZE, "offset": offset},
    )
    items = _records(frame)
    links = _history_resource_links(db, [row["id"] for row in items])
    for item in items:
        item["academic_year_label"] = academic_year_label(item["academic_year_start"])
        item["links"] = links[int(item["id"])]
    result = payload(items, page, total)
    result["target_event_id"] = event_id
    return result


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
        _check_history_links(connection, values["match_ids"], values["photo_ids"])
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
        _replace_history_links(
            connection, event_id, values["match_ids"], values["photo_ids"],
        )
    return {"ok": True, "id": event_id, "revision": int(row["revision"])}


@router.patch("/history/events/{event_id}")
def update_history_event(event_id: int, body: HistoryEventUpdateBody, request: Request):
    user, db = _senior_context(request, "team_history")
    values = _validation(validate_history_event, body.model_dump())
    now = _now()
    with db.transaction() as connection:
        _check_history_links(connection, values["match_ids"], values["photo_ids"])
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
        _replace_history_links(
            connection, event_id, values["match_ids"], values["photo_ids"],
        )
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
def history_memberships(request: Request, page: int = 1, order: str = "oldest"):
    _user, db = _member_context(request, "team_history")
    order_sql = {
        "oldest": "joined_academic_year ASC,display_name ASC,id ASC",
        "newest": "joined_academic_year DESC,display_name ASC,id DESC",
    }.get(str(order or "").strip().lower())
    if order_sql is None:
        raise HTTPException(400, "任期排序方向無效。")
    page, _, offset = bounds(page)
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_COMMITTEE_MEMBERSHIPS}")
    frame = db.query(
        f"""SELECT id,member_user_id,display_name,joined_academic_year,
                   ended_academic_year,exit_type,revision,created_by,updated_by,
                   created_at,updated_at
            FROM {TABLE_COMMITTEE_MEMBERSHIPS}
            ORDER BY {order_sql}
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


@router.get("/forum/resources")
def forum_resource_options(
    request: Request,
    search: str = "",
    kind: str = "videos",
    page: int = 1,
    event_id: int | None = None,
):
    _user, db = _ghost_context(request)
    query = str(search or "").strip().lower()[:100]
    pattern = f"%{query}%"
    resource_kind = str(kind or "videos").strip().lower()
    if resource_kind not in {"videos", "photos", "history_events"}:
        raise HTTPException(400, "資源類型無效。")
    if event_id is not None and event_id < 1:
        raise HTTPException(400, "隊史事件連結無效。")
    page, _, offset = bounds(page)
    common = {"empty": not bool(query), "search": pattern}

    if resource_kind == "videos":
        video_where = """COALESCE(v.is_visible,TRUE)=TRUE AND (
            :empty OR LOWER(
                COALESCE(v.video_title,'')||' '||COALESCE(v.match_label,'')||' '||
                COALESCE(v.match_id,'')||' '||COALESCE(v.standalone_topic_text,'')||' '||
                COALESCE(v.standalone_pro_team,'')||' '||
                COALESCE(v.standalone_con_team,'')||' '||COALESCE(m.topic_text,'')||' '||
                COALESCE(m.pro_team,'')||' '||COALESCE(m.con_team,'')
            ) LIKE :search
        )"""
        source_sql = (
            f"FROM {TABLE_MATCH_VIDEOS} v "
            f"LEFT JOIN {TABLE_MATCHES} m ON m.match_id=v.match_id"
        )
        total = scalar_count(
            db,
            f"SELECT COUNT(*) total {source_sql} WHERE {video_where}",
            common,
        )
        videos = db.query(
            f"""SELECT v.id,v.match_id,v.video_title,
                       COALESCE(NULLIF(v.match_label,''),NULLIF(v.match_id,''),
                                v.video_title) match_display,
                       COALESCE(NULLIF(m.topic_text,''),
                                NULLIF(v.standalone_topic_text,''),'') topic_text,
                       COALESCE(NULLIF(m.pro_team,''),
                                NULLIF(v.standalone_pro_team,''),'') pro_team,
                       COALESCE(NULLIF(m.con_team,''),
                                NULLIF(v.standalone_con_team,''),'') con_team,
                       m.match_date
                {source_sql} WHERE {video_where}
                ORDER BY m.match_date DESC NULLS LAST,
                         v.display_order ASC NULLS LAST,
                         v.created_at DESC NULLS LAST,v.id DESC
                LIMIT :limit OFFSET :offset""",
            {**common, "limit": PAGE_SIZE, "offset": offset},
        )
        return {"kind": resource_kind, **payload(_records(videos), page, total)}

    if resource_kind == "photos":
        photo_where = """:empty OR LOWER(
            COALESCE(album_label,'')||' '||COALESCE(photo_title,'')||' '||
            COALESCE(caption,'')
        ) LIKE :search"""
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

    event_where = """(:event_id IS NULL OR id=:event_id) AND (
        :empty OR LOWER(
            COALESCE(title,'')||' '||COALESCE(description,'')||' '||
            academic_year_start::text||' '||COALESCE(event_date::text,'')
        ) LIKE :search
    )"""
    event_params = {**common, "event_id": event_id}
    total = scalar_count(
        db,
        f"SELECT COUNT(*) total FROM {TABLE_HISTORY_EVENTS} WHERE {event_where}",
        event_params,
    )
    events = db.query(
        f"""SELECT id,academic_year_start,event_date,title,description
            FROM {TABLE_HISTORY_EVENTS} WHERE {event_where}
            ORDER BY academic_year_start DESC,event_date DESC NULLS LAST,id DESC
            LIMIT :limit OFFSET :offset""",
        {**event_params, "limit": PAGE_SIZE, "offset": offset},
    )
    items = _records(events)
    for item in items:
        item["academic_year_label"] = academic_year_label(item["academic_year_start"])
    return {"kind": resource_kind, **payload(items, page, total)}


# Senior committee forum --------------------------------------------------


@router.get("/forum/data")
def forum_data(request: Request):
    user, _db_value = _ghost_context(request)
    return {"user_id": user, "can_post": True}


@router.post("/forum/session")
def forum_session(request: Request):
    user, db = _ghost_context(request)
    now = _now()
    with db.transaction() as connection:
        _ensure_forum_profile(connection, user, now)
    retryable = db.query(
        f"""SELECT n.id notification_id,n.event_kind,n.attempted_at,
                   t.id thread_id,t.title
            FROM {TABLE_GHOST_FORUM_NOTIFICATIONS} n
            JOIN {TABLE_GHOST_FORUM_POSTS} p ON p.id=n.post_id
            JOIN {TABLE_GHOST_FORUM_THREADS} t ON t.id=p.thread_id
            WHERE p.author_user_id=:user
              AND (n.state IN ('pending','retryable') OR
                   (n.state='sending' AND n.attempted_at<:stale))
              AND t.deleted_at IS NULL
            ORDER BY n.attempted_at DESC NULLS LAST,n.id DESC LIMIT 20""",
        {
            "user": user,
            "stale": now - timedelta(
                seconds=GHOST_FORUM_NOTIFICATION_CLAIM_TTL_SECONDS
            ),
        },
    )
    return {
        "user_id": user,
        "can_post": True,
        "retryable_notifications": _records(retryable),
    }


@router.get("/forum/stickers")
def forum_stickers(request: Request):
    _user, _db_value = _ghost_context(request)
    return {
        "items": [
            {
                "id": item.sticker_id,
                "label": item.label,
                "url": (
                    "/api/community/forum/stickers/"
                    f"{quote(item.sticker_id, safe='')}?v={quote(APP_VERSION, safe='')}"
                ),
            }
            for item in list_stickers()
        ]
    }


@router.get("/forum/stickers/{sticker_id}")
def forum_sticker_image(sticker_id: str, request: Request):
    _user, _db_value = _ghost_context(request)
    sticker = get_sticker(sticker_id)
    if sticker is None:
        raise HTTPException(404, "找不到 Sticker。")
    return FileResponse(
        sticker.path,
        media_type="image/webp",
        headers={
            "Cache-Control": (
                f"private, max-age={CACHE_STATIC_MAX_AGE_SECONDS}, immutable"
            ),
            "X-Content-Type-Options": "nosniff",
            "Vary": "Cookie",
        },
    )


@router.get("/forum/threads")
def forum_threads(request: Request, page: int = 1, search: str = "", sort: str = "activity"):
    user, db = _ghost_context(request)
    page, _, offset = bounds(page)
    query = str(search or "").strip().lower()[:100]
    where = "t.deleted_at IS NULL"
    params = {"user": user}
    search_join = ""
    search_columns = "NULL::BIGINT matched_post_id,NULL::TEXT matched_excerpt"
    if query:
        search_join = (
            "LEFT JOIN LATERAL ("
            f"SELECT sp.id,LEFT(sp.body,300) excerpt FROM {TABLE_GHOST_FORUM_POSTS} sp "
            "WHERE sp.thread_id=t.id AND sp.deleted_at IS NULL "
            "AND LOWER(sp.body) LIKE :search "
            "ORDER BY sp.created_at DESC,sp.id DESC LIMIT 1"
            ") hit ON TRUE"
        )
        search_columns = "hit.id matched_post_id,hit.excerpt matched_excerpt"
        where += " AND (LOWER(t.title) LIKE :search OR hit.id IS NOT NULL)"
        params["search"] = f"%{query}%"
    base_join = f"""LEFT JOIN {TABLE_GHOST_FORUM_USER_PROFILES} profile
                         ON profile.user_id=:user
                     LEFT JOIN {TABLE_GHOST_FORUM_THREAD_USER_STATE} state
                         ON state.thread_id=t.id AND state.user_id=:user
                     {search_join}"""
    total = scalar_count(
        db,
        f"SELECT COUNT(*) total FROM {TABLE_GHOST_FORUM_THREADS} t {base_join} WHERE {where}",
        params,
    )
    order = "t.created_at DESC,t.id DESC" if sort == "newest" else "t.last_activity_at DESC,t.id DESC"
    params.update(limit=PAGE_SIZE, offset=offset)
    frame = db.query(
        f"""SELECT t.id,t.title,t.author_user_id,t.revision,t.created_at,t.updated_at,
                   t.last_activity_at,(t.author_user_id=:user) can_edit,
                   COALESCE(state.muted,FALSE) muted,
                   (SELECT COUNT(*) FROM {TABLE_GHOST_FORUM_POSTS} unread
                    WHERE unread.thread_id=t.id AND unread.deleted_at IS NULL
                      AND profile.user_id IS NOT NULL
                      AND unread.created_at>profile.unread_since
                      AND (state.last_read_post_id IS NULL
                           OR unread.id>state.last_read_post_id)) unread_count,
                   (SELECT COUNT(*) FROM {TABLE_GHOST_FORUM_POSTS} p
                    WHERE p.thread_id=t.id AND p.deleted_at IS NULL
                      AND p.is_first_post=FALSE) post_count,
                   (SELECT LEFT(p.body,300) FROM {TABLE_GHOST_FORUM_POSTS} p
                    WHERE p.thread_id=t.id AND p.is_first_post=TRUE) excerpt,
                   {search_columns}
            FROM {TABLE_GHOST_FORUM_THREADS} t {base_join} WHERE {where}
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
            raise HTTPException(409, "老鬼專區帖文已達系統上限。")
        if int(post_count or 0) >= GHOST_FORUM_POST_LIMIT:
            raise HTTPException(409, "老鬼專區留言已達系統上限。")
        _check_forum_links(
            connection,
            values["video_ids"],
            values["photo_ids"],
            values["history_event_ids"],
        )
        row = connection.execute(
            text(
                f"""INSERT INTO {TABLE_GHOST_FORUM_THREADS}
                    (title,author_user_id,created_at,updated_at,last_activity_at)
                    VALUES(:title,:user,:now,:now,:now) RETURNING id,revision"""
            ),
            {"title": values["title"], "user": user, "now": now},
        ).mappings().one()
        thread_id = int(row["id"])
        post_row = connection.execute(
            text(
                f"""INSERT INTO {TABLE_GHOST_FORUM_POSTS}
                    (thread_id,author_user_id,body,is_first_post,created_at,updated_at)
                    VALUES(:thread,:user,:body,TRUE,:now,:now) RETURNING id"""
            ),
            {"thread": thread_id, "user": user, "body": values["body"], "now": now},
        ).mappings().one()
        post_id = int(post_row["id"])
        _replace_forum_links(
            connection,
            thread_id,
            values["video_ids"],
            values["photo_ids"],
            values["history_event_ids"],
        )
        _ensure_forum_profile(connection, user, now)
        connection.execute(
            text(
                f"""INSERT INTO {TABLE_GHOST_FORUM_THREAD_USER_STATE}
                    (thread_id,user_id,last_read_post_id,muted,updated_at)
                    VALUES(:thread,:user,:post,FALSE,:now)
                    ON CONFLICT (thread_id,user_id) DO UPDATE SET
                        last_read_post_id=EXCLUDED.last_read_post_id,updated_at=EXCLUDED.updated_at"""
            ),
            {"thread": thread_id, "user": user, "post": post_id, "now": now},
        )
        notification_id = _queue_forum_notification(connection, post_id, "thread")
    notification = _dispatch_forum_notification(db, notification_id)
    return {
        "ok": True,
        "id": thread_id,
        "post_id": post_id,
        "revision": int(row["revision"]),
        "notification": notification,
    }


@router.get("/forum/threads/{thread_id}")
def forum_thread(
    thread_id: int,
    request: Request,
    page: int = 1,
    latest: bool = False,
    post: int | None = None,
):
    user, db = _ghost_context(request)
    thread_frame = db.query(
        f"""SELECT thread.id,thread.title,thread.author_user_id,thread.revision,
                   thread.created_at,thread.updated_at,thread.last_activity_at,
                   (thread.author_user_id=:user) can_edit,
                   COALESCE(state.muted,FALSE) muted
            FROM {TABLE_GHOST_FORUM_THREADS} thread
            LEFT JOIN {TABLE_GHOST_FORUM_THREAD_USER_STATE} state
              ON state.thread_id=thread.id AND state.user_id=:user
            WHERE thread.id=:id AND thread.deleted_at IS NULL""",
        {"id": thread_id, "user": user},
    )
    if thread_frame.empty:
        raise HTTPException(404, "找不到帖文。")
    total = scalar_count(
        db,
        f"SELECT COUNT(*) total FROM {TABLE_GHOST_FORUM_POSTS} WHERE thread_id=:id",
        {"id": thread_id},
    )
    target_post_id = None
    if post is not None:
        target = db.query(
            f"""SELECT id,created_at FROM {TABLE_GHOST_FORUM_POSTS}
                WHERE id=:post AND thread_id=:thread""",
            {"post": post, "thread": thread_id},
        )
        if target.empty:
            raise HTTPException(404, "找不到指定留言。")
        target_row = target.iloc[0]
        position = scalar_count(
            db,
            f"""SELECT COUNT(*) total FROM {TABLE_GHOST_FORUM_POSTS}
                WHERE thread_id=:thread
                  AND (created_at<:created OR (created_at=:created AND id<=:post))""",
            {
                "thread": thread_id,
                "created": target_row["created_at"],
                "post": post,
            },
        )
        page = max(1, (position + PAGE_SIZE - 1) // PAGE_SIZE)
        target_post_id = int(post)
    elif latest:
        page = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page, _, offset = bounds(page)
    posts = db.query(
        f"""SELECT p.id,p.author_user_id,
                   CASE WHEN p.deleted_at IS NULL THEN p.body ELSE '' END body,
                   CASE WHEN p.deleted_at IS NULL THEN p.sticker_id ELSE NULL END sticker_id,
                   p.quoted_post_id,p.is_first_post,p.revision,p.created_at,p.updated_at,
                   p.deleted_at,
                   (p.author_user_id=:user AND p.deleted_at IS NULL
                    AND p.sticker_id IS NULL) can_edit,
                   (p.author_user_id=:user AND p.deleted_at IS NULL
                    AND p.is_first_post=FALSE) can_delete,
                   q.author_user_id quoted_author,
                   CASE WHEN q.deleted_at IS NULL THEN LEFT(q.body,500) ELSE '' END quoted_body,
                   CASE WHEN q.deleted_at IS NULL THEN q.sticker_id ELSE NULL END quoted_sticker_id,
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
    thread_item["links"] = _forum_resource_links(db, [thread_id])[thread_id]
    return {
        "thread": thread_item,
        "posts": payload(_records(posts), page, total),
        "target_post_id": target_post_id,
    }


@router.post("/forum/threads/{thread_id}/read")
def mark_forum_thread_read(thread_id: int, body: ForumReadBody, request: Request):
    user, db = _ghost_context(request)
    now = _now()
    with db.transaction() as connection:
        thread_exists = connection.execute(
            text(
                f"SELECT 1 FROM {TABLE_GHOST_FORUM_THREADS} "
                "WHERE id=:thread AND deleted_at IS NULL"
            ),
            {"thread": thread_id},
        ).scalar()
        if not thread_exists:
            raise HTTPException(404, "找不到帖文。")
        if body.post_id is None:
            post_id = connection.execute(
                text(
                    f"SELECT MAX(id) FROM {TABLE_GHOST_FORUM_POSTS} "
                    "WHERE thread_id=:thread"
                ),
                {"thread": thread_id},
            ).scalar()
        else:
            post_id = connection.execute(
                text(
                    f"SELECT id FROM {TABLE_GHOST_FORUM_POSTS} "
                    "WHERE id=:post AND thread_id=:thread"
                ),
                {"post": body.post_id, "thread": thread_id},
            ).scalar()
        if post_id is None:
            raise HTTPException(404, "找不到指定留言。")
        _ensure_forum_profile(connection, user, now)
        connection.execute(
            text(
                f"""INSERT INTO {TABLE_GHOST_FORUM_THREAD_USER_STATE}
                    (thread_id,user_id,last_read_post_id,muted,updated_at)
                    VALUES(:thread,:user,:post,FALSE,:now)
                    ON CONFLICT (thread_id,user_id) DO UPDATE SET
                        last_read_post_id=CASE
                            WHEN {TABLE_GHOST_FORUM_THREAD_USER_STATE}.last_read_post_id IS NULL
                              OR EXCLUDED.last_read_post_id>{TABLE_GHOST_FORUM_THREAD_USER_STATE}.last_read_post_id
                            THEN EXCLUDED.last_read_post_id
                            ELSE {TABLE_GHOST_FORUM_THREAD_USER_STATE}.last_read_post_id
                        END,
                        updated_at=EXCLUDED.updated_at"""
            ),
            {"thread": thread_id, "user": user, "post": int(post_id), "now": now},
        )
    return {"ok": True, "last_read_post_id": int(post_id)}


@router.patch("/forum/threads/{thread_id}/state")
def update_forum_thread_state(
    thread_id: int, body: ForumThreadStateBody, request: Request,
):
    user, db = _ghost_context(request)
    now = _now()
    with db.transaction() as connection:
        thread_exists = connection.execute(
            text(
                f"SELECT 1 FROM {TABLE_GHOST_FORUM_THREADS} "
                "WHERE id=:thread AND deleted_at IS NULL"
            ),
            {"thread": thread_id},
        ).scalar()
        if not thread_exists:
            raise HTTPException(404, "找不到帖文。")
        _ensure_forum_profile(connection, user, now)
        connection.execute(
            text(
                f"""INSERT INTO {TABLE_GHOST_FORUM_THREAD_USER_STATE}
                    (thread_id,user_id,last_read_post_id,muted,updated_at)
                    VALUES(:thread,:user,NULL,:muted,:now)
                    ON CONFLICT (thread_id,user_id) DO UPDATE SET
                        muted=EXCLUDED.muted,updated_at=EXCLUDED.updated_at"""
            ),
            {"thread": thread_id, "user": user, "muted": body.muted, "now": now},
        )
    return {"ok": True, "muted": body.muted}


@router.post("/forum/notifications/{notification_id}/retry")
def retry_forum_notification(notification_id: int, request: Request):
    user, db = _ghost_context(request)
    result = _dispatch_forum_notification(
        db, notification_id, author_user_id=user,
    )
    if result["state"] == "not_retryable":
        raise HTTPException(409, "通知不存在、不屬於你或目前不可重試。")
    return result


@router.patch("/forum/threads/{thread_id}")
def update_forum_thread(thread_id: int, body: ForumThreadUpdateBody, request: Request):
    user, db = _ghost_context(request)
    values = _validation(
        validate_thread,
        {**body.model_dump(), "body": "unchanged"},
    )
    now = _now()
    with db.transaction() as connection:
        _check_forum_links(
            connection,
            values["video_ids"],
            values["photo_ids"],
            values["history_event_ids"],
        )
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
            raise HTTPException(409, "標題已更新、不存在或不屬於你。")
        _replace_forum_links(
            connection,
            thread_id,
            values["video_ids"],
            values["photo_ids"],
            values["history_event_ids"],
        )
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
        raise HTTPException(409, "帖文已更新、不存在或不屬於你。")
    return {"ok": True}


@router.post("/forum/threads/{thread_id}/posts")
def create_forum_post(thread_id: int, body: ForumPostBody, request: Request):
    user, db = _ghost_context(request)
    post_body, sticker_id = _forum_reply_content(body.body, body.sticker_id)
    now = _now()
    with db.transaction() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('ghost_forum_inventory'))"))
        thread = connection.execute(
            text(f"SELECT title FROM {TABLE_GHOST_FORUM_THREADS} WHERE id=:id AND deleted_at IS NULL FOR UPDATE"),
            {"id": thread_id},
        ).mappings().one_or_none()
        if thread is None:
            raise HTTPException(404, "找不到帖文。")
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
                raise HTTPException(400, "引用留言不屬於此帖文。")
        row = connection.execute(
            text(
                f"""INSERT INTO {TABLE_GHOST_FORUM_POSTS}
                    (thread_id,author_user_id,body,sticker_id,quoted_post_id,created_at,updated_at)
                    VALUES(:thread,:user,:body,:sticker,:quote,:now,:now) RETURNING id,revision"""
            ),
            {
                "thread": thread_id,
                "user": user,
                "body": post_body,
                "sticker": sticker_id,
                "quote": body.quoted_post_id,
                "now": now,
            },
        ).mappings().one()
        connection.execute(
            text(f"UPDATE {TABLE_GHOST_FORUM_THREADS} SET last_activity_at=:now WHERE id=:id"),
            {"now": now, "id": thread_id},
        )
        _ensure_forum_profile(connection, user, now)
        connection.execute(
            text(
                f"""INSERT INTO {TABLE_GHOST_FORUM_THREAD_USER_STATE}
                    (thread_id,user_id,last_read_post_id,muted,updated_at)
                    VALUES(:thread,:user,:post,FALSE,:now)
                    ON CONFLICT (thread_id,user_id) DO UPDATE SET
                        last_read_post_id=EXCLUDED.last_read_post_id,updated_at=EXCLUDED.updated_at"""
            ),
            {
                "thread": thread_id,
                "user": user,
                "post": int(row["id"]),
                "now": now,
            },
        )
        notification_id = _queue_forum_notification(
            connection, int(row["id"]), "reply",
        )
    post_id = int(row["id"])
    notification = _dispatch_forum_notification(db, notification_id)
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
            WHERE id=:id AND author_user_id=:user AND revision=:revision
              AND deleted_at IS NULL AND sticker_id IS NULL""",
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
            SET body='',sticker_id=NULL,deleted_at=:now,revision=revision+1,updated_at=:now
            WHERE id=:id AND author_user_id=:user AND revision=:revision
              AND is_first_post=FALSE AND deleted_at IS NULL""",
        {"now": _now(), "id": post_id, "user": user, "revision": revision},
    )
    if not changed:
        raise HTTPException(409, "首篇須刪除整篇帖文；其餘留言必須屬於你及未被更新。")
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
