import datetime
import csv
import io
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import streamlit as st

from auth import check_admin
from functions import ensure_match_videos_table, ensure_video_interaction_tables, execute_query, query_params, render_page_guidance
from schema import TABLE_MATCHES, TABLE_MATCH_VIDEOS, TABLE_VIDEO_CHAPTERS


SOURCE_EXISTING = "連結現有場次"
SOURCE_STANDALONE = "手動輸入舊比賽"
ADD_FIELD_DEFAULTS = {
    "add_match_label": "",
    "add_standalone_topic_text": "",
    "add_standalone_pro_team": "",
    "add_standalone_con_team": "",
    "add_video_title": "",
    "add_youtube_url": "",
    "add_display_order": 0,
    "add_is_visible": True,
}
CHAPTER_LABELS = ["正主", "反主", "正一", "反一", "正二", "反二", "正三", "反三", "攻辯", "台下", "交互", "自由辯論", "反結", "正結"]


def _is_youtube_url(url):
    try:
        parsed = urlparse(_normalize_youtube_url(url))
    except Exception:
        return False
    host = parsed.netloc.lower()
    return (
        parsed.scheme in ("http", "https")
        and (
            host == "youtu.be"
            or host.endswith(".youtu.be")
            or host == "youtube.com"
            or host.endswith(".youtube.com")
        )
    )


def _normalize_youtube_url(url):
    text = str(url or "").strip()
    if text and "://" not in text:
        text = f"https://{text}"
    return text


def _clean_text(value):
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in ("none", "nan", "nat"):
        return ""
    return text


def _validate_video_input(video_title, youtube_url, video_source, match_label=""):
    errors = []
    if not video_title.strip():
        errors.append("請輸入片段標題。")
    if not youtube_url.strip():
        errors.append("請輸入 YouTube 連結。")
    elif not _is_youtube_url(youtube_url.strip()):
        errors.append("請輸入有效的 YouTube 連結。")
    if video_source == SOURCE_STANDALONE and not match_label.strip():
        errors.append("請輸入舊比賽名稱或場次。")
    return errors


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_bool(value, default=True):
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "y", "顯示", "公開", "是"):
        return True
    if text in ("0", "false", "no", "n", "隱藏", "否"):
        return False
    return default


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


def _seconds_to_label(seconds):
    seconds = int(seconds or 0)
    minutes, sec = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    if hour:
        return f"{hour}:{minute:02d}:{sec:02d}"
    return f"{minute}:{sec:02d}"


# YouTube Studio 影片標題常見格式：﹙聯中2015﹚拉布對本港社會發展利多於弊﹙正﹚
# 首個括號＝賽事／場次，末個括號若為正／反／甲／乙＝辯方，中間＝辯題。
_BRACKET_RE = re.compile(r"[（(﹙]\s*([^（(﹙）)﹚]*?)\s*[）)﹚]")


def _side_of(token):
    token = str(token or "").strip()
    if not token:
        return ""
    if token[0] == "正" or token == "甲":
        return "pro"
    if token[0] == "反" or token == "乙":
        return "con"
    return ""


def _parse_studio_title(raw_title):
    """從 YouTube Studio 影片標題拆解出（場次、辯題、辯方）。"""
    title = str(raw_title or "").strip()
    groups = list(_BRACKET_RE.finditer(title))
    if not groups:
        return "", title, ""

    first = groups[0]
    match_label = first.group(1).strip()
    remove_spans = [first.span()]

    side = ""
    last = groups[-1]
    if last is not first:
        side = _side_of(last.group(1))
        if side:
            remove_spans.append(last.span())

    kept = []
    cursor = 0
    for start, end in sorted(remove_spans):
        kept.append(title[cursor:start])
        cursor = end
    kept.append(title[cursor:])
    topic = re.sub(r"\s+", " ", "".join(kept)).strip(" 　｜|:：-—・")
    return match_label, topic, side


def _read_import_rows(uploaded_file, pasted_csv):
    raw_text = ""
    if uploaded_file is not None:
        raw_text = uploaded_file.getvalue().decode("utf-8-sig")
    elif pasted_csv.strip():
        raw_text = pasted_csv.strip()
    if not raw_text:
        return []
    reader = csv.DictReader(io.StringIO(raw_text))
    return [row for row in reader]


