"""Media replay, administration and match-photo logic."""

import csv
import datetime as dt
import io
import itertools
import os
import re
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import text

from account_access import (
    NON_MEMBER_ACCOUNT_DB_KEYS,
    is_non_member_account,
    sql_account_id_literals,
)
from core.vote_logic import _resolve_db
from schema import (
    TABLE_ACCOUNTS,
    TABLE_MATCHES,
    TABLE_MATCH_PHOTOS,
    TABLE_MATCH_VIDEOS,
    TABLE_R2_UPLOAD_INTENTS,
    TABLE_VIDEO_CHAPTERS,
    TABLE_VIDEO_COMMENTS,
    TABLE_VIDEO_PROGRESS,
    TABLE_VIDEO_ROSTER,
    TABLE_VIDEO_VIEWS,
    TABLE_VIDEO_VOTES,
)
from system_limits import (
    ACCOUNT_LIST_LIMIT,
    API_PAGE_SIZE,
    PHOTO_BATCH_MAX_ITEMS,
    VIDEO_COMMENT_MAX_PER_USER_DAY, VIDEO_COMMENT_MAX_PER_VIDEO,
    VIDEO_COMMENT_RATE_WINDOW_HOURS,
    VIDEO_IMPORT_MAX_ROWS, VIDEO_OPTION_LIMIT, VIDEO_REPLAY_LIST_LIMIT,
    VIDEO_TOTAL_LIMIT,
)


HKT = ZoneInfo("Asia/Hong_Kong")
SOURCE_EXISTING = "連結現有場次"
SOURCE_STANDALONE = "手動輸入舊比賽"
OTHER_ALBUM = "其他相片"
CHAPTER_LABELS = ["正主", "反主", "正一", "反一", "正二", "反二", "正三", "反三", "攻辯", "台下", "交互", "自由辯論", "反結", "正結"]
INDIVIDUAL_SPEECH_LABELS = ["正主", "反主", "正一", "反一", "正二", "反二", "正三", "反三", "反結", "正結"]
CHAPTER_ORDER = {label: index for index, label in enumerate(CHAPTER_LABELS)}
INDIVIDUAL_SPEECH_ORDER = {label: index for index, label in enumerate(INDIVIDUAL_SPEECH_LABELS)}
VOTE_LABELS = {"pro": "正方勝出", "con": "反方勝出", "undecided": "難以判斷"}
BRACKET_RE = re.compile(r"[（(﹙]\s*([^（(﹙）)﹚]*?)\s*[）)﹚]")
PRESERVE_BEST_DEBATER = object()
_NON_MEMBER_ACCOUNT_SQL = sql_account_id_literals(NON_MEMBER_ACCOUNT_DB_KEYS)


def now_hkt():
    return dt.datetime.now(HKT).replace(tzinfo=None)


def clean_text(value):
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("none", "nan", "nat", "<na>") else text


def format_value(value, default="未設定"):
    return clean_text(value) or default


def safe_int(value, default=0):
    try:
        if value is None or value != value:
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def safe_bool(value):
    if isinstance(value, bool):
        return value
    return clean_text(value).lower() in {"true", "t", "1", "yes"}


def safe_text_list(value):
    if isinstance(value, (list, tuple, set)):
        return [item for item in (clean_text(entry) for entry in value) if item]
    return []


def format_time(value):
    if hasattr(value, "strftime"):
        return value.strftime("%m-%d %H:%M")
    return clean_text(value)[:16]


def json_time(value):
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    return value.isoformat() if hasattr(value, "isoformat") else clean_text(value)


def seconds_to_label(seconds):
    seconds = safe_int(seconds)
    minutes, sec = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour}:{minute:02d}:{sec:02d}" if hour else f"{minute}:{sec:02d}"


def parse_time_to_seconds(value):
    text = clean_text(value)
    if not text:
        return None
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    if not all(part.strip().isdigit() for part in parts):
        return None
    nums = [int(part.strip()) for part in parts]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def youtube_video_id(url):
    parsed = urlparse(clean_text(url))
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/")[0]
    if "youtube.com" in host:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [""])[0]
        match = re.search(r"/(?:embed|shorts)/([^/?#]+)", parsed.path)
        if match:
            return match.group(1)
    return ""


def normalize_youtube_url(url):
    text = clean_text(url)
    return f"https://{text}" if text and "://" not in text else text


def is_youtube_url(url):
    try:
        parsed = urlparse(normalize_youtube_url(url))
    except Exception:
        return False
    host = parsed.netloc.lower()
    return parsed.scheme in ("http", "https") and (
        host == "youtu.be" or host.endswith(".youtu.be") or host == "youtube.com" or host.endswith(".youtube.com")
    )


