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
import bcrypt
from zoneinfo import ZoneInfo
from sqlalchemy import text

CATEGORIES = [
    "國際與時事", "科技與未來", "文化與生活",
    "香港社會政策", "青少年與教育", "哲理／價值觀"
]
DIFFICULTY_OPTIONS = {1: "Lv1 — 概念日常", 2: "Lv2 — 一般議題", 3: "Lv3 — 進階專業"}


def normalize_judge_name(name: str) -> str:
    raw = str(name or "").replace("\u3000", " ").strip()
    raw = " ".join(raw.split())
    return "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in raw)


def _serialize_score_data(score_data):
    data_to_save = score_data.copy()
    if "raw_df_a" in data_to_save and isinstance(data_to_save["raw_df_a"], pd.DataFrame):
        data_to_save["raw_df_a"] = data_to_save["raw_df_a"].to_json()
    if "raw_df_b" in data_to_save and isinstance(data_to_save["raw_df_b"], pd.DataFrame):
        data_to_save["raw_df_b"] = data_to_save["raw_df_b"].to_json()
    return json.dumps(data_to_save, ensure_ascii=False)


def _deserialize_score_data(raw_data):
    data = raw_data if isinstance(raw_data, dict) else json.loads(raw_data)
    if "raw_df_a" in data and data["raw_df_a"]:
        data["raw_df_a"] = pd.read_json(io.StringIO(data["raw_df_a"]))
    if "raw_df_b" in data and data["raw_df_b"]:
        data["raw_df_b"] = pd.read_json(io.StringIO(data["raw_df_b"]))
    return data

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


def _log_login(user_id: str, login_type: str):
    login_time = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
    execute_query(
        "INSERT INTO login_record (user_id, login_type, login_time) VALUES (:user_id, :login_type, :login_time)",
        {"user_id": user_id, "login_type": login_type, "login_time": login_time}
    )


def check_admin():
    if "admin_logged_in" not in st.session_state:
        st.session_state["admin_logged_in"] = False

    if not st.session_state["admin_logged_in"]:
        st.subheader("賽會人員登入")
        pwd = st.text_input("請輸入賽會人員密碼", type="password")
        if st.button("登入"):
            if pwd == st.secrets["admin_password"]:
                st.session_state["admin_logged_in"] = True
                _log_login("admin", "admin")
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
    df = conn.query("SELECT * FROM matches", ttl=0)
    debaters_df = conn.query("SELECT * FROM debaters", ttl=0)

    data_dict = {}
    for i, row in df.iterrows():
        match_id = str(row["match_id"])
        raw = row.to_dict()
        # Normalise DB column names to the app-internal keys used by judging.py / match_info.py
        raw["que"] = raw.pop("topic", raw.get("que", ""))
        raw["pro"] = raw.pop("pro_team", raw.get("pro", ""))
        raw["con"] = raw.pop("con_team", raw.get("con", ""))
        # Reconstruct pro_1~con_4 keys from the normalised debaters table
        match_debaters = debaters_df[debaters_df["match_id"] == match_id]
        for _, d in match_debaters.iterrows():
            key = f"{str(d['side']).strip()}_{int(d['position'])}"
            raw[key] = str(d["name"]).strip() if d["name"] else ""
        data_dict[match_id] = raw

    return data_dict


