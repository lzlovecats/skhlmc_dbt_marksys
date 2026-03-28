import streamlit as st
import pandas as pd
from functions import get_connection
st.header("查閱辯題庫")

try:
    conn = get_connection()
    df = conn.query("SELECT * FROM topics", ttl=60)
    vote_df = conn.query("SELECT category, status FROM topic_votes", ttl=60)
except Exception as e:
    st.error(f"連線錯誤: {e}")
    st.stop()

if df.empty:
    st.info("辯題庫目前為空。")
    st.stop()

# 難度標準 mapping
DIFFICULTY_OPTIONS = {
    1: "Lv1 — 概念日常",
    2: "Lv2 — 一般議題",
    3: "Lv3 — 進階專業"
}
if "difficulty" in df.columns:
    df["difficulty_label"] = df["difficulty"].map(DIFFICULTY_OPTIONS)

col1, col2, col3 = st.columns(3)

with col1:
    authors = ["全部"] + sorted(df["author"].dropna().unique().tolist())
    sel_author = st.selectbox("👤 作者篩選", authors)

with col2:
    categories = ["全部"] + sorted(df["category"].dropna().unique().tolist())
    sel_category = st.selectbox("🏷️ 類別篩選", categories)

with col3:
    if "difficulty_label" in df.columns:
        difficulties = ["全部"] + sorted(df["difficulty_label"].dropna().unique().tolist())
    else:
        difficulties = ["全部"]
    sel_difficulty = st.selectbox("⭐ 難度篩選", difficulties)

# 進行篩選
filtered_df = df.copy()
if sel_author != "全部":
    filtered_df = filtered_df[filtered_df["author"] == sel_author]
if sel_category != "全部":
    filtered_df = filtered_df[filtered_df["category"] == sel_category]
if sel_difficulty != "全部":
    filtered_df = filtered_df[filtered_df["difficulty_label"] == sel_difficulty]

# 顯示的欄位重整理
display_df = filtered_df.copy()
if "difficulty_label" in display_df.columns:
    display_df = display_df.drop(columns=["difficulty"])
    display_df = display_df.rename(columns={"difficulty_label": "difficulty"})
display_columns = ["topic", "author", "category", "difficulty"]
# 確保欄位存在
display_columns = [c for c in display_columns if c in display_df.columns]
display_df = display_df[display_columns]
display_df = display_df.rename(columns={
    "topic": "辯題",
    "author": "作者",
    "category": "類別",
    "difficulty": "難度"
})

st.caption(f"共找到 {len(display_df)} 條符合條件的辯題")
st.dataframe(display_df, use_container_width=True, hide_index=True)

st.divider()
st.subheader("📊 類別分佈 (所有辯題)")

if "category" in df.columns:
    cat_counts = df["category"].value_counts().reset_index()
    cat_counts.columns = ["類別", "辯題數量"]
    total = len(df)
    cat_counts["佔比"] = cat_counts["辯題數量"].apply(lambda x: f"{x/total*100:.1f}%")
    
    col_chart, col_table = st.columns([2, 1])
    
    with col_chart:
        chart_data = cat_counts.set_index("類別")["辯題數量"]
        st.bar_chart(chart_data)
        
    with col_table:
        st.dataframe(cat_counts, use_container_width=True, hide_index=True)

if "difficulty_label" in df.columns:
    st.divider()
    st.subheader("📈 難度分佈 (所有辯題)")

    diff_counts = df["difficulty_label"].fillna("未分類").value_counts().reset_index()
    diff_counts.columns = ["難度", "辯題數量"]
    total = len(df)
    diff_counts["佔比"] = diff_counts["辯題數量"].apply(lambda x: f"{x/total*100:.1f}%")

    col_chart, col_table = st.columns([2, 1])

    with col_chart:
        chart_data = diff_counts.set_index("難度")["辯題數量"]
        st.bar_chart(chart_data)

    with col_table:
        st.dataframe(diff_counts, use_container_width=True, hide_index=True)

if not vote_df.empty and "category" in vote_df.columns and "status" in vote_df.columns:
    resolved_vote_df = vote_df[vote_df["status"].isin(["passed", "rejected"])].copy()
    if not resolved_vote_df.empty:
        st.divider()
        st.subheader("🗳️ 類別投票通過率")
        st.caption("只計已完成表決的辯題動議（已通過 + 已否決）。")

        resolved_vote_df["category"] = resolved_vote_df["category"].fillna("未分類")
        cat_vote_stats = resolved_vote_df.groupby("category").agg(
            動議數量=("status", "count"),
            通過數=("status", lambda x: (x == "passed").sum())
        ).reset_index()
        cat_vote_stats["投票通過率"] = cat_vote_stats["通過數"] / cat_vote_stats["動議數量"]
        cat_vote_stats = cat_vote_stats.sort_values(
            by=["投票通過率", "動議數量"],
            ascending=[False, False]
        )

        display_vote_stats = cat_vote_stats.rename(columns={"category": "類別"}).copy()
        display_vote_stats["投票通過率"] = display_vote_stats["投票通過率"].apply(lambda x: f"{x:.1%}")

        col_chart, col_table = st.columns([2, 1])

        with col_chart:
            chart_data = cat_vote_stats.set_index("category")["投票通過率"] * 100
            st.bar_chart(chart_data)

        with col_table:
            st.dataframe(display_vote_stats, use_container_width=True, hide_index=True)
