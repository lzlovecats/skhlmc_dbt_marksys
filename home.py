import streamlit as st

st.title("聖呂中辯電子分紙系統")
st.caption("請根據你的身份選擇對應功能")

st.divider()

col1, col2 = st.columns(2)

with col1:
    with st.container(border=True):
        st.markdown("### ⚖️ 評判")
        st.write("填寫電子分紙，提交比賽評分。")
        st.page_link("judging.py", label="前往電子分紙", icon="📝")

    with st.container(border=True):
        st.markdown("### 🏆 比賽隊伍")
        st.write("查閱所參與比賽的評判評分紙。")
        st.page_link("review.py", label="查閱比賽分紙", icon="📄")

    with st.container(border=True):
        st.markdown("### 🌐 一般人員")
        st.write("瀏覽公開辯題庫及相關統計。")
        st.page_link("open_db.py", label="查閱辯題庫", icon="📚")

with col2:
    with st.container(border=True):
        st.markdown("### 🎛️ 賽會人員")
        st.write("管理比賽場次、查閱結果、辯題庫及賽程抽籤。")
        st.page_link("match_info.py", label="比賽場次管理", icon="📋")
        st.page_link("management.py", label="查閱比賽結果", icon="📊")
        st.page_link("db_mgmt.py", label="資料庫管理控制台", icon="🖥️")
        st.page_link("draw_match_schedule.py", label="抽取賽程", icon="🎲")

    with st.container(border=True):
        st.markdown("### 🗳️ 內部委員會成員")
        st.write("辯題徵集、投票及罷免系統。")
        st.page_link("vote.py", label="辯題投票系統", icon="🗳️")
