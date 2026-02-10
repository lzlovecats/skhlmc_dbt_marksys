import streamlit as st

# Set up basic structure of the webpage
st.set_page_config(page_title="聖呂中辯電子分紙系統", layout="wide", page_icon="📑")

# Define pages
page_judging = st.Page("judging.py", title="電子分紙（評判用）")
page_match_mgmt = st.Page("match_info.py", title="比賽場次管理（賽會人員用）")
page_mgmt = st.Page("management.py", title="查閱比賽結果（賽會人員用）")
page_score_sheet = st.Page("review.py", title="查閱比賽分紙（一般人員用）")

# Arrange pages
pg = st.navigation([page_judging, page_match_mgmt, page_mgmt, page_score_sheet])

# Show logout when admin logged in
if st.session_state.get("admin_logged_in"):
    with st.sidebar:
        st.write("")
        if st.button("登出賽會人員帳戶", use_container_width=True):
            st.session_state["admin_logged_in"] = False
            st.rerun()

# Show caption
with st.sidebar:
    st.caption("🛠️ 系統版本：1.8.4 (Indirect)")
    st.caption("🧑‍💻 Developed by lzlovecats @ 2026")

pg.run()
