import streamlit as st
import json
import logging
import pandas as pd
import random
import math
import re
import secrets
import hashlib
import extra_streamlit_components as stx
import datetime
import os
import io
import bcrypt
from zoneinfo import ZoneInfo
from sqlalchemy import text
from schema import (
    TABLE_ACCOUNTS,
    TABLE_DEBATERS,
    TABLE_DEBATER_SCORES,
    TABLE_LOGIN_RECORDS,
    TABLE_MATCHES,
    TABLE_NOTIFICATION_READS,
    TABLE_TELEGRAM_LINK_TOKENS,
    TABLE_SCORE_DRAFTS,
    TABLE_SCORES,
    TABLE_TOPIC_REMOVAL_VOTE_BALLOTS,
    TABLE_TOPIC_REMOVAL_VOTES,
    TABLE_TOPIC_VOTE_BALLOTS,
    TABLE_TOPIC_VOTES,
    TABLE_TOPICS,
    VIEW_COMMITTEE_VOTE_ACTIVITY,
)

logger = logging.getLogger(__name__)

MAINTENANCE_MODE = False
MAINTENANCE_DEADLINE_TEXT = "2026年4月3日 23:59（香港時間）"

CATEGORIES = [
    "國際與時事", "科技與未來", "文化與生活",
    "香港社會政策", "青少年與教育", "哲理／價值觀"
]
DIFFICULTY_OPTIONS = {1: "Lv1 — 概念日常", 2: "Lv2 — 一般議題", 3: "Lv3 — 進階專業"}
TELEGRAM_LINK_CODE_TTL_MINUTES = 10
_TELEGRAM_LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalize_judge_name(name: str) -> str:
    raw = str(name or "").replace("\u3000", " ").strip()
    raw = " ".join(raw.split())
    return "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in raw)


def normalize_telegram_link_code(code: str) -> str:
    return re.sub(r"[^A-Z2-9]", "", str(code or "").upper())


def hash_telegram_link_code(code: str) -> str:
    normalized = normalize_telegram_link_code(code)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def generate_telegram_link_code() -> str:
    raw = "".join(secrets.choice(_TELEGRAM_LINK_CODE_ALPHABET) for _ in range(12))
    return "-".join([raw[0:4], raw[4:8], raw[8:12]])


def is_committee_member_active(total_votes: int, participated_votes: int, last10_participated: int) -> bool:
    if total_votes <= 0:
        return False
    return (participated_votes / total_votes) >= 0.4 and last10_participated >= 3


def _serialize_score_data(score_data):
    data_to_save = score_data.copy()
    if isinstance(data_to_save.get("raw_df_a"), pd.DataFrame):
        data_to_save["raw_df_a"] = data_to_save["raw_df_a"].to_dict(orient="records")
    if isinstance(data_to_save.get("raw_df_b"), pd.DataFrame):
        data_to_save["raw_df_b"] = data_to_save["raw_df_b"].to_dict(orient="records")
    return json.dumps(data_to_save, ensure_ascii=False)


def _deserialize_score_data(raw_data):
    data = raw_data if isinstance(raw_data, dict) else json.loads(raw_data)
    for key in ("raw_df_a", "raw_df_b"):
        if key in data and data[key]:
            if isinstance(data[key], str):
                # Backward compat: old format used df.to_json() producing a JSON string
                data[key] = pd.read_json(io.StringIO(data[key]))
            elif isinstance(data[key], list):
                data[key] = pd.DataFrame(data[key])
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
        f"INSERT INTO {TABLE_LOGIN_RECORDS} (user_id, login_type, logged_in_at) "
        "VALUES (:user_id, :login_type, :login_time)",
        {"user_id": user_id, "login_type": login_type, "login_time": login_time}
    )


def get_system_config(key: str):
    """Read a value from the system_config table. Returns None if not found."""
    try:
        conn = get_connection()
        result = conn.query(
            "SELECT value FROM system_config WHERE key = :key",
            params={"key": key},
            ttl=0,
        )
        if result.empty:
            return None
        return result.iloc[0]["value"]
    except Exception as e:
        logger.warning("get_system_config(%s) failed: %s", key, e)
        return None


