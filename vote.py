import streamlit as st
from functions import check_committee_login, get_connection, execute_query, del_cookie, committee_cookie_manager, return_gemini_reminder, return_chatgpt_reminder, return_gemini_depose_reminder, return_chatgpt_depose_reminder
import time
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

st.header("辯題徵集、投票及罷免系統")

# Get committee cookie manager first
cm = committee_cookie_manager()

@st.dialog("嚟自Gemini嘅提醒")
def show_gemini_reminder():
    content = return_gemini_reminder()
    st.markdown(content)
 
@st.dialog("嚟自ChatGPT嘅提醒")
def show_chatgpt_reminder(): 
    content = return_chatgpt_reminder()
    st.markdown(content)

@st.dialog("嚟自Gemini嘅提醒")
def depose_show_gemini_reminder():
    content = return_gemini_depose_reminder()
    st.markdown(content)

@st.dialog("嚟自ChatGPT嘅提醒")
def depose_show_chatgpt_reminder():
    content = return_chatgpt_depose_reminder()
    st.markdown(content)

if not check_committee_login():
    st.stop()

user_id = st.session_state["committee_user"]
st.info(f"已登入帳戶：**{user_id}**")

@st.cache_data(ttl=1)
def get_vote_data():
    conn = get_connection()
    df = conn.query("SELECT * FROM topic_votes ORDER BY created_at DESC", ttl=0)
    
    # Fill NaN values with empty strings to avoid errors when splitting
    df = df.fillna("")
    
    pending = df[df['status'] == 'pending'].to_dict('records')
    passed = df[df['status'] == 'passed']['topic'].tolist()
    rejected = df[df['status'] == 'rejected']['topic'].tolist()
    return pending, passed, rejected

tab1, tab2 ,tab3, tab4= st.tabs(["📝 提出新辯題", "📊 辯題投票", "✂️ 罷免投票", "🔐 管理帳戶"])

with tab1:
    st.subheader("提出新辯題")
    new_topic = st.text_input("請輸入完整辯題")

    # If there are >= 10 pending topics, block new submissions and remind voting first.
    pending_vote_data, _, _ = get_vote_data()
    pending_count = len(pending_vote_data) if pending_vote_data else 0
    submit_disabled = pending_count >= 10
    if submit_disabled:
        st.warning(
            f"目前已有 **{pending_count}** 個待表決辯題。"
            "請先到「📊 辯題投票」完成投票，直到待表決辯題數量少於10個後再提交新辯題。"
        )
    
    if st.button("提交辯題", disabled=submit_disabled):
        if not new_topic.strip():
            st.warning("你未輸入任何文字！")
        else:
            conn = get_connection()
            all_topics_df = conn.query("SELECT topic FROM topics", ttl=0)
            all_votes_df = conn.query("SELECT topic FROM topic_votes WHERE status = 'pending'", ttl=0)
            
            existing_topics = all_topics_df["topic"].tolist() if not all_topics_df.empty else []
            existing_votes = all_votes_df["topic"].tolist() if not all_votes_df.empty else []
            
            if new_topic in existing_votes or new_topic in existing_topics:
                st.error("此辯題已存在！")
            else:
                hk_time = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
                query = "INSERT INTO topic_votes (topic, author, status, agree_users, against_users, created_at) VALUES (:new_topic, :user_id, 'pending', :agree_users, :against_users, :created_at)"
                param = {"new_topic": new_topic, "user_id": user_id, "agree_users": "{}", "against_users": "{}", "created_at": hk_time}
                execute_query(query, param)
                get_vote_data.clear()
                st.success("辯題已加入投票區！")

    st.divider()
    st.subheader("提出罷免動議")

    try:
        conn = get_connection()
        df = conn.query("SELECT * FROM topics", ttl=0)
    except Exception as e:
        st.error(f"連線錯誤: {e}")
        st.stop()
    
    topics_to_depose = st.multiselect(
            "請選擇要提出罷免動議的辯題 (可多選)",
            options=df["topic"].to_list()
        )

    if st.button("提出罷免動議"):
        if not topics_to_depose:
            st.warning("你未選擇任何辯題！")
        else:
            conn = get_connection()
            exist_votes = conn.query("SELECT topic FROM topic_depose_votes", ttl=0)
            exist_depose_topics = exist_votes["topic"].tolist()
            if len(exist_depose_topics) >= 10:
                st.warning("目前已有10個辯題罷免動議。請先到「✂️ 罷免投票」完成投票，直到辯題罷免動議數量少於10個後再提交新動議。")
                st.stop()
            proposed = True
            for t in topics_to_depose:
                if t in exist_depose_topics:
                    proposed = False
                else:
                    hk_time = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
                    query = "INSERT INTO topic_depose_votes (topic, mover, agree_users, against_users, created_at) VALUES (:topic, :user_id, :agree_users, :against_users, :created_at)"
                    param = {"topic": t, "user_id": user_id, "agree_users": "{}", "against_users": "{}", "created_at": hk_time}
                    execute_query(query, param)
            get_vote_data.clear()
            if proposed:
                st.success("罷免動議已提出！")
            else:
                st.warning("有辯題已存在於罷免動議區，該辯題將不會被重複提出。其他辯題已成功提出罷免動議。")


