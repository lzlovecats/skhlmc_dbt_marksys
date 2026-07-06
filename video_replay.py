import datetime
import json
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components

from functions import (
    _sign_cookie,
    check_committee_login,
    committee_cookie_manager,
    del_cookie,
    ensure_video_interaction_tables,
    execute_query,
    query_params,
    render_committee_auth_bridge,
    render_page_guidance,
)
from schema import (
    TABLE_MATCHES,
    TABLE_MATCH_VIDEOS,
    TABLE_VIDEO_CHAPTERS,
    TABLE_VIDEO_COMMENTS,
    TABLE_VIDEO_PROGRESS,
    TABLE_VIDEO_VIEWS,
    TABLE_VIDEO_VOTES,
)


CHAPTER_LABELS = ["正主", "反主", "正一", "反一", "正二", "反二", "正三", "反三", "台下", "交互", "自由辯論", "反結", "正結"]
VOTE_LABELS = {"pro": "正方勝出", "con": "反方勝出", "undecided": "難以判斷"}


def _format_value(value, default="未設定"):
    if value is None:
        return default
    text = str(value).strip()
    if not text or text.lower() in ("none", "nan", "nat"):
        return default
    return text


def _format_match_time(row):
    match_date = _format_value(row.get("match_date"), "")
    match_time = _format_value(row.get("match_time"), "")
    parts = []
    if match_date:
        parts.append(match_date)
    if match_time:
        parts.append(match_time[:5])
    return " ".join(parts) if parts else "未設定日期時間"


def _youtube_video_id(url):
    parsed = urlparse(str(url or "").strip())
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


def _seconds_to_label(seconds):
    seconds = _safe_int(seconds)
    minutes, sec = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    if hour:
        return f"{hour}:{minute:02d}:{sec:02d}"
    return f"{minute}:{sec:02d}"


def _parse_time_to_seconds(value):
    text = str(value or "").strip()
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


def _safe_int(value, default=0):
    try:
        if value is None or value != value:
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _current_url_for_video(video_id, start_seconds=0):
    try:
        parsed = urlparse(st.context.url)
        query = {"video_id": str(video_id)}
        if start_seconds:
            query["t"] = str(int(start_seconds))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(query), ""))
    except Exception:
        suffix = f"?video_id={video_id}"
        if start_seconds:
            suffix += f"&t={int(start_seconds)}"
        return suffix


def _query_param(name, default=None):
    try:
        value = st.query_params.get(name, default)
        if isinstance(value, list):
            return value[0] if value else default
        return value
    except Exception:
        return default


def _queue_replay_toast(message, icon=None):
    # st.toast() issued right before st.rerun() is discarded by the rerun,
    # so queue it in session_state and flush on the next run instead.
    st.session_state["_replay_toast"] = {"message": message, "icon": icon}


def _flush_replay_toast():
    toast = st.session_state.pop("_replay_toast", None)
    if toast:
        st.toast(toast["message"], icon=toast.get("icon"))


def _record_view_once(video_id, user_id):
    key = f"video_view_recorded_{video_id}"
    if st.session_state.get(key):
        return
    execute_query(
        f"INSERT INTO {TABLE_VIDEO_VIEWS} (video_id, user_id, viewed_at) VALUES (:video_id, :user_id, :viewed_at)",
        {
            "video_id": int(video_id),
            "user_id": user_id,
            "viewed_at": datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None),
        },
    )
    st.session_state[key] = True


