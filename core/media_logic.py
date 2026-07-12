"""Streamlit-free media replay, administration, and match-photo logic."""

import csv
import datetime as dt
import io
import re
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pandas as pd

from core.vote_logic import _resolve_db
from schema import (
    CREATE_MATCH_PHOTOS,
    CREATE_MATCH_VIDEOS,
    CREATE_VIDEO_CHAPTERS,
    CREATE_VIDEO_COMMENTS,
    CREATE_VIDEO_PROGRESS,
    CREATE_VIDEO_VIEWS,
    CREATE_VIDEO_VOTES,
    TABLE_MATCHES,
    TABLE_MATCH_PHOTOS,
    TABLE_MATCH_VIDEOS,
    TABLE_VIDEO_CHAPTERS,
    TABLE_VIDEO_COMMENTS,
    TABLE_VIDEO_PROGRESS,
    TABLE_VIDEO_VIEWS,
    TABLE_VIDEO_VOTES,
)


HKT = ZoneInfo("Asia/Hong_Kong")
SOURCE_EXISTING = "連結現有場次"
SOURCE_STANDALONE = "手動輸入舊比賽"
OTHER_ALBUM = "其他相片"
CHAPTER_LABELS = ["正主", "反主", "正一", "反一", "正二", "反二", "正三", "反三", "攻辯", "台下", "交互", "自由辯論", "反結", "正結"]
CHAPTER_ORDER = {label: index for index, label in enumerate(CHAPTER_LABELS)}
VOTE_LABELS = {"pro": "正方勝出", "con": "反方勝出", "undecided": "難以判斷"}
BRACKET_RE = re.compile(r"[（(﹙]\s*([^（(﹙）)﹚]*?)\s*[）)﹚]")


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


def ensure_match_videos_table(db=None):
    db = _resolve_db(db)
    db.execute(CREATE_MATCH_VIDEOS)
    db.execute(f"ALTER TABLE {TABLE_MATCH_VIDEOS} ALTER COLUMN match_id DROP NOT NULL")
    for column in ("match_label", "standalone_topic_text", "standalone_pro_team", "standalone_con_team"):
        db.execute(f"ALTER TABLE {TABLE_MATCH_VIDEOS} ADD COLUMN IF NOT EXISTS {column} TEXT")
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_match_videos_match_id ON {TABLE_MATCH_VIDEOS}(match_id)")
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_match_videos_visible_order ON {TABLE_MATCH_VIDEOS}(is_visible, display_order)")


def ensure_video_interaction_tables(db=None):
    db = _resolve_db(db)
    ensure_match_videos_table(db)
    for sql in (CREATE_VIDEO_VIEWS, CREATE_VIDEO_COMMENTS, CREATE_VIDEO_VOTES, CREATE_VIDEO_CHAPTERS, CREATE_VIDEO_PROGRESS):
        db.execute(sql)
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_video_views_video_id ON {TABLE_VIDEO_VIEWS}(video_id)")
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_video_views_user_updated ON {TABLE_VIDEO_VIEWS}(user_id, viewed_at DESC)")
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_video_comments_video_created ON {TABLE_VIDEO_COMMENTS}(video_id, created_at DESC)")
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_video_votes_video_choice ON {TABLE_VIDEO_VOTES}(video_id, vote_choice)")
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_video_progress_user_updated ON {TABLE_VIDEO_PROGRESS}(user_id, updated_at DESC)")


def ensure_match_photos_table(db=None):
    db = _resolve_db(db)
    ensure_match_videos_table(db)
    db.execute(CREATE_MATCH_PHOTOS)
    db.execute(f"ALTER TABLE {TABLE_MATCH_PHOTOS} ADD COLUMN IF NOT EXISTS photo_date DATE")
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_match_photos_album_created ON {TABLE_MATCH_PHOTOS}(album_label, created_at DESC)")
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_match_photos_date_created ON {TABLE_MATCH_PHOTOS}(photo_date DESC, created_at DESC)")