def save_match_to_db(match_data):
    match_id = match_data['match_id'].strip()

    exist_match = query_params(
        "SELECT 1 FROM matches WHERE match_id = :match_id",
        {"match_id": match_id}
    )

    match_params = {
        "match_id": match_id,
        "date": match_data['date'] if match_data['date'] else None,
        "time": match_data['time'] if match_data['time'] else None,
        "topic": match_data['que'],
        "pro_team": match_data['pro'],
        "con_team": match_data['con'],
        "access_code": match_data['access_code'],
        "review_password": match_data.get('review_password', '') or None
    }

    if not exist_match.empty:
        execute_query("""
            UPDATE matches SET
                date = :date, time = :time, topic = :topic,
                pro_team = :pro_team, con_team = :con_team,
                access_code = :access_code,
                review_password = :review_password
            WHERE match_id = :match_id
        """, match_params)
    else:
        execute_query("""
            INSERT INTO matches (match_id, date, time, topic, pro_team, con_team, access_code, review_password)
            VALUES (:match_id, :date, :time, :topic, :pro_team, :con_team, :access_code, :review_password)
        """, match_params)

    # Upsert debater names into the normalised debaters table
    for side, positions in (("pro", [1, 2, 3, 4]), ("con", [1, 2, 3, 4])):
        for pos in positions:
            execute_query("""
                INSERT INTO debaters (match_id, side, position, name)
                VALUES (:match_id, :side, :position, :name)
                ON CONFLICT (match_id, side, position) DO UPDATE SET name = EXCLUDED.name
            """, {
                "match_id": match_id,
                "side": side,
                "position": pos,
                "name": match_data.get(f"{side}_{pos}", "") or ""
            })


def save_draft_to_db(match_id, judge_name, team_side, score_data):
    conn = get_connection()
    normalized_judge_name = normalize_judge_name(judge_name)
    json_str = _serialize_score_data(score_data)
    updated_at = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)

    with conn.session as s:
        already_submitted = s.execute(text("""
            SELECT 1
            FROM scores
            WHERE match_id = :match_id AND judge_name = :judge_name
        """), {
            "match_id": match_id,
            "judge_name": normalized_judge_name
        }).fetchone()

        if already_submitted:
            raise ValueError("你已提交過評分！無法修改評分！")

        s.execute(text("""
            INSERT INTO temp_scores (match_id, judge_name, team_side, data, is_final, updated_at)
            VALUES (:match_id, :judge_name, :team_side, :data, FALSE, :updated_at)
            ON CONFLICT (match_id, judge_name, team_side)
            DO UPDATE SET
                data = EXCLUDED.data,
                is_final = FALSE,
                updated_at = EXCLUDED.updated_at
        """), {
            "match_id": match_id,
            "judge_name": normalized_judge_name,
            "team_side": team_side,
            "data": json_str,
            "updated_at": updated_at
        })
        s.commit()
    return True


def load_draft_from_db(match_id, judge_name):
    conn = get_connection()
    normalized_judge_name = normalize_judge_name(judge_name)

    exist_temp_data = query_params(
        """
        SELECT *
        FROM temp_scores
        WHERE match_id = :match_id
          AND judge_name = :judge_name
          AND COALESCE(is_final, FALSE) = FALSE
        ORDER BY updated_at DESC
        """,
        {"match_id": match_id, "judge_name": normalized_judge_name}
    )
    
    drafts = {"正方": None, "反方": None}
    for i, row in exist_temp_data.iterrows():
        team_side = str(row["team_side"]).strip()
        if team_side in drafts:
            try:
                if drafts[team_side] is None:
                    drafts[team_side] = _deserialize_score_data(row["data"])
            except Exception:
                pass  # Corrupt draft data — silently skip, judge will re-enter scores
                
    return drafts


def has_final_submission(match_id, judge_name):
    normalized_judge_name = normalize_judge_name(judge_name)
    existing_submit = query_params(
        "SELECT 1 FROM scores WHERE match_id = :match_id AND judge_name = :judge_name",
        {"match_id": match_id, "judge_name": normalized_judge_name}
    )
    return not existing_submit.empty


