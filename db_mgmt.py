import streamlit as st
import pandas as pd
from functions import check_admin, get_connection
st.header("辯題庫管理")

if not check_admin():
    st.stop()

if "success_upload" not in st.session_state:
    st.session_state["success_upload"] = False

if st.session_state["success_upload"]:
    st.success("已成功上傳辯題至Google Cloud！")
    st.session_state["success_upload"] = False

try:
    ss = get_connection()
    ws = ss.worksheet("Topic")
except Exception as e:
    st.error(f"連線錯誤: {e}")
    st.stop()

df = pd.DataFrame(ws.get_all_records())

tab1, tab2, tab3 = st.tabs(["👀 檢視現有辯題", "➕ 新增辯題", "🗑️ 刪除辯題"])

with tab1:
    st.dataframe(df, use_container_width=True, hide_index=True)

with tab2:
    st.subheader("上傳辯題至辯題庫")

    new_topic = st.text_input("輸入新辯題")
    if st.button("確定上傳"):
        if not new_topic.strip():
            st.warning("未輸入內容！")
        else:
            duplicated = False
            all_values = ws.get_all_values()
            past_topic = []
            for i in range(len(all_values)):
                if i == 0: continue  # Skip header
                past_topic.append(all_values[i][0])
            if new_topic in past_topic:
                st.warning("已有同樣辯題存在於辯題庫！")
                duplicated = True
            if not duplicated:
                try:
                    with st.spinner("上傳辯題至Google Cloud..."):
                        new_topic = [new_topic.strip()]
                        ws.append_row(new_topic)
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
                with st.spinner("正在從Google Cloud上刪除資料..."):
                    try:
                        current_col_values = ws.col_values(1)
                        rows_to_del_indices = []
    
                        for t in topics_to_delete:
                                indices = [i + 1 for i, x in enumerate(current_col_values) if x == t]
                                rows_to_del_indices.extend(indices)

                        rows_to_del_indices = sorted(list(set(rows_to_del_indices)), reverse=True)

                        for row_idx in rows_to_del_indices:
                                ws.delete_rows(row_idx)
                        st.success("刪除完成！")
                    except Exception as e:
                        st.error(f"刪除失敗: {e}")
                    
