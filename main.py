import streamlit as st
import numpy as np
import pandas as pd

page_match_mgmt = st.Page("match_info.py", title="賽事資料管理系統（賽會人員用）")
page_judging = st.Page("judging.py", title="電子分紙（評判用）")
page_mgmt = st.Page("management.py", title="分數管理（賽會人員用）")

def check_admin():
    if "admin_logged_in" not in st.session_state:
        st.session_state["admin_logged_in"] = False
	if not st.session_state["admin_logged_in"]:
        st.subheader("賽會人員登入")
        pwd = st.text_input("請輸入賽會人員密碼", type="password")
        if st.button("登入"):
            if pwd == st.secrets["admin_password"]:
                st.session_state["admin_logged_in"] = True
                st.rerun()
            else:
                st.error("密碼錯誤")
        return False
    return True

pg = st.navigation([page_judging, page_match_mgmt, page_mgmt])
st.set_page_config(page_title="電子分紙系統（Beta）", layout="wide")
if "賽會人員用" in st.active_navigation_details["title"]:
    if not check_admin():
        st.stop()
pg.run()