def submit_final_scores(match_id, judge_name, pro_data, con_data):
    conn = get_connection()
    normalized_judge_name = normalize_judge_name(judge_name)
    submitted_at = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
    submitted_at_db = submitted_at.replace(tzinfo=None)
    mark_time = submitted_at.strftime("%H:%M:%S")

    side_payloads = {
        "正方": _serialize_score_data(pro_data),
        "反方": _serialize_score_data(con_data),
    }

    with conn.session as s:
        existing_submit = s.execute(text("""
            SELECT 1
            FROM scores
            WHERE match_id = :match_id AND judge_name = :judge_name
        """), {
            "match_id": match_id,
            "judge_name": normalized_judge_name
        }).fetchone()

        if existing_submit:
            s.rollback()
            return False

        for team_side, payload in side_payloads.items():
            s.execute(text("""
                INSERT INTO temp_scores (match_id, judge_name, team_side, data, is_final, updated_at)
                VALUES (:match_id, :judge_name, :team_side, :data, TRUE, :updated_at)
                ON CONFLICT (match_id, judge_name, team_side)
                DO UPDATE SET
                    data = EXCLUDED.data,
                    is_final = TRUE,
                    updated_at = EXCLUDED.updated_at
            """), {
                "match_id": match_id,
                "judge_name": normalized_judge_name,
                "team_side": team_side,
                "data": payload,
                "updated_at": submitted_at_db
            })

        s.execute(text("""
            INSERT INTO scores (
                match_id, judge_name, pro_total, con_total, mark_time,
                pro_free, con_free, pro_deduction, con_deduction, pro_coherence, con_coherence
            ) VALUES (
                :match_id, :judge_name, :pro_total, :con_total, :mark_time,
                :pro_free, :con_free, :pro_deduction, :con_deduction, :pro_coherence, :con_coherence
            )
        """), {
            "match_id": match_id,
            "judge_name": normalized_judge_name,
            "pro_total": pro_data["final_total"],
            "con_total": con_data["final_total"],
            "mark_time": mark_time,
            "pro_free": pro_data["total_b"],
            "con_free": con_data["total_b"],
            "pro_deduction": pro_data["deduction"],
            "con_deduction": con_data["deduction"],
            "pro_coherence": pro_data["coherence"],
            "con_coherence": con_data["coherence"]
        })

        for i, score in enumerate(pro_data["ind_scores"]):
            s.execute(text("""
                INSERT INTO debater_scores (match_id, judge_name, side, position, score)
                VALUES (:match_id, :judge_name, :side, :position, :score)
                ON CONFLICT (match_id, judge_name, side, position)
                DO UPDATE SET score = EXCLUDED.score
            """), {
                "match_id": match_id,
                "judge_name": normalized_judge_name,
                "side": "pro",
                "position": i + 1,
                "score": int(score)
            })

        for i, score in enumerate(con_data["ind_scores"]):
            s.execute(text("""
                INSERT INTO debater_scores (match_id, judge_name, side, position, score)
                VALUES (:match_id, :judge_name, :side, :position, :score)
                ON CONFLICT (match_id, judge_name, side, position)
                DO UPDATE SET score = EXCLUDED.score
            """), {
                "match_id": match_id,
                "judge_name": normalized_judge_name,
                "side": "con",
                "position": i + 1,
                "score": int(score)
            })

        s.commit()

    return True


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
        # Return a wide-format DataFrame identical to the old scores table schema
        # so that management.py and review.py need no changes.
        # pro_name / con_name are derived from matches; individual scores from debater_scores.
        query = """
            SELECT
                s.match_id, s.judge_name, s.pro_total, s.con_total, s.mark_time,
                s.pro_free, s.con_free, s.pro_deduction, s.con_deduction,
                s.pro_coherence, s.con_coherence,
                m.pro_team AS pro_name,
                m.con_team AS con_name,
                MAX(CASE WHEN ds.side = 'pro' AND ds.position = 1 THEN ds.score END) AS pro1_m,
                MAX(CASE WHEN ds.side = 'pro' AND ds.position = 2 THEN ds.score END) AS pro2_m,
                MAX(CASE WHEN ds.side = 'pro' AND ds.position = 3 THEN ds.score END) AS pro3_m,
                MAX(CASE WHEN ds.side = 'pro' AND ds.position = 4 THEN ds.score END) AS pro4_m,
                MAX(CASE WHEN ds.side = 'con' AND ds.position = 1 THEN ds.score END) AS con1_m,
                MAX(CASE WHEN ds.side = 'con' AND ds.position = 2 THEN ds.score END) AS con2_m,
                MAX(CASE WHEN ds.side = 'con' AND ds.position = 3 THEN ds.score END) AS con3_m,
                MAX(CASE WHEN ds.side = 'con' AND ds.position = 4 THEN ds.score END) AS con4_m
            FROM scores s
            LEFT JOIN matches m ON s.match_id = m.match_id
            LEFT JOIN debater_scores ds
                ON s.match_id = ds.match_id AND s.judge_name = ds.judge_name
            GROUP BY
                s.match_id, s.judge_name, s.pro_total, s.con_total, s.mark_time,
                s.pro_free, s.con_free, s.pro_deduction, s.con_deduction,
                s.pro_coherence, s.con_coherence,
                m.pro_team, m.con_team
        """
        return query_params(query)
    except Exception as e:
        st.error(f"讀取評分失敗: {e}")
        return None