def _replay_rows(
    user_id,
    db,
    limit=VIDEO_REPLAY_LIST_LIMIT,
    mine_only=False,
    participant_user_ids=None,
):
    participant_user_ids = safe_text_list(participant_user_ids)
    return db.query(
        f"""
        SELECT v.id, v.match_id, COALESCE(NULLIF(v.match_label, ''), v.match_id) AS match_display,
               v.video_title, v.youtube_url, v.display_order, v.created_at, m.match_date, m.match_time,
               COALESCE(NULLIF(m.topic_text, ''), NULLIF(v.standalone_topic_text, '')) AS topic_text,
               COALESCE(NULLIF(m.pro_team, ''), NULLIF(v.standalone_pro_team, '')) AS pro_team,
               COALESCE(NULLIF(m.con_team, ''), NULLIF(v.standalone_con_team, '')) AS con_team,
               COALESCE(view_stats.view_count, 0) AS view_count,
               COALESCE(vote_stats.pro_votes, 0) AS pro_votes,
               COALESCE(vote_stats.con_votes, 0) AS con_votes,
               COALESCE(vote_stats.undecided_votes, 0) AS undecided_votes,
               COALESCE(roster_stats.participated_by_me, FALSE) AS participated_by_me,
               COALESCE(roster_stats.my_roles_text, '') AS my_roles_text,
               roster_stats.roster_user_ids,
               COALESCE(roster_stats.roster_search_text, '') AS roster_search_text,
               progress.watched_seconds, progress.duration_seconds, progress.updated_at AS progress_updated_at
        FROM {TABLE_MATCH_VIDEOS} v
        LEFT JOIN {TABLE_MATCHES} m ON v.match_id = m.match_id
        LEFT JOIN (SELECT video_id, COUNT(*) AS view_count FROM {TABLE_VIDEO_VIEWS} GROUP BY video_id) view_stats ON view_stats.video_id = v.id
        LEFT JOIN (
            SELECT video_id,
                   SUM(CASE WHEN vote_choice = 'pro' THEN 1 ELSE 0 END) AS pro_votes,
                   SUM(CASE WHEN vote_choice = 'con' THEN 1 ELSE 0 END) AS con_votes,
                   SUM(CASE WHEN vote_choice = 'undecided' THEN 1 ELSE 0 END) AS undecided_votes
            FROM {TABLE_VIDEO_VOTES} GROUP BY video_id
        ) vote_stats ON vote_stats.video_id = v.id
        LEFT JOIN (
            SELECT r.video_id,
                   BOOL_OR(r.member_user_id = :user_id) AS participated_by_me,
                   STRING_AGG(r.role_label, ',' ORDER BY
                       CASE r.role_label
                           WHEN '正主' THEN 0 WHEN '反主' THEN 1
                           WHEN '正一' THEN 2 WHEN '反一' THEN 3
                           WHEN '正二' THEN 4 WHEN '反二' THEN 5
                           WHEN '正三' THEN 6 WHEN '反三' THEN 7
                           WHEN '反結' THEN 8 WHEN '正結' THEN 9
                           ELSE 99
                       END
                   ) FILTER (WHERE r.member_user_id = :user_id) AS my_roles_text,
                   ARRAY_AGG(DISTINCT r.member_user_id ORDER BY r.member_user_id) AS roster_user_ids,
                   STRING_AGG(DISTINCT r.member_user_id, ' ' ORDER BY r.member_user_id) AS roster_search_text
            FROM {TABLE_VIDEO_ROSTER} r
            GROUP BY r.video_id
        ) roster_stats ON roster_stats.video_id = v.id
        LEFT JOIN {TABLE_VIDEO_PROGRESS} progress ON progress.video_id = v.id AND progress.user_id = :user_id
        WHERE COALESCE(v.is_visible, TRUE) = TRUE
          AND (:mine_only = FALSE OR EXISTS (
              SELECT 1 FROM {TABLE_VIDEO_ROSTER} mine
              WHERE mine.video_id=v.id AND mine.member_user_id=:user_id
          ))
          AND (:filter_participants = FALSE OR EXISTS (
              SELECT 1 FROM {TABLE_VIDEO_ROSTER} selected_roster
              WHERE selected_roster.video_id=v.id
                AND selected_roster.member_user_id = ANY(
                    CAST(:participant_user_ids AS TEXT[])
                )
          ))
        ORDER BY progress.updated_at DESC NULLS LAST, m.match_date DESC NULLS LAST, m.match_time DESC NULLS LAST,
                 v.display_order ASC, v.created_at DESC
        LIMIT :limit
        """, {
            "user_id": user_id,
            "mine_only": bool(mine_only),
            "filter_participants": bool(participant_user_ids),
            "participant_user_ids": participant_user_ids,
            "limit": max(1, int(limit)),
        }
    )


def _replay_record(row):
    return {
        "id": safe_int(row["id"]), "match_display": format_value(row.get("match_display")),
        "video_title": clean_text(row.get("video_title")), "youtube_url": clean_text(row.get("youtube_url")),
        "youtube_id": youtube_video_id(row.get("youtube_url")), "match_date": clean_text(row.get("match_date")),
        "match_time": clean_text(row.get("match_time")), "topic_text": format_value(row.get("topic_text")),
        "pro_team": format_value(row.get("pro_team")), "con_team": format_value(row.get("con_team")),
        "view_count": safe_int(row.get("view_count")), "pro_votes": safe_int(row.get("pro_votes")),
        "con_votes": safe_int(row.get("con_votes")), "undecided_votes": safe_int(row.get("undecided_votes")),
        "participated_by_me": safe_bool(row.get("participated_by_me")),
        "my_roles": [role for role in clean_text(row.get("my_roles_text")).split(",") if role],
        "roster_user_ids": safe_text_list(row.get("roster_user_ids")),
        "roster_search_text": clean_text(row.get("roster_search_text")),
        "watched_seconds": safe_int(row.get("watched_seconds")), "duration_seconds": safe_int(row.get("duration_seconds")),
        "progress_updated_at": json_time(row.get("progress_updated_at")),
    }


