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