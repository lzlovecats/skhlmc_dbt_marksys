import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import streamlit as st

from functions import check_admin, ensure_match_videos_table, execute_query, query_params, render_page_guidance
from schema import TABLE_MATCHES, TABLE_MATCH_VIDEOS


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
    if not ensure_match_videos_table():
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
            is_visible = st.checkbox("在公開重溫頁顯示", value=True, key="add_is_visible")
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
    metric_col2.metric("公開顯示", visible_count)
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
            edit_visible = st.checkbox("在公開重溫頁顯示", value=bool(selected_row["is_visible"]))
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
            "只有標記為顯示的片段會出現在公開重溫頁，排序數字較小的片段會較先顯示。",
        ],
    )

    if not check_admin():
        st.stop()

    render_video_admin()
