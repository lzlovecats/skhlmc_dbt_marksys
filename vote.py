import json
import math
import streamlit as st
from functions import check_committee_login, get_connection, execute_query, del_cookie, committee_cookie_manager, return_gemini_reminder, return_chatgpt_reminder, return_gemini_depose_reminder, return_chatgpt_depose_reminder, get_active_user_count, get_member_participation_stats
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

st.header("辯題徵集、投票及罷免系統")

TOPIC_REJECTION_REASONS = [
    "立論方向不清晰",
    "概念界定含糊",
    "正反論證責任失衡",
    "立場失衡或明顯偏題",
    "與現有辯題重複或過於相似",
    "討論價值不足",
    "題目表述可再修訂"
]

DEPOSE_REASONS = [
    "題目已過時",
    "正反論證責任失衡",
    "題目表述含糊",
    "程度過於簡單或過於複雜",
    "題目與現有題庫類似或重複",
    "題目討論價值不足",
    "已有更好版本可取代"
]


def parse_reason_map(raw_value):
    if isinstance(raw_value, dict):
        return raw_value
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def parse_reason_list(raw_value):
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except (TypeError, json.JSONDecodeError):
        pass
    return [str(raw_value).strip()] if str(raw_value).strip() else []


def dump_json(data):
    return json.dumps(data, ensure_ascii=False)


def collect_reasons(selected_reasons, other_reason):
    reasons = [reason.strip() for reason in selected_reasons if reason.strip()]
    other_reason = other_reason.strip()
    if other_reason:
        reasons.append(f"其他：{other_reason}")
    return reasons


def render_reason_lines(reason_map, empty_text):
    if not reason_map:
        st.caption(empty_text)
        return
    for voter, reasons in reason_map.items():
        formatted = "；".join(parse_reason_list(reasons))
        if formatted:
            st.caption(f"{voter}：{formatted}")

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
st.caption("活躍成員標準：整體投票率達40% 及 在最近十次投票中至少參與三次")
st.info(f"已登入帳戶：**{user_id}**")

_active_count, active_user_list = get_active_user_count()
is_active = user_id == "admin" or user_id in active_user_list
ENTRY_THRESHOLD = max(5, math.ceil(_active_count * 0.4))
DEPOSE_THRESHOLD = max(6, math.ceil(_active_count * 0.5))

if user_id != "admin":
    if is_active:
        st.success("帳戶狀態：活躍成員")
    else:
        st.warning("帳戶狀態：非活躍成員，你將不能提出新辯題或罷免動議，但仍可參與投票。")

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

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 提出新辯題", "📊 辯題投票", "✂️ 罷免投票", "👥 成員參與率", "🔐 管理帳戶"])