def _verify_config_password(plain: str, stored: str) -> bool:
    """Verify a password against a stored value (bcrypt hash or plaintext)."""
    if stored.startswith("$2b$") or stored.startswith("$2a$"):
        try:
            return bcrypt.checkpw(plain.encode(), stored.encode())
        except Exception as e:
            logger.warning("bcrypt verification failed: %s", e)
            return False
    return plain == stored


def check_admin():
    if "admin_logged_in" not in st.session_state:
        st.session_state["admin_logged_in"] = False

    if not st.session_state["admin_logged_in"]:
        st.subheader("賽會人員登入")
        pwd = st.text_input("請輸入賽會人員密碼", type="password")
        if st.button("登入"):
            stored = get_system_config("admin_password")
            if stored is None:
                st.error("系統錯誤：未能讀取密碼，請聯絡開發人員")
            elif _verify_config_password(pwd, stored):
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
    df = conn.query(
        f"""
        SELECT
            match_id,
            match_date,
            match_time,
            topic_text,
            pro_team,
            con_team,
            access_code_hash,
            review_password_hash
        FROM {TABLE_MATCHES}
        """,
        ttl=0,
    )
    debaters_df = conn.query(
        f"SELECT match_id, side, position, debater_name FROM {TABLE_DEBATERS}",
        ttl=0,
    )

    data_dict = {}
    for i, row in df.iterrows():
        match_id = str(row["match_id"])
        raw = row.to_dict()
        if raw.get("match_date") is not None and hasattr(raw["match_date"], "strftime"):
            raw["match_date"] = raw["match_date"].strftime("%Y-%m-%d")
        if raw.get("match_time") is not None and hasattr(raw["match_time"], "strftime"):
            raw["match_time"] = raw["match_time"].strftime("%H:%M")

        match_debaters = debaters_df[debaters_df["match_id"] == match_id]
        for _, d in match_debaters.iterrows():
            key = f"{str(d['side']).strip()}_{int(d['position'])}"
            raw[key] = str(d["debater_name"]).strip() if d["debater_name"] else ""
        data_dict[match_id] = raw

    return data_dict


def save_match_to_db(match_data):
    match_id = match_data['match_id'].strip()
    conn = get_connection()

    with conn.session as s:
        exist_match = s.execute(text(f"""
            SELECT
                access_code_hash,
                review_password_hash
            FROM {TABLE_MATCHES}
            WHERE match_id = :match_id
        """), {"match_id": match_id}).fetchone()

        raw_access_code = match_data.get("access_code_hash", "") or ""
        raw_review_password = match_data.get("review_password_hash", "") or ""
        clear_access_code = bool(match_data.get("clear_access_code"))
        clear_review_password = bool(match_data.get("clear_review_password"))
        existing_access_code = None
        existing_review_password = None
        if exist_match is not None:
            existing_access_code = exist_match._mapping["access_code_hash"]
            existing_review_password = exist_match._mapping["review_password_hash"]
            if pd.isna(existing_access_code):
                existing_access_code = None
            if pd.isna(existing_review_password):
                existing_review_password = None

        if clear_access_code:
            resolved_access_code = None
        elif raw_access_code:
            resolved_access_code = hash_password(raw_access_code)
        elif exist_match is not None:
            resolved_access_code = existing_access_code
        else:
            resolved_access_code = None

        if clear_review_password:
            resolved_review_password = None
        elif raw_review_password:
            resolved_review_password = hash_password(raw_review_password)
        elif exist_match is not None:
            resolved_review_password = existing_review_password
        else:
            resolved_review_password = None

        match_params = {
            "match_id": match_id,
            "match_date": match_data["match_date"] if match_data["match_date"] else None,
            "match_time": match_data["match_time"] if match_data["match_time"] else None,
            "topic_text": match_data["topic_text"],
            "pro_team": match_data["pro_team"],
            "con_team": match_data["con_team"],
            "access_code_hash": resolved_access_code,
            "review_password_hash": resolved_review_password
        }

        if exist_match is not None:
            s.execute(text(f"""
                UPDATE {TABLE_MATCHES} SET
                    match_date = :match_date, match_time = :match_time, topic_text = :topic_text,
                    pro_team = :pro_team, con_team = :con_team,
                    access_code_hash = :access_code_hash,
                    review_password_hash = :review_password_hash
                WHERE match_id = :match_id
            """), match_params)
        else:
            s.execute(text(f"""
                INSERT INTO {TABLE_MATCHES} (
                    match_id, match_date, match_time, topic_text, pro_team, con_team,
                    access_code_hash, review_password_hash
                )
                VALUES (
                    :match_id, :match_date, :match_time, :topic_text, :pro_team, :con_team,
                    :access_code_hash, :review_password_hash
                )
            """), match_params)

        # Upsert debater names into the normalised debaters table
        debater_params = [
            {"match_id": match_id, "side": side, "position": pos,
             "name": match_data.get(f"{side}_{pos}", "") or ""}
            for side in ("pro", "con") for pos in (1, 2, 3, 4)
        ]
        s.execute(text(f"""
            INSERT INTO {TABLE_DEBATERS} (match_id, side, position, debater_name)
            VALUES (:match_id, :side, :position, :name)
            ON CONFLICT (match_id, side, position) DO UPDATE SET debater_name = EXCLUDED.debater_name
        """), debater_params)
        s.commit()