def _render_youtube_player(video_id, youtube_id, start_seconds, auth_token):
    html = """
    <div id="playerWrap" style="width:100%; aspect-ratio:16/9; background:#111827;">
        <div id="youtubePlayer"></div>
    </div>
    <script>
    (function () {
        const parentWin = window.parent;
        const win = window;
        const doc = document;
        const videoId = __VIDEO_ID__;
        const youtubeId = __YOUTUBE_ID__;
        const startSeconds = __START_SECONDS__;
        const authToken = __AUTH_TOKEN__;
        let player = null;
        let progressTimer = null;

        function postJson(url, payload) {
            return parentWin.fetch(url, {
                method: "POST",
                credentials: "include",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + authToken
                },
                body: JSON.stringify(payload)
            }).catch(function () {});
        }

        function saveProgress() {
            if (!player || typeof player.getCurrentTime !== "function") {
                return;
            }
            postJson("/api/video/progress", {
                video_id: videoId,
                watched_seconds: Math.floor(player.getCurrentTime() || 0),
                duration_seconds: Math.floor(player.getDuration() || 0)
            });
        }

        function onReady(event) {
            // View counting is handled once per session server-side
            // (see _record_view_once); the player only reports playback progress.
            if (startSeconds > 0) {
                event.target.seekTo(startSeconds, true);
            }
        }

        function onStateChange(event) {
            if (event.data === 1) {
                saveProgress();
                if (!progressTimer) {
                    progressTimer = win.setInterval(saveProgress, 15000);
                }
            } else if (event.data === 0 || event.data === 2) {
                saveProgress();
            }
        }

        win.onYouTubeIframeAPIReady = function () {
            player = new win.YT.Player(doc.getElementById("youtubePlayer"), {
                width: "100%",
                height: "100%",
                videoId: youtubeId,
                playerVars: {
                    start: startSeconds,
                    rel: 0,
                    playsinline: 1
                },
                events: {
                    onReady: onReady,
                    onStateChange: onStateChange
                }
            });
        };

        if (!win.YT || !win.YT.Player) {
            const tag = doc.createElement("script");
            tag.src = "https://www.youtube.com/iframe_api";
            doc.head.appendChild(tag);
        } else {
            win.onYouTubeIframeAPIReady();
        }
    })();
    </script>
    """.replace("__VIDEO_ID__", json.dumps(int(video_id))) \
       .replace("__YOUTUBE_ID__", json.dumps(youtube_id)) \
       .replace("__START_SECONDS__", json.dumps(int(start_seconds or 0))) \
       .replace("__AUTH_TOKEN__", json.dumps(auth_token))
    components.html(html, height=420)