def replay_data(
    user_id,
    selected_video_id=None,
    mine_only=False,
    participant_user_ids=None,
    db=None,
):
    db = _resolve_db(db)
    member_accounts = member_account_options(db)
    valid_members = set(member_accounts)
    participant_user_ids = list(
        dict.fromkeys(
            member
            for member in safe_text_list(participant_user_ids)
            if member in valid_members and not is_non_member_account(member)
        )
    )
    videos = [
        _replay_record(row)
        for _, row in _replay_rows(
            user_id,
            db,
            mine_only=mine_only,
            participant_user_ids=participant_user_ids,
        ).iterrows()
    ]
    if not videos:
        return {
            "videos": [], "selected": None, "chapters": [], "roster": [],
            "comments": [],
            "my_vote": None, "vote_labels": VOTE_LABELS,
            "chapter_labels": CHAPTER_LABELS,
            "individual_speech_labels": INDIVIDUAL_SPEECH_LABELS,
            "member_accounts": member_accounts,
            "best_debater_role": None,
            "best_debater_roles": [],
        }
    ids = {video["id"] for video in videos}
    selected_id = safe_int(selected_video_id, videos[0]["id"])
    if selected_id not in ids:
        selected_id = videos[0]["id"]
    selected = next(video for video in videos if video["id"] == selected_id)
    chapters = db.query(
        f"""SELECT c.chapter_label, c.start_seconds, c.display_order,
                   COALESCE(c.is_best_debater, FALSE) AS is_best_debater,
                   r.member_user_id AS speaker_user_id
            FROM {TABLE_VIDEO_CHAPTERS} c
            LEFT JOIN {TABLE_VIDEO_ROSTER} r
              ON r.video_id=c.video_id AND r.role_label=c.chapter_label
            WHERE c.video_id=:video_id
            ORDER BY c.display_order ASC, c.start_seconds ASC""",
        {"video_id": selected_id},
    )
    chapter_items = []
    for _, row in chapters.iterrows():
        speaker_user_id = clean_text(row.get("speaker_user_id"))
        chapter_items.append(
            {
                "chapter_label": clean_text(row["chapter_label"]),
                "start_seconds": safe_int(row["start_seconds"]),
                "label": seconds_to_label(row["start_seconds"]),
                "display_order": safe_int(row.get("display_order")),
                "is_best_debater": safe_bool(row.get("is_best_debater")),
                "speaker_user_id": (
                    speaker_user_id
                    if speaker_user_id
                    and not is_non_member_account(speaker_user_id)
                    else None
                ),
            }
        )
    chapter_items.sort(key=lambda row: (CHAPTER_ORDER.get(row["chapter_label"], row["display_order"] + len(CHAPTER_LABELS)), row["start_seconds"]))
    roster_rows = db.query(
        f"""SELECT role_label, member_user_id FROM {TABLE_VIDEO_ROSTER}
            WHERE video_id=:video_id""",
        {"video_id": selected_id},
    )
    roster_items = []
    for _, row in roster_rows.iterrows():
        member_user_id = clean_text(row.get("member_user_id"))
        if not member_user_id or is_non_member_account(member_user_id):
            continue
        roster_items.append(
            {
                "role_label": clean_text(row.get("role_label")),
                "user_id": member_user_id,
            }
        )
    roster_items.sort(
        key=lambda row: INDIVIDUAL_SPEECH_ORDER.get(
            row["role_label"], len(INDIVIDUAL_SPEECH_LABELS)
        )
    )
    best_debater_roles = [
        row["chapter_label"] for row in chapter_items if row["is_best_debater"]
    ]
    comment_items = []
    vote = db.query(f"SELECT vote_choice FROM {TABLE_VIDEO_VOTES} WHERE video_id = :video_id AND user_id = :user_id", {"video_id": selected_id, "user_id": user_id})
    my_vote = clean_text(vote.iloc[0]["vote_choice"]) if not vote.empty else None
    return {
        "videos": videos, "selected": selected, "chapters": chapter_items,
        "roster": roster_items,
        "comments": comment_items, "my_vote": my_vote,
        "vote_labels": VOTE_LABELS, "chapter_labels": CHAPTER_LABELS,
        "individual_speech_labels": INDIVIDUAL_SPEECH_LABELS,
        "member_accounts": member_accounts,
        # Keep the singular field temporarily for older replay clients.
        "best_debater_role": best_debater_roles[0] if best_debater_roles else None,
        "best_debater_roles": best_debater_roles,
    }


def save_vote(video_id, user_id, vote_choice, db=None):
    if vote_choice not in VOTE_LABELS:
        return {"ok": False, "message": "請選擇有效的投票選項。"}
    db = _resolve_db(db)
    db.execute(
        f"""INSERT INTO {TABLE_VIDEO_VOTES} (video_id, user_id, vote_choice, updated_at)
        VALUES (:video_id, :user_id, :vote_choice, :updated_at)
        ON CONFLICT (video_id, user_id) DO UPDATE SET vote_choice = EXCLUDED.vote_choice, updated_at = EXCLUDED.updated_at""",
        {"video_id": int(video_id), "user_id": user_id, "vote_choice": vote_choice, "updated_at": now_hkt()},
    )
    return {"ok": True, "message": "已成功提交投票"}


def add_comment(video_id, user_id, comment_text, db=None):
    comment_text = clean_text(comment_text)
    if not comment_text:
        return {"ok": False, "message": "請輸入留言內容。"}
    db = _resolve_db(db)
    now = now_hkt()
    params = {
        "video_id": int(video_id), "user_id": user_id,
        "comment_text": comment_text, "created_at": now,
        "user_cutoff": now - dt.timedelta(hours=VIDEO_COMMENT_RATE_WINDOW_HOURS),
    }
    # One short transaction makes both quotas authoritative under concurrent
    # requests. The per-user limit is intentionally global across all videos;
    # scoping it to one video allowed the nominal daily cap to multiply by the
    # full video inventory.
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('video_comment_quota'))"))
        counts = conn.execute(text(f"""SELECT
            (SELECT COUNT(*) FROM {TABLE_VIDEO_COMMENTS}
              WHERE video_id=:video_id) AS video_count,
            (SELECT COUNT(*) FROM {TABLE_VIDEO_COMMENTS}
              WHERE user_id=:user_id AND created_at>=:user_cutoff) AS user_day_count
        """), params).mappings().first()
        if counts and int(counts["video_count"] or 0) >= VIDEO_COMMENT_MAX_PER_VIDEO:
            return {"ok": False, "message": "此片段留言已達保護上限。"}
        if counts and int(counts["user_day_count"] or 0) >= VIDEO_COMMENT_MAX_PER_USER_DAY:
            return {"ok": False, "message": "你今日的片段留言次數已達上限。"}
        conn.execute(text(
            f"INSERT INTO {TABLE_VIDEO_COMMENTS} (video_id, user_id, comment_text, created_at) "
            "VALUES (:video_id, :user_id, :comment_text, :created_at)"
        ), params)
    return {"ok": True, "message": "已成功留言"}


