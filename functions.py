import streamlit as st
import json
import pandas as pd
import random
import math
import extra_streamlit_components as stx
import datetime
import time
import os
import io
from sqlalchemy import text

CATEGORIES = [
    "國際與時事", "科技與未來", "文化與生活",
    "香港社會政策", "青少年與教育", "哲理／價值觀"
]
DIFFICULTY_OPTIONS = {1: "Lv1 — 概念日常", 2: "Lv2 — 一般議題", 3: "Lv3 — 進階專業"}

def get_cookie(cookie_manager, key, default=None):
    try:
        value = cookie_manager.get(key)
        return default if value is None else value
    except Exception:
        return default


def set_cookie(cookie_manager, key, value, expires_at=None):
    try:
        if expires_at is None:
            cookie_manager.set(key, value)
        else:
            cookie_manager.set(key, value, expires_at=expires_at)
        return True
    except Exception:
        return False


def del_cookie(cookie_manager, key):
    try:
        cookie_manager.delete(key)
        return True
    except Exception:
        return False


def committee_cookie_manager():
    if "committee_cookie_manager" not in st.session_state:
        st.session_state["committee_cookie_manager"] = stx.CookieManager(key="committee_cookies")
    return st.session_state["committee_cookie_manager"]


def check_admin():
    if "admin_logged_in" not in st.session_state:
        st.session_state["admin_logged_in"] = False

    if not st.session_state["admin_logged_in"]:
        st.subheader("賽會人員登入")
        pwd = st.text_input("請輸入賽會人員密碼", type="password")
        if st.button("登入"):
            if pwd == st.secrets["admin_password"]:
                st.session_state["admin_logged_in"] = True
                st.rerun()
            else:
                st.error("密碼錯誤")
        return False
    return True


def check_score():
    if "score_logged_in" not in st.session_state:
        st.session_state["score_logged_in"] = False

    if not st.session_state["score_logged_in"]:
        st.subheader("查閱比賽分紙登入")
        pwd = st.text_input("請輸入由賽會人員提供的密碼", type="password")
        if st.button("登入"):
            if pwd == st.secrets["score_password"]:
                st.session_state["score_logged_in"] = True
                st.rerun()
            else:
                st.error("密碼錯誤")
        return False
    return True


def get_connection():
    try:
        conn = st.connection("postgresql", type="sql")
        return conn
    except Exception as e:
        st.error(f"連線錯誤: {e}")
        return None


def execute_query(sql_str, params=None):
    conn = get_connection()
    with conn.session as s:
        s.execute(text(sql_str), params or {})
        s.commit()


def query_params(sql_str, params=None):
    conn = get_connection()
    with conn.session as s:
        result = s.execute(text(sql_str), params or {})
        rows = result.fetchall()
        columns = list(result.keys())
    return pd.DataFrame(rows, columns=columns)


def execute_query_count(sql_str, params=None):
    conn = get_connection()
    with conn.session as s:
        result = s.execute(text(sql_str), params or {})
        count = result.rowcount
        s.commit()
    return count


def load_matches_from_db():
    conn = get_connection()
    df = conn.query("SELECT * FROM MATCHES", ttl=0)

    data_dict = {}
    for i, row in df.iterrows():
        match_id = str(row["match_id"])
        raw = row.to_dict()
        # Normalise DB column names to the app-internal keys used by judging.py / match_info.py
        raw["que"] = raw.pop("topic", raw.get("que", ""))
        raw["pro"] = raw.pop("pro_team", raw.get("pro", ""))
        raw["con"] = raw.pop("con_team", raw.get("con", ""))
        # BPCHAR(10) columns come back padded with trailing spaces — strip them
        for key in ["pro_1", "pro_2", "pro_3", "pro_4", "con_1", "con_2", "con_3", "con_4"]:
            if key in raw and raw[key]:
                raw[key] = str(raw[key]).strip()
        data_dict[match_id] = raw

    return data_dict