def save_draft_to_db(match_id, judge_name, team_side, score_data):
    conn = get_connection()
    normalized_judge_name = normalize_judge_name(judge_name)
    json_str = _serialize_score_data(score_data)
    updated_at = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)

    with conn.session as s:
        already_submitted = s.execute(text("""
            SELECT 1
            FROM {table_scores}
            WHERE match_id = :match_id AND judge_name = :judge_name
        """.format(table_scores=TABLE_SCORES)), {
            "match_id": match_id,
            "judge_name": normalized_judge_name
        }).fetchone()

        if already_submitted:
            raise ValueError("你已提交過評分！無法修改評分！")

        s.execute(text(f"""
            INSERT INTO {TABLE_SCORE_DRAFTS} (match_id, judge_name, side, score_payload, is_final, updated_at)
            VALUES (:match_id, :judge_name, :side, :score_payload, FALSE, :updated_at)
            ON CONFLICT (match_id, judge_name, side)
            DO UPDATE SET
                score_payload = EXCLUDED.score_payload,
                is_final = FALSE,
                updated_at = EXCLUDED.updated_at
        """), {
            "match_id": match_id,
            "judge_name": normalized_judge_name,
            "side": team_side,
            "score_payload": json_str,
            "updated_at": updated_at
        })
        s.commit()
    return True


def load_draft_from_db(match_id, judge_name):
    conn = get_connection()
    normalized_judge_name = normalize_judge_name(judge_name)

    exist_temp_data = query_params(
        f"""
        SELECT match_id, judge_name, side, score_payload, is_final, updated_at
        FROM {TABLE_SCORE_DRAFTS}
        WHERE match_id = :match_id
          AND judge_name = :judge_name
          AND COALESCE(is_final, FALSE) = FALSE
        ORDER BY updated_at DESC
        """,
        {"match_id": match_id, "judge_name": normalized_judge_name}
    )
    
    drafts = {"正方": None, "反方": None}
    for i, row in exist_temp_data.iterrows():
        team_side = str(row["side"]).strip()
        if team_side in drafts:
            try:
                if drafts[team_side] is None:
                    drafts[team_side] = _deserialize_score_data(row["score_payload"])
            except Exception:
                pass  # Corrupt draft data — silently skip, judge will re-enter scores
                
    return drafts


def has_final_submission(match_id, judge_name):
    normalized_judge_name = normalize_judge_name(judge_name)
    existing_submit = query_params(
        f"SELECT 1 FROM {TABLE_SCORES} WHERE match_id = :match_id AND judge_name = :judge_name",
        {"match_id": match_id, "judge_name": normalized_judge_name}
    )
    return not existing_submit.empty