def get_best_debater_results(match_id, match_results):
    debaters_row = query_params(
        "SELECT side, position, name FROM debaters WHERE match_id = :match_id",
        {"match_id": match_id}
    )
    if not debaters_row.empty:
        debater_names = {
            (str(r["side"]).strip(), int(r["position"])): str(r["name"]).strip()
            for _, r in debaters_row.iterrows()
        }

        def _label(pos, side, position):
            name = debater_names.get((side, position), "")
            return f"{pos}（{name}）" if name else pos

        role_map = {
            "pro1_m": _label("正方主辯", "pro", 1),
            "pro2_m": _label("正方一副", "pro", 2),
            "pro3_m": _label("正方二副", "pro", 3),
            "pro4_m": _label("正方結辯", "pro", 4),
            "con1_m": _label("反方主辯", "con", 1),
            "con2_m": _label("反方一副", "con", 2),
            "con3_m": _label("反方二副", "con", 3),
            "con4_m": _label("反方結辯", "con", 4),
        }
    else:
        role_map = {
            "pro1_m": "正方主辯",
            "pro2_m": "正方一副",
            "pro3_m": "正方二副",
            "pro4_m": "正方結辯",
            "con1_m": "反方主辯",
            "con2_m": "反方一副",
            "con3_m": "反方二副",
            "con4_m": "反方結辯",
        }

    rank_cols = ["pro1_m", "pro2_m", "pro3_m", "pro4_m", "con1_m", "con2_m", "con3_m", "con4_m"]
    scores_only = match_results[rank_cols].apply(pd.to_numeric, errors="coerce")
    if scores_only.isna().any().any():
        return None, None

    all_ranks = []
    for _, row in scores_only.iterrows():
        all_ranks.append(row.rank(ascending=False, method='min'))

    df_ranks = pd.DataFrame(all_ranks)
    total_rank_sum = df_ranks.sum()

    best_debater_results = []
    for col_id in rank_cols:
        best_debater_results.append({
            "辯位": role_map.get(col_id, col_id),
            "名次總和": int(total_rank_sum[col_id]),
            "平均得分": round(scores_only[col_id].mean(), 2)
        })

    df_final_best = pd.DataFrame(best_debater_results).sort_values(
        by=["名次總和", "平均得分"],
        ascending=[True, False]
    )
    return df_final_best, df_final_best.iloc[0]


def return_user_manual():
    return load_markdown_asset("user_manual.md")


def return_rules():
    return load_markdown_asset("rules.md")


def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, stored: str) -> bool:
    """Accept bcrypt hashes and legacy plaintext passwords during migration."""
    try:
        return bcrypt.checkpw(plain.encode(), stored.encode())
    except Exception:
        return plain == stored


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
            acc_row = conn.query(
                "SELECT userpw FROM accounts WHERE userid = :uid",
                params={"uid": uid.strip()},
                ttl=0,
            )
            login_success = (
                not acc_row.empty
                and _verify_password(upw.strip(), str(acc_row.iloc[0]["userpw"]))
            )

            if login_success:
                refresh_acc_type(uid.strip())
                _log_login(uid.strip(), "committee")
                st.session_state["committee_user"] = uid.strip()
                set_cookie(cookie_manager, "committee_user", uid.strip(), expires_at=return_expire_day())
                st.success(f"你好，{uid.strip()}！")
                time.sleep(1)
                st.rerun()
            else:
                st.error("User ID或Password錯誤！")


def return_expire_day():
    return datetime.datetime.now() + datetime.timedelta(days=1)