with tab1:
    st.subheader("提出新辯題")
    new_topic = st.text_input("請輸入完整辯題")

    # If there are >= 10 pending topics, block new submissions and remind voting first.
    pending_vote_data, _, _ = get_vote_data()
    pending_count = len(pending_vote_data) if pending_vote_data else 0
    submit_disabled = pending_count >= 10 or not is_active
    if not is_active:
        st.info("非活躍成員不能提出新辯題。")
    elif pending_count >= 10:
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
                hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
                hk_time = hk_now.strftime("%Y-%m-%d %H:%M:%S")
                deadline = (hk_now.date() + timedelta(days=7)).strftime("%Y-%m-%d")
                query = "INSERT INTO topic_votes (topic, author, status, agree_users, against_users, created_at, deadline) VALUES (:new_topic, :user_id, 'pending', :agree_users, :against_users, :created_at, :deadline)"
                param = {"new_topic": new_topic, "user_id": user_id, "agree_users": "{}", "against_users": "{}", "created_at": hk_time, "deadline": deadline}
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
    depose_reason_choices = st.multiselect(
        "請選擇提出罷免動議的原因（可多選）",
        options=DEPOSE_REASONS,
        key="depose_reason_choices"
    )
    depose_reason_other = st.text_area(
        "其他補充原因（如有）",
        key="depose_reason_other",
        placeholder="例如：題目最近已在其他比賽打過。"
    )

    if not is_active:
        st.info("非活躍成員不能提出罷免動議。")

    if st.button("提出罷免動議", disabled=not is_active):
        if not topics_to_depose:
            st.warning("你未選擇任何辯題！")
        elif not collect_reasons(depose_reason_choices, depose_reason_other):
            st.warning("請至少交代一個罷免原因。")
        else:
            conn = get_connection()
            exist_votes = conn.query("SELECT topic FROM topic_depose_votes", ttl=0)
            exist_depose_topics = exist_votes["topic"].tolist()
            if len(exist_depose_topics) >= 10:
                st.warning("目前已有10個辯題罷免動議。請先到「✂️ 罷免投票」完成投票，直到辯題罷免動議數量少於10個後再提交新動議。")
                st.stop()
            proposed = True
            proposal_reasons = collect_reasons(depose_reason_choices, depose_reason_other)
            for t in topics_to_depose:
                if t in exist_depose_topics:
                    proposed = False
                else:
                    hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
                    hk_time = hk_now.strftime("%Y-%m-%d %H:%M:%S")
                    deadline = (hk_now.date() + timedelta(days=7)).strftime("%Y-%m-%d")
                    query = """
                    INSERT INTO topic_depose_votes (
                        topic, mover, agree_users, against_users, created_at, proposal_reasons, deadline
                    ) VALUES (
                        :topic, :user_id, :agree_users, :against_users, :created_at, :proposal_reasons, :deadline
                    )
                    """
                    param = {
                        "topic": t,
                        "user_id": user_id,
                        "agree_users": "{}",
                        "against_users": "{}",
                        "created_at": hk_time,
                        "proposal_reasons": dump_json(proposal_reasons),
                        "deadline": deadline
                    }
                    execute_query(query, param)
            get_vote_data.clear()
            if proposed:
                st.success("罷免動議已提出！")
            else:
                st.warning("有辯題已存在於罷免動議區，該辯題將不會被重複提出。其他辯題已成功提出罷免動議。")


