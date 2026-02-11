import streamlit as st
import pandas as pd
from functions import get_connection
st.header("查閱辯題庫")

try:
    ss = get_connection()
    ws = ss.worksheet("Topic")
except Exception as e:
    st.error(f"連線錯誤: {e}")
    st.stop()

df = pd.DataFrame(ws.get_all_records())
st.dataframe(df, use_container_width=True, hide_index=True)