def submit_final_scores(match_id, judge_name, pro_data, con_data):
    conn = get_connection()
    normalized_judge_name = normalize_judge_name(judge_name)
    submitted_at = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
    submitted_at_db = submitted_at.replace(tzinfo=None)
    submitted_time = submitted_at.strftime("%H:%M:%S")

    side_payloads = {
        "正方": _serialize_score_data(pro_data),
        "反方": _serialize_score_data(con_data),
    }

    with conn.session as s:
        existing_submit = s.execute(text(f"""
            SELECT 1
            FROM {TABLE_SCORES}
            WHERE match_id = :match_id AND judge_name = :judge_name
        """), {
            "match_id": match_id,
            "judge_name": normalized_judge_name
        }).fetchone()

        if existing_submit:
            s.rollback()
            return False

        for team_side, payload in side_payloads.items():
            s.execute(text(f"""
                INSERT INTO {TABLE_SCORE_DRAFTS} (match_id, judge_name, side, score_payload, is_final, updated_at)
                VALUES (:match_id, :judge_name, :side, :score_payload, TRUE, :updated_at)
                ON CONFLICT (match_id, judge_name, side)
                DO UPDATE SET
                    score_payload = EXCLUDED.score_payload,
                    is_final = TRUE,
                    updated_at = EXCLUDED.updated_at
            """), {
                "match_id": match_id,
                "judge_name": normalized_judge_name,
                "side": team_side,
                "score_payload": payload,
                "updated_at": submitted_at_db
            })

        s.execute(text(f"""
            INSERT INTO {TABLE_SCORES} (
                match_id, judge_name, pro_total_score, con_total_score, submitted_time,
                pro_free_debate_score, con_free_debate_score, pro_deduction_points, con_deduction_points,
                pro_coherence_score, con_coherence_score
            ) VALUES (
                :match_id, :judge_name, :pro_total, :con_total, :submitted_time,
                :pro_free, :con_free, :pro_deduction, :con_deduction, :pro_coherence, :con_coherence
            )
        """), {
            "match_id": match_id,
            "judge_name": normalized_judge_name,
            "pro_total": pro_data["final_total"],
            "con_total": con_data["final_total"],
            "submitted_time": submitted_time,
            "pro_free": pro_data["total_b"],
            "con_free": con_data["total_b"],
            "pro_deduction": pro_data["deduction"],
            "con_deduction": con_data["deduction"],
            "pro_coherence": pro_data["coherence"],
            "con_coherence": con_data["coherence"]
        })

        debater_params = []
        for side, data in [("pro", pro_data), ("con", con_data)]:
            for i, score in enumerate(data["ind_scores"]):
                debater_params.append({
                    "match_id": match_id,
                    "judge_name": normalized_judge_name,
                    "side": side,
                    "position": i + 1,
                    "score": int(score)
                })
        s.execute(text(f"""
            INSERT INTO {TABLE_DEBATER_SCORES} (match_id, judge_name, side, position, debater_score)
            VALUES (:match_id, :judge_name, :side, :position, :score)
            ON CONFLICT (match_id, judge_name, side, position)
            DO UPDATE SET debater_score = EXCLUDED.debater_score
        """), debater_params)

        s.commit()

    return True