def save_chapters(video_id, chapters, best_debater_role=PRESERVE_BEST_DEBATER, db=None):
    invalid = []
    values = []
    for index, label in enumerate(CHAPTER_LABELS):
        entry = next((item for item in chapters if item.get("chapter_label") == label), None)
        if not entry or not entry.get("enabled"):
            continue
        seconds = parse_time_to_seconds(entry.get("time_text"))
        if seconds is None:
            invalid.append(label)
        else:
            values.append((label, index, seconds))
    if invalid:
        return {"ok": False, "message": "以下章節時間格式無效：" + "、".join(invalid)}
    db = _resolve_db(db)
    has_chapter_markers = any("is_best_debater" in item for item in chapters)
    preserving_best = (
        not has_chapter_markers
        and best_debater_role is PRESERVE_BEST_DEBATER
    )
    if has_chapter_markers:
        best_roles = {
            clean_text(item.get("chapter_label"))
            for item in chapters
            if safe_bool(item.get("is_best_debater"))
        }
    elif preserving_best:
        existing = db.query(
            f"""SELECT chapter_label FROM {TABLE_VIDEO_CHAPTERS}
                WHERE video_id=:video_id AND is_best_debater=TRUE""",
            {"video_id": int(video_id)},
        )
        best_roles = {
            clean_text(value)
            for value in existing.get("chapter_label", [])
            if clean_text(value)
        }
    else:
        legacy_best_role = clean_text(best_debater_role)
        best_roles = {legacy_best_role} if legacy_best_role else set()
    if best_roles - set(INDIVIDUAL_SPEECH_LABELS):
        return {"ok": False, "message": "最佳辯論員必須選擇個人發言辯位。"}
    enabled_labels = {label for label, _index, _seconds in values}
    disabled_best_roles = best_roles - enabled_labels
    if disabled_best_roles:
        if preserving_best:
            best_roles -= disabled_best_roles
        else:
            return {"ok": False, "message": "最佳辯論員必須同時啟用該辯位的章節時間。"}
    ordered_best_roles = [
        label for label in INDIVIDUAL_SPEECH_LABELS if label in best_roles
    ]
    params = [
        {"video_id": int(video_id), "chapter_label": label,
         "start_seconds": seconds, "display_order": index,
         "is_best_debater": label in best_roles, "updated_at": now_hkt()}
        for label, index, seconds in values
    ]
    with db.transaction() as conn:
        conn.execute(text(f"DELETE FROM {TABLE_VIDEO_CHAPTERS} WHERE video_id=:video_id"),
                     {"video_id": int(video_id)})
        if params:
            conn.execute(text(f"""INSERT INTO {TABLE_VIDEO_CHAPTERS}
                (video_id,chapter_label,start_seconds,display_order,is_best_debater,updated_at)
                VALUES(:video_id,:chapter_label,:start_seconds,:display_order,:is_best_debater,:updated_at)"""),
                params)
    return {
        "ok": True,
        "message": "章節時間表已更新。",
        # Keep the singular field temporarily for older replay clients.
        "best_debater_role": ordered_best_roles[0] if ordered_best_roles else None,
        "best_debater_roles": ordered_best_roles,
    }


def _match_rows(db):
    return db.query(f"SELECT match_id, match_date, match_time, topic_text, pro_team, con_team FROM {TABLE_MATCHES} ORDER BY match_date DESC NULLS LAST, match_time DESC NULLS LAST, match_id LIMIT :limit",
                    {"limit": VIDEO_OPTION_LIMIT})


def match_options(db=None):
    db = _resolve_db(db)
    rows = _match_rows(db)
    options = []
    for _, row in rows.iterrows():
        match_id = clean_text(row["match_id"])
        teams = " vs ".join(item for item in (clean_text(row.get("pro_team")), clean_text(row.get("con_team"))) if item)
        details = "｜".join(item for item in (teams, clean_text(row.get("topic_text"))) if item)
        options.append({"match_id": match_id, "label": f"{match_id} - {details}" if details else match_id})
    return options


def member_account_options(db=None):
    db = _resolve_db(db)
    rows = db.query(
        f"""SELECT user_id FROM {TABLE_ACCOUNTS}
            WHERE LOWER(user_id) NOT IN ({_NON_MEMBER_ACCOUNT_SQL})
              AND COALESCE(account_disabled, FALSE)=FALSE
            ORDER BY user_id LIMIT :limit""",
        {"limit": ACCOUNT_LIST_LIMIT},
    )
    return [
        value
        for value in (clean_text(item) for item in rows.get("user_id", []))
        if value
    ]


def validate_video_input(video_title, youtube_url, video_source, match_label=""):
    errors = []
    if not clean_text(video_title):
        errors.append("請輸入片段標題。")
    if not clean_text(youtube_url):
        errors.append("請輸入 YouTube 連結。")
    elif not is_youtube_url(youtube_url):
        errors.append("請輸入有效的 YouTube 連結。")
    if video_source == SOURCE_STANDALONE and not clean_text(match_label):
        errors.append("請輸入舊比賽名稱或場次。")
    return errors


def _match_params(data):
    if data.get("video_source") == SOURCE_EXISTING:
        return {"match_id": clean_text(data.get("match_id")) or None, "match_label": None, "standalone_topic_text": None, "standalone_pro_team": None, "standalone_con_team": None}
    return {"match_id": None, "match_label": clean_text(data.get("match_label")) or None, "standalone_topic_text": clean_text(data.get("standalone_topic_text")) or None, "standalone_pro_team": clean_text(data.get("standalone_pro_team")) or None, "standalone_con_team": clean_text(data.get("standalone_con_team")) or None}


