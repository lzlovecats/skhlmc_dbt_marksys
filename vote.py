import streamlit as st
import pandas as pd
from functions import check_committee_login, get_connection

st.header("🗳️ 辯題徵集及投票系統")

if not check_committee_login():
    st.stop()

user_id = st.session_state["committee_user"]
st.info(f"已登入帳戶：**{user_id}**")

conn = get_connection()
try:
    ws_vote = conn.worksheet("Vote")
    ws_topic = conn.worksheet("Topic")
except Exception as e:
    st.error(f"無法連接Google Cloud: {e}")
    st.stop()

tab1, tab2 = st.tabs(["📝 提出新辯題", "📊 辯題投票"])

with tab1:
    st.subheader("提出新辯題")
    new_topic = st.text_input("請輸入完整辯題")
    
    if st.button("提交辯題"):
        if not new_topic.strip():
            st.warning("你未輸入任何文字！")
        else:
            existing_votes = ws_vote.col_values(1)
            existing_topics = ws_topic.col_values(1)
            
            if new_topic in existing_votes or new_topic in existing_topics:
                st.error("此辯題已存在！")
            else:
                ws_vote.append_row([new_topic, "", ""])
                st.success("辯題已加入投票區！")

with tab2:
    st.subheader("待表決辯題")
    st.caption("只要同意票數 ≥ 5 且 同意 > 不同意，系統會自動將辯題新增至辯題庫。")
    
    vote_data = ws_vote.get_all_records()
    
    if not vote_data:
        st.info("目前沒有待表決的辯題。")
    else:
        for i, row in enumerate(vote_data):
            topic = row['topic']
            
            # 處理投票名單 (將字串 "user1,user2" 轉為 list)
            # 如果欄位是空的，split 會產生空字串，要 filter 掉
            flavor_list = [u for u in str(row.get('flavor', '')).split(',') if u.strip()]
            against_list = [u for u in str(row.get('against', '')).split(',') if u.strip()]
            
            f_count = len(flavor_list)
            a_count = len(against_list)
            
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])
                with c1:
                    st.write(f"**{topic}**")
                    st.caption(f"目前票數 - 同意: {f_count} | 不同意: {a_count}")
                    
                with c2:
                    if user_id in flavor_list:
                        st.button("已同意", key=f"f_done_{i}", disabled=True)
                    elif user_id in against_list:
                        st.button("已反對", key=f"f_blocked_{i}", disabled=True)
                    else:
                        if st.button("✅ 同意", key=f"vote_f_{i}"):
                            with st.spinner("處理你的投票中，請稍等⋯")
                                flavor_list.append(user_id)
                                new_flavor_str = ",".join(flavor_list)
                                ws_vote.update_cell(i + 2, 2, new_flavor_str)
                                st.toast("已投下同意票！")
                                st.rerun()

                with c3:
                    if user_id in against_list:
                        st.button("已反對", key=f"a_done_{i}", disabled=True)
                    elif user_id in flavor_list:
                        st.button("已同意", key=f"a_blocked_{i}", disabled=True)
                    else:
                        if st.button("❌ 不同意", key=f"vote_a_{i}"):
                            with st.spinner("處理你的投票中，請稍等⋯")
                                against_list.append(user_id)
                                new_against_str = ",".join(against_list)
                                ws_vote.update_cell(i + 2, 3, new_against_str)
                                st.toast("已投下不同意票！")
                                st.rerun()

            if f_count >= 5 and f_count > a_count:
                st.success(f"辯題「{topic}」獲得足夠票數，正在寫入辯題庫...")
                
                ws_topic.append_row([topic])
                ws_vote.delete_rows(i + 2)
                st.balloons()
                st.rerun()
