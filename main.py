import streamlit as st
from functions import get_registration_status, is_maintenance_mode, render_maintenance_notice, show_manual, show_rules

# Set up basic structure of the webpage
st.set_page_config(page_title="聖呂中辯電子分紙系統", layout="wide", page_icon="📑")

if is_maintenance_mode():
    st.title("聖呂中辯電子分紙系統")
    render_maintenance_notice()
    st.stop()

# Define pages
page_home = st.Page("home.py", title="主頁", icon="🏠", default=True)
page_judging = st.Page("judging.py", title="電子分紙")
page_match_mgmt = st.Page("match_info.py", title="比賽場次管理")
page_mgmt = st.Page("management.py", title="查閱比賽結果")
page_registration_admin = st.Page("registration_admin.py", title="比賽報名管理")
page_db_mgmt = st.Page("db_mgmt.py", title="資料庫管理控制台")
page_draw_schedule = st.Page("draw_match_schedule.py", title="抽取賽程")
page_score_sheet = st.Page("review.py", title="查閱比賽分紙")
page_video_replay = st.Page("video_replay.py", title="比賽片段重溫")
page_video_admin = st.Page("video_admin.py", title="比賽片段管理")
page_registration = st.Page("registration.py", title="比賽報名", url_path="registration")
page_open_db = st.Page("open_db.py", title="查閱辯題庫")
page_vote = st.Page("vote.py", title="辯題徵集、投票及罷免", url_path="vote")
page_dev_settings = st.Page("dev_settings.py", title="開發者設定")

registration_status = get_registration_status()
public_pages = [page_video_replay, page_open_db]
if registration_status["is_open"]:
    public_pages.insert(0, page_registration)

# Arrange pages by user role
pg = st.navigation({
    "": [page_home],
    "評判": [page_judging],
    "參賽隊伍": [page_score_sheet],
    "一般人員": public_pages,
    "賽會人員": [page_registration_admin, page_match_mgmt, page_video_admin, page_mgmt, page_db_mgmt, page_draw_schedule],
    "內部委員會成員": [page_vote],
    "開發者": [page_dev_settings],
})

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
    st.caption("🛠️ 系統版本：2.15.1")
    st.caption("🛜 開發及維護：[lzlovecats](https://github.com/lzlovecats) @ 2026")

pg.run()
