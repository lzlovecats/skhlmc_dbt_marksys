import streamlit as st
import json
import pandas as pd
import random
import extra_streamlit_components as stx
import datetime
import time
import os
import io
from sqlalchemy import text

def get_cookie(cookie_manager, key, default=None):
    try:
        value = cookie_manager.get(key)
        return default if value is None else value
    except Exception as e:
        st.write(e)
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


def load_matches_from_db():
    conn = get_connection()
    df = conn.query("SELECT * FROM MATCHES", ttl=0)

    data_dict = {}
    for i, row in df.iterrows():
        match_id = str(row["match_id"])
        data_dict[match_id] = row.to_dict()

    return data_dict


def save_match_to_db(match_data):
    conn = get_connection()

    exist_match = conn.query(f"SELECT * FROM MATCHES WHERE match_id = '{match_data['match_id']}'")

    params = {
        "match_id": match_data['match_id'],
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

    exist_temp_data = conn.query(f"SELECT * FROM temp_scores WHERE match_id = '{match_id}' AND judge_name = '{judge_name}'", ttl=0)

    for i, row in exist_temp_data.iterrows():
        if str(row["team_side"]).strip() == team_side:
            execute_query(
                "UPDATE temp_scores SET data = :data WHERE match_id = :match_id AND judge_name = :judge_name AND team_side = :team_side",
                {"data": json_str, "match_id": match_id, "judge_name": judge_name, "team_side": team_side}
            )
            return True
        
    execute_query(
        "INSERT INTO temp_scores (match_id, judge_name, team_side, data) VALUES (:match_id, :judge_name, :team_side, :data)",
        {"match_id": match_id, "judge_name": judge_name, "team_side": team_side, "data": json_str}
    )
    return True


def load_draft_from_db(match_id, judge_name):
    conn = get_connection()

    exist_temp_data = conn.query(f"SELECT * FROM temp_scores WHERE match_id = '{match_id}' AND judge_name = '{judge_name}'", ttl=0)
    
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
            except Exception as e:
                st.write(f"Error loading draft data: {e}")
                
    return drafts


def load_topic_from_db():
    conn = get_connection()
    all_records = conn.query("SELECT * FROM topics", ttl=0)
    topics = []
    for i, row in all_records.iterrows():
        topics.append(row["topic"])
    return topics


def draw_a_topic():
    all_topic = load_topic_from_db()
    if all_topic:
        return random.choice(all_topic)
    else:
        st.error("抽取辯題失敗：辯題庫為空或出現錯誤。")
        return ""


def draw_pro_con(team1, team2):
    t_list = []
    draw_num = random.randint(0, 1)
    if draw_num == 0:
        t_list.append(team1)
        t_list.append(team2)
    elif draw_num == 1:
        t_list.append(team2)
        t_list.append(team1)
    return t_list


def load_markdown_asset(filename):
    file_path = os.path.join("assets", filename)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"出現錯誤：{filename}無法存取。"


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
                st.session_state["committee_user"] = uid
                set_cookie(cookie_manager, "committee_user", uid, expires_at=return_expire_day())
                st.success(f"你好，{uid}！")
                time.sleep(1)
                st.rerun()
            else:
                st.error("User ID或Password錯誤！")


def return_expire_day():
    return datetime.datetime.now() + datetime.timedelta(days=1)


def return_gemini_reminder():
    return load_markdown_asset("gemini_reminder.md")


def return_chatgpt_reminder():
    return load_markdown_asset("chatgpt_reminder.md")


def return_gemini_depose_reminder():
    return load_markdown_asset("gemini_depose_reminder.md")


def return_chatgpt_depose_reminder():
    return load_markdown_asset("chatgpt_depose_reminder.md")