def _replay_rows(user_id, db):
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
        LEFT JOIN {TABLE_VIDEO_PROGRESS} progress ON progress.video_id = v.id AND progress.user_id = :user_id
        WHERE COALESCE(v.is_visible, TRUE) = TRUE
        ORDER BY progress.updated_at DESC NULLS LAST, m.match_date DESC NULLS LAST, m.match_time DESC NULLS LAST,
                 v.display_order ASC, v.created_at DESC
        """, {"user_id": user_id}
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
        "watched_seconds": safe_int(row.get("watched_seconds")), "duration_seconds": safe_int(row.get("duration_seconds")),
        "progress_updated_at": json_time(row.get("progress_updated_at")),
    }


def replay_data(user_id, selected_video_id=None, db=None):
    db = _resolve_db(db)
    ensure_video_interaction_tables(db)
    videos = [_replay_record(row) for _, row in _replay_rows(user_id, db).iterrows()]
    if not videos:
        return {"videos": [], "selected": None, "chapters": [], "comments": [], "my_vote": None, "vote_labels": VOTE_LABELS, "chapter_labels": CHAPTER_LABELS}
    ids = {video["id"] for video in videos}
    selected_id = safe_int(selected_video_id, videos[0]["id"])
    if selected_id not in ids:
        selected_id = videos[0]["id"]
    selected = next(video for video in videos if video["id"] == selected_id)
    chapters = db.query(
        f"SELECT chapter_label, start_seconds, display_order FROM {TABLE_VIDEO_CHAPTERS} WHERE video_id = :video_id ORDER BY display_order ASC, start_seconds ASC",
        {"video_id": selected_id},
    )
    chapter_items = [{"chapter_label": clean_text(row["chapter_label"]), "start_seconds": safe_int(row["start_seconds"]), "label": seconds_to_label(row["start_seconds"]), "display_order": safe_int(row.get("display_order"))} for _, row in chapters.iterrows()]
    chapter_items.sort(key=lambda row: (CHAPTER_ORDER.get(row["chapter_label"], row["display_order"] + len(CHAPTER_LABELS)), row["start_seconds"]))
    comment_items = []
    vote = db.query(f"SELECT vote_choice FROM {TABLE_VIDEO_VOTES} WHERE video_id = :video_id AND user_id = :user_id", {"video_id": selected_id, "user_id": user_id})
    my_vote = clean_text(vote.iloc[0]["vote_choice"]) if not vote.empty else None
    return {"videos": videos, "selected": selected, "chapters": chapter_items, "comments": comment_items, "my_vote": my_vote, "vote_labels": VOTE_LABELS, "chapter_labels": CHAPTER_LABELS}


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
    db.execute(
        f"INSERT INTO {TABLE_VIDEO_COMMENTS} (video_id, user_id, comment_text, created_at) VALUES (:video_id, :user_id, :comment_text, :created_at)",
        {"video_id": int(video_id), "user_id": user_id, "comment_text": comment_text, "created_at": now_hkt()},
    )
    return {"ok": True, "message": "已成功留言"}


def save_chapters(video_id, chapters, db=None):
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
    db.execute(f"DELETE FROM {TABLE_VIDEO_CHAPTERS} WHERE video_id = :video_id", {"video_id": int(video_id)})
    for label, index, seconds in values:
        db.execute(
            f"INSERT INTO {TABLE_VIDEO_CHAPTERS} (video_id, chapter_label, start_seconds, display_order, updated_at) VALUES (:video_id, :chapter_label, :start_seconds, :display_order, :updated_at)",
            {"video_id": int(video_id), "chapter_label": label, "start_seconds": seconds, "display_order": index, "updated_at": now_hkt()},
        )
    return {"ok": True, "message": "章節時間表已更新。"}


def _match_rows(db):
    return db.query(f"SELECT match_id, match_date, match_time, topic_text, pro_team, con_team FROM {TABLE_MATCHES} ORDER BY match_date DESC NULLS LAST, match_time DESC NULLS LAST, match_id")


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
    ensure_video_interaction_tables(db)
    db.execute(
        f"""INSERT INTO {TABLE_MATCH_VIDEOS} (match_id, match_label, video_title, youtube_url, standalone_topic_text, standalone_pro_team, standalone_con_team, is_visible, display_order, created_at, updated_at)
        VALUES (:match_id, :match_label, :video_title, :youtube_url, :standalone_topic_text, :standalone_pro_team, :standalone_con_team, :is_visible, :display_order, :created_at, :updated_at)""",
        _video_insert_params(data),
    )
    return {"ok": True, "message": "比賽片段已新增。"}


def _admin_video_rows(db):
    return db.query(
        f"""SELECT v.id, v.match_id, v.match_label, v.video_title, v.youtube_url, v.standalone_topic_text, v.standalone_pro_team, v.standalone_con_team, COALESCE(v.is_visible, TRUE) AS is_visible, v.display_order, v.created_at, v.updated_at,
        COALESCE(NULLIF(v.match_label, ''), v.match_id) AS match_display,
        COALESCE(NULLIF(m.topic_text, ''), NULLIF(v.standalone_topic_text, '')) AS topic_text,
        COALESCE(NULLIF(m.pro_team, ''), NULLIF(v.standalone_pro_team, '')) AS pro_team,
        COALESCE(NULLIF(m.con_team, ''), NULLIF(v.standalone_con_team, '')) AS con_team, m.match_date, m.match_time
        FROM {TABLE_MATCH_VIDEOS} v LEFT JOIN {TABLE_MATCHES} m ON v.match_id = m.match_id
        ORDER BY m.match_date DESC NULLS LAST, m.match_time DESC NULLS LAST, v.display_order ASC, v.created_at DESC"""
    )


def _admin_video_record(row):
    match_id = clean_text(row.get("match_id"))
    return {"id": safe_int(row["id"]), "match_id": match_id or None, "match_label": clean_text(row.get("match_label")), "video_title": clean_text(row.get("video_title")), "youtube_url": clean_text(row.get("youtube_url")), "standalone_topic_text": clean_text(row.get("standalone_topic_text")), "standalone_pro_team": clean_text(row.get("standalone_pro_team")), "standalone_con_team": clean_text(row.get("standalone_con_team")), "is_visible": bool(row.get("is_visible")), "display_order": safe_int(row.get("display_order")), "match_display": format_value(row.get("match_display")), "topic_text": clean_text(row.get("topic_text")), "pro_team": clean_text(row.get("pro_team")), "con_team": clean_text(row.get("con_team")), "source_type": "現有場次" if match_id else "舊比賽", "created_at": json_time(row.get("created_at")), "updated_at": json_time(row.get("updated_at"))}


def video_admin_data(selected_video_id=None, page=1, page_size=20, db=None):
    db = _resolve_db(db)
    ensure_video_interaction_tables(db)
    all_videos = [_admin_video_record(row) for _, row in _admin_video_rows(db).iterrows()]
    page_size = max(1, min(int(page_size or 20), 100))
    total = len(all_videos)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), total_pages))
    start = (page - 1) * page_size
    videos = all_videos[start:start + page_size]
    selected_id = safe_int(selected_video_id, videos[0]["id"] if videos else 0)
    if videos and selected_id not in {video["id"] for video in videos}:
        selected_id = videos[0]["id"]
    chapters = []
    if selected_id:
        rows = db.query(f"SELECT chapter_label, start_seconds FROM {TABLE_VIDEO_CHAPTERS} WHERE video_id = :video_id", {"video_id": selected_id})
        chapters = [{"chapter_label": clean_text(row["chapter_label"]), "start_seconds": safe_int(row["start_seconds"]), "label": seconds_to_label(row["start_seconds"])} for _, row in rows.iterrows()]
    return {"matches": match_options(db), "videos": videos, "selected_video_id": selected_id or None, "chapters": chapters, "chapter_labels": CHAPTER_LABELS, "source_existing": SOURCE_EXISTING, "source_standalone": SOURCE_STANDALONE, "pagination": {"page": page, "page_size": page_size, "total": total, "total_pages": total_pages}}


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


def parse_import_csv(raw_text):
    return list(csv.DictReader(io.StringIO(raw_text.lstrip("\ufeff")))) if clean_text(raw_text) else []


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
    rows = parse_import_csv(raw_text)
    if not rows:
        return {"ok": False, "message": "請先上載或貼上 CSV。"}
    db = _resolve_db(db)
    ensure_video_interaction_tables(db)
    existing = db.query(f"SELECT youtube_url FROM {TABLE_MATCH_VIDEOS}")
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
    ensure_match_videos_table(db)
    rows = db.query(
        f"""SELECT DISTINCT ON (album_label) id AS match_video_id, album_label, match_date, match_time
        FROM (SELECT v.id, COALESCE(NULLIF(v.match_label, ''), NULLIF(v.match_id, ''), v.video_title) AS album_label,
                     m.match_date, m.match_time, v.display_order, v.created_at
              FROM {TABLE_MATCH_VIDEOS} v LEFT JOIN {TABLE_MATCHES} m ON v.match_id = m.match_id
              WHERE COALESCE(v.is_visible, TRUE) = TRUE) albums
        WHERE album_label IS NOT NULL AND album_label != ''
        ORDER BY album_label, match_date DESC NULLS LAST, match_time DESC NULLS LAST, display_order ASC, created_at DESC"""
    )
    options = [{"label": OTHER_ALBUM, "video_id": None}]
    for _, row in rows.iterrows():
        label = clean_text(row["album_label"])
        if label and label != OTHER_ALBUM:
            options.append({"label": label, "video_id": safe_int(row["match_video_id"])})
    return options


def photo_data(db=None):
    db = _resolve_db(db)
    ensure_match_photos_table(db)
    options = album_options(db)
    return {"albums": options, "photos": [], "other_album": OTHER_ALBUM}


def upload_photos(user_id, album_label, match_video_id, photo_date, photo_title, caption, files, db=None):
    if not files:
        return {"ok": False, "message": "請先選擇圖片。"}
    db = _resolve_db(db)
    ensure_match_photos_table(db)
    parsed_date = None
    if clean_text(photo_date):
        try:
            parsed_date = dt.date.fromisoformat(clean_text(photo_date))
        except ValueError:
            return {"ok": False, "message": "相片日期格式無效。"}
    for item in files:
        db.execute(
            f"""INSERT INTO {TABLE_MATCH_PHOTOS} (match_video_id, album_label, photo_date, photo_title, caption, file_name, mime_type, image_data, uploaded_by, created_at)
            VALUES (:match_video_id, :album_label, :photo_date, :photo_title, :caption, :file_name, :mime_type, :image_data, :uploaded_by, :created_at)""",
            {"match_video_id": int(match_video_id) if match_video_id not in (None, "") else None, "album_label": clean_text(album_label), "photo_date": parsed_date, "photo_title": clean_text(photo_title) or None, "caption": clean_text(caption) or None, "file_name": clean_text(item["file_name"]), "mime_type": clean_text(item.get("mime_type")) or "image/jpeg", "image_data": item["image_data"], "uploaded_by": user_id, "created_at": now_hkt()},
        )
    return {"ok": True, "message": "圖片已成功上載。"}


def photo_bytes(photo_id, db=None):
    db = _resolve_db(db)
    rows = db.query(f"SELECT file_name, mime_type, image_data FROM {TABLE_MATCH_PHOTOS} WHERE id = :id", {"id": int(photo_id)})
    if rows.empty:
        return None
    row = rows.iloc[0]
    data = row["image_data"]
    return {"file_name": clean_text(row["file_name"]) or f"match-photo-{int(photo_id)}.jpg", "mime_type": clean_text(row["mime_type"]) or "image/jpeg", "image_data": bytes(data) if data is not None else b""}
