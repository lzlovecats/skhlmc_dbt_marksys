import streamlit as st

from functions import ensure_match_videos_table, query_params, render_page_guidance
from schema import TABLE_MATCHES, TABLE_MATCH_VIDEOS


def _format_match_time(row):
    match_date = _format_value(row.get("match_date"), "")
    match_time = _format_value(row.get("match_time"), "")
    parts = []
    if match_date:
        parts.append(match_date)
    if match_time:
        parts.append(match_time[:5])
    return " ".join(parts) if parts else "未設定日期時間"


def _format_value(value, default="未設定"):
    if value is None:
        return default
    text = str(value).strip()
    if not text or text.lower() in ("none", "nan", "nat"):
        return default
    return text


st.header("比賽片段重溫")
if st.button("🔄重新整理"):
    st.cache_data.clear()
    st.rerun()

render_page_guidance(
    [
        "此頁列出賽會已公開的 YouTube 比賽片段連結。",
        "可按場次、隊伍、辯題或片段標題搜尋。",
        "按下片段連結後會離開本系統並開啟 YouTube。",
    ],
)

if not ensure_match_videos_table():
    st.error("未能建立或讀取比賽片段資料表，請稍後再試或聯絡開發人員。")
    st.stop()

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
        COALESCE(NULLIF(m.con_team, ''), NULLIF(v.standalone_con_team, '')) AS con_team
    FROM {TABLE_MATCH_VIDEOS} v
    LEFT JOIN {TABLE_MATCHES} m ON v.match_id = m.match_id
    WHERE COALESCE(v.is_visible, TRUE) = TRUE
    ORDER BY m.match_date DESC NULLS LAST,
             m.match_time DESC NULLS LAST,
             v.display_order ASC,
             v.created_at DESC
    """
)

if videos_df.empty:
    st.info("目前未有可重溫的比賽片段。")
    st.stop()

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

st.caption(f"共找到 {len(filtered_df)} 條比賽片段")

if filtered_df.empty:
    st.info("沒有符合條件的比賽片段。請調整搜尋關鍵字或篩選條件後再試。")
    st.stop()

for _, row in filtered_df.iterrows():
    with st.container(border=True):
        st.markdown(f"### {row['video_title']}")
        st.caption(f"場次：{_format_value(row['match_display'])}｜{_format_match_time(row)}")
        st.write(f"**辯題：** {_format_value(row['topic_text'])}")
        st.write(f"**正方：** {_format_value(row['pro_team'])}")
        st.write(f"**反方：** {_format_value(row['con_team'])}")
        st.link_button("開啟 YouTube 片段", row["youtube_url"], use_container_width=True)
