import streamlit as st
import pandas as pd
from functions import get_connection
st.header("查閱辯題庫")

try:
    conn = get_connection()
    df = conn.query("SELECT * FROM topics", ttl=0)
except Exception as e:
    st.error(f"連線錯誤: {e}")
    st.stop()
st.dataframe(df, use_container_width=True, hide_index=True)

st.subheader("類別分佈")
if not df.empty and "category" in df.columns:
    cat_counts = df["category"].value_counts().reset_index()
    cat_counts.columns = ["類別", "辯題數量"]
    total = len(df)
    cat_counts["佔比"] = cat_counts["辯題數量"].apply(lambda x: f"{x/total*100:.1f}%")
    st.dataframe(cat_counts, use_container_width=True, hide_index=True)