def load_topic_from_db(difficulty=None):
    conn = get_connection()
    if difficulty:
        all_records = conn.query(
            f"SELECT topic_text FROM {TABLE_TOPICS} WHERE difficulty = :d",
            params={"d": difficulty},
            ttl=0,
        )
    else:
        all_records = conn.query(f"SELECT topic_text FROM {TABLE_TOPICS}", ttl=0)
    return all_records["topic_text"].tolist()


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
        scores_df = query_params("""
            SELECT s.match_id, s.judge_name,
                   s.pro_total_score,
                   s.con_total_score,
                   s.submitted_time,
                   s.pro_free_debate_score,
                   s.con_free_debate_score,
                   s.pro_deduction_points,
                   s.con_deduction_points,
                   s.pro_coherence_score,
                   s.con_coherence_score,
                   m.pro_team, m.con_team
            FROM {table_scores} s
            LEFT JOIN {table_matches} m ON s.match_id = m.match_id
        """.format(table_scores=TABLE_SCORES, table_matches=TABLE_MATCHES))
        if scores_df.empty:
            return scores_df

        ds_df = query_params(
            f"SELECT match_id, judge_name, side, position, debater_score FROM {TABLE_DEBATER_SCORES}"
        )
        if ds_df.empty:
            for col in ["pro1_m", "pro2_m", "pro3_m", "pro4_m", "con1_m", "con2_m", "con3_m", "con4_m"]:
                scores_df[col] = None
            return scores_df

        # Pivot debater_scores into wide format
        ds_df["col_name"] = ds_df["side"] + ds_df["position"].astype(str) + "_m"
        pivot = ds_df.pivot_table(index=["match_id", "judge_name"], columns="col_name", values="debater_score", aggfunc="first").reset_index()
        result = scores_df.merge(pivot, on=["match_id", "judge_name"], how="left")
        # Ensure all expected columns exist
        for col in ["pro1_m", "pro2_m", "pro3_m", "pro4_m", "con1_m", "con2_m", "con3_m", "con4_m"]:
            if col not in result.columns:
                result[col] = None
        return result
    except Exception as e:
        st.error(f"讀取評分失敗: {e}")
        return None


def get_best_debater_results(match_id, match_results):
    debaters_row = query_params(
        f"SELECT side, position, debater_name FROM {TABLE_DEBATERS} WHERE match_id = :match_id",
        {"match_id": match_id}
    )
    if not debaters_row.empty:
        debater_names = {
            (str(r["side"]).strip(), int(r["position"])): str(r["debater_name"]).strip()
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


def is_maintenance_mode() -> bool:
    return MAINTENANCE_MODE


def render_maintenance_notice():
    with st.container(border=True):
        st.warning("系統維護中")
        st.write("目前系統正在進行更新工程，需要時間。")
        st.write(f"預計會喺 **{MAINTENANCE_DEADLINE_TEXT}** 前完成。")
        st.caption("所有功能現已暫停使用，請稍後再試。")


def extract_markdown_section(content, heading_level, target_heading):
    prefix = "#" * heading_level
    match = re.search(
        rf"^{re.escape(prefix)}\s+{re.escape(target_heading)}\s*$.*?(?=^{re.escape(prefix)}\s|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(0).strip() if match else None


def render_user_manual_content(role_key: str):
    role = st.radio(
        "請先選擇你的身份：",
        ["評判", "賽會人員", "比賽隊伍", "一般人員", "內部委員會成員"],
        horizontal=True,
        key=role_key,
    )
    st.divider()

    role_section_map = {
        "評判": "一、評判",
        "賽會人員": "二、賽會人員",
        "比賽隊伍": "三、比賽隊伍",
        "一般人員": "四、一般人員",
        "內部委員會成員": "五、內部委員會成員",
    }

    manual_content = return_user_manual()
    section_text = extract_markdown_section(manual_content, 3, role_section_map[role])
    if section_text:
        st.markdown(section_text)
    else:
        st.markdown(manual_content)


def render_rules_content(role_key: str):
    role = st.radio(
        "請先選擇你的身份：",
        ["評判", "賽會人員", "參賽隊伍"],
        horizontal=True,
        key=role_key,
    )
    st.divider()

    role_section_map = {
        "評判": "一、評判",
        "賽會人員": "二、賽會人員",
        "參賽隊伍": "三、參賽隊伍",
    }

    rules_content = return_rules()
    disclaimer_end = rules_content.find("---")
    if disclaimer_end != -1:
        st.markdown(rules_content[: disclaimer_end + 3])
        rules_body = rules_content[disclaimer_end + 3 :]
    else:
        rules_body = rules_content

    section_text = extract_markdown_section(rules_body, 2, role_section_map[role])
    if section_text:
        st.markdown(section_text)
    else:
        st.markdown(rules_body)


@st.dialog("聖呂中辯電子分紙系統：用戶使用手冊", width="large")
def show_manual():
    render_user_manual_content("manual_dialog_role")


@st.dialog("校園隨想辯論比賽：賽規", width="large")
def show_rules():
    render_rules_content("rules_dialog_role")


def render_home_reference():
    with st.container(border=True):
        st.markdown("### 📚 使用手冊及賽規")
        st.caption("可由首頁直接開啟；左側 sidebar 亦保留相同按鈕。")
        manual_col, rules_col = st.columns(2)

        with manual_col:
            if st.button("📖 閱讀使用手冊", use_container_width=True, key="home_show_manual"):
                show_manual()

        with rules_col:
            if st.button("📋 查看賽規", use_container_width=True, key="home_show_rules"):
                show_rules()


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
                f"SELECT password_hash FROM {TABLE_ACCOUNTS} WHERE user_id = :uid",
                params={"uid": uid.strip()},
                ttl=0,
            )
            login_success = (
                not acc_row.empty
                and _verify_password(upw.strip(), str(acc_row.iloc[0]["password_hash"]))
            )

            if login_success:
                refresh_acc_type(uid.strip())
                _log_login(uid.strip(), "committee")
                st.session_state["committee_user"] = uid.strip()
                set_cookie(cookie_manager, "committee_user", uid.strip(), expires_at=return_expire_day())
                st.success(f"你好，{uid.strip()}！")
                st.rerun()
            else:
                st.error("User ID或Password錯誤！")


def return_expire_day():
    return datetime.datetime.now() + datetime.timedelta(days=1)


def issue_telegram_link_code(user_id: str) -> tuple[str, datetime.datetime]:
    code = generate_telegram_link_code()
    token_hash = hash_telegram_link_code(code)
    expires_at = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")) + datetime.timedelta(minutes=TELEGRAM_LINK_CODE_TTL_MINUTES)
    conn = get_connection()

    with conn.session as s:
        s.execute(
            text(
                f"DELETE FROM {TABLE_TELEGRAM_LINK_TOKENS} "
                "WHERE user_id = :user_id AND consumed_at IS NULL"
            ),
            {"user_id": user_id},
        )
        s.execute(
            text(
                f"INSERT INTO {TABLE_TELEGRAM_LINK_TOKENS} "
                "(token_hash, user_id, issued_at, expires_at) "
                f"VALUES (:token_hash, :user_id, NOW(), NOW() + INTERVAL '{TELEGRAM_LINK_CODE_TTL_MINUTES} minutes')"
            ),
            {"token_hash": token_hash, "user_id": user_id},
        )
        s.commit()

    return code, expires_at


_ACTIVITY_VIEW_SQL = f"""
SELECT
    user_id,
    telegram_chat_id,
    account_status,
    total_votes,
    participated_votes,
    last10_participated,
    total_ballots,
    agree_ballots,
    overall_rate_pct,
    agree_rate_pct,
    is_active
FROM {VIEW_COMMITTEE_VOTE_ACTIVITY}
ORDER BY user_id
"""


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "t", "1", "yes"}


def refresh_acc_type(user_id: str) -> str | None:
    acc = query_params(
        f"SELECT account_status FROM {TABLE_ACCOUNTS} WHERE user_id = :user_id",
        {"user_id": user_id}
    )
    if acc.empty or str(acc.iloc[0]["account_status"]).strip() == "admin" or str(acc.iloc[0]["account_status"]).strip() == "developer":
        return None

    result = query_params(
        f"SELECT is_active FROM {VIEW_COMMITTEE_VOTE_ACTIVITY} WHERE user_id = :user_id",
        {"user_id": user_id},
    )
    if result.empty:
        new_status = "inactive"
    else:
        new_status = "active" if _coerce_bool(result.iloc[0]["is_active"]) else "inactive"
    execute_query(
        f"UPDATE {TABLE_ACCOUNTS} SET account_status = :account_status WHERE user_id = :user_id",
        {"account_status": new_status, "user_id": user_id}
    )
    return new_status


