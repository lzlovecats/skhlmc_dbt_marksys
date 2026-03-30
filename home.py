import streamlit as st
from functions import get_connection, query_params

st.title("聖呂中辯電子分紙系統")
st.caption("請根據你的身份選擇對應功能")

st.divider()

with st.expander("🔧 系統狀態", expanded=False):
    try:
        conn = get_connection()
        conn.query("SELECT 1", ttl=0)
        st.success("數據庫連線正常")
    except Exception as e:
        st.error(f"數據庫連線失敗: {e}")

    try:
        queue_df = query_params("SELECT COUNT(*) AS cnt FROM tg_notification_queue WHERE processed = FALSE")
        queue_depth = int(queue_df.iloc[0]["cnt"]) if not queue_df.empty else 0
        st.metric("Telegram 推送佇列待處理", queue_depth)
    except Exception:
        st.caption("無法讀取推送佇列狀態。")

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
