import streamlit as st
import pandas as pd
from functions import check_admin, get_connection, execute_query, CATEGORIES, DIFFICULTY_OPTIONS
st.header("辯題庫管理")

if not check_admin():
    st.stop()

if "success_upload" not in st.session_state:
    st.session_state["success_upload"] = False

if st.session_state["success_upload"]:
    st.success("已成功上傳辯題至數據庫！")
    st.session_state["success_upload"] = False

try:
    conn = get_connection()
    df = conn.query("SELECT * FROM topics", ttl=0)
except Exception as e:
    st.error(f"連線錯誤: {e}")
    st.stop()

tab1, tab2, tab3 = st.tabs(["👀 檢視現有辯題", "➕ 新增辯題", "🗑️ 刪除辯題"])

with tab1:
    st.dataframe(df, use_container_width=True, hide_index=True)

with tab2:
    st.subheader("上傳辯題至辯題庫")

    new_topic = st.text_input("輸入新辯題")
    new_category = st.selectbox("辯題類別", options=CATEGORIES)
    new_difficulty = st.selectbox("辯題難度", options=[1, 2, 3], format_func=lambda x: DIFFICULTY_OPTIONS[x])
    if st.button("確定上傳"):
        if not new_topic.strip():
            st.warning("未輸入內容！")
        else:
            duplicated = False
            if new_topic in df["topic"].values:
                st.warning("已有同樣辯題存在於辯題庫！")
                duplicated = True
            if not duplicated:
                try:
                    with st.spinner("上傳辯題至數據庫..."):
                        execute_query(
                            "INSERT INTO topics (topic, author, category, difficulty) VALUES (:topic, 'admin', :category, :difficulty)",
                            {"topic": new_topic.strip(), "category": new_category, "difficulty": new_difficulty}
                        )
                        st.session_state["success_upload"] = True
                        st.rerun()
                except Exception as e:
                    st.error(f"上傳失敗: {e}")

with tab3:
    st.subheader("刪除辯題")

    topics_to_delete = st.multiselect(
            "請選擇要刪除的辯題 (可多選)",
            options=df["topic"].to_list()  # Change to Python list
        )
    
    if topics_to_delete:
        st.warning(f"你即將刪除{len(topics_to_delete)}條辯題，此動作無法復原！")
        if st.button("確認刪除", type="primary"):
                with st.spinner("正在從數據庫刪除資料..."):
                    try:
                        for t in topics_to_delete:
                            execute_query("DELETE FROM topics WHERE topic = :topic", {"topic": t})
                        st.success("刪除完成！")
                        st.rerun()
                    except Exception as e:
                        st.error(f"刪除失敗: {e}")