def save_match_to_db(match_data):
    conn = get_connection()

    exist_match = query_params(
        "SELECT * FROM MATCHES WHERE match_id = :match_id",
        {"match_id": match_data['match_id'].strip()}
    )

    params = {
        "match_id": match_data['match_id'].strip(),
        "date": match_data['date'] if match_data['date'] else None,
        "time": match_data['time'] if match_data['time'] else None,
        "topic": match_data['que'],
        "pro_team": match_data['pro'],
        "con_team": match_data['con'],
        "pro_1": match_data['pro_1'],
        "pro_2": match_data['pro_2'],
        "pro_3": match_data['pro_3'],
        "pro_4": match_data['pro_4'],
        "con_1": match_data['con_1'],
        "con_2": match_data['con_2'],
        "con_3": match_data['con_3'],
        "con_4": match_data['con_4'],
        "access_code": match_data['access_code']
    }

    if not exist_match.empty:
        query = """
            UPDATE MATCHES SET 
                date = :date, time = :time, topic = :topic,
                pro_team = :pro_team, con_team = :con_team,
                pro_1 = :pro_1, pro_2 = :pro_2, pro_3 = :pro_3, pro_4 = :pro_4,
                con_1 = :con_1, con_2 = :con_2, con_3 = :con_3, con_4 = :con_4,
                access_code = :access_code
            WHERE match_id = :match_id
        """
        execute_query(query, params)
    else:
        query = """
            INSERT INTO MATCHES (
                match_id, date, time, topic, pro_team, con_team, 
                pro_1, pro_2, pro_3, pro_4, con_1, con_2, con_3, con_4, access_code
            ) VALUES (
                :match_id, :date, :time, :topic, :pro_team, :con_team,
                :pro_1, :pro_2, :pro_3, :pro_4, :con_1, :con_2, :con_3, :con_4, :access_code
            )
        """
        execute_query(query, params)


def save_draft_to_db(match_id, judge_name, team_side, score_data):
    conn = get_connection()

    data_to_save = score_data.copy()
    if "raw_df_a" in data_to_save:
        data_to_save["raw_df_a"] = data_to_save["raw_df_a"].to_json()
    if "raw_df_b" in data_to_save:
        data_to_save["raw_df_b"] = data_to_save["raw_df_b"].to_json()

    json_str = json.dumps(data_to_save, ensure_ascii=False)

    exist_record = query_params(
        "SELECT * FROM temp_scores WHERE match_id = :match_id AND judge_name = :judge_name AND team_side = :team_side",
        {"match_id": match_id, "judge_name": judge_name, "team_side": team_side}
    )

    if not exist_record.empty:
        execute_query(
            "UPDATE temp_scores SET data = :data WHERE match_id = :match_id AND judge_name = :judge_name AND team_side = :team_side",
            {"data": json_str, "match_id": match_id, "judge_name": judge_name, "team_side": team_side}
        )
    else:
        execute_query(
            "INSERT INTO temp_scores (match_id, judge_name, team_side, data) VALUES (:match_id, :judge_name, :team_side, :data)",
            {"match_id": match_id, "judge_name": judge_name, "team_side": team_side, "data": json_str}
        )
    return True


def load_draft_from_db(match_id, judge_name):
    conn = get_connection()

    exist_temp_data = query_params(
        "SELECT * FROM temp_scores WHERE match_id = :match_id AND judge_name = :judge_name",
        {"match_id": match_id, "judge_name": judge_name}
    )
    
    drafts = {"正方": None, "反方": None}
    for i, row in exist_temp_data.iterrows():
        team_side = str(row["team_side"]).strip()
        if team_side in drafts:
            try:
                data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
                if "raw_df_a" in data and data["raw_df_a"]:
                    data["raw_df_a"] = pd.read_json(io.StringIO(data["raw_df_a"]))
                if "raw_df_b" in data and data["raw_df_b"]:
                    data["raw_df_b"] = pd.read_json(io.StringIO(data["raw_df_b"]))
                drafts[team_side] = data
            except Exception:
                pass  # Corrupt draft data — silently skip, judge will re-enter scores
                
    return drafts


def load_topic_from_db(difficulty=None):
    conn = get_connection()
    if difficulty:
        all_records = conn.query(
            "SELECT * FROM topics WHERE difficulty = :d", params={"d": difficulty}, ttl=0
        )
    else:
        all_records = conn.query("SELECT * FROM topics", ttl=0)
    return all_records["topic"].tolist()


def draw_a_topic(difficulty=None):
    all_topic = load_topic_from_db(difficulty=difficulty)
    if all_topic:
        return random.choice(all_topic)
    else:
        st.error("抽取辯題失敗：辯題庫為空或出現錯誤。")
        return ""


def draw_pro_con(team1, team2):
    return random.sample([team1, team2], 2)


