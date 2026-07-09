import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from auth import require_committee
from functions import (
    clear_field_draft,
    ensure_match_photos_table,
    execute_query,
    query_params,
    render_page_guidance,
)
from schema import TABLE_MATCHES, TABLE_MATCH_PHOTOS, TABLE_MATCH_VIDEOS


OTHER_ALBUM = "其他相片"


def _format_value(value, default="未設定"):
    if value is None:
        return default
    text = str(value).strip()
    if not text or text.lower() in ("none", "nan", "nat"):
        return default
    return text


def _format_time(value):
    if hasattr(value, "strftime"):
        return value.strftime("%m-%d %H:%M")
    return str(value or "")[:16]


def _to_bytes(value):
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return bytes(value)


def _queue_photo_toast(message, icon=None):
    st.session_state["_photo_toast"] = {"message": message, "icon": icon}


def _flush_photo_toast():
    toast = st.session_state.pop("_photo_toast", None)
    if toast:
        st.toast(toast["message"], icon=toast.get("icon"))


def _load_album_options():
    videos_df = query_params(
        f"""
        SELECT DISTINCT ON (album_label)
            id AS match_video_id,
            album_label,
            match_date,
            match_time
        FROM (
            SELECT
                v.id,
                COALESCE(NULLIF(v.match_label, ''), NULLIF(v.match_id, ''), v.video_title) AS album_label,
                m.match_date,
                m.match_time,
                v.display_order,
                v.created_at
            FROM {TABLE_MATCH_VIDEOS} v
            LEFT JOIN {TABLE_MATCHES} m ON v.match_id = m.match_id
            WHERE COALESCE(v.is_visible, TRUE) = TRUE
        ) albums
        WHERE album_label IS NOT NULL
          AND album_label != ''
        ORDER BY album_label, match_date DESC NULLS LAST, match_time DESC NULLS LAST, display_order ASC, created_at DESC
        """
    )
    options = [{"label": OTHER_ALBUM, "video_id": None}]
    for _, row in videos_df.iterrows():
        album_label = str(row["album_label"])
        if album_label != OTHER_ALBUM:
            options.append({"label": album_label, "video_id": int(row["match_video_id"])})
    return options


st.header("比賽圖片回顧")
render_page_guidance(
    [
        "所有內部委員會成員都可以上載及查看每場賽事的精華圖片。",
        "上載時可選擇「比賽片段」已有的場次；未能歸類的圖片可放入「其他相片」。",
    ],
)

user_id = require_committee()

if not ensure_match_photos_table():
    st.error("未能建立或讀取比賽圖片資料表，請稍後再試或聯絡開發人員。")
    st.stop()

_flush_photo_toast()

album_options = _load_album_options()
album_labels = [option["label"] for option in album_options]
album_video_ids = {option["label"]: option["video_id"] for option in album_options}

with st.container(border=True):
    st.subheader("上載精華圖片")
    with st.form("photo_upload_form", clear_on_submit=True):
        selected_album = st.selectbox("所屬場次", options=album_labels)
        photo_date = st.date_input("相片日期（可留空）", value=None, format="YYYY-MM-DD")
        photo_title = st.text_input("圖片標題（可留空）", key="photo_upload_title")
        caption = st.text_area("圖片說明（可留空）", key="photo_upload_caption")
        uploaded_files = st.file_uploader(
            "選擇圖片",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
        )
        upload_photos = st.form_submit_button("上載圖片", type="primary", use_container_width=True)

    if upload_photos:
        if not uploaded_files:
            st.warning("請先選擇圖片。")
        else:
            now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
            for uploaded_file in uploaded_files:
                execute_query(
                    f"""
                    INSERT INTO {TABLE_MATCH_PHOTOS} (
                        match_video_id, album_label, photo_date, photo_title, caption,
                        file_name, mime_type, image_data, uploaded_by, created_at
                    )
                    VALUES (
                        :match_video_id, :album_label, :photo_date, :photo_title, :caption,
                        :file_name, :mime_type, :image_data, :uploaded_by, :created_at
                    )
                    """,
                    {
                        "match_video_id": album_video_ids.get(selected_album),
                        "album_label": selected_album,
                        "photo_date": photo_date,
                        "photo_title": photo_title.strip() or None,
                        "caption": caption.strip() or None,
                        "file_name": uploaded_file.name,
                        "mime_type": uploaded_file.type or "image/jpeg",
                        "image_data": uploaded_file.getvalue(),
                        "uploaded_by": user_id,
                        "created_at": now_hk,
                    },
                )
            clear_field_draft("photo_upload_title", "photo_upload_caption")
            _queue_photo_toast("圖片已成功上載。", icon="☑️")
            st.rerun()

