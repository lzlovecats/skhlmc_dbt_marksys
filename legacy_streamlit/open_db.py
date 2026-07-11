import streamlit as st
from core import open_db_logic as od

st.header("查閱辯題庫")


@st.cache_data(ttl=60)
def get_open_db_data():
    return od.fetch_open_db_data()


try:
    topics_df, topic_vote_stats_df = get_open_db_data()
except Exception as e:
    st.error(f"連線錯誤: {e}")
    st.stop()

if topics_df.empty:
    st.info("辯題庫目前為空。")
    st.stop()

topics_df = od.with_difficulty_label(topics_df)

search_term = st.text_input("🔍 搜尋辯題", placeholder="輸入關鍵字搜尋...")

col1, col2, col3 = st.columns(3)

with col1:
    sel_author = st.selectbox("👤 作者篩選", od.filter_options(topics_df)["authors"])

with col2:
    sel_category = st.selectbox("🏷️ 類別篩選", od.filter_options(topics_df)["categories"])

with col3:
    sel_difficulty = st.selectbox("⭐ 難度篩選", od.filter_options(topics_df)["difficulties"])

# 進行篩選
filtered_df = od.filter_topics(topics_df, search_term, sel_author, sel_category, sel_difficulty)

# 顯示的欄位重整理
display_df = od.display_topics(filtered_df)

st.caption(f"共找到 {len(display_df)} 條符合條件的辯題")
if display_df.empty:
    st.info("沒有符合條件的辯題。請調整搜尋關鍵字或篩選條件後再試。")
else:
    st.dataframe(display_df, width="stretch", hide_index=True)

st.divider()
st.subheader("📊 類別分佈 (所有辯題)")

if "category" in topics_df.columns:
    cat_counts = od.category_distribution(topics_df)
    
    chart_data = cat_counts.set_index("類別")["辯題數量"]
    st.bar_chart(chart_data)
    st.dataframe(cat_counts, width="stretch", hide_index=True)

if "difficulty_label" in topics_df.columns:
    st.divider()
    st.subheader("📈 難度分佈 (所有辯題)")

    diff_counts = od.difficulty_distribution(topics_df)

    chart_data = diff_counts.set_index("難度")["辯題數量"]
    st.bar_chart(chart_data)
    st.dataframe(diff_counts, width="stretch", hide_index=True)

if not topic_vote_stats_df.empty and "category" in topic_vote_stats_df.columns and "status" in topic_vote_stats_df.columns:
    display_vote_stats = od.category_vote_pass_rate(topic_vote_stats_df)
    if not display_vote_stats.empty:
        st.divider()
        st.subheader("🗳️ 類別投票通過率")
        st.caption("只計已完成表決的辯題動議（已通過 + 已否決）。")

        chart_data = display_vote_stats.set_index("類別")["投票通過率"].str.rstrip("%").astype(float)
        st.bar_chart(chart_data)
        st.dataframe(display_vote_stats, width="stretch", hide_index=True)