def _video_insert_params(data):
    return {**_match_params(data), "video_title": clean_text(data.get("video_title")), "youtube_url": normalize_youtube_url(data.get("youtube_url")), "is_visible": bool(data.get("is_visible", True)), "display_order": safe_int(data.get("display_order")), "created_at": now_hkt(), "updated_at": now_hkt()}


def add_video(data, db=None):
    errors = validate_video_input(data.get("video_title"), data.get("youtube_url"), data.get("video_source"), data.get("match_label"))
    if errors:
        return {"ok": False, "errors": errors}
    db = _resolve_db(db)
    count = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_MATCH_VIDEOS}")
    if not count.empty and int(count.iloc[0]["n"] or 0) >= VIDEO_TOTAL_LIMIT:
        return {"ok": False, "message": f"片段總數已達 {VIDEO_TOTAL_LIMIT} 項保護上限，請先封存舊資料。"}
    db.execute(
        f"""INSERT INTO {TABLE_MATCH_VIDEOS} (match_id, match_label, video_title, youtube_url, standalone_topic_text, standalone_pro_team, standalone_con_team, is_visible, display_order, created_at, updated_at)
        VALUES (:match_id, :match_label, :video_title, :youtube_url, :standalone_topic_text, :standalone_pro_team, :standalone_con_team, :is_visible, :display_order, :created_at, :updated_at)""",
        _video_insert_params(data),
    )
    return {"ok": True, "message": "比賽片段已新增。"}


def _admin_video_rows(db, limit=20, offset=0):
    return db.query(
        f"""SELECT v.id, v.match_id, v.match_label, v.video_title, v.youtube_url, v.standalone_topic_text, v.standalone_pro_team, v.standalone_con_team, COALESCE(v.is_visible, TRUE) AS is_visible, v.display_order, v.created_at, v.updated_at,
        COALESCE(NULLIF(v.match_label, ''), v.match_id) AS match_display,
        COALESCE(NULLIF(m.topic_text, ''), NULLIF(v.standalone_topic_text, '')) AS topic_text,
        COALESCE(NULLIF(m.pro_team, ''), NULLIF(v.standalone_pro_team, '')) AS pro_team,
        COALESCE(NULLIF(m.con_team, ''), NULLIF(v.standalone_con_team, '')) AS con_team, m.match_date, m.match_time
        FROM {TABLE_MATCH_VIDEOS} v LEFT JOIN {TABLE_MATCHES} m ON v.match_id = m.match_id
        ORDER BY m.match_date DESC NULLS LAST, m.match_time DESC NULLS LAST, v.display_order ASC, v.created_at DESC
        LIMIT :limit OFFSET :offset""", {"limit": max(1, int(limit)), "offset": max(0, int(offset))}
    )


def _admin_video_record(row):
    match_id = clean_text(row.get("match_id"))
    return {"id": safe_int(row["id"]), "match_id": match_id or None, "match_label": clean_text(row.get("match_label")), "video_title": clean_text(row.get("video_title")), "youtube_url": clean_text(row.get("youtube_url")), "standalone_topic_text": clean_text(row.get("standalone_topic_text")), "standalone_pro_team": clean_text(row.get("standalone_pro_team")), "standalone_con_team": clean_text(row.get("standalone_con_team")), "is_visible": bool(row.get("is_visible")), "display_order": safe_int(row.get("display_order")), "match_display": format_value(row.get("match_display")), "topic_text": clean_text(row.get("topic_text")), "pro_team": clean_text(row.get("pro_team")), "con_team": clean_text(row.get("con_team")), "source_type": "現有場次" if match_id else "舊比賽", "created_at": json_time(row.get("created_at")), "updated_at": json_time(row.get("updated_at"))}


def video_admin_data(selected_video_id=None, page=1, page_size=API_PAGE_SIZE, db=None):
    db = _resolve_db(db)
    page_size = max(1, min(int(page_size or API_PAGE_SIZE), API_PAGE_SIZE))
    count = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_MATCH_VIDEOS}")
    total = int(count.iloc[0]["n"] or 0) if not count.empty else 0
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), total_pages))
    start = (page - 1) * page_size
    videos = [_admin_video_record(row) for _, row in _admin_video_rows(db, page_size, start).iterrows()]
    selected_id = safe_int(selected_video_id, videos[0]["id"] if videos else 0)
    if videos and selected_id not in {video["id"] for video in videos}:
        selected_id = videos[0]["id"]
    chapters = []
    roster = []
    best_debater_roles = []
    if selected_id:
        rows = db.query(
            f"""SELECT c.chapter_label, c.start_seconds,
                       COALESCE(c.is_best_debater, FALSE) AS is_best_debater,
                       r.member_user_id AS speaker_user_id
                FROM {TABLE_VIDEO_CHAPTERS} c
                LEFT JOIN {TABLE_VIDEO_ROSTER} r
                  ON r.video_id=c.video_id AND r.role_label=c.chapter_label
                WHERE c.video_id=:video_id""",
            {"video_id": selected_id},
        )
        chapters = [
            {
                "chapter_label": clean_text(row["chapter_label"]),
                "start_seconds": safe_int(row["start_seconds"]),
                "label": seconds_to_label(row["start_seconds"]),
                "is_best_debater": safe_bool(row.get("is_best_debater")),
                "speaker_user_id": clean_text(row.get("speaker_user_id")) or None,
            }
            for _, row in rows.iterrows()
        ]
        chapters.sort(key=lambda row: CHAPTER_ORDER.get(row["chapter_label"], len(CHAPTER_LABELS)))
        best_debater_roles = [
            row["chapter_label"] for row in chapters if row["is_best_debater"]
        ]
        roster_rows = db.query(
            f"""SELECT role_label, member_user_id FROM {TABLE_VIDEO_ROSTER}
                WHERE video_id=:video_id""",
            {"video_id": selected_id},
        )
        roster = [
            {
                "role_label": clean_text(row["role_label"]),
                "user_id": clean_text(row["member_user_id"]),
            }
            for _, row in roster_rows.iterrows()
        ]
        roster.sort(key=lambda row: INDIVIDUAL_SPEECH_ORDER.get(row["role_label"], len(INDIVIDUAL_SPEECH_LABELS)))
    return {
        "matches": match_options(db), "videos": videos,
        "selected_video_id": selected_id or None, "chapters": chapters,
        "chapter_labels": CHAPTER_LABELS,
        "individual_speech_labels": INDIVIDUAL_SPEECH_LABELS,
        # Keep the singular field temporarily for older admin clients.
        "best_debater_role": best_debater_roles[0] if best_debater_roles else None,
        "best_debater_roles": best_debater_roles,
        "member_accounts": member_account_options(db), "roster": roster,
        "source_existing": SOURCE_EXISTING,
        "source_standalone": SOURCE_STANDALONE,
        "pagination": {
            "page": page, "page_size": page_size, "total": total,
            "total_pages": total_pages,
        },
    }


