import streamlit as st
from functions import check_committee_login, get_connection, fetch_vote_data_cached

st.header("🗳️ 辯題徵集及投票系統")

if not check_committee_login():
    st.stop()

user_id = st.session_state["committee_user"]
st.info(f"已登入帳戶：**{user_id}**")

conn = get_connection()
try:
    ws_vote = conn.worksheet("Vote")
    ws_topic = conn.worksheet("Topic")
    ws_voted = conn.worksheet("Voted")
except Exception as e:
    st.error(f"無法連接Google Cloud: {e}")
    st.stop()

tab1, tab2 ,tab3= st.tabs(["📝 提出新辯題", "📊 辯題投票", "🔐 管理帳戶"])

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
                fetch_vote_data_cached.clear()
                st.success("辯題已加入投票區！")

with tab2:
    st.subheader("待表決辯題")
    st.caption("只要同意票數 ≥ 5 且 同意 > 不同意，系統會自動將辯題新增至辯題庫。")
    st.caption("只要不同意票數 ≥ 5 且 不同意 > 同意，系統會自動刪除辯題。")

    if st.button("🔄 點擊刷新最新票數"):
        fetch_vote_data_cached.clear()
        st.rerun()
    
    vote_data, voted_data_raw = fetch_vote_data_cached()
    
    if not vote_data:
        st.info("目前沒有待表決的辯題。")
    else:
        for i, row in reversed(list(enumerate(vote_data))):
            topic = row['topic']
            
            # 處理投票名單 (將字串 "user1,user2" 轉為 list)
            # 如果欄位是空的，split 會產生空字串，要 filter 掉
            flavor_str = str(row.get('flavor', ''))
            against_str = str(row.get('against', ''))
            
            flavor_list = [u.strip() for u in flavor_str.split(',') if u.strip()]
            against_list = [u.strip() for u in against_str.split(',') if u.strip()]
            
            f_count = len(flavor_list)
            a_count = len(against_list)
            
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])


                def after_vote():
                    fetch_vote_data_cached.clear()
                    st.rerun()

                with c1:
                    st.write(f"**{topic}**")
                    st.caption(f"目前票數 - 同意: {f_count} | 不同意: {a_count}")
                    
                with c2:
                    if user_id in flavor_list:
                        if st.button("已同意 (點擊撤回)", key=f"f_done_{i}"):
                            with st.spinner("撤回投票中..."):
                                flavor_list.remove(user_id)
                                new_flavor_str = ",".join(flavor_list)
                                ws_vote.update_cell(i + 2, 2, new_flavor_str)
                                st.toast("已撤回同意票！", icon="↩️")
                                after_vote()
                    elif user_id in against_list:
                        if st.button("轉投同意", key=f"switch_to_f_{i}"):
                            with st.spinner("更改投票中..."):
                                against_list.remove(user_id)
                                flavor_list.append(user_id)
                                new_against_str = ",".join(against_list)
                                new_flavor_str = ",".join(flavor_list)
                                ws_vote.update_cell(i + 2, 3, new_against_str)
                                ws_vote.update_cell(i + 2, 2, new_flavor_str)
                                st.toast("已轉投同意票！", icon="↪️️")
                                after_vote()
                    else:
                        if st.button("✅ 同意", key=f"vote_f_{i}"):
                            with st.spinner("處理你的投票中，請稍等⋯"):
                                flavor_list.append(user_id)
                                new_flavor_str = ",".join(flavor_list)
                                ws_vote.update_cell(i + 2, 2, new_flavor_str)
                                st.toast("已投下同意票！", icon="☑️")
                                after_vote()

                with c3:
                    if user_id in against_list:
                        if st.button("已反對 (點擊撤回)", key=f"a_done_{i}"):
                            with st.spinner("撤回投票中..."):
                                against_list.remove(user_id)
                                new_against_str = ",".join(against_list)
                                ws_vote.update_cell(i + 2, 3, new_against_str)
                                st.toast("已撤回不同意票！", icon="↩️")
                                after_vote()
                    elif user_id in flavor_list:
                        if st.button("轉投反對", key=f"switch_to_a_{i}"):
                            with st.spinner("更改投票中..."):
                                flavor_list.remove(user_id)
                                against_list.append(user_id)
                                new_flavor_str = ",".join(flavor_list)
                                new_against_str = ",".join(against_list)
                                ws_vote.update_cell(i + 2, 2, new_flavor_str)
                                ws_vote.update_cell(i + 2, 3, new_against_str)
                                st.toast("已轉投不同意票！", icon="↪️️")
                                after_vote()
                    else:
                        if st.button("❌ 不同意", key=f"vote_a_{i}"):
                            with st.spinner("處理你的投票中，請稍等⋯"):
                                against_list.append(user_id)
                                new_against_str = ",".join(against_list)
                                ws_vote.update_cell(i + 2, 3, new_against_str)
                                st.toast("已投下不同意票！", icon="☑️")
                                after_vote()

            if f_count >= 5 and f_count > a_count:
                st.success(f"辯題「{topic}」已獲得足夠票數，正在寫入辯題庫...")
                
                ws_topic.append_row([topic])
                ws_vote.delete_rows(i + 2)
                ws_voted.append_row([topic, ""])
                fetch_vote_data_cached.clear()
                st.balloons()
                st.rerun()
            
            if a_count >= 5 and a_count > f_count:
                st.error(f"辯題「{topic}」已獲得{a_count}票不同意票，正在刪除辯題...")
                
                ws_vote.delete_rows(i + 2)
                ws_voted.append_row(["", topic])
                fetch_vote_data_cached.clear()
                st.snow()
                st.rerun()
                
    st.divider()
    
    passed_list = []
    rejected_list = []
    
    if len(voted_data_raw) > 1:
        for row in voted_data_raw[1:]:
            if len(row) > 0 and row[0].strip():
                passed_list.append(row[0].strip())
            # Column B (index 1) 為 Rejected
            if len(row) > 1 and row[1].strip():
                rejected_list.append(row[1].strip())

    with st.expander("📜 已通過辯題記錄 (Passed)", expanded=False):
        if passed_list:
            for p in reversed(passed_list):
                st.write(f"✅ {p}")
        else:
            st.caption("暫無記錄")
            
    with st.expander("🗑️ 已否決辯題記錄 (Rejected)", expanded=False):
        if rejected_list:
            for r in reversed(rejected_list):
                st.write(f"❌ {r}")
        else:
            st.caption("暫無記錄")

with tab3:
    st.subheader("帳戶管理")
    
    with st.expander("更改密碼", expanded=False):
        with st.form("change_user_password"):
            new_pw = st.text_input("輸入新密碼", type="password")
            submit_new_pw = st.form_submit_button("確認更改")
        
        if submit_new_pw:
            if not new_pw.strip():
                st.warning("你未輸入密碼！")
            else:
                conn = get_connection()
                try:
                    ws = conn.worksheet("Account")
                    records = ws.get_all_records()
                    
                    Found = False
                    for i, row in enumerate(records):
                        if str(row.get("userid")) == str(user_id):
                            ws.update_cell(i+2, 2, new_pw.strip())
                            Found = True
                            break
                    if Found:
                        st.success("帳戶密碼已更改！下次登入請使用新密碼！")
                    else:
                        st.error("找不到你的帳戶紀錄，請聯絡管理員")
                except Exception as e:
                    st.error(f"無法連接至數據庫: {e}")
    
    st.divider()
    if st.button("登出", type="primary"):
        st.session_state["committee_user"] = None
        st.rerun()