st.divider()
st.subheader("圖片回顧")

photos_df = query_params(
    f"""
    SELECT
        id,
        album_label,
        photo_date,
        photo_title,
        caption,
        file_name,
        mime_type,
        image_data,
        uploaded_by,
        created_at
    FROM {TABLE_MATCH_PHOTOS}
    ORDER BY
        photo_date DESC NULLS LAST,
        CASE WHEN album_label = :other_album THEN 1 ELSE 0 END,
        album_label ASC,
        created_at DESC
    """,
    {"other_album": OTHER_ALBUM},
)

if photos_df.empty:
    st.info("目前未有已上載的比賽圖片。")
    st.stop()

filter_options = ["全部"] + sorted(photos_df["album_label"].dropna().astype(str).unique().tolist())
sort_options = [
    "相片日期（新至舊）",
    "相片日期（舊至新）",
    "上載時間（新至舊）",
    "上載時間（舊至新）",
]
filter_col, sort_col, view_col = st.columns([2, 2, 1])
selected_filter = filter_col.selectbox("場次篩選", options=filter_options)
selected_sort = sort_col.selectbox("排序", options=sort_options)
display_mode = view_col.segmented_control("顯示（僅適用於電腦版）", options=["縮圖牆", "標準"], default="縮圖牆")
search_term = st.text_input("搜尋", placeholder="輸入場次、標題、說明或上載者")

filtered_df = photos_df.copy()
if selected_filter != "全部":
    filtered_df = filtered_df[filtered_df["album_label"].astype(str) == selected_filter]
if search_term:
    keyword = search_term.strip().lower()
    search_cols = ["album_label", "photo_date", "photo_title", "caption", "uploaded_by", "file_name"]
    search_text = filtered_df[search_cols].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
    filtered_df = filtered_df[search_text.str.contains(keyword, regex=False)]

if selected_sort == "相片日期（舊至新）":
    filtered_df = filtered_df.sort_values(
        by=["photo_date", "created_at"],
        ascending=[True, False],
        na_position="last",
        kind="mergesort",
    )
elif selected_sort == "上載時間（新至舊）":
    filtered_df = filtered_df.sort_values(by=["created_at"], ascending=[False], kind="mergesort")
elif selected_sort == "上載時間（舊至新）":
    filtered_df = filtered_df.sort_values(by=["created_at"], ascending=[True], kind="mergesort")
else:
    filtered_df = filtered_df.sort_values(
        by=["photo_date", "created_at"],
        ascending=[False, False],
        na_position="last",
        kind="mergesort",
    )

if filtered_df.empty:
    st.info("沒有符合條件的圖片。請調整搜尋關鍵字或篩選條件後再試。")
    st.stop()

st.caption(f"共 {len(filtered_df)} 張圖片")
compact_view = display_mode == "縮圖牆"
photo_cols = st.columns(5 if compact_view else 3)
for index, (_, photo) in enumerate(filtered_df.iterrows()):
    with photo_cols[index % len(photo_cols)]:
        image_bytes = _to_bytes(photo["image_data"])
        title = _format_value(photo["photo_title"], photo["file_name"])
        photo_date = _format_value(photo["photo_date"], "")
        date_text = f" ｜ {photo_date}" if photo_date else ""
        if compact_view:
            st.image(image_bytes, use_container_width=True)
            st.caption(title if len(title) <= 18 else f"{title[:18]}...")
            if photo_date:
                st.caption(photo_date)
            with st.popover("詳情", use_container_width=True):
                st.write(title)
                st.caption(f"{photo['album_label']}{date_text} ｜ {_format_time(photo['created_at'])} ｜ {photo['uploaded_by']}")
                if photo["caption"]:
                    st.write(photo["caption"])
                st.download_button(
                    "下載原圖",
                    data=image_bytes,
                    file_name=photo["file_name"] or f"match-photo-{int(photo['id'])}.jpg",
                    mime=photo["mime_type"] or "image/jpeg",
                    use_container_width=True,
                )
        else:
            st.image(image_bytes, caption=title, use_container_width=True)
            st.caption(f"{photo['album_label']}{date_text} ｜ {_format_time(photo['created_at'])} ｜ {photo['uploaded_by']}")
            if photo["caption"]:
                st.write(photo["caption"])
            with st.popover("下載", use_container_width=True):
                st.download_button(
                    "下載原圖",
                    data=image_bytes,
                    file_name=photo["file_name"] or f"match-photo-{int(photo['id'])}.jpg",
                    mime=photo["mime_type"] or "image/jpeg",
                    use_container_width=True,
                )