def save_video_roster(video_id, roster, db=None):
    db = _resolve_db(db)
    parsed = []
    seen_roles = set()
    for item in roster:
        role = clean_text(item.get("role_label"))
        member = clean_text(item.get("user_id"))
        if role not in INDIVIDUAL_SPEECH_LABELS:
            return {"ok": False, "message": "出賽陣容包含無效辯位。"}
        if role in seen_roles:
            return {"ok": False, "message": f"出賽陣容重複設定辯位：{role}"}
        seen_roles.add(role)
        if member:
            parsed.append({"role_label": role, "user_id": member})

    video = db.query(
        f"SELECT 1 AS found FROM {TABLE_MATCH_VIDEOS} WHERE id=:video_id LIMIT 1",
        {"video_id": int(video_id)},
    )
    if video.empty:
        return {"ok": False, "message": "找不到指定的比賽片段。"}

    existing_rows = db.query(
        f"""SELECT role_label, member_user_id FROM {TABLE_VIDEO_ROSTER}
            WHERE video_id=:video_id""",
        {"video_id": int(video_id)},
    )
    existing_by_role = {
        clean_text(row["role_label"]): clean_text(row["member_user_id"])
        for _, row in existing_rows.iterrows()
    }
    valid_members = set(member_account_options(db))
    invalid_members = sorted({
        row["user_id"]
        for row in parsed
        if (
            is_non_member_account(row["user_id"])
            or (
                row["user_id"] not in valid_members
                and existing_by_role.get(row["role_label"]) != row["user_id"]
            )
        )
    })
    if invalid_members:
        return {
            "ok": False,
            "message": "以下委員帳戶不存在、已停用或不是一般委員：" + "、".join(invalid_members),
        }

    params = [
        {
            "video_id": int(video_id), "role_label": row["role_label"],
            "member_user_id": row["user_id"], "updated_at": now_hkt(),
        }
        for row in parsed
    ]
    with db.transaction() as conn:
        conn.execute(
            text(f"DELETE FROM {TABLE_VIDEO_ROSTER} WHERE video_id=:video_id"),
            {"video_id": int(video_id)},
        )
        if params:
            conn.execute(
                text(f"""INSERT INTO {TABLE_VIDEO_ROSTER}
                    (video_id,role_label,member_user_id,updated_at)
                    VALUES(:video_id,:role_label,:member_user_id,:updated_at)"""),
                params,
            )
    return {"ok": True, "message": "出賽陣容已更新。", "roster": parsed}


def update_video(video_id, data, db=None):
    errors = validate_video_input(data.get("video_title"), data.get("youtube_url"), data.get("video_source"), data.get("match_label"))
    if errors:
        return {"ok": False, "errors": errors}
    params = _video_insert_params(data)
    params.update({"id": int(video_id), "updated_at": now_hkt()})
    db = _resolve_db(db)
    db.execute(
        f"""UPDATE {TABLE_MATCH_VIDEOS} SET match_id = :match_id, match_label = :match_label, video_title = :video_title, youtube_url = :youtube_url, standalone_topic_text = :standalone_topic_text, standalone_pro_team = :standalone_pro_team, standalone_con_team = :standalone_con_team, is_visible = :is_visible, display_order = :display_order, updated_at = :updated_at WHERE id = :id""",
        params,
    )
    return {"ok": True, "message": "片段資料已更新。"}


def delete_video(video_id, db=None):
    _resolve_db(db).execute(f"DELETE FROM {TABLE_MATCH_VIDEOS} WHERE id = :id", {"id": int(video_id)})
    return {"ok": True, "message": "片段已刪除。"}


def _side_of(token):
    token = clean_text(token)
    return "pro" if token and (token[0] == "正" or token == "甲") else "con" if token and (token[0] == "反" or token == "乙") else ""


def parse_studio_title(raw_title):
    title = clean_text(raw_title)
    groups = list(BRACKET_RE.finditer(title))
    if not groups:
        return "", title, ""
    first, last = groups[0], groups[-1]
    spans, side = [first.span()], ""
    if last is not first:
        side = _side_of(last.group(1))
        if side:
            spans.append(last.span())
    kept, cursor = [], 0
    for start, end in sorted(spans):
        kept.append(title[cursor:start]); cursor = end
    kept.append(title[cursor:])
    return first.group(1).strip(), re.sub(r"\s+", " ", "".join(kept)).strip(" 　｜|:：-—・"), side


def _row_value(row, *keys):
    lower = {str(key).strip().lower(): value for key, value in row.items()}
    for key in keys:
        if key in row and row.get(key) is not None:
            return row.get(key)
        if str(key).strip().lower() in lower:
            return lower[str(key).strip().lower()]
    return ""