def _render_mobile_styles():
    # 手機（≤640px）響應式排版：沿用 home.py 已確立嘅模式。此版本 Streamlit
    # 嘅 st.columns 喺窄螢幕唔會自動堆疊，須用 CSS 強制 column 全闊度直向排列。
    st.markdown(
        """
        <style>
        @media (max-width: 640px) {
            .block-container {
                padding-left: 1rem;
                padding-right: 1rem;
                padding-top: 1rem;
            }
            div[data-testid="stButton"] button {
                min-height: 2.75rem;
                align-items: center;
            }
            div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
                min-width: 100% !important;
                flex: 1 1 100% !important;
            }
            .st-key-skh-chapter-grid div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
                min-width: 50% !important;
                flex: 1 1 50% !important;
            }
            h1 {
                font-size: 1.65rem !important;
                line-height: 1.25 !important;
            }
            h3 {
                font-size: 1.08rem !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


st.header("比賽片段重溫")
_render_mobile_styles()
render_page_guidance(
    [
        "此頁只供內部委員會成員登入後使用。",
        "可在系統內直接播放片段，並使用章節按鈕跳轉至不同辯位或環節。",
        "所有內部委員會成員都可以更新片段章節時間點。",
        "系統會記錄最近觀看的片段及觀看位置，方便下次繼續。",
    ],
)

if not check_committee_login():
    st.stop()

user_id = st.session_state["committee_user"]
if user_id == "admin":
    st.error("賽會人員帳戶不能使用此頁面。請改用內部委員會成員帳戶登入。")
    if st.button("登出"):
        st.session_state["committee_user"] = None
        del_cookie(committee_cookie_manager(), "committee_user")
        render_committee_auth_bridge(clear=True)
        st.rerun()
    st.stop()

if not ensure_video_interaction_tables():
    st.error("未能建立或讀取比賽片段資料表，請稍後再試或聯絡開發人員。")
    st.stop()

_flush_replay_toast()

if st.button("重新整理"):
    st.cache_data.clear()
    st.rerun()

videos_df = query_params(
    f"""
    SELECT
        v.id,
        v.match_id,
        COALESCE(NULLIF(v.match_label, ''), v.match_id) AS match_display,
        v.video_title,
        v.youtube_url,
        v.display_order,
        v.created_at,
        m.match_date,
        m.match_time,
        COALESCE(NULLIF(m.topic_text, ''), NULLIF(v.standalone_topic_text, '')) AS topic_text,
        COALESCE(NULLIF(m.pro_team, ''), NULLIF(v.standalone_pro_team, '')) AS pro_team,
        COALESCE(NULLIF(m.con_team, ''), NULLIF(v.standalone_con_team, '')) AS con_team,
        COALESCE(view_stats.view_count, 0) AS view_count,
        COALESCE(vote_stats.pro_votes, 0) AS pro_votes,
        COALESCE(vote_stats.con_votes, 0) AS con_votes,
        COALESCE(vote_stats.undecided_votes, 0) AS undecided_votes,
        progress.watched_seconds,
        progress.duration_seconds,
        progress.updated_at AS progress_updated_at
    FROM {TABLE_MATCH_VIDEOS} v
    LEFT JOIN {TABLE_MATCHES} m ON v.match_id = m.match_id
    LEFT JOIN (
        SELECT video_id, COUNT(*) AS view_count
        FROM {TABLE_VIDEO_VIEWS}
        GROUP BY video_id
    ) view_stats ON view_stats.video_id = v.id
    LEFT JOIN (
        SELECT
            video_id,
            SUM(CASE WHEN vote_choice = 'pro' THEN 1 ELSE 0 END) AS pro_votes,
            SUM(CASE WHEN vote_choice = 'con' THEN 1 ELSE 0 END) AS con_votes,
            SUM(CASE WHEN vote_choice = 'undecided' THEN 1 ELSE 0 END) AS undecided_votes
        FROM {TABLE_VIDEO_VOTES}
        GROUP BY video_id
    ) vote_stats ON vote_stats.video_id = v.id
    LEFT JOIN {TABLE_VIDEO_PROGRESS} progress
        ON progress.video_id = v.id
       AND progress.user_id = :user_id
    WHERE COALESCE(v.is_visible, TRUE) = TRUE
    ORDER BY progress.updated_at DESC NULLS LAST,
             m.match_date DESC NULLS LAST,
             m.match_time DESC NULLS LAST,
             v.display_order ASC,
             v.created_at DESC
    """,
    {"user_id": user_id},
)

if videos_df.empty:
    st.info("目前未有可重溫的比賽片段。")
    st.stop()

query_video_id = _query_param("video_id")
try:
    selected_video_id = int(query_video_id) if query_video_id else int(videos_df.iloc[0]["id"])
except (TypeError, ValueError):
    selected_video_id = int(videos_df.iloc[0]["id"])
if selected_video_id not in videos_df["id"].astype(int).tolist():
    selected_video_id = int(videos_df.iloc[0]["id"])

def _select_video(video_id, start_seconds=None):
    st.query_params["video_id"] = str(int(video_id))
    if start_seconds is None:
        # No explicit target → resume from the video's own saved position.
        if "t" in st.query_params:
            del st.query_params["t"]
    else:
        # Explicit target (incl. 0 for "play from start") is authoritative.
        st.query_params["t"] = str(max(0, int(start_seconds)))
    st.rerun()


def _truncate(text, limit=42):
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


filter_col1, filter_col2 = st.columns([1, 2])
with filter_col1:
    match_options = ["全部"] + sorted(videos_df["match_display"].dropna().astype(str).unique().tolist())
    selected_match = st.selectbox("場次篩選", options=match_options)
with filter_col2:
    search_term = st.text_input("搜尋", placeholder="輸入場次、隊伍、辯題或片段標題")

filtered_df = videos_df.copy()
if selected_match != "全部":
    filtered_df = filtered_df[filtered_df["match_display"].astype(str) == selected_match]
if search_term:
    keyword = search_term.strip().lower()
    search_cols = ["match_display", "video_title", "topic_text", "pro_team", "con_team"]
    search_text = filtered_df[search_cols].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
    filtered_df = filtered_df[search_text.str.contains(keyword, regex=False)]

if filtered_df.empty:
    st.info("沒有符合條件的比賽片段。請調整搜尋關鍵字或篩選條件後再試。")
    st.stop()

if selected_video_id not in filtered_df["id"].astype(int).tolist():
    selected_video_id = int(filtered_df.iloc[0]["id"])

selected_row = filtered_df[filtered_df["id"].astype(int) == int(selected_video_id)].iloc[0]
youtube_id = _youtube_video_id(selected_row["youtube_url"])
resume_seconds = _safe_int(selected_row.get("watched_seconds"))
# An explicit ?t= (chapter jump or "play from start", including t=0) overrides the
# saved position; otherwise resume where the user last stopped.
t_param = _query_param("t", None)
if t_param not in (None, ""):
    try:
        start_seconds = max(0, int(t_param))
    except (TypeError, ValueError):
        start_seconds = 0
else:
    start_seconds = resume_seconds

# Player is placed first so it stays prominent (and appears on top when the
# two columns stack on mobile); the playlist sits on the side.
player_col, list_col = st.columns([2, 1])

with player_col:
    st.subheader(str(selected_row["video_title"]))
    meta_bits = [f"場次：{_format_value(selected_row['match_display'])}", _format_match_time(selected_row)]
    st.caption("　｜　".join(bit for bit in meta_bits if bit))
    st.write(f"**辯題：** {_format_value(selected_row['topic_text'])}")
    info_col1, info_col2, info_col3 = st.columns(3)
    info_col1.caption(f"👁 觀看 {int(selected_row['view_count'])}")
    info_col2.caption(f"🟦 正方 {int(selected_row['pro_votes'])}")
    info_col3.caption(f"🟥 反方 {int(selected_row['con_votes'])}")

    if not youtube_id:
        st.error("此片段的 YouTube 連結格式無法辨識，請聯絡賽會人員修正。")
    else:
        _record_view_once(selected_video_id, user_id)
        _render_youtube_player(selected_video_id, youtube_id, start_seconds, _sign_cookie(user_id))

    if resume_seconds:
        resume_col1, resume_col2 = st.columns(2)
        with resume_col1:
            st.caption(f"⏱ 你上次看到：{_seconds_to_label(resume_seconds)}")
        with resume_col2:
            if start_seconds and st.button("從頭播放", key="restart_video", use_container_width=True):
                _select_video(selected_video_id, 0)

    chapter_rows = query_params(
        f"""
        SELECT chapter_label, start_seconds, display_order
        FROM {TABLE_VIDEO_CHAPTERS}
        WHERE video_id = :video_id
        ORDER BY display_order ASC, start_seconds ASC
        """,
        {"video_id": int(selected_video_id)},
    )
    if not chapter_rows.empty:
        st.caption("章節跳轉")
        with st.container(key="skh-chapter-grid"):
            chapter_cols = st.columns(4)
            for position, (_, chapter) in enumerate(chapter_rows.iterrows()):
                with chapter_cols[position % 4]:
                    if st.button(
                        f"{chapter['chapter_label']} {_seconds_to_label(chapter['start_seconds'])}",
                        key=f"chapter_{selected_video_id}_{chapter['chapter_label']}",
                        use_container_width=True,
                    ):
                        _select_video(selected_video_id, int(chapter["start_seconds"]))
    else:
        st.caption("此片段暫未設定章節。")

    with st.expander("更新章節時間點", expanded=chapter_rows.empty):
        st.caption("所有內部委員會成員均可更新；時間可輸入秒數、mm:ss 或 hh:mm:ss。未啟用的章節不會顯示。")
        chapter_map = {
            str(row["chapter_label"]): int(row["start_seconds"])
            for _, row in chapter_rows.iterrows()
        } if not chapter_rows.empty else {}

        with st.form(f"replay_chapter_form_{selected_video_id}"):
            chapter_inputs = []
            for chapter_index, chapter_label in enumerate(CHAPTER_LABELS):
                chapter_col1, chapter_col2 = st.columns([1, 2])
                with chapter_col1:
                    enabled = st.checkbox(
                        chapter_label,
                        value=chapter_label in chapter_map,
                        key=f"replay_chapter_enabled_{selected_video_id}_{chapter_label}",
                    )
                with chapter_col2:
                    current_value = _seconds_to_label(chapter_map[chapter_label]) if chapter_label in chapter_map else ""
                    time_text = st.text_input(
                        "開始時間",
                        value=current_value,
                        key=f"replay_chapter_time_{selected_video_id}_{chapter_label}",
                        label_visibility="collapsed",
                        placeholder="例：12:34",
                    )
                chapter_inputs.append((chapter_label, chapter_index, enabled, time_text))
            save_chapters = st.form_submit_button("儲存章節時間表", type="primary", use_container_width=True)

        if save_chapters:
            chapter_values = []
            invalid_labels = []
            for chapter_label, chapter_index, enabled, time_text in chapter_inputs:
                if not enabled:
                    continue
                start_seconds_for_chapter = _parse_time_to_seconds(time_text)
                if start_seconds_for_chapter is None:
                    invalid_labels.append(chapter_label)
                else:
                    chapter_values.append((chapter_label, chapter_index, start_seconds_for_chapter))

            if invalid_labels:
                st.error("以下章節時間格式無效：" + "、".join(invalid_labels))
            else:
                now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
                execute_query(
                    f"DELETE FROM {TABLE_VIDEO_CHAPTERS} WHERE video_id = :video_id",
                    {"video_id": int(selected_video_id)},
                )
                for chapter_label, chapter_index, start_seconds_for_chapter in chapter_values:
                    execute_query(
                        f"""
                        INSERT INTO {TABLE_VIDEO_CHAPTERS} (
                            video_id, chapter_label, start_seconds, display_order, updated_at
                        )
                        VALUES (
                            :video_id, :chapter_label, :start_seconds, :display_order, :updated_at
                        )
                        """,
                        {
                            "video_id": int(selected_video_id),
                            "chapter_label": chapter_label,
                            "start_seconds": int(start_seconds_for_chapter),
                            "display_order": int(chapter_index),
                            "updated_at": now_hk,
                        },
                    )
                _queue_replay_toast("章節時間表已更新。", icon="✅")
                st.rerun()

    with st.popover("🔗 分享此片段連結", use_container_width=True):
        st.caption("複製以下連結，一按即可開啟本片段：")
        st.code(_current_url_for_video(selected_video_id, start_seconds), language=None)

with list_col:
    st.caption(f"片單（共 {len(filtered_df)} 條）")
    playlist_box = st.container(height=520, border=False)
    with playlist_box:
        for _, row in filtered_df.iterrows():
            video_id = int(row["id"])
            is_current = video_id == selected_video_id
            watched_seconds = _safe_int(row.get("watched_seconds"))
            with st.container(border=True):
                badge = "▶ 播放中　" if is_current else ""
                st.markdown(f"**{badge}{_truncate(row['video_title'])}**")
                caption = f"{_format_value(row['match_display'])}　👁 {int(row['view_count'])}"
                if watched_seconds:
                    caption += f"　⏱ {_seconds_to_label(watched_seconds)}"
                st.caption(caption)
                if not is_current:
                    if st.button("播放", key=f"select_video_{video_id}", use_container_width=True):
                        _select_video(video_id)

st.divider()
vote_col, comment_col = st.columns([1, 2])

with vote_col:
    st.subheader("勝負投票")
    my_vote_df = query_params(
        f"SELECT vote_choice FROM {TABLE_VIDEO_VOTES} WHERE video_id = :video_id AND user_id = :user_id",
        {"video_id": int(selected_video_id), "user_id": user_id},
    )
    current_vote = my_vote_df.iloc[0]["vote_choice"] if not my_vote_df.empty else None
    vote_options = ["pro", "con", "undecided"]
    selected_vote = st.radio(
        "你認為哪方勝出？",
        options=vote_options,
        format_func=lambda value: VOTE_LABELS[value],
        index=vote_options.index(current_vote) if current_vote in vote_options else 0,
    )
    if current_vote:
        st.caption(f"你目前投：{VOTE_LABELS.get(current_vote, current_vote)}")
    if st.button("提交投票", type="primary", use_container_width=True):
        execute_query(
            f"""
            INSERT INTO {TABLE_VIDEO_VOTES} (video_id, user_id, vote_choice, updated_at)
            VALUES (:video_id, :user_id, :vote_choice, :updated_at)
            ON CONFLICT (video_id, user_id) DO UPDATE SET
                vote_choice = EXCLUDED.vote_choice,
                updated_at = EXCLUDED.updated_at
            """,
            {
                "video_id": int(selected_video_id),
                "user_id": user_id,
                "vote_choice": selected_vote,
                "updated_at": datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None),
            },
        )
        _queue_replay_toast("已成功提交投票", icon="☑️")
        st.rerun()

    pro_votes = int(selected_row["pro_votes"])
    con_votes = int(selected_row["con_votes"])
    undecided_votes = int(selected_row["undecided_votes"])
    total_votes = pro_votes + con_votes + undecided_votes
    st.caption(f"目前共 {total_votes} 票")
    result_col1, result_col2, result_col3 = st.columns(3)
    result_col1.metric("正方", pro_votes)
    result_col2.metric("反方", con_votes)
    result_col3.metric("難判", undecided_votes)

with comment_col:
    comments_df = query_params(
        f"""
        SELECT user_id, comment_text, created_at
        FROM {TABLE_VIDEO_COMMENTS}
        WHERE video_id = :video_id
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"video_id": int(selected_video_id)},
    )
    st.subheader(f"留言區（{len(comments_df)}）")
    with st.form("video_comment_form", clear_on_submit=True):
        comment_text = st.text_area("留言", placeholder="就此片段發表意見。")
        submit_comment = st.form_submit_button("發表留言", type="primary")
    if submit_comment:
        if not comment_text.strip():
            st.warning("請輸入留言內容。")
        else:
            execute_query(
                f"""
                INSERT INTO {TABLE_VIDEO_COMMENTS} (video_id, user_id, comment_text, created_at)
                VALUES (:video_id, :user_id, :comment_text, :created_at)
                """,
                {
                    "video_id": int(selected_video_id),
                    "user_id": user_id,
                    "comment_text": comment_text.strip(),
                    "created_at": datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None),
                },
            )
            _queue_replay_toast("已成功留言", icon="☑️")
            st.rerun()

    if comments_df.empty:
        st.caption("暫時未有留言。")
    else:
        for _, comment in comments_df.iterrows():
            created = comment["created_at"]
            created_text = created.strftime("%m-%d %H:%M") if hasattr(created, "strftime") else str(created)[:16]
            with st.container(border=True):
                st.caption(f"**{comment['user_id']}**　{created_text}")
                st.write(comment["comment_text"])