with tab2:
    st.subheader("待表決辯題")
    st.caption("只要同意票數 ≥ 5 且 同意 > 不同意，系統會自動將辯題新增至辯題庫。")
    st.caption("只要不同意票數 ≥ 5 且 不同意 > 同意，系統會自動刪除辯題。")

    button_col1, button_col2, button_col3 = st.columns([1, 1, 1])
    with button_col1:
        if st.button("🔄 查看最新投票情況"):
            get_vote_data.clear()
            st.rerun()

    with button_col2:
        if st.button("💡 Gemini提提你"):
            show_gemini_reminder()

    with button_col3:
        if st.button("🔍 ChatGPT提提你"):
            show_chatgpt_reminder()
    st.divider()
    
    vote_data, passed_list, rejected_list = get_vote_data()
    
    if not vote_data:
        st.info("目前沒有待表決的辯題。")
    else:
        conn = get_connection()
        for i, row in enumerate(vote_data):
            topic = row["topic"]
            author = row["author"]

            agree_list = row.get("agree_users", "")
            against_list = row.get("against_users", "")
            
            f_count = len(agree_list)
            a_count = len(against_list)
            
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])


                def after_vote():
                    get_vote_data.clear()
                    st.rerun()

                with c1:
                    st.write(f"**{topic}**")
                    st.caption(f"提出者：{author} | 目前票數 - 同意: {f_count} | 不同意: {a_count}")

                    f_progress = min(f_count / 5.0, 1.0)
                    a_progress = min(a_count / 5.0, 1.0)
                    
                    st.progress(f_progress, text=f"同意票進度: {f_count} / 5")
                    st.progress(a_progress, text=f"不同意票進度: {a_count} / 5")
                    
                with c2:
                    if user_id in agree_list:
                        if st.button("已同意 (點擊撤回)", key=f"f_done_{i}"):
                            with st.spinner("撤回投票中..."):
                                agree_list.remove(user_id)
                                query = "UPDATE topic_votes SET agree_users=:new_agree_str WHERE topic=:topic"
                                param = {"new_agree_str": agree_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已撤回同意票！", icon="↩️")
                                after_vote()
                    elif user_id in against_list:
                        if st.button("轉投同意", key=f"switch_to_f_{i}"):
                            with st.spinner("更改投票中..."):
                                against_list.remove(user_id)
                                agree_list.append(user_id)
                                query = "UPDATE topic_votes SET against_users=:new_against_str, agree_users=:new_agree_str WHERE topic=:topic"
                                param = {"new_against_str": against_list, "new_agree_str": agree_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已轉投同意票！", icon="↪️️")
                                after_vote()
                    else:
                        if st.button("✅ 同意", key=f"vote_f_{i}"):
                            with st.spinner("處理你的投票中，請稍等⋯"):
                                agree_list.append(user_id)
                                query = "UPDATE topic_votes SET agree_users=:new_agree_str WHERE topic=:topic"
                                param = {"new_agree_str": agree_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已投下同意票！", icon="☑️")
                                after_vote()

                with c3:
                    if user_id in against_list:
                        if st.button("已反對 (點擊撤回)", key=f"a_done_{i}"):
                            with st.spinner("撤回投票中..."):
                                against_list.remove(user_id)
                                query = "UPDATE topic_votes SET against_users=:new_against_str WHERE topic=:topic"
                                param = {"new_against_str": against_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已撤回不同意票！", icon="↩️")
                                after_vote()
                    elif user_id in agree_list:
                        if st.button("轉投反對", key=f"switch_to_a_{i}"):
                            with st.spinner("更改投票中..."):
                                agree_list.remove(user_id)
                                against_list.append(user_id)
                                query = "UPDATE topic_votes SET agree_users=:new_agree_str, against_users=:new_against_str WHERE topic=:topic"
                                param = {"new_agree_str": agree_list, "new_against_str": against_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已轉投不同意票！", icon="↪️️")
                                after_vote()
                    else:
                        if st.button("❌ 不同意", key=f"vote_a_{i}"):
                            with st.spinner("處理你的投票中，請稍等⋯"):
                                against_list.append(user_id)
                                query = "UPDATE topic_votes SET against_users=:new_against_str WHERE topic=:topic"
                                param = {"new_against_str": against_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已投下不同意票！", icon="☑️")
                                after_vote()

            if f_count >= 5 and f_count > a_count:
                st.success(f"辯題「{topic}」已獲得足夠票數，正在寫入辯題庫...")
                
                query = "INSERT INTO topics (topic, author) VALUES (:topic, :author)"
                param = {"topic": topic, "author": author}
                execute_query(query, param)
                query = "UPDATE topic_votes SET status='passed', agree_users=:new_agree_str, against_users=:new_against_str WHERE topic=:topic"
                param = {"new_agree_str": agree_list, "new_against_str": against_list, "topic": topic}
                execute_query(query, param)
                get_vote_data.clear()
                st.balloons()
                st.rerun()
            
            if a_count >= 5 and a_count > f_count:
                st.error(f"辯題「{topic}」已獲得{a_count}票不同意票，正在刪除辯題...")
                
                query = "UPDATE topic_votes SET status='rejected', agree_users=:new_agree_str, against_users=:new_against_str WHERE topic=:topic"
                param = {"new_agree_str": agree_list, "new_against_str": against_list, "topic": topic}
                execute_query(query, param)
                get_vote_data.clear()
                st.snow()
                st.rerun()
                
    st.divider()
    
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
    st.subheader("罷免投票")
    st.caption("只要同意罷免票數 ≥ 5 且 同意 > 不同意，系統會自動刪除辯題。")
    st.caption("只要不同意罷免票數 ≥ 5 且 不同意 > 同意，系統會自動刪除罷免動議。")

    button_col1, button_col2, button_col3 = st.columns([1, 1, 1])
    with button_col1:
        if st.button("🔄 查看最新罷免投票情況"):
            get_vote_data.clear()
            st.rerun()
    with button_col2:
        if st.button("💡 Gemini提醒你"):
            depose_show_gemini_reminder()
    with button_col3:
        if st.button("🔍 ChatGPT提醒你"):
            depose_show_chatgpt_reminder()

    conn = get_connection()
    df_depose = conn.query("SELECT * FROM topic_depose_votes ORDER BY created_at DESC", ttl=0)
    vote_data = df_depose.to_dict('records')

    if not vote_data:
        st.info("目前沒有待罷免的辯題。")
    else:
        for i, row in enumerate(vote_data):
            topic = row["topic"]
            mover = row["mover"]

            agree_list = row.get("agree_users", "")
            if not isinstance(agree_list, list):
                agree_list = []
            against_list = row.get("against_users", "")
            if not isinstance(against_list, list):
                against_list = []
            
            f_count = len(agree_list)
            a_count = len(against_list)
            
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])

                def after_vote_depose():
                    get_vote_data.clear()
                    st.rerun()

                with c1:
                    st.write(f"**{topic}**")
                    st.caption(f"提出者: {mover} | 目前票數 - 同意罷免: {f_count} | 不同意罷免: {a_count}")

                    f_progress = min(f_count / 5.0, 1.0)
                    a_progress = min(a_count / 5.0, 1.0)
                    
                    st.progress(f_progress, text=f"同意罷免進度: {f_count} / 5")
                    st.progress(a_progress, text=f"不同意罷免進度: {a_count} / 5")
                    
                with c2:
                    if user_id in agree_list:
                        if st.button("已同意 (點擊撤回)", key=f"depose_f_done_{i}"):
                            with st.spinner("撤回投票中..."):
                                agree_list.remove(user_id)
                                query = "UPDATE topic_depose_votes SET agree_users=:new_agree_str WHERE topic=:topic"
                                param = {"new_agree_str": agree_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已撤回同意罷免票！", icon="↩️")
                                after_vote_depose()
                    elif user_id in against_list:
                        if st.button("轉投同意", key=f"depose_switch_to_f_{i}"):
                            with st.spinner("更改投票中..."):
                                against_list.remove(user_id)
                                agree_list.append(user_id)
                                query = "UPDATE topic_depose_votes SET against_users=:new_against_str, agree_users=:new_agree_str WHERE topic=:topic"
                                param = {"new_against_str": against_list, "new_agree_str": agree_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已轉投同意罷免票！", icon="↪️️")
                                after_vote_depose()
                    else:
                        if st.button("✅ 同意罷免", key=f"depose_vote_f_{i}"):
                            with st.spinner("處理你的投票中，請稍等⋯"):
                                agree_list.append(user_id)
                                query = "UPDATE topic_depose_votes SET agree_users=:new_agree_str WHERE topic=:topic"
                                param = {"new_agree_str": agree_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已投下同意罷免票！", icon="☑️")
                                after_vote_depose()

                with c3:
                    if user_id in against_list:
                        if st.button("已反對 (點擊撤回)", key=f"depose_a_done_{i}"):
                            with st.spinner("撤回投票中..."):
                                against_list.remove(user_id)
                                query = "UPDATE topic_depose_votes SET against_users=:new_against_str WHERE topic=:topic"
                                param = {"new_against_str": against_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已撤回不同意罷免票！", icon="↩️")
                                after_vote_depose()
                    elif user_id in agree_list:
                        if st.button("轉投反對", key=f"depose_switch_to_a_{i}"):
                            with st.spinner("更改投票中..."):
                                agree_list.remove(user_id)
                                against_list.append(user_id)
                                query = "UPDATE topic_depose_votes SET agree_users=:new_agree_str, against_users=:new_against_str WHERE topic=:topic"
                                param = {"new_agree_str": agree_list, "new_against_str": against_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已轉投不同意罷免票！", icon="↪️️")
                                after_vote_depose()
                    else:
                        if st.button("❌ 不同意罷免", key=f"depose_vote_a_{i}"):
                            with st.spinner("處理你的投票中，請稍等⋯"):
                                against_list.append(user_id)
                                query = "UPDATE topic_depose_votes SET against_users=:new_against_str WHERE topic=:topic"
                                param = {"new_against_str": against_list, "topic": topic}
                                execute_query(query, param)
                                st.toast("已投下不同意罷免票！", icon="☑️")
                                after_vote_depose()

            if f_count >= 5 and f_count > a_count:
                st.error(f"罷免動議「{topic}」已獲通過，正在從辯題庫刪除該辯題...")
                
                query1 = "DELETE FROM topic_depose_votes WHERE topic=:topic"
                query2 = "DELETE FROM topics WHERE topic=:topic"
                param = {"topic": topic}
                execute_query(query1, param)
                execute_query(query2, param)
                get_vote_data.clear()
                st.snow()
                st.rerun()
            
            if a_count >= 5 and a_count > f_count:
                st.success(f"罷免動議「{topic}」已被否決，正在刪除該罷免動議...")
                
                query = "DELETE FROM topic_depose_votes WHERE topic=:topic"
                param = {"topic": topic}
                execute_query(query, param)
                get_vote_data.clear()
                st.balloons()
                st.rerun()


with tab4:
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
                    execute_query("UPDATE accounts SET userpw = :userpw WHERE userid = :userid", {"userpw": new_pw.strip(), "userid": user_id})
                    st.success("帳戶密碼已更改！下次登入請使用新密碼！")
                except Exception as e:
                    st.error(f"無法連接至數據庫: {e}")
    
    st.divider()
    if st.button("登出", type="primary"):
        st.session_state["committee_user"] = None
        del_cookie(cm, "committee_user")
        time.sleep(1)
        st.rerun()
