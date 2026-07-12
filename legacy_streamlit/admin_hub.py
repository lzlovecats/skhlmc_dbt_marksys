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
    st.link_button("前往比賽報名管理", "/registration-admin", width="stretch")
elif tab == "場次管理":
    st.link_button("前往比賽場次管理", "/match-info", width="stretch")
elif tab == "片段管理":
    st.link_button("前往比賽片段管理", "/video-admin", width="stretch")
elif tab == "抽取賽程":
    st.link_button("前往抽取賽程", "/draw-match-schedule", width="stretch")
