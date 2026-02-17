import streamlit as st
from functions import return_user_manual, return_rules

# Set up basic structure of the webpage
st.set_page_config(page_title="聖呂中辯電子分紙系統", layout="wide", page_icon="📑")

@st.dialog("聖呂中辯電子分紙系統：用戶使用手冊")
def show_manual():
    manual_content = return_user_manual()
    st.markdown(manual_content)

@st.dialog("校園隨想辯論比賽：賽規")
def show_rules():
    rules_content = return_rules()
    st.markdown(rules_content)


# Define pages
page_judging = st.Page("judging.py", title="電子分紙（評判用）")
page_match_mgmt = st.Page("match_info.py", title="比賽場次管理（賽會人員用）")
page_mgmt = st.Page("management.py", title="查閱比賽結果（賽會人員用）")
page_vote = st.Page("vote.py", title="辯題徵集系統（內部用）")
page_db_mgmt = st.Page("db_mgmt.py", title="辯題庫管理（內部用）")
page_score_sheet = st.Page("review.py", title="查閱比賽分紙（比賽隊伍用）")
page_open_db = st.Page("open_db.py", title="查閱辯題庫（一般人員用）")

# Arrange pages
pg = st.navigation([page_judging, page_match_mgmt, page_mgmt, page_vote, page_db_mgmt, page_score_sheet, page_open_db])

# Show logout when admin logged in
if st.session_state.get("admin_logged_in"):
    with st.sidebar:
        st.write("")
        if st.button("登出賽會人員帳戶", use_container_width=True):
            st.session_state["admin_logged_in"] = False
            st.rerun()

# Show manual
with st.sidebar:
    if st.button("📖 閱讀使用手冊", use_container_width=True):
        show_manual()

with st.sidebar:
    if st.button("📋 查看賽規", use_container_width=True):
        show_rules()

# Show caption
with st.sidebar:
    st.caption("🛠️ 系統版本：1.12.0 (Indirect)")
    st.caption("🧑‍💻 Developed by lzlovecats @ 2026")

pg.run()
