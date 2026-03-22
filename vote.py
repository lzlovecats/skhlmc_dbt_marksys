import json
import math
import streamlit as st
from functions import check_committee_login, get_connection, execute_query, del_cookie, committee_cookie_manager, return_gemini_reminder, return_chatgpt_reminder, return_gemini_depose_reminder, return_chatgpt_depose_reminder, get_active_user_count, get_member_participation_stats, CATEGORIES, DIFFICULTY_OPTIONS
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

st.header("辯題徵集、投票及罷免系統")

TOPIC_REJECTION_REASONS = [
    "表述或界定不清",
    "正反責任失衡",
    "與現有題目重複或相似",
    "討論價值不足",
    "題目表述可再修訂",
    "類別分類不當",
    "難度分類不當",
]

DEPOSE_REASONS = [
    "題目已過時",
    "表述或界定不清",
    "正反責任失衡",
    "與現有題目重複或相似",
    "討論價值不足",
    "已有更佳版本可取代",
    "類別分類不當",
    "難度分類不當",
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
    from collections import Counter
    all_reasons = []
    for reasons in reason_map.values():
        all_reasons.extend(parse_reason_list(reasons))
    if not all_reasons:
        st.caption(empty_text)
        return
    for reason, count in Counter(all_reasons).most_common():
        suffix = f"（{count} 人）" if count > 1 else ""
        st.caption(f"• {reason}{suffix}")


def parse_deadline_row(row, key="deadline"):
    # row: the row of the vote data
    """Returns (deadline_passed: bool, deadline_str: str)."""
    deadline_val = row.get(key, "")
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
    return deadline_passed, deadline_str


def clear_caches():
    get_vote_data.clear()
    from functions import get_active_user_count, get_member_participation_stats, _get_combined_vote_records
    get_active_user_count.clear()
    get_member_participation_stats.clear()
    _get_combined_vote_records.clear()


def _after_vote():
    clear_caches()
    st.rerun()


def render_refresh_button(key):
    if st.button("🔄 重新整理", key=key):
        clear_caches()
        st.rerun()


def render_vote_buttons(i, user_id, topic, agree_list, against_list, against_reason_map,
                        table, agree_label, against_label, after_vote_fn, col2, col3,
                        against_dialog_fn=None, agree_switch_toast="已轉投同意票！"):
    """Renders the agree (col2) and against (col3) vote button columns."""
    with col2:
        if user_id in agree_list:
            if st.button("已同意 (點擊撤回)", key=f"{table}_f_done_{i}"):
                with st.spinner("撤回投票中..."):
                    execute_query(
                        f"UPDATE {table} SET agree_users = array_remove(agree_users, :user_id) WHERE topic = :topic",
                        {"user_id": user_id, "topic": topic}
                    )
                    st.toast("已撤回同意票！", icon="↩️")
                    after_vote_fn()
        elif user_id in against_list:
            if st.button("轉投同意", key=f"{table}_switch_to_f_{i}"):
                with st.spinner("更改投票中..."):
                    against_reason_map.pop(user_id, None)
                    execute_query(
                        f"UPDATE {table} SET against_users = array_remove(against_users, :user_id), agree_users = array_append(agree_users, :user_id), against_reasons = :against_reasons WHERE topic = :topic",
                        {"user_id": user_id, "against_reasons": dump_json(against_reason_map), "topic": topic}
                    ) if table == "topic_votes" else execute_query(
                        f"UPDATE {table} SET against_users = array_remove(against_users, :user_id), agree_users = array_append(agree_users, :user_id) WHERE topic = :topic",
                        {"user_id": user_id, "topic": topic}
                    )
                    st.toast(agree_switch_toast, icon="↪️️")
                    after_vote_fn()
        else:
            if st.button(f"✅ {agree_label}", key=f"{table}_vote_f_{i}"):
                with st.spinner("處理你的投票中，請稍等⋯"):
                    execute_query(
                        f"UPDATE {table} SET agree_users = array_append(agree_users, :user_id) WHERE topic = :topic",
                        {"user_id": user_id, "topic": topic}
                    )
                    st.toast("已投下同意票！", icon="☑️")
                    after_vote_fn()

    with col3:
        if user_id in against_list:
            if st.button("已反對 (點擊撤回)", key=f"{table}_a_done_{i}"):
                with st.spinner("撤回投票中..."):
                    against_reason_map.pop(user_id, None)
                    execute_query(
                        f"UPDATE {table} SET against_users = array_remove(against_users, :user_id), against_reasons = :against_reasons WHERE topic = :topic",
                        {"user_id": user_id, "against_reasons": dump_json(against_reason_map), "topic": topic}
                    ) if table == "topic_votes" else execute_query(
                        f"UPDATE {table} SET against_users = array_remove(against_users, :user_id) WHERE topic = :topic",
                        {"user_id": user_id, "topic": topic}
                    )
                    st.toast("已撤回不同意票！", icon="↩️")
                    after_vote_fn()
        elif user_id in agree_list:
            if st.button("轉投反對", key=f"{table}_switch_to_a_{i}"):
                if against_dialog_fn:
                    against_dialog_fn(topic, user_id, against_reason_map, is_switch=True)
                else:
                    with st.spinner("更改投票中..."):
                        execute_query(
                            f"UPDATE {table} SET agree_users = array_remove(agree_users, :user_id), against_users = array_append(against_users, :user_id) WHERE topic = :topic",
                            {"user_id": user_id, "topic": topic}
                        )
                        st.toast("已轉投不同意票！", icon="↪️️")
                        after_vote_fn()
        else:
            if st.button(f"❌ {against_label}", key=f"{table}_vote_a_{i}"):
                if against_dialog_fn:
                    against_dialog_fn(topic, user_id, against_reason_map, is_switch=False)
                else:
                    with st.spinner("處理你的投票中，請稍等⋯"):
                        execute_query(
                            f"UPDATE {table} SET against_users = array_append(against_users, :user_id) WHERE topic = :topic",
                            {"user_id": user_id, "topic": topic}
                        )
                        st.toast("已投下不同意票！", icon="☑️")
                        after_vote_fn()


def check_vote_resolution(f_count, a_count, threshold, topic, agree_list, against_list,
                          mode, author=None, category=None, difficulty=None):
    """Check vote counts and auto-resolve if threshold met. mode: 'topic' or 'depose'."""
    if mode == "topic":
        if f_count >= threshold and f_count > a_count:
            st.success(f"辯題「{topic}」已獲得足夠票數，正在寫入辯題庫...")
            execute_query("INSERT INTO topics (topic, author, category, difficulty) VALUES (:topic, :author, :category, :difficulty)",
                          {"topic": topic, "author": author, "category": category, "difficulty": difficulty})
            execute_query(
                "UPDATE topic_votes SET status='passed', agree_users=:new_agree_str, against_users=:new_against_str WHERE topic=:topic",
                {"new_agree_str": agree_list, "new_against_str": against_list, "topic": topic}
            )
            clear_caches()
            st.balloons()
            st.rerun()
        if a_count >= threshold and a_count > f_count:
            st.error(f"辯題「{topic}」已獲得{a_count}票不同意票，正在刪除辯題...")
            execute_query(
                "UPDATE topic_votes SET status='rejected', agree_users=:new_agree_str, against_users=:new_against_str WHERE topic=:topic",
                {"new_agree_str": agree_list, "new_against_str": against_list, "topic": topic}
            )
            clear_caches()
            st.rerun()
    elif mode == "depose":
        if f_count >= threshold and f_count > a_count:
            st.error(f"罷免動議「{topic}」已獲通過，正在從辯題庫刪除該辯題...")
            execute_query("DELETE FROM topic_depose_votes WHERE topic=:topic", {"topic": topic})
            execute_query("DELETE FROM topics WHERE topic=:topic", {"topic": topic})
            clear_caches()
            st.rerun()
        if a_count >= threshold and a_count > f_count:
            st.success(f"罷免動議「{topic}」已被否決，正在刪除該罷免動議...")
            execute_query("DELETE FROM topic_depose_votes WHERE topic=:topic", {"topic": topic})
            clear_caches()
            st.balloons()
            st.rerun()


# Get committee cookie manager first
cm = committee_cookie_manager()

@st.dialog("嚟自Gemini嘅提醒")
def show_gemini_reminder(reminder_fn):
    st.markdown(reminder_fn())

@st.dialog("嚟自ChatGPT嘅提醒")
def show_chatgpt_reminder(reminder_fn):
    st.markdown(reminder_fn())

@st.dialog("投反對票")
def cast_against_vote_dialog(topic, user_id, against_reason_map, is_switch=False):
    st.write(f"**{topic}**")
    if is_switch:
        st.info("你目前已投同意票，確認後將轉為反對票。")
    selected_reasons = st.multiselect(
        "請選擇不同意原因（至少選一項）",
        options=TOPIC_REJECTION_REASONS
    )
    other_reason = st.text_area(
        "其他原因（如有）",
        placeholder="如需要，可補充具體修訂意見。"
    )
    if st.button("確認投票", type="primary"):
        reasons = collect_reasons(selected_reasons, other_reason)
        if not reasons:
            st.warning("請至少選擇或輸入一個不同意原因。")
        else:
            with st.spinner("處理你的投票中，請稍等⋯"):
                against_reason_map[user_id] = reasons
                if is_switch:
                    execute_query(
                        "UPDATE topic_votes SET agree_users = array_remove(agree_users, :user_id), against_users = array_append(against_users, :user_id), against_reasons = :against_reasons WHERE topic = :topic",
                        {"user_id": user_id, "against_reasons": dump_json(against_reason_map), "topic": topic}
                    )
                    st.toast("已轉投不同意票！", icon="↪️️")
                else:
                    execute_query(
                        "UPDATE topic_votes SET against_users = array_append(against_users, :user_id), against_reasons = :against_reasons WHERE topic = :topic",
                        {"user_id": user_id, "against_reasons": dump_json(against_reason_map), "topic": topic}
                    )
                    st.toast("已投下不同意票！", icon="☑️")
                clear_caches()
                st.rerun()

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
    df = conn.query("SELECT * FROM topic_votes ORDER BY created_at DESC", ttl=5)
    df = df.fillna("")
    for col in ["agree_users", "against_users"]:
        df[col] = df[col].apply(lambda x: x if isinstance(x, list) else [])

    pending = df[df['status'] == 'pending'].to_dict('records')
    passed = df[df['status'] == 'passed']['topic'].tolist()
    rejected = df[df['status'] == 'rejected']['topic'].tolist()
    return pending, passed, rejected

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 提出動議", "📊 辯題投票", "✂️ 罷免投票", "👥 成員參與率", "🔐 管理帳戶"])

with tab1:
    st.subheader("提出新辯題")
    st.caption(f"目前活躍成員：{_active_count} 人 ｜ 入庫門檻：{ENTRY_THRESHOLD} 票")
    new_topic = st.text_input("請輸入完整辯題")
    new_category = st.selectbox("辯題類別", options=CATEGORIES)
    st.caption("辯題難度標準：")
    st.caption("Lv1：概念日常、背景知識少，適合完全無經驗的新手")
    st.caption("Lv2：需要一定議題認識或邏輯鋪陳，但唔需要專業知識")
    st.caption("Lv3：涉及專業政策、複雜概念界定、或需要大量資料支撐")
    new_difficulty = st.selectbox(
        "辯題難度",
        options=[1, 2, 3],
        format_func=lambda x: DIFFICULTY_OPTIONS[x]
    )

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

    if "confirm_imbalance" not in st.session_state:
        st.session_state["confirm_imbalance"] = False

    if st.button("提交辯題", disabled=submit_disabled):
        if not new_topic.strip():
            st.warning("你未輸入任何文字！")
        else:
            conn = get_connection()
            all_topics_df = conn.query("SELECT topic, category FROM topics", ttl=5)
            all_votes_df = conn.query("SELECT topic FROM topic_votes WHERE status = 'pending'", ttl=5)

            existing_topics = all_topics_df["topic"].tolist() if not all_topics_df.empty else []
            existing_votes = all_votes_df["topic"].tolist() if not all_votes_df.empty else []

            if new_topic in existing_votes or new_topic in existing_topics:
                st.error("此辯題已存在！")
            else:
                if not all_topics_df.empty:
                    total_topics = len(all_topics_df)
                    cat_count = int((all_topics_df["category"] == new_category).sum())
                    cat_ratio = cat_count / total_topics
                else:
                    total_topics = 0
                    cat_count = 0
                    cat_ratio = 0

                if cat_ratio > 0.2:
                    st.session_state["confirm_imbalance"] = True
                    st.session_state["pending_topic_data"] = {
                        "new_topic": new_topic, "new_category": new_category, "new_difficulty": new_difficulty
                    }
                    st.warning(
                        f"⚠️ 類別「{new_category}」目前已佔辯題庫 **{cat_ratio*100:.1f}%**"
                        f"（共 {total_topics} 題中有 {cat_count} 題）。"
                        "繼續新增同類辯題將令辯題庫失衡。"
                    )
                else:
                    hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
                    hk_time = hk_now.strftime("%Y-%m-%d %H:%M:%S")
                    deadline = (hk_now.date() + timedelta(days=7)).strftime("%Y-%m-%d")
                    query = "INSERT INTO topic_votes (topic, author, status, agree_users, against_users, created_at, deadline, threshold, category, difficulty) VALUES (:new_topic, :user_id, 'pending', :agree_users, :against_users, :created_at, :deadline, :threshold, :category, :difficulty)"
                    param = {"new_topic": new_topic, "user_id": user_id, "agree_users": "{}", "against_users": "{}", "created_at": hk_time, "deadline": deadline, "threshold": ENTRY_THRESHOLD, "category": new_category, "difficulty": new_difficulty}
                    execute_query(query, param)
                    clear_caches()
                    st.success("辯題已加入投票區！")

    if st.session_state.get("confirm_imbalance"):
        d = st.session_state["pending_topic_data"]
        st.warning(
            f"⚠️ 類別「{d['new_category']}」目前佔比已超過 20%，繼續新增同類辯題將令辯題庫失衡。是否確認繼續？"
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ 確認繼續提交"):
                hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
                hk_time = hk_now.strftime("%Y-%m-%d %H:%M:%S")
                deadline = (hk_now.date() + timedelta(days=7)).strftime("%Y-%m-%d")
                query = "INSERT INTO topic_votes (topic, author, status, agree_users, against_users, created_at, deadline, threshold, category, difficulty) VALUES (:new_topic, :user_id, 'pending', :agree_users, :against_users, :created_at, :deadline, :threshold, :category, :difficulty)"
                param = {"new_topic": d["new_topic"], "user_id": user_id, "agree_users": "{}", "against_users": "{}", "created_at": hk_time, "deadline": deadline, "threshold": ENTRY_THRESHOLD, "category": d["new_category"], "difficulty": d["new_difficulty"]}
                execute_query(query, param)
                clear_caches()
                st.session_state["confirm_imbalance"] = False
                st.success("辯題已加入投票區！")
        with col2:
            if st.button("❌ 取消"):
                st.session_state["confirm_imbalance"] = False
                st.rerun()

    st.divider()
    st.subheader("提出罷免動議")
    st.caption(f"目前活躍成員：{_active_count} 人 ｜ 罷免門檻：{DEPOSE_THRESHOLD} 票")

    try:
        conn = get_connection()
        df = conn.query("SELECT * FROM topics", ttl=5)
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
            exist_votes = conn.query("SELECT topic FROM topic_depose_votes", ttl=5)
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
                        topic, mover, agree_users, against_users, created_at, proposal_reasons, deadline, threshold
                    ) VALUES (
                        :topic, :user_id, :agree_users, :against_users, :created_at, :proposal_reasons, :deadline, :threshold
                    )
                    """
                    param = {
                        "topic": t,
                        "user_id": user_id,
                        "agree_users": "{}",
                        "against_users": "{}",
                        "created_at": hk_time,
                        "proposal_reasons": dump_json(proposal_reasons),
                        "deadline": deadline,
                        "threshold": DEPOSE_THRESHOLD
                    }
                    execute_query(query, param)
            clear_caches()
            if proposed:
                st.success("罷免動議已提出！")
            else:
                st.warning("有辯題已存在於罷免動議區，該辯題將不會被重複提出。其他辯題已成功提出罷免動議。")


with tab2:
    st.subheader("待表決辯題")
    st.caption(f"只要同意票數達入庫門檻 且 同意 > 不同意，系統會自動將辯題新增至辯題庫。")
    st.caption(f"只要不同意票數達入庫門檻 且 不同意 > 同意，系統會自動刪除辯題。")

    button_col1, button_col2, button_col3 = st.columns([1, 1, 1])
    with button_col1:
        render_refresh_button("refresh_vote_tab2")

    with button_col2:
        if st.button("💡 Gemini提提你", key="gemini_tab2"):
            show_gemini_reminder(return_gemini_reminder)

    with button_col3:
        if st.button("🔍 ChatGPT提提你", key="chatgpt_tab2"):
            show_chatgpt_reminder(return_chatgpt_reminder)
    st.divider()
    
    vote_data, passed_list, rejected_list = get_vote_data()
    
    if not vote_data:
        st.info("目前沒有待表決的辯題。")
    else:
        conn = get_connection()

        for i, row in enumerate(vote_data):
            topic = row["topic"]
            author = row["author"]

            agree_list = row["agree_users"]
            against_list = row["against_users"]
            against_reason_map = parse_reason_map(row.get("against_reasons", ""))

            f_count = len(agree_list)
            a_count = len(against_list)
            row_threshold = int(row.get("threshold") or ENTRY_THRESHOLD)

            deadline_passed, deadline_str = parse_deadline_row(row)

            # Auto-reject expired topics before rendering the card (avoids flash)
            if deadline_passed:
                st.warning(f"辯題「{topic}」投票期限（{deadline_str} 23:59）已過，未達入庫標準，系統自動否決。")
                query = "UPDATE topic_votes SET status='rejected', agree_users=:new_agree_str, against_users=:new_against_str WHERE topic=:topic"
                param = {"new_agree_str": agree_list, "new_against_str": against_list, "topic": topic}
                execute_query(query, param)
                clear_caches()
                st.rerun()

            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])

                with c1:
                    st.write(f"**{topic}**")
                    cat = row.get("category") or "—"
                    diff = row.get("difficulty")
                    diff_label = DIFFICULTY_OPTIONS.get(int(diff), "—") if diff else "—"
                    st.caption(f"🏷️ {cat}　｜　{diff_label}")
                    deadline_display = f" | 截止：{deadline_str} 23:59" if deadline_str else ""
                    st.caption(f"提出者：{author} | 入庫門檻：{row_threshold} 票 | 目前票數 - 同意: {f_count} | 不同意: {a_count}{deadline_display}")

                    f_progress = min(f_count / row_threshold, 1.0)
                    a_progress = min(a_count / row_threshold, 1.0)

                    st.progress(f_progress, text=f"同意票進度: {f_count} / {row_threshold}")
                    st.progress(a_progress, text=f"不同意票進度: {a_count} / {row_threshold}")
                    with st.expander("查看不同意理由", expanded=False):
                        render_reason_lines(against_reason_map, "暫時未有已記錄的不同意理由。")
                    
                render_vote_buttons(
                    i, user_id, topic, agree_list, against_list, against_reason_map,
                    table="topic_votes", agree_label="同意", against_label="不同意",
                    after_vote_fn=_after_vote, col2=c2, col3=c3,
                    against_dialog_fn=cast_against_vote_dialog
                )

            check_vote_resolution(f_count, a_count, row_threshold, topic, agree_list, against_list,
                                   mode="topic", author=author,
                                   category=row.get("category"), difficulty=row.get("difficulty"))

    st.divider()
    
    with st.expander("📜 已通過辯題記錄 (最近十個)", expanded=False):
        if passed_list:
            for item in passed_list[:10]:
                st.write(f"✅ {item}")
        else:
            st.caption("暫無記錄")

    with st.expander("🗑️ 已否決辯題記錄 (最近十個)", expanded=False):
        if rejected_list:
            for item in rejected_list[:10]:
                st.write(f"❌ {item}")
        else:
            st.caption("暫無記錄")


with tab3:
    st.subheader("罷免投票")
    st.caption(f"只要同意罷免票數達罷免門檻 且 同意 > 不同意，系統會自動刪除辯題。")
    st.caption(f"只要不同意罷免票數達罷免門檻 且 不同意 > 同意，系統會自動刪除罷免動議。")

    button_col1, button_col2, button_col3 = st.columns([1, 1, 1])
    with button_col1:
        render_refresh_button("refresh_vote_tab3")
    with button_col2:
        if st.button("💡 Gemini提醒你", key="gemini_tab3"):
            show_gemini_reminder(return_gemini_depose_reminder)
    with button_col3:
        if st.button("🔍 ChatGPT提醒你", key="chatgpt_tab3"):
            show_chatgpt_reminder(return_chatgpt_depose_reminder)

    conn = get_connection()
    df_depose = conn.query("SELECT * FROM topic_depose_votes ORDER BY created_at DESC", ttl=5)
    for col in ["agree_users", "against_users"]:
        df_depose[col] = df_depose[col].apply(lambda x: x if isinstance(x, list) else [])
    vote_data = df_depose.to_dict('records')

    topics_meta_df = conn.query("SELECT topic, category, difficulty FROM topics", ttl=5)
    topic_meta = {r["topic"]: (r.get("category"), r.get("difficulty")) for _, r in topics_meta_df.iterrows()}

    if not vote_data:
        st.info("目前沒有待罷免的辯題。")
    else:
        for i, row in enumerate(vote_data):
            topic = row["topic"]
            mover = row["mover"]
            proposal_reasons = parse_reason_list(row.get("proposal_reasons", ""))

            agree_list = row["agree_users"]
            against_list = row["against_users"]

            f_count = len(agree_list)
            a_count = len(against_list)
            row_depose_threshold = int(row.get("threshold") or DEPOSE_THRESHOLD)

            depose_deadline_passed, depose_deadline_str = parse_deadline_row(row)

            # Auto-dismiss expired motions before rendering the card (avoids flash)
            # Note: expired depose motions are hard-deleted (no audit trail needed).
            # Topic vote expiries use UPDATE status='rejected' to preserve the rejection log in tab2.
            if depose_deadline_passed:
                st.warning(f"罷免動議「{topic}」投票期限（{depose_deadline_str} 23:59）已過，未達罷免標準，動議自動取消。")
                query = "DELETE FROM topic_depose_votes WHERE topic=:topic"
                param = {"topic": topic}
                execute_query(query, param)
                clear_caches()
                st.rerun()

            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])

                with c1:
                    st.write(f"**{topic}**")
                    meta = topic_meta.get(topic, (None, None))
                    depose_cat = meta[0] or "—"
                    depose_diff = meta[1]
                    depose_diff_label = DIFFICULTY_OPTIONS.get(int(depose_diff), "—") if depose_diff else "—"
                    st.caption(f"🏷️ {depose_cat}　｜　{depose_diff_label}")
                    depose_deadline_display = f" | 截止：{depose_deadline_str} 23:59" if depose_deadline_str else ""
                    st.caption(f"提出者: {mover} | 罷免門檻：{row_depose_threshold} 票 | 目前票數 - 同意罷免: {f_count} | 不同意罷免: {a_count}{depose_deadline_display}")
                    if proposal_reasons:
                        st.caption(f"提出原因：{'；'.join(proposal_reasons)}")

                    f_progress = min(f_count / row_depose_threshold, 1.0)
                    a_progress = min(a_count / row_depose_threshold, 1.0)

                    st.progress(f_progress, text=f"同意罷免進度: {f_count} / {row_depose_threshold}")
                    st.progress(a_progress, text=f"不同意罷免進度: {a_count} / {row_depose_threshold}")
                    
                render_vote_buttons(
                    i, user_id, topic, agree_list, against_list, against_reason_map={},
                    table="topic_depose_votes", agree_label="同意罷免", against_label="不同意罷免",
                    after_vote_fn=_after_vote, col2=c2, col3=c3,
                    agree_switch_toast="已轉投同意罷免票！"
                )

            check_vote_resolution(f_count, a_count, row_depose_threshold, topic, agree_list, against_list,
                                   mode="depose")



with tab4:
    st.subheader("成員參與率")
    st.caption("計算辯題投票及罷免投票的整體參與情況。活躍成員標準：整體投票率 ≥ 40% 且 最近10次投票至少參與3次。")

    if st.button("🔄 重新整理", key="refresh_member_stats"):
        st.cache_data.clear()

    member_stats, total_topic_votes = get_member_participation_stats()
    num_of_active, _ = get_active_user_count()
    st.caption(f"辯題投票 + 罷免投票總數：{total_topic_votes} 個")
    st.caption(f"目前活躍成員：{num_of_active} 人")

    if member_stats and user_id != "admin":
        current_user_stats = next((s for s in member_stats if s["用戶"] == user_id), None)
        if current_user_stats:
            st.subheader("我的參與情況")
            m1, m2, m3 = st.columns(3)
            m1.metric("整體投票率", current_user_stats["整體投票率"])
            m2.metric("最近10次參與", f"{current_user_stats['最近10次參與']} / 10")
            m3.metric("活躍狀態", current_user_stats["活躍狀態"])
            st.divider()

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
