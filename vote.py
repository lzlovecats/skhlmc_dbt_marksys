import streamlit as st
from extra_streamlit_components import CookieManager
from functions import check_committee_login, get_connection, del_cookie
import time

st.header("🗳️ 辯題徵集及投票系統")

if not check_committee_login():
    st.stop()

user_id = st.session_state["committee_user"]
st.info(f"已登入帳戶：**{user_id}**")

@st.cache_resource
def get_cached_worksheets():
    conn = get_connection()
    return {
        "Vote": conn.worksheet("Vote"),
        "Topic": conn.worksheet("Topic"),
        "Voted": conn.worksheet("Voted"),
        "Account": conn.worksheet("Account")
    }

try:
    sheets = get_cached_worksheets()
    ws_vote = sheets["Vote"]
    ws_topic = sheets["Topic"]
    ws_voted = sheets["Voted"]
except Exception as e:
    st.error(f"無法連接Google Cloud: {e}")
    st.stop()

# Define a local cached function to read data using the existing worksheets
@st.cache_data(ttl=10)
def get_vote_data(_ws_vote, _ws_voted):
    return _ws_vote.get_all_records(), _ws_voted.get_all_values()

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
                ws_vote.append_row([new_topic, "", "", user_id])
                get_vote_data.clear()
                st.success("辯題已加入投票區！")

with tab2:
    st.subheader("待表決辯題")
    st.caption("只要同意票數 ≥ 5 且 同意 > 不同意，系統會自動將辯題新增至辯題庫。")
    st.caption("只要不同意票數 ≥ 5 且 不同意 > 同意，系統會自動刪除辯題。")

    if st.button("🔄 查看最新投票情況"):
        get_vote_data.clear()
        st.rerun()
    
    vote_data, voted_data_raw = get_vote_data(ws_vote, ws_voted)
    
    if not vote_data:
        st.info("目前沒有待表決的辯題。")
    else:
        for i, row in reversed(list(enumerate(vote_data))):
            topic = row["topic"]
            author = row["author"]

            agree_str = str(row.get("agree", ""))
            against_str = str(row.get("against", ""))
            
            agree_list = [u.strip() for u in agree_str.split(',') if u.strip()]
            against_list = [u.strip() for u in against_str.split(',') if u.strip()]
            
            f_count = len(agree_list)
            a_count = len(against_list)
            
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])


                def after_vote():
                    get_vote_data.clear()
                    st.rerun()

                with c1:
                    st.write(f"**{topic}**")
                    st.caption(f"目前票數 - 同意: {f_count} | 不同意: {a_count}")
                    
                with c2:
                    if user_id in agree_list:
                        if st.button("已同意 (點擊撤回)", key=f"f_done_{i}"):
                            with st.spinner("撤回投票中..."):
                                agree_list.remove(user_id)
                                new_agree_str = ",".join(agree_list)
                                ws_vote.update_cell(i + 2, 2, new_agree_str)
                                st.toast("已撤回同意票！", icon="↩️")
                                after_vote()
                    elif user_id in against_list:
                        if st.button("轉投同意", key=f"switch_to_f_{i}"):
                            with st.spinner("更改投票中..."):
                                against_list.remove(user_id)
                                agree_list.append(user_id)
                                new_against_str = ",".join(against_list)
                                new_agree_str = ",".join(agree_list)
                                ws_vote.update_cell(i + 2, 3, new_against_str)
                                ws_vote.update_cell(i + 2, 2, new_agree_str)
                                st.toast("已轉投同意票！", icon="↪️️")
                                after_vote()
                    else:
                        if st.button("✅ 同意", key=f"vote_f_{i}"):
                            with st.spinner("處理你的投票中，請稍等⋯"):
                                agree_list.append(user_id)
                                new_agree_str = ",".join(agree_list)
                                ws_vote.update_cell(i + 2, 2, new_agree_str)
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
                    elif user_id in agree_list:
                        if st.button("轉投反對", key=f"switch_to_a_{i}"):
                            with st.spinner("更改投票中..."):
                                agree_list.remove(user_id)
                                against_list.append(user_id)
                                new_agree_str = ",".join(agree_list)
                                new_against_str = ",".join(against_list)
                                ws_vote.update_cell(i + 2, 2, new_agree_str)
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
                
                ws_topic.append_row([topic, author])
                ws_vote.delete_rows(i + 2)
                ws_voted.append_row([topic, "", ",".join(agree_list), ",".join(against_list), author])
                get_vote_data.clear()
                st.balloons()
                st.rerun()
            
            if a_count >= 5 and a_count > f_count:
                st.error(f"辯題「{topic}」已獲得{a_count}票不同意票，正在刪除辯題...")
                
                ws_vote.delete_rows(i + 2)
                ws_voted.append_row(["", topic, ",".join(agree_list), ",".join(against_list), author])
                get_vote_data.clear()
                st.snow()
                st.rerun()
                
    st.divider()
    
    passed_list = []
    rejected_list = []
    
    if len(voted_data_raw) > 1:
        for row in voted_data_raw[1:]:
            if len(row) > 0 and row[0].strip():
                passed_list.append(row[0].strip())
            if len(row) > 1 and row[1].strip():
                rejected_list.append(row[1].strip())

    with st.expander("📜 已通過辯題記錄 (最近十個)", expanded=False):
        if passed_list:
            for p in range(len(passed_list)):
                if p < 10: # Display only the last 10 passed topics
                    st.write(f"✅ {list(reversed(passed_list))[p]}")
                else:
                    break
        else:
            st.caption("暫無記錄")
            
    with st.expander("🗑️ 已否決辯題記錄 (最近十個)", expanded=False):
        if rejected_list:
            for k in range(len(rejected_list)):
                if k < 10: # Display only the last 10 rejected topics
                    st.write(f"❌ {list(reversed(rejected_list))[k]}")
                else:
                    break
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
                try:
                    ws = sheets["Account"]
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
        cookie_manager = st.session_state.get("committee_cookie_manager")
        del_cookie(cookie_manager, "committee_user")
        st.session_state["vote_just_logout"] = True
        time.sleep(1)
        st.rerun()