def load_markdown_asset(filename):
    file_path = os.path.join("assets", filename)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"出現錯誤：{filename}無法存取。"


def get_score_data():
    try:
        conn = get_connection()
        data = conn.query("SELECT * FROM scores", ttl=0)
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"讀取評分失敗: {e}")
        return None


def return_user_manual():
    return load_markdown_asset("user_manual.md")


def return_rules():
    return load_markdown_asset("rules.md")


def check_committee_login():
    cookie_manager = committee_cookie_manager()

    if "committee_user" not in st.session_state:
        st.session_state["committee_user"] = None

    # Check cookies for auto-login. CookieManager returns default {} on first run until the
    # browser component runs; give it one rerun so the component can return real cookies.
    if st.session_state["committee_user"] is None:
        if not st.session_state.get("_committee_cookie_rerun_done"):
            st.session_state["_committee_cookie_rerun_done"] = True
            st.rerun()
        cookie_manager.get_all(key="committee_cookies_get")
        committee_cookie = get_cookie(cookie_manager, "committee_user")
        if committee_cookie:
            st.session_state["committee_user"] = committee_cookie
            st.rerun()

    if st.session_state["committee_user"]:
        return True

    st.subheader("賽會人員個人帳戶登入")

    with st.form("committee_login"):
        uid = st.text_input("用戶名稱 (User ID)")
        upw = st.text_input("密碼 (Password)", type="password")
        submitted = st.form_submit_button("登入")

        if submitted:
            conn = get_connection()
            all_acc = conn.query("SELECT * FROM accounts", ttl=0)

            login_success = False
            for i, row in all_acc.iterrows():   
                if str(row.get("userid")) == str(uid) and str(row.get("userpw")) == str(upw):
                    login_success = True
                    break

            if login_success:
                refresh_acc_type(uid)
                st.session_state["committee_user"] = uid
                set_cookie(cookie_manager, "committee_user", uid, expires_at=return_expire_day())
                st.success(f"你好，{uid}！")
                time.sleep(1)
                st.rerun()
            else:
                st.error("User ID或Password錯誤！")


def return_expire_day():
    return datetime.datetime.now() + datetime.timedelta(days=1)


_ACTIVITY_SQL = """
WITH all_votes AS (
    SELECT agree_users, against_users, created_at FROM topic_votes
    WHERE CARDINALITY(agree_users) > 0 OR CARDINALITY(against_users) > 0
    UNION ALL
    SELECT agree_users, against_users, created_at FROM topic_depose_votes
    WHERE CARDINALITY(agree_users) > 0 OR CARDINALITY(against_users) > 0
),
past_10 AS (
    SELECT agree_users, against_users
    FROM all_votes
    ORDER BY created_at DESC
    LIMIT 10
)
SELECT
    (SELECT COUNT(*) FROM all_votes) AS total_votes,
    (SELECT COUNT(*) FROM all_votes
     WHERE :user_id = ANY(agree_users) OR :user_id = ANY(against_users)) AS total_participated,
    (SELECT COUNT(*) FROM past_10
     WHERE :user_id = ANY(agree_users) OR :user_id = ANY(against_users)) AS votes_in_last_10
"""


def refresh_acc_type(user_id: str) -> str | None:
    acc = query_params(
        "SELECT acc_type FROM accounts WHERE userid = :user_id",
        {"user_id": user_id}
    )
    if acc.empty or str(acc.iloc[0]["acc_type"]).strip() == "admin":
        return None

    result = query_params(_ACTIVITY_SQL, {"user_id": user_id})
    total_votes      = int(result.iloc[0]["total_votes"])
    total_participated = int(result.iloc[0]["total_participated"])
    votes_in_last_10 = int(result.iloc[0]["votes_in_last_10"])

    overall_rate = total_participated / total_votes if total_votes > 0 else 0.0
    # Active criteria (matches user manual): overall rate ≥ 40% AND last-10 participation ≥ 3
    new_status = "active" if (overall_rate >= 0.4 and votes_in_last_10 >= 3) else "inactive"
    execute_query(
        "UPDATE accounts SET acc_type = :acc_type WHERE userid = :user_id",
        {"acc_type": new_status, "user_id": user_id}
    )
    return new_status


def refresh_all_acc_type():
    conn = get_connection()
    all_accounts = conn.query("SELECT userid FROM accounts WHERE acc_type != 'admin'", ttl=0)

    for _, row in all_accounts.iterrows():
        refresh_acc_type(row["userid"])