def refresh_all_acc_type():
    """Batch-update account_status for all non-admin/developer accounts in a single query."""
    execute_query(f"""
        UPDATE {TABLE_ACCOUNTS} AS accounts
        SET account_status = CASE
            WHEN activity.is_active THEN 'active'
            ELSE 'inactive'
        END
        FROM {VIEW_COMMITTEE_VOTE_ACTIVITY} AS activity
        WHERE accounts.user_id = activity.user_id
    """)


def compute_threshold(base_min: int, pct: float) -> int:
    """計算動態投票門檻：max(base_min, ceil(pct * active_users))"""
    result = query_params(
        f"SELECT COUNT(*) AS cnt FROM {TABLE_ACCOUNTS} WHERE account_status = 'active'"
    )
    active_count = int(result.iloc[0]["cnt"])
    return max(base_min, math.ceil(pct * active_count))


def return_gemini_reminder():
    return load_markdown_asset("gemini_reminder.md")


def return_chatgpt_reminder():
    return load_markdown_asset("chatgpt_reminder.md")


@st.cache_data(ttl=60)
def _compute_all_user_stats():
    """Compute participation stats for all users in a single SQL query."""
    return query_params(_ACTIVITY_VIEW_SQL)


@st.cache_data(ttl=60)
def get_active_user_count():
    """
    Active user 定義：整體投票率（辯題投票 + 罷免投票）≥ 40% AND 最近10次投票最少投3次。
    Returns (active_count, active_user_list)
    """
    df = _compute_all_user_stats()
    if df.empty:
        return 0, []
    total_votes = int(df.iloc[0]["total_votes"])
    if total_votes == 0:
        return 0, []
    active = df[df["is_active"].apply(_coerce_bool)]
    return len(active), [str(user_id).strip() for user_id in active["user_id"].tolist()]


@st.cache_data(ttl=60)
def get_member_participation_stats():
    """
    Returns (stats_list, total_votes) with per-member participation details
    across both topic_votes and topic_removal_votes.
    """
    df = _compute_all_user_stats()
    if df.empty:
        return [], 0
    total_votes = int(df.iloc[0]["total_votes"])

    stats = []
    for _, row in df.iterrows():
        participated = int(row["participated_votes"])
        overall_rate = participated / total_votes if total_votes > 0 else 0
        last10 = int(row["last10_participated"])
        total_ballots = int(row["total_ballots"])
        agree_ballots = int(row["agree_ballots"])
        agree_rate = agree_ballots / total_ballots if total_ballots > 0 else None
        is_active = _coerce_bool(row["is_active"])

        stats.append({
            "用戶": str(row["user_id"]).strip(),
            "整體投票次數": f"{participated} / {total_votes}",
            "整體投票率": f"{overall_rate:.1%}",
            "最近10次參與": last10,
            "同意票數": f"{agree_ballots} / {total_ballots}",
            "投票同意率": f"{agree_rate:.1%}" if agree_rate is not None else "—",
            "活躍狀態": "✅ 活躍" if is_active else "❌ 非活躍",
        })

    return stats, total_votes


def show_noti_popup(user_id: str) -> None:
    """
    Show a one-time notification dialog backed by the DB `notification_reads` table.

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

    from schema import CREATE_NOTIFICATION_READS

    execute_query(CREATE_NOTIFICATION_READS)
    content = "\n".join(lines[content_start:]).strip()
    seen = query_params(
        f"SELECT 1 FROM {TABLE_NOTIFICATION_READS} WHERE notification_id = :nid AND user_id = :uid",
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
                f"INSERT INTO {TABLE_NOTIFICATION_READS} (notification_id, notification_title, user_id, read_at) "
                "VALUES (:nid, :title, :uid, :seen_at) "
                "ON CONFLICT (notification_id, user_id) DO NOTHING",
                {"nid": noti_id, "title": noti_title, "uid": user_id, "seen_at": seen_at},
            )
            st.rerun()

    _render()


def return_gemini_depose_reminder():
    return load_markdown_asset("gemini_depose_reminder.md")


def return_chatgpt_depose_reminder():
    return load_markdown_asset("chatgpt_depose_reminder.md")
