import streamlit as st
import numpy as np
import pandas as pd

st.set_page_config(page_title="聖呂中辯電子分紙系統", layout="wide", page_icon="📑")

page_judging = st.Page("judging.py", title="電子分紙（評判用）")
page_match_mgmt = st.Page("match_info.py", title="賽事資料管理（賽會人員用）")
page_mgmt = st.Page("management.py", title="分數管理（賽會人員用）")
page_score_sheet = st.Page("review.py", title="查閱比賽分紙")

pg = st.navigation([page_judging, page_match_mgmt, page_mgmt, page_score_sheet])

if st.session_state.get("admin_logged_in"):
    with st.sidebar:
        if st.button("結束賽會人員登入", use_container_width=True):
            st.session_state["admin_logged_in"] = False
            st.rerun()

with st.sidebar:
    st.caption("🛠️ 系統版本：1.7.3 (Direct)")
    st.caption("🧑‍💻 Developed by lzlovecats @ 2026")

pg.run()