def parse_import_csv(raw_text, max_rows=None):
    if not clean_text(raw_text):
        return []
    reader = csv.DictReader(io.StringIO(raw_text.lstrip("\ufeff")))
    return list(reader if max_rows is None else itertools.islice(reader, max_rows + 1))


def parse_bool(value, default=True):
    text = clean_text(value).lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "y", "顯示", "公開", "是"):
        return True
    if text in ("0", "false", "no", "n", "隱藏", "否"):
        return False
    return default


def import_videos(raw_text, parse_from_title=True, db=None):
    rows = parse_import_csv(raw_text, VIDEO_IMPORT_MAX_ROWS)
    if not rows:
        return {"ok": False, "message": "請先上載或貼上 CSV。"}
    if len(rows) > VIDEO_IMPORT_MAX_ROWS:
        return {"ok": False, "message": f"每次最多匯入 {VIDEO_IMPORT_MAX_ROWS} 行，請分批處理。"}
    db = _resolve_db(db)
    total_frame = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_MATCH_VIDEOS}")
    total_videos = int(total_frame.iloc[0]["n"] or 0) if not total_frame.empty else 0
    if total_videos >= VIDEO_TOTAL_LIMIT:
        return {"ok": False, "message": f"片段總數已達 {VIDEO_TOTAL_LIMIT} 項保護上限，請先封存舊資料。"}
    rows = rows[:max(0, VIDEO_TOTAL_LIMIT - total_videos)]
    existing = db.query(f"SELECT youtube_url FROM {TABLE_MATCH_VIDEOS} LIMIT :limit",
                        {"limit": VIDEO_TOTAL_LIMIT})
    existing_urls = {clean_text(url) for url in existing["youtube_url"].tolist()} if not existing.empty else set()
    inserted = skipped = duplicated = 0
    for row in rows:
        title = clean_text(_row_value(row, "video_title", "title", "Video title", "影片標題"))
        url = clean_text(_row_value(row, "youtube_url", "url", "Video URL", "YouTube 連結"))
        external_id = clean_text(_row_value(row, "video_id", "Video ID", "影片 ID", "Content", "content"))
        if external_id.lower() == "total":
            continue
        if not url and external_id:
            url = f"https://www.youtube.com/watch?v={external_id}"
        match_label = clean_text(_row_value(row, "match_label", "場次", "比賽名稱"))
        topic_text = clean_text(_row_value(row, "topic_text", "辯題"))
        pro_team = clean_text(_row_value(row, "pro_team", "正方"))
        con_team = clean_text(_row_value(row, "con_team", "反方"))
        if parse_from_title and not match_label and not topic_text and title:
            match_label, topic_text, _ = parse_studio_title(title)
        if not title or not url or not is_youtube_url(url):
            skipped += 1; continue
        normalized = normalize_youtube_url(url)
        if normalized in existing_urls:
            duplicated += 1; continue
        existing_urls.add(normalized)
        now = now_hkt()
        db.execute(
            f"""INSERT INTO {TABLE_MATCH_VIDEOS} (match_id, match_label, video_title, youtube_url, standalone_topic_text, standalone_pro_team, standalone_con_team, is_visible, display_order, created_at, updated_at)
            VALUES (NULL, :match_label, :video_title, :youtube_url, :standalone_topic_text, :standalone_pro_team, :standalone_con_team, :is_visible, :display_order, :created_at, :updated_at)""",
            {"match_label": match_label or None, "video_title": title, "youtube_url": normalized, "standalone_topic_text": topic_text or None, "standalone_pro_team": pro_team or None, "standalone_con_team": con_team or None, "is_visible": parse_bool(_row_value(row, "is_visible", "顯示"), True), "display_order": safe_int(_row_value(row, "display_order", "排序")), "created_at": now, "updated_at": now},
        )
        inserted += 1
    message = f"已匯入 {inserted} 條片段，略過格式無效 {skipped} 條，略過重複 {duplicated} 條。"
    return {"ok": True, "type": "success" if inserted else "warning", "message": message}


def album_options(db=None):
    db = _resolve_db(db)
    rows = db.query(
        f"""SELECT DISTINCT ON (album_label) id AS match_video_id, album_label, match_date, match_time
        FROM (SELECT v.id, COALESCE(NULLIF(v.match_label, ''), NULLIF(v.match_id, ''), v.video_title) AS album_label,
                     m.match_date, m.match_time, v.display_order, v.created_at
              FROM {TABLE_MATCH_VIDEOS} v LEFT JOIN {TABLE_MATCHES} m ON v.match_id = m.match_id
              WHERE COALESCE(v.is_visible, TRUE) = TRUE) albums
        WHERE album_label IS NOT NULL AND album_label != ''
        ORDER BY album_label, match_date DESC NULLS LAST, match_time DESC NULLS LAST, display_order ASC, created_at DESC
        LIMIT :limit""", {"limit": VIDEO_OPTION_LIMIT}
    )
    options = [{"label": OTHER_ALBUM, "video_id": None}]
    for _, row in rows.iterrows():
        label = clean_text(row["album_label"])
        if label and label != OTHER_ALBUM:
            options.append({"label": label, "video_id": safe_int(row["match_video_id"])})
    return options


def photo_data(db=None):
    db = _resolve_db(db)
    options = album_options(db)
    return {"albums": options, "photos": [], "other_album": OTHER_ALBUM}


def update_photo_metadata(user_id, photo_id, album_label, match_video_id,
                          photo_date, photo_title, caption, db=None):
    """Update one uploader-owned photo without touching its stored media."""
    db = _resolve_db(db)
    clean_album = clean_text(album_label)
    clean_title = clean_text(photo_title)
    clean_caption = clean_text(caption)
    if not clean_album or len(clean_album) > 200:
        raise ValueError("所屬場次無效。")
    if len(clean_title) > 300:
        raise ValueError("圖片標題不可超過300字。")
    if len(clean_caption) > 2000:
        raise ValueError("圖片說明不可超過2000字。")

    if match_video_id in (None, ""):
        clean_video_id = None
    else:
        try:
            clean_video_id = int(match_video_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("所屬場次無效。") from exc
        if clean_video_id <= 0:
            raise ValueError("所屬場次無效。")

    current = db.query(
        f"""SELECT album_label,match_video_id FROM {TABLE_MATCH_PHOTOS}
        WHERE id=:id AND uploaded_by=:uploaded_by LIMIT 1""",
        {"id": int(photo_id), "uploaded_by": str(user_id)},
    )
    if current.empty:
        return False
    old_album = clean_text(current.iloc[0].get("album_label"))
    old_video_id = safe_int(current.iloc[0].get("match_video_id")) or None
    requested_pair = (clean_album, clean_video_id)
    old_pair = (old_album, old_video_id)
    if requested_pair != old_pair:
        allowed_pairs = {
            (option["label"], option["video_id"])
            for option in album_options(db)
        }
        if requested_pair not in allowed_pairs:
            raise ValueError("所屬場次與比賽片段不相符，請重新選擇。")

    clean_date = clean_text(photo_date)
    parsed_date = None
    if clean_date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", clean_date):
            raise ValueError("相片日期格式無效。")
        try:
            parsed_date = dt.date.fromisoformat(clean_date)
        except ValueError as exc:
            raise ValueError("相片日期格式無效。") from exc

    changed = db.execute_count(
        f"""UPDATE {TABLE_MATCH_PHOTOS}
        SET match_video_id=:match_video_id,album_label=:album_label,
            photo_date=:photo_date,photo_title=:photo_title,caption=:caption
        WHERE id=:id AND uploaded_by=:uploaded_by
          AND album_label=:old_album_label
          AND match_video_id IS NOT DISTINCT FROM :old_match_video_id""",
        {
            "id": int(photo_id),
            "uploaded_by": str(user_id),
            "old_album_label": old_album,
            "old_match_video_id": old_video_id,
            "match_video_id": clean_video_id,
            "album_label": clean_album,
            "photo_date": parsed_date,
            "photo_title": clean_title or None,
            "caption": clean_caption or None,
        },
    )
    return bool(changed)


def register_r2_photos(user_id, album_label, match_video_id, photo_date,
                       photo_title, caption, files, db=None):
    """Persist metadata after direct-to-R2 uploads have been verified."""
    if not files or len(files) > PHOTO_BATCH_MAX_ITEMS:
        return {"ok": False, "message": f"每次必須上載一至{PHOTO_BATCH_MAX_ITEMS}張圖片。"}
    db = _resolve_db(db)
    parsed_date = None
    if clean_text(photo_date):
        try:
            parsed_date = dt.date.fromisoformat(clean_text(photo_date))
        except ValueError:
            return {"ok": False, "message": "相片日期格式無效。"}
    params = []
    for item in files:
        params.append(
            {
                "match_video_id": int(match_video_id) if match_video_id not in (None, "") else None,
                "album_label": clean_text(album_label), "photo_date": parsed_date,
                "photo_title": clean_text(photo_title)[:300] or None,
                "caption": clean_text(caption)[:2000] or None,
                "file_name": clean_text(item["file_name"])[:240],
                "mime_type": clean_text(item.get("mime_type"))[:80] or "image/webp",
                "r2_key": item["r2_key"], "thumbnail_r2_key": item["thumbnail_r2_key"],
                "byte_size": int(item.get("byte_size") or 0),
                "sha256": clean_text(item.get("sha256"))[:64],
                "width": int(item.get("width") or 0) or None,
                "height": int(item.get("height") or 0) or None,
                "uploaded_by": user_id, "created_at": now_hkt(),
            }
        )
    with db.transaction() as conn:
        intent_ids = [str(item.get("intent_id") or "") for item in files]
        claimed = conn.execute(text(f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
            SET status='completed',completed_at=:now
            WHERE intent_id=ANY(:ids) AND status='issued'"""), {
            "ids": intent_ids, "now": now_hkt(),
        }).rowcount
        if claimed != len(intent_ids):
            raise ValueError("一個或多個R2 upload intents已使用或失效")
        conn.execute(text(
            f"""INSERT INTO {TABLE_MATCH_PHOTOS}
                (match_video_id,album_label,photo_date,photo_title,caption,file_name,
                 mime_type,r2_key,thumbnail_r2_key,byte_size,sha256,
                 width,height,uploaded_by,created_at)
                VALUES(:match_video_id,:album_label,:photo_date,:photo_title,:caption,
                 :file_name,:mime_type,:r2_key,:thumbnail_r2_key,:byte_size,
                 :sha256,:width,:height,:uploaded_by,:created_at)"""), params)
    return {"ok": True, "message": "圖片已成功上載。"}


def photo_media(photo_id, db=None):
    """Return lightweight R2 metadata without fetching object bytes."""
    db = _resolve_db(db)
    rows = db.query(
        f"SELECT file_name,mime_type,r2_key,thumbnail_r2_key FROM {TABLE_MATCH_PHOTOS} WHERE id=:id",
        {"id": int(photo_id)},
    )
    if rows.empty:
        return None
    row = rows.iloc[0]
    return {
        "file_name": clean_text(row.get("file_name")) or f"match-photo-{int(photo_id)}.jpg",
        "mime_type": clean_text(row.get("mime_type")) or "image/webp",
        # Both the browser upload path and the legacy migration tool encode
        # thumbnails as WebP even when an old original was JPEG or PNG.
        "thumbnail_mime_type": "image/webp",
        "r2_key": clean_text(row.get("r2_key")),
        "thumbnail_r2_key": clean_text(row.get("thumbnail_r2_key")),
    }