with tab2:
    st.subheader("待表決辯題")
    st.caption(f"目前活躍成員：{_active_count} 人 ｜ 入庫門檻：{ENTRY_THRESHOLD} 票")
    st.caption(f"只要同意票數 ≥ {ENTRY_THRESHOLD} 且 同意 > 不同意，系統會自動將辯題新增至辯題庫。")
    st.caption(f"只要不同意票數 ≥ {ENTRY_THRESHOLD} 且 不同意 > 同意，系統會自動刪除辯題。")

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
            if not isinstance(agree_list, list):
                agree_list = []
            if not isinstance(against_list, list):
                against_list = []
            against_reason_map = parse_reason_map(row.get("against_reasons", ""))
            
            f_count = len(agree_list)
            a_count = len(against_list)

            deadline_val = row.get("deadline", "")
            deadline_passed = False
            deadline_str = ""
            if deadline_val and deadline_val != "":
                try:
                    if hasattr(deadline_val, 'date'):
                        deadline_date = deadline_val.date() if hasattr(deadline_val, 'hour') else deadline_val
                    else:
                        deadline_date = datetime.strptime(str(deadline_val)[:10], "%Y-%m-%d").date()
                    today_hk = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
                    deadline_passed = today_hk > deadline_date
                    deadline_str = deadline_date.strftime("%Y-%m-%d")
                except Exception:
                    pass

            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])


                def after_vote():
                    get_vote_data.clear()
                    st.rerun()

                with c1:
                    st.write(f"**{topic}**")
                    deadline_display = f" | 截止：{deadline_str} 23:59" if deadline_str else ""
                    st.caption(f"提出者：{author} | 目前票數 - 同意: {f_count} | 不同意: {a_count}{deadline_display}")

                    f_progress = min(f_count / ENTRY_THRESHOLD, 1.0)
                    a_progress = min(a_count / ENTRY_THRESHOLD, 1.0)

                    st.progress(f_progress, text=f"同意票進度: {f_count} / {ENTRY_THRESHOLD}")
                    st.progress(a_progress, text=f"不同意票進度: {a_count} / {ENTRY_THRESHOLD}")
                    with st.expander("查看不同意理由", expanded=False):
                        render_reason_lines(against_reason_map, "暫時未有已記錄的不同意理由。")
                    
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
                                against_reason_map.pop(user_id, None)
                                query = "UPDATE topic_votes SET against_users=:new_against_str, agree_users=:new_agree_str, against_reasons=:against_reasons WHERE topic=:topic"
                                param = {
                                    "new_against_str": against_list,
                                    "new_agree_str": agree_list,
                                    "against_reasons": dump_json(against_reason_map),
                                    "topic": topic
                                }
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
                    selected_reason_choices = st.multiselect(
                        "不同意原因",
                        options=TOPIC_REJECTION_REASONS,
                        key=f"against_reason_choices_{i}"
                    )
                    other_reason = st.text_area(
                        "其他原因",
                        key=f"against_reason_other_{i}",
                        placeholder="如需要，可補充具體修訂意見。"
                    )
                    if user_id in against_list:
                        if st.button("已反對 (點擊撤回)", key=f"a_done_{i}"):
                            with st.spinner("撤回投票中..."):
                                against_list.remove(user_id)
                                against_reason_map.pop(user_id, None)
                                query = "UPDATE topic_votes SET against_users=:new_against_str, against_reasons=:against_reasons WHERE topic=:topic"
                                param = {
                                    "new_against_str": against_list,
                                    "against_reasons": dump_json(against_reason_map),
                                    "topic": topic
                                }
                                execute_query(query, param)
                                st.toast("已撤回不同意票！", icon="↩️")
                                after_vote()
                    elif user_id in agree_list:
                        if st.button("轉投反對", key=f"switch_to_a_{i}"):
                            selected_reasons = collect_reasons(selected_reason_choices, other_reason)
                            if not selected_reasons:
                                st.warning("請先選擇或輸入不同意原因。")
                                st.stop()
                            with st.spinner("更改投票中..."):
                                agree_list.remove(user_id)
                                against_list.append(user_id)
                                against_reason_map[user_id] = selected_reasons
                                query = "UPDATE topic_votes SET agree_users=:new_agree_str, against_users=:new_against_str, against_reasons=:against_reasons WHERE topic=:topic"
                                param = {
                                    "new_agree_str": agree_list,
                                    "new_against_str": against_list,
                                    "against_reasons": dump_json(against_reason_map),
                                    "topic": topic
                                }
                                execute_query(query, param)
                                st.toast("已轉投不同意票！", icon="↪️️")
                                after_vote()
                    else:
                        if st.button("❌ 不同意", key=f"vote_a_{i}"):
                            selected_reasons = collect_reasons(selected_reason_choices, other_reason)
                            if not selected_reasons:
                                st.warning("請先選擇或輸入不同意原因。")
                                st.stop()
                            with st.spinner("處理你的投票中，請稍等⋯"):
                                against_list.append(user_id)
                                against_reason_map[user_id] = selected_reasons
                                query = "UPDATE topic_votes SET against_users=:new_against_str, against_reasons=:against_reasons WHERE topic=:topic"
                                param = {
                                    "new_against_str": against_list,
                                    "against_reasons": dump_json(against_reason_map),
                                    "topic": topic
                                }
                                execute_query(query, param)
                                st.toast("已投下不同意票！", icon="☑️")
                                after_vote()

            if f_count >= ENTRY_THRESHOLD and f_count > a_count:
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

            if a_count >= ENTRY_THRESHOLD and a_count > f_count:
                st.error(f"辯題「{topic}」已獲得{a_count}票不同意票，正在刪除辯題...")

                query = "UPDATE topic_votes SET status='rejected', agree_users=:new_agree_str, against_users=:new_against_str WHERE topic=:topic"
                param = {"new_agree_str": agree_list, "new_against_str": against_list, "topic": topic}
                execute_query(query, param)
                get_vote_data.clear()
                st.snow()
                st.rerun()

            if deadline_passed:
                st.warning(f"辯題「{topic}」投票期限（{deadline_str} 23:59）已過，未達入庫標準，系統自動否決。")
                query = "UPDATE topic_votes SET status='rejected', agree_users=:new_agree_str, against_users=:new_against_str WHERE topic=:topic"
                param = {"new_agree_str": agree_list, "new_against_str": against_list, "topic": topic}
                execute_query(query, param)
                get_vote_data.clear()
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
    st.caption(f"目前活躍成員：{_active_count} 人 ｜ 罷免門檻：{DEPOSE_THRESHOLD} 票")
    st.caption(f"只要同意罷免票數 ≥ {DEPOSE_THRESHOLD} 且 同意 > 不同意，系統會自動刪除辯題。")
    st.caption(f"只要不同意罷免票數 ≥ {DEPOSE_THRESHOLD} 且 不同意 > 同意，系統會自動刪除罷免動議。")

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
            proposal_reasons = parse_reason_list(row.get("proposal_reasons", ""))

            agree_list = row.get("agree_users", "")
            if not isinstance(agree_list, list):
                agree_list = []
            against_list = row.get("against_users", "")
            if not isinstance(against_list, list):
                against_list = []
            
            f_count = len(agree_list)
            a_count = len(against_list)

            depose_deadline_val = row.get("deadline", "")
            depose_deadline_passed = False
            depose_deadline_str = ""
            if depose_deadline_val and depose_deadline_val != "":
                try:
                    if hasattr(depose_deadline_val, 'date'):
                        depose_deadline_date = depose_deadline_val.date() if hasattr(depose_deadline_val, 'hour') else depose_deadline_val
                    else:
                        depose_deadline_date = datetime.strptime(str(depose_deadline_val)[:10], "%Y-%m-%d").date()
                    today_hk = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
                    depose_deadline_passed = today_hk > depose_deadline_date
                    depose_deadline_str = depose_deadline_date.strftime("%Y-%m-%d")
                except Exception:
                    pass

            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])

                def after_vote_depose():
                    get_vote_data.clear()
                    st.rerun()

                with c1:
                    st.write(f"**{topic}**")
                    depose_deadline_display = f" | 截止：{depose_deadline_str} 23:59" if depose_deadline_str else ""
                    st.caption(f"提出者: {mover} | 目前票數 - 同意罷免: {f_count} | 不同意罷免: {a_count}{depose_deadline_display}")
                    if proposal_reasons:
                        st.caption(f"提出原因：{'；'.join(proposal_reasons)}")

                    f_progress = min(f_count / DEPOSE_THRESHOLD, 1.0)
                    a_progress = min(a_count / DEPOSE_THRESHOLD, 1.0)

                    st.progress(f_progress, text=f"同意罷免進度: {f_count} / {DEPOSE_THRESHOLD}")
                    st.progress(a_progress, text=f"不同意罷免進度: {a_count} / {DEPOSE_THRESHOLD}")
                    
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

            if f_count >= DEPOSE_THRESHOLD and f_count > a_count:
                st.error(f"罷免動議「{topic}」已獲通過，正在從辯題庫刪除該辯題...")

                query1 = "DELETE FROM topic_depose_votes WHERE topic=:topic"
                query2 = "DELETE FROM topics WHERE topic=:topic"
                param = {"topic": topic}
                execute_query(query1, param)
                execute_query(query2, param)
                get_vote_data.clear()
                st.snow()
                st.rerun()

            if a_count >= DEPOSE_THRESHOLD and a_count > f_count:
                st.success(f"罷免動議「{topic}」已被否決，正在刪除該罷免動議...")

                query = "DELETE FROM topic_depose_votes WHERE topic=:topic"
                param = {"topic": topic}
                execute_query(query, param)
                get_vote_data.clear()
                st.balloons()
                st.rerun()

            if depose_deadline_passed:
                st.warning(f"罷免動議「{topic}」投票期限（{depose_deadline_str} 23:59）已過，未達罷免標準，動議自動取消。")
                query = "DELETE FROM topic_depose_votes WHERE topic=:topic"
                param = {"topic": topic}
                execute_query(query, param)
                get_vote_data.clear()
                st.rerun()


with tab4:
    st.subheader("成員參與率")
    st.caption("計算辯題投票及罷免投票的整體參與情況。活躍成員標準：整體投票率 ≥ 40% 且 最近10次投票至少參與3次。")

    if st.button("🔄 查看最新數據", key="refresh_member_stats"):
        st.cache_data.clear()

    member_stats, total_topic_votes = get_member_participation_stats()
    st.caption(f"辯題投票 + 罷免投票總數：{total_topic_votes} 個")

    if member_stats:
        st.dataframe(member_stats, use_container_width=True, hide_index=True)
    else:
        st.info("暫無成員資料。")


with tab5:
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