_ACTIVITY_SQL = """
WITH tv_events AS (
    SELECT DISTINCT tv.topic, tv.created_at
    FROM topic_votes tv
    WHERE EXISTS (SELECT 1 FROM topic_vote_ballots b WHERE b.topic = tv.topic)
),
tdv_events AS (
    SELECT DISTINCT tdv.topic, tdv.created_at
    FROM topic_depose_votes tdv
    WHERE EXISTS (SELECT 1 FROM depose_vote_ballots b WHERE b.topic = tdv.topic)
),
all_events AS (
    SELECT topic, created_at, 'tv'  AS src FROM tv_events
    UNION ALL
    SELECT topic, created_at, 'tdv' AS src FROM tdv_events
),
past_10 AS (
    SELECT topic, src FROM all_events ORDER BY created_at DESC LIMIT 10
)
SELECT
    (SELECT COUNT(*) FROM all_events) AS total_votes,
    (SELECT COUNT(*) FROM all_events ae
     WHERE (ae.src = 'tv'  AND EXISTS (SELECT 1 FROM topic_vote_ballots  b WHERE b.topic = ae.topic AND b.user_id = :user_id))
        OR (ae.src = 'tdv' AND EXISTS (SELECT 1 FROM depose_vote_ballots b WHERE b.topic = ae.topic AND b.user_id = :user_id))
    ) AS total_participated,
    (SELECT COUNT(*) FROM past_10 p
     WHERE (p.src = 'tv'  AND EXISTS (SELECT 1 FROM topic_vote_ballots  b WHERE b.topic = p.topic AND b.user_id = :user_id))
        OR (p.src = 'tdv' AND EXISTS (SELECT 1 FROM depose_vote_ballots b WHERE b.topic = p.topic AND b.user_id = :user_id))
    ) AS votes_in_last_10
"""


def refresh_acc_type(user_id: str) -> str | None:
    acc = query_params(
        "SELECT acc_type FROM accounts WHERE userid = :user_id",
        {"user_id": user_id}
    )
    if acc.empty or str(acc.iloc[0]["acc_type"]).strip() == "admin" or str(acc.iloc[0]["acc_type"]).strip() == "developer":
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


@st.cache_data(ttl=60)
def _get_combined_vote_records():
    """
    Fetches all vote events from topic_votes and topic_depose_votes that have
    at least one ballot, reconstructs (agree_list, against_list) per event
    from the ballot tables, sorted chronologically.
    Returns (vote_records, all_users).
    """
    from collections import OrderedDict
    conn = get_connection()
    accounts_df = conn.query("SELECT userid FROM accounts", ttl=0)
    all_users = accounts_df["userid"].tolist() if not accounts_df.empty else []

    tv_ballots = query_params("""
        SELECT tv.topic, tv.created_at, b.user_id, b.vote
        FROM topic_votes tv
        JOIN topic_vote_ballots b ON tv.topic = b.topic
        ORDER BY tv.created_at ASC
    """)
    tdv_ballots = query_params("""
        SELECT tdv.topic, tdv.created_at, b.user_id, b.vote
        FROM topic_depose_votes tdv
        JOIN depose_vote_ballots b ON tdv.topic = b.topic
        ORDER BY tdv.created_at ASC
    """)

    event_map = OrderedDict()
    for df, prefix in ((tv_ballots, "tv_"), (tdv_ballots, "tdv_")):
        if df.empty:
            continue
        for topic, group in df.groupby("topic", sort=False):
            key = prefix + str(topic)
            ts = group["created_at"].iloc[0]
            is_agree = group["vote"].str.strip() == "agree"
            agree = group.loc[is_agree, "user_id"].astype(str).tolist()
            against = group.loc[~is_agree, "user_id"].astype(str).tolist()
            event_map[key] = {"ts": ts, "agree": agree, "against": against}

    sorted_events = sorted(event_map.values(), key=lambda e: e["ts"])
    vote_records = [(e["agree"], e["against"]) for e in sorted_events]
    return vote_records, all_users


@st.cache_data(ttl=60)
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