def _row_value(row, *keys):
    lower_map = {str(key).strip().lower(): value for key, value in row.items()}
    for key in keys:
        if key in row and row.get(key) is not None:
            return row.get(key)
        value = lower_map.get(str(key).strip().lower())
        if value is not None:
            return value
    return ""


def _clear_add_video_fields():
    for key, value in ADD_FIELD_DEFAULTS.items():
        st.session_state[key] = value


def _resolve_match_params(video_source, selected_match=None, match_label="", topic_text="", pro_team="", con_team=""):
    if video_source == SOURCE_EXISTING:
        return {
            "match_id": selected_match,
            "match_label": None,
            "standalone_topic_text": None,
            "standalone_pro_team": None,
            "standalone_con_team": None,
        }
    return {
        "match_id": None,
        "match_label": match_label.strip(),
        "standalone_topic_text": topic_text.strip() or None,
        "standalone_pro_team": pro_team.strip() or None,
        "standalone_con_team": con_team.strip() or None,
    }


def render_video_admin():
    if not ensure_match_videos_table() or not ensure_video_interaction_tables():
        st.error("未能建立或讀取比賽片段資料表，請稍後再試或聯絡開發人員。")
        st.stop()

    if "video_action_message" not in st.session_state:
        st.session_state["video_action_message"] = None
    if "delete_confirm_video_id" not in st.session_state:
        st.session_state["delete_confirm_video_id"] = None

    if st.session_state["video_action_message"]:
        action_message = st.session_state["video_action_message"]
        if action_message["type"] == "success":
            st.success(action_message["content"])
            st.toast(action_message["content"], icon="✅")
        elif action_message["type"] == "warning":
            st.warning(action_message["content"])
            st.toast(action_message["content"], icon="⚠️")
        st.session_state["video_action_message"] = None

    matches_df = query_params(
        f"""
        SELECT match_id, match_date, match_time, topic_text, pro_team, con_team
        FROM {TABLE_MATCHES}
        ORDER BY match_date DESC NULLS LAST, match_time DESC NULLS LAST, match_id
        """
    )
    match_options = matches_df["match_id"].astype(str).tolist() if not matches_df.empty else []
    now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)

    def _format_match_option(match_id):
        match_row = matches_df[matches_df["match_id"].astype(str) == str(match_id)].iloc[0]
        topic_text = _clean_text(match_row.get("topic_text"))
        pro_team = _clean_text(match_row.get("pro_team"))
        con_team = _clean_text(match_row.get("con_team"))
        teams = " vs ".join([team for team in [pro_team, con_team] if team])
        details = "｜".join([item for item in [teams, topic_text] if item])
        return f"{match_id} - {details}" if details else str(match_id)

    def _source_options():
        if match_options:
            return [SOURCE_EXISTING, SOURCE_STANDALONE]
        return [SOURCE_STANDALONE]

    with st.container(border=True):
        st.subheader("新增片段")
        add_source = st.radio(
            "片段所屬比賽",
            options=_source_options(),
            horizontal=True,
            key="add_video_source",
            on_change=_clear_add_video_fields,
        )
        selected_match = None
        if add_source == SOURCE_EXISTING:
            selected_match = st.selectbox(
                "選擇比賽場次",
                options=match_options,
                format_func=_format_match_option,
                key="add_selected_match",
                on_change=_clear_add_video_fields,
            )
        else:
            st.button("清除新增欄位內容", use_container_width=True, on_click=_clear_add_video_fields)

        with st.form("add_video_form"):
            match_label = ""
            standalone_topic_text = ""
            standalone_pro_team = ""
            standalone_con_team = ""

            if add_source == SOURCE_STANDALONE:
                match_label = st.text_input("比賽名稱／場次", placeholder="例：第 1 屆決賽", key="add_match_label")
                standalone_topic_text = st.text_input("辯題（可留空）", key="add_standalone_topic_text")
                team_col1, team_col2 = st.columns(2)
                with team_col1:
                    standalone_pro_team = st.text_input("正方隊名（可留空）", key="add_standalone_pro_team")
                with team_col2:
                    standalone_con_team = st.text_input("反方隊名（可留空）", key="add_standalone_con_team")

            video_title = st.text_input("片段標題", placeholder="例：全場片段／上半場／下半場", key="add_video_title")
            youtube_url = st.text_input("YouTube 連結", key="add_youtube_url")
            display_order = st.number_input("排序", min_value=0, step=1, value=0, key="add_display_order")
            is_visible = st.checkbox("在會員重溫頁顯示", value=True, key="add_is_visible")
            add_video = st.form_submit_button("新增片段", type="primary", use_container_width=True)

        if add_video:
            errors = _validate_video_input(video_title, youtube_url, add_source, match_label)
            if errors:
                for error in errors:
                    st.error(error)
            else:
                match_params = _resolve_match_params(
                    add_source,
                    selected_match,
                    match_label,
                    standalone_topic_text,
                    standalone_pro_team,
                    standalone_con_team,
                )
                execute_query(
                    f"""
                    INSERT INTO {TABLE_MATCH_VIDEOS} (
                        match_id,
                        match_label,
                        video_title,
                        youtube_url,
                        standalone_topic_text,
                        standalone_pro_team,
                        standalone_con_team,
                        is_visible,
                        display_order,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :match_id,
                        :match_label,
                        :video_title,
                        :youtube_url,
                        :standalone_topic_text,
                        :standalone_pro_team,
                        :standalone_con_team,
                        :is_visible,
                        :display_order,
                        :created_at,
                        :updated_at
                    )
                    """,
                    {
                        **match_params,
                        "video_title": video_title.strip(),
                        "youtube_url": _normalize_youtube_url(youtube_url),
                        "is_visible": bool(is_visible),
                        "display_order": int(display_order),
                        "created_at": now_hk,
                        "updated_at": now_hk,
                    },
                )
                st.session_state["video_action_message"] = {"type": "success", "content": "比賽片段已新增。"}
                st.rerun()

    with st.expander("批量匯入 YouTube 片段", expanded=False):
        st.caption("可直接上載 YouTube Studio 匯出的「Table data.csv」（Content＝影片 ID、Video title＝標題），系統會自動由標題拆解場次及辯題。")
        st.caption("亦支援自訂欄位：video_title, youtube_url 或 video_id / Content, match_label, topic_text, pro_team, con_team, display_order, is_visible")
        st.caption("重複的 YouTube 連結會自動略過，可安全地重覆匯入。")
        uploaded_csv = st.file_uploader("上載 CSV", type=["csv"], key="video_import_csv")
        pasted_csv = st.text_area(
            "或貼上 CSV 內容",
            placeholder="Content,Video title,...（或 video_title,youtube_url,match_label,...）",
            key="video_import_text",
        )
        parse_from_title = st.checkbox(
            "由影片標題自動拆解場次／辯題（YouTube Studio 格式）",
            value=True,
            key="video_import_parse_title",
        )
        if st.button("匯入片段", type="primary", use_container_width=True):
            rows = _read_import_rows(uploaded_csv, pasted_csv)
            if not rows:
                st.warning("請先上載或貼上 CSV。")
            else:
                existing_df = query_params(f"SELECT youtube_url FROM {TABLE_MATCH_VIDEOS}")
                existing_urls = (
                    {str(u).strip() for u in existing_df["youtube_url"].tolist()}
                    if not existing_df.empty
                    else set()
                )
                inserted = 0
                skipped = 0
                duplicated = 0
                for row in rows:
                    title = _clean_text(_row_value(row, "video_title", "title", "Video title", "影片標題"))
                    url = _clean_text(_row_value(row, "youtube_url", "url", "Video URL", "YouTube 連結"))
                    video_id = _clean_text(_row_value(row, "video_id", "Video ID", "影片 ID", "Content", "content"))
                    if video_id.lower() == "total":
                        continue
                    if not url and video_id:
                        url = f"https://www.youtube.com/watch?v={video_id}"
                    match_label = _clean_text(_row_value(row, "match_label", "場次", "比賽名稱"))
                    topic_text = _clean_text(_row_value(row, "topic_text", "辯題"))
                    pro_team = _clean_text(_row_value(row, "pro_team", "正方"))
                    con_team = _clean_text(_row_value(row, "con_team", "反方"))
                    display_order = _to_int(_row_value(row, "display_order", "排序"), 0)
                    is_visible = _parse_bool(_row_value(row, "is_visible", "顯示"), True)

                    # YouTube Studio 匯出只有標題，無場次／辯題欄位時，由標題自動拆解。
                    if parse_from_title and not match_label and not topic_text and title:
                        parsed_label, parsed_topic, _side = _parse_studio_title(title)
                        match_label = parsed_label
                        topic_text = parsed_topic

                    if not title or not url or not _is_youtube_url(url):
                        skipped += 1
                        continue

                    normalized_url = _normalize_youtube_url(url)
                    if normalized_url in existing_urls:
                        duplicated += 1
                        continue
                    existing_urls.add(normalized_url)

                    execute_query(
                        f"""
                        INSERT INTO {TABLE_MATCH_VIDEOS} (
                            match_id,
                            match_label,
                            video_title,
                            youtube_url,
                            standalone_topic_text,
                            standalone_pro_team,
                            standalone_con_team,
                            is_visible,
                            display_order,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            NULL,
                            :match_label,
                            :video_title,
                            :youtube_url,
                            :standalone_topic_text,
                            :standalone_pro_team,
                            :standalone_con_team,
                            :is_visible,
                            :display_order,
                            :created_at,
                            :updated_at
                        )
                        """,
                        {
                            "match_label": match_label or None,
                            "video_title": title,
                            "youtube_url": normalized_url,
                            "standalone_topic_text": topic_text or None,
                            "standalone_pro_team": pro_team or None,
                            "standalone_con_team": con_team or None,
                            "is_visible": bool(is_visible),
                            "display_order": display_order,
                            "created_at": now_hk,
                            "updated_at": now_hk,
                        },
                    )
                    inserted += 1
                st.session_state["video_action_message"] = {
                    "type": "success" if inserted else "warning",
                    "content": f"已匯入 {inserted} 條片段，略過格式無效 {skipped} 條，略過重複 {duplicated} 條。",
                }
                st.rerun()

    videos_df = query_params(
        f"""
        SELECT
            v.id,
            v.match_id,
            v.match_label,
            v.video_title,
            v.youtube_url,
            v.standalone_topic_text,
            v.standalone_pro_team,
            v.standalone_con_team,
            COALESCE(v.is_visible, TRUE) AS is_visible,
            v.display_order,
            v.created_at,
            v.updated_at,
            COALESCE(NULLIF(v.match_label, ''), v.match_id) AS match_display,
            COALESCE(NULLIF(m.topic_text, ''), NULLIF(v.standalone_topic_text, '')) AS topic_text,
            COALESCE(NULLIF(m.pro_team, ''), NULLIF(v.standalone_pro_team, '')) AS pro_team,
            COALESCE(NULLIF(m.con_team, ''), NULLIF(v.standalone_con_team, '')) AS con_team,
            m.match_date,
            m.match_time
        FROM {TABLE_MATCH_VIDEOS} v
        LEFT JOIN {TABLE_MATCHES} m ON v.match_id = m.match_id
        ORDER BY m.match_date DESC NULLS LAST,
                 m.match_time DESC NULLS LAST,
                 v.display_order ASC,
                 v.created_at DESC
        """
    )

    st.divider()
    st.subheader("已登記片段")

    if videos_df.empty:
        st.info("目前未有已登記的比賽片段。")
        return

    visible_count = int(videos_df["is_visible"].sum())
    metric_col1, metric_col2, metric_col3 = st.columns(3)
    metric_col1.metric("全部片段", len(videos_df))
    metric_col2.metric("會員頁顯示", visible_count)
    metric_col3.metric("已隱藏", len(videos_df) - visible_count)

    display_df = videos_df.copy()
    display_df["source_type"] = display_df["match_id"].apply(lambda x: "舊比賽" if not _clean_text(x) else "現有場次")
    display_df["is_visible"] = display_df["is_visible"].map({True: "顯示", False: "隱藏"})
    display_df = display_df.rename(columns={
        "id": "編號",
        "source_type": "類型",
        "match_display": "場次",
        "video_title": "片段標題",
        "youtube_url": "YouTube 連結",
        "is_visible": "狀態",
        "display_order": "排序",
        "topic_text": "辯題",
        "pro_team": "正方",
        "con_team": "反方",
        "created_at": "建立時間",
        "updated_at": "更新時間",
    })
    st.dataframe(
        display_df[["編號", "類型", "場次", "片段標題", "狀態", "排序", "辯題", "正方", "反方", "YouTube 連結", "建立時間", "更新時間"]],
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.subheader("編輯片段")

    video_ids = videos_df["id"].astype(int).tolist()
    selected_video_id = st.selectbox(
        "選擇片段",
        options=video_ids,
        format_func=lambda video_id: (
            f"{video_id} - "
            f"{videos_df[videos_df['id'] == video_id].iloc[0]['match_display']} - "
            f"{videos_df[videos_df['id'] == video_id].iloc[0]['video_title']}"
        ),
    )
    selected_row = videos_df[videos_df["id"] == selected_video_id].iloc[0]
    current_match = _clean_text(selected_row["match_id"])
    default_edit_source = SOURCE_EXISTING if current_match in match_options else SOURCE_STANDALONE
    edit_options = _source_options()
    if default_edit_source not in edit_options:
        default_edit_source = SOURCE_STANDALONE

    with st.container(border=True):
        edit_source = st.radio(
            "片段所屬比賽",
            options=edit_options,
            index=edit_options.index(default_edit_source),
            horizontal=True,
            key=f"edit_video_source_{selected_video_id}",
        )
        with st.form("edit_video_form"):
            edit_match = None
            edit_match_label = _clean_text(selected_row["match_label"]) or _clean_text(selected_row["match_display"])
            edit_topic_text = _clean_text(selected_row["standalone_topic_text"]) or _clean_text(selected_row["topic_text"])
            edit_pro_team = _clean_text(selected_row["standalone_pro_team"]) or _clean_text(selected_row["pro_team"])
            edit_con_team = _clean_text(selected_row["standalone_con_team"]) or _clean_text(selected_row["con_team"])

            if edit_source == SOURCE_EXISTING:
                selected_match_index = match_options.index(current_match) if current_match in match_options else 0
                edit_match = st.selectbox(
                    "比賽場次",
                    options=match_options,
                    index=selected_match_index,
                    format_func=_format_match_option,
                )
            else:
                edit_match_label = st.text_input("比賽名稱／場次", value=edit_match_label)
                edit_topic_text = st.text_input("辯題（可留空）", value=edit_topic_text)
                edit_team_col1, edit_team_col2 = st.columns(2)
                with edit_team_col1:
                    edit_pro_team = st.text_input("正方隊名（可留空）", value=edit_pro_team)
                with edit_team_col2:
                    edit_con_team = st.text_input("反方隊名（可留空）", value=edit_con_team)

            edit_title = st.text_input("片段標題", value=str(selected_row["video_title"]))
            edit_url = st.text_input("YouTube 連結", value=str(selected_row["youtube_url"]))
            edit_order = st.number_input(
                "排序",
                min_value=0,
                step=1,
                value=_to_int(selected_row["display_order"]),
            )
            edit_visible = st.checkbox("在會員重溫頁顯示", value=bool(selected_row["is_visible"]))
            update_video = st.form_submit_button("儲存片段資料", type="primary", use_container_width=True)

        if update_video:
            errors = _validate_video_input(edit_title, edit_url, edit_source, edit_match_label)
            if errors:
                for error in errors:
                    st.error(error)
            else:
                match_params = _resolve_match_params(
                    edit_source,
                    edit_match,
                    edit_match_label,
                    edit_topic_text,
                    edit_pro_team,
                    edit_con_team,
                )
                execute_query(
                    f"""
                    UPDATE {TABLE_MATCH_VIDEOS}
                    SET match_id = :match_id,
                        match_label = :match_label,
                        video_title = :video_title,
                        youtube_url = :youtube_url,
                        standalone_topic_text = :standalone_topic_text,
                        standalone_pro_team = :standalone_pro_team,
                        standalone_con_team = :standalone_con_team,
                        is_visible = :is_visible,
                        display_order = :display_order,
                        updated_at = :updated_at
                    WHERE id = :id
                    """,
                    {
                        **match_params,
                        "video_title": edit_title.strip(),
                        "youtube_url": _normalize_youtube_url(edit_url),
                        "is_visible": bool(edit_visible),
                        "display_order": int(edit_order),
                        "updated_at": now_hk,
                        "id": int(selected_video_id),
                    },
                )
                st.session_state["video_action_message"] = {"type": "success", "content": "片段資料已更新。"}
                st.rerun()

    with st.container(border=True):
        st.subheader("章節時間表")
        st.caption("時間可輸入秒數、mm:ss 或 hh:mm:ss；未啟用的章節不會在前台顯示。")
        chapter_df = query_params(
            f"""
            SELECT chapter_label, start_seconds
            FROM {TABLE_VIDEO_CHAPTERS}
            WHERE video_id = :video_id
            """,
            {"video_id": int(selected_video_id)},
        )
        chapter_map = {
            str(row["chapter_label"]): int(row["start_seconds"])
            for _, row in chapter_df.iterrows()
        } if not chapter_df.empty else {}

        with st.form("chapter_form"):
            chapter_inputs = []
            for chapter_index, chapter_label in enumerate(CHAPTER_LABELS):
                chapter_col1, chapter_col2 = st.columns([1, 2])
                with chapter_col1:
                    enabled = st.checkbox(
                        chapter_label,
                        value=chapter_label in chapter_map,
                        key=f"chapter_enabled_{selected_video_id}_{chapter_label}",
                    )
                with chapter_col2:
                    current_value = _seconds_to_label(chapter_map[chapter_label]) if chapter_label in chapter_map else ""
                    time_text = st.text_input(
                        "開始時間",
                        value=current_value,
                        key=f"chapter_time_{selected_video_id}_{chapter_label}",
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
                start_seconds = _parse_time_to_seconds(time_text)
                if start_seconds is None:
                    invalid_labels.append(chapter_label)
                else:
                    chapter_values.append((chapter_label, chapter_index, start_seconds))

            if invalid_labels:
                st.error("以下章節時間格式無效：" + "、".join(invalid_labels))
            else:
                execute_query(
                    f"DELETE FROM {TABLE_VIDEO_CHAPTERS} WHERE video_id = :video_id",
                    {"video_id": int(selected_video_id)},
                )
                for chapter_label, chapter_index, start_seconds in chapter_values:
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
                            "start_seconds": int(start_seconds),
                            "display_order": int(chapter_index),
                            "updated_at": now_hk,
                        },
                    )
                st.session_state["video_action_message"] = {"type": "success", "content": "章節時間表已更新。"}
                st.rerun()

    with st.expander("危險操作", expanded=st.session_state["delete_confirm_video_id"] == selected_video_id):
        st.warning(f"刪除片段「{selected_row['video_title']}」後無法復原。")
        if st.session_state["delete_confirm_video_id"] != selected_video_id:
            if st.button("刪除片段", type="secondary", key="delete_video_btn", use_container_width=True):
                st.session_state["delete_confirm_video_id"] = selected_video_id
                st.rerun()
        else:
            del_col1, del_col2 = st.columns(2)
            with del_col1:
                if st.button("確定刪除", type="primary", key="confirm_delete_video_btn", use_container_width=True):
                    execute_query(
                        f"DELETE FROM {TABLE_MATCH_VIDEOS} WHERE id = :id",
                        {"id": int(selected_video_id)},
                    )
                    st.session_state["video_action_message"] = {"type": "success", "content": "片段已刪除。"}
                    st.session_state["delete_confirm_video_id"] = None
                    st.rerun()
            with del_col2:
                if st.button("取消", type="secondary", key="cancel_delete_video_btn", use_container_width=True):
                    st.session_state["delete_confirm_video_id"] = None
                    st.rerun()


if __name__ == "__main__":
    st.header("比賽片段管理")
    render_page_guidance(
        [
            "使用賽會人員密碼登入後，可為每場比賽新增多條 YouTube 片段連結。",
            "片段可連結現有場次；舊比賽未有場次資料時，可手動輸入比賽名稱、辯題及隊名。",
            "只有標記為顯示的片段會出現在會員重溫頁，排序數字較小的片段會較先顯示。",
        ],
    )

    if not check_admin():
        st.stop()

    render_video_admin()