def compute_threshold(base_min: int, pct: float) -> int:
    """計算動態投票門檻：max(base_min, ceil(pct * active_users))"""
    result = query_params(
        "SELECT COUNT(*) AS cnt FROM accounts WHERE acc_type = 'active'"
    )
    active_count = int(result.iloc[0]["cnt"])
    return max(base_min, math.ceil(pct * active_count))


def return_gemini_reminder():
    return load_markdown_asset("gemini_reminder.md")


def return_chatgpt_reminder():
    return load_markdown_asset("chatgpt_reminder.md")


def _get_combined_vote_records():
    """
    Fetches and combines all vote records from topic_votes AND topic_depose_votes,
    sorted by created_at. Returns (vote_records, all_users) where vote_records is
    a list of (agree_list, against_list) tuples in chronological order.

    Only rows where at least one of agree_users / against_users is non-empty are
    counted (i.e. votes that have at least one participation record).
    Rows where both arrays are empty are excluded at the SQL level via CARDINALITY.
    """
    conn = get_connection()
    accounts_df = conn.query("SELECT userid FROM accounts", ttl=0)
    all_users = accounts_df["userid"].tolist() if not accounts_df.empty else []

    tv_df = conn.query(
        "SELECT agree_users, against_users, created_at FROM topic_votes"
        " WHERE CARDINALITY(agree_users) != 0 OR CARDINALITY(against_users) != 0"
        " ORDER BY created_at ASC",
        ttl=0,
    ).fillna("")
    tdv_df = conn.query(
        "SELECT agree_users, against_users, created_at FROM topic_depose_votes"
        " WHERE CARDINALITY(agree_users) != 0 OR CARDINALITY(against_users) != 0"
        " ORDER BY created_at ASC",
        ttl=0,
    ).fillna("")

    combined_df = pd.concat([tv_df, tdv_df], ignore_index=True)
    if not combined_df.empty:
        combined_df = combined_df.sort_values("created_at").reset_index(drop=True)

    vote_records = [
        (
            row["agree_users"] if isinstance(row["agree_users"], list) else [],
            row["against_users"] if isinstance(row["against_users"], list) else [],
        )
        for _, row in combined_df.iterrows()
    ]

    return vote_records, all_users


def get_active_user_count():
    """
    Active user 定義：整體投票率（辯題投票 + 罷免投票）≥ 40% AND 最近10次投票最少投3次。
    Returns (active_count, active_user_list)
    """
    vote_records, all_users = _get_combined_vote_records()
    total_votes = len(vote_records)
    if total_votes == 0 or not all_users:
        return 0, []

    last_10 = vote_records[-10:]

    active_users = []
    for user in all_users:
        total_participated = sum(
            1 for agree, against in vote_records if user in agree or user in against
        )
        overall_rate = total_participated / total_votes

        last10_participated = sum(
            1 for agree, against in last_10 if user in agree or user in against
        )

        if overall_rate >= 0.4 and last10_participated >= 3:
            active_users.append(user)

    return len(active_users), active_users


def get_member_participation_stats():
    """
    Returns (stats_list, total_votes) with per-member participation details
    across both topic_votes and topic_depose_votes.
    """
    vote_records, all_users = _get_combined_vote_records()
    total_votes = len(vote_records)
    last_10 = vote_records[-10:]

    stats = []
    for user in all_users:
        if user == "admin": continue  # Skip admin account from stats
        total_participated = sum(
            1 for agree, against in vote_records if user in agree or user in against
        ) if total_votes > 0 else 0
        overall_rate = total_participated / total_votes if total_votes > 0 else 0

        last10_participated = sum(
            1 for agree, against in last_10 if user in agree or user in against
        )

        is_active = overall_rate >= 0.4 and last10_participated >= 3

        stats.append({
            "用戶": user,
            "整體投票次數": f"{total_participated} / {total_votes}",
            "整體投票率": f"{overall_rate:.1%}",
            "最近10次參與": last10_participated,
            "活躍狀態": "✅ 活躍" if is_active else "❌ 非活躍",
        })

    return stats, total_votes


def return_gemini_depose_reminder():
    return load_markdown_asset("gemini_depose_reminder.md")


def return_chatgpt_depose_reminder():
    return load_markdown_asset("chatgpt_depose_reminder.md")