@st.cache_data(ttl=60)
def get_member_participation_stats():
    """
    Returns (stats_list, total_votes) with per-member participation details
    across both topic_votes and topic_depose_votes.
    """
    vote_records, all_users = _get_combined_vote_records()
    total_votes = len(vote_records)
    last_10 = vote_records[-10:]
    vote_summary_df = query_params("""
        SELECT user_id, COUNT(*) AS total_ballots,
               SUM(CASE WHEN vote = 'agree' THEN 1 ELSE 0 END) AS agree_ballots
        FROM (
            SELECT user_id, vote FROM topic_vote_ballots
            UNION ALL
            SELECT user_id, vote FROM depose_vote_ballots
        ) combined_ballots
        GROUP BY user_id
    """)
    vote_summary_map = {}
    if not vote_summary_df.empty:
        for _, row in vote_summary_df.iterrows():
            vote_summary_map[str(row["user_id"])] = {
                "total_ballots": int(row["total_ballots"]),
                "agree_ballots": int(row["agree_ballots"]),
            }

    stats = []
    for user in all_users:
        if user == "admin" or user == "developer" or user == "": continue  # Skip admin and developer accounts from stats
        total_participated = sum(
            1 for agree, against in vote_records if user in agree or user in against
        ) if total_votes > 0 else 0
        overall_rate = total_participated / total_votes if total_votes > 0 else 0

        last10_participated = sum(
            1 for agree, against in last_10 if user in agree or user in against
        )

        vote_summary = vote_summary_map.get(user, {"total_ballots": 0, "agree_ballots": 0})
        total_ballots = vote_summary["total_ballots"]
        agree_ballots = vote_summary["agree_ballots"]
        agree_rate = agree_ballots / total_ballots if total_ballots > 0 else None

        is_active = overall_rate >= 0.4 and last10_participated >= 3

        stats.append({
            "用戶": user,
            "整體投票次數": f"{total_participated} / {total_votes}",
            "整體投票率": f"{overall_rate:.1%}",
            "最近10次參與": last10_participated,
            "同意票數": f"{agree_ballots} / {total_ballots}",
            "投票同意率": f"{agree_rate:.1%}" if agree_rate is not None else "—",
            "活躍狀態": "✅ 活躍" if is_active else "❌ 非活躍",
        })

    return stats, total_votes


def show_noti_popup(user_id: str) -> None:
    """
    Show a one-time notification dialog backed by the DB `noti` table.

    assets/noti.md format:
        NOTI_ID: 1
        NOTI_TITLE: Title
        ---
        Markdown body
    """
    raw = load_markdown_asset("noti.md")
    lines = raw.splitlines()

    noti_id = None
    noti_title = None
    content_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("NOTI_ID:"):
            try:
                noti_id = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif stripped.startswith("NOTI_TITLE:"):
            noti_title = stripped.split(":", 1)[1].strip()
        elif stripped == "---":
            content_start = i + 1
            break

    if noti_id is None or noti_title is None:
        return

    from schema import CREATE_NOTI

    execute_query(CREATE_NOTI)
    content = "\n".join(lines[content_start:]).strip()
    seen = query_params(
        "SELECT 1 FROM noti WHERE notiid = :nid AND userid = :uid",
        {"nid": noti_id, "uid": user_id},
    )
    if not seen.empty:
        return

    @st.dialog(noti_title)
    def _render():
        st.markdown(content)
        if st.button("我已閱讀 ✓", type="primary", use_container_width=True):
            seen_at = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
            execute_query(
                "INSERT INTO noti (notiid, notititle, userid, seen_at) "
                "VALUES (:nid, :title, :uid, :seen_at) "
                "ON CONFLICT (notiid, userid) DO NOTHING",
                {"nid": noti_id, "title": noti_title, "uid": user_id, "seen_at": seen_at},
            )
            st.rerun()

    _render()


def return_gemini_depose_reminder():
    return load_markdown_asset("gemini_depose_reminder.md")


def return_chatgpt_depose_reminder():
    return load_markdown_asset("chatgpt_depose_reminder.md")
