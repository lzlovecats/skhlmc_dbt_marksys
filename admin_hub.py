import streamlit as st

from auth import check_admin
from functions import render_page_guidance

st.header("賽務管理易")
render_page_guidance(
    [
        "此頁面整合了報名管理、場次管理、片段管理及抽取賽程四項功能。",
        "使用賽會人員密碼登入後，可透過上方分頁切換不同管理功能。",
    ],
)

if not check_admin():
    st.stop()

tab = st.segmented_control(
    "管理功能",
    options=["報名管理", "場次管理", "片段管理", "抽取賽程"],
    default="場次管理",
    label_visibility="collapsed",
    width="stretch",
)

if tab == "報名管理":
    from registration_admin import render_registration_admin
    render_registration_admin()
elif tab == "場次管理":
    from match_info import render_match_info
    render_match_info()
elif tab == "片段管理":
    from video_admin import render_video_admin
    render_video_admin()
elif tab == "抽取賽程":
    from draw_match_schedule import render_draw_schedule
    render_draw_schedule()
