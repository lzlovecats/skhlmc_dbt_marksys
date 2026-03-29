import re
import streamlit as st
import pandas as pd
from sqlalchemy import text
from functions import check_admin, get_connection, get_system_config, _verify_config_password

st.header("數據庫管理控制台")

if not check_admin():
    st.stop()

# ── Secondary SQL password verification ──────────────────────────────────────

if "sql_verified" not in st.session_state:
    st.session_state["sql_verified"] = False

if not st.session_state["sql_verified"]:
    st.subheader("數據庫訪問驗證")
    st.caption("此頁面需要額外的 SQL 訪問密碼。")
    sql_pwd = st.text_input("請輸入 SQL 訪問密碼", type="password")
    if st.button("驗證"):
        stored = get_system_config("sql_password")
        if stored is None:
            st.error("系統錯誤：未能讀取 SQL 訪問密碼，請聯絡開發人員")
        elif _verify_config_password(sql_pwd, stored):
            st.session_state["sql_verified"] = True
            st.rerun()
        else:
            st.error("密碼錯誤")
    st.stop()

st.caption("⚠️ 此頁面直接操作生產數據庫，請謹慎使用。")

# ── Table schema reference ────────────────────────────────────────────────────

_SCHEMAS = {
    "accounts — 委員會成員帳戶": [
        ("userid",    "TEXT (PK)",  "成員帳號（登入用）"),
        ("userpw",    "TEXT",       "密碼（bcrypt 加密）"),
        ("acc_type",  "TEXT",       "'admin' | 'active' | 'inactive'"),
        ("tg_userid", "TEXT",       "Telegram 用戶 ID（未連結則為 NULL）"),
        ("tg_chatid", "TEXT",       "Telegram Chat ID（未連結則為 NULL）"),
    ],
    "matches — 比賽場次": [
        ("match_id",        "TEXT (PK)", "場次編號（例如：第一屆初賽）"),
        ("date",            "DATE",      "比賽日期"),
        ("time",            "TIME",      "比賽時間"),
        ("topic",           "TEXT",      "辯題"),
        ("pro_team",        "TEXT",      "正方隊伍名稱"),
        ("con_team",        "TEXT",      "反方隊伍名稱"),
        ("access_code",     "TEXT",      "評判入場密碼"),
        ("review_password", "TEXT",      "隊伍查卷密碼"),
    ],
    "debaters — 辯員名單": [
        ("match_id", "TEXT (PK, FK→matches)", "所屬場次"),
        ("side",     "TEXT (PK)",             "'pro'（正方）| 'con'（反方）"),
        ("position", "INTEGER (PK)",          "1=主辯 2=一副 3=二副 4=結辯"),
        ("name",     "TEXT",                  "辯員姓名"),
    ],
    "scores — 評判評分（正式提交）": [
        ("match_id",      "TEXT (FK→matches)", "所屬場次"),
        ("judge_name",    "TEXT",              "評判姓名（已標準化）"),
        ("pro_total",     "INTEGER",           "正方總分"),
        ("con_total",     "INTEGER",           "反方總分"),
        ("mark_time",     "TEXT",              "提交時間"),
        ("pro_free",      "INTEGER",           "正方自由辯論分"),
        ("con_free",      "INTEGER",           "反方自由辯論分"),
        ("pro_deduction", "INTEGER",           "正方扣分"),
        ("con_deduction", "INTEGER",           "反方扣分"),
        ("pro_coherence", "INTEGER",           "正方連貫性分"),
        ("con_coherence", "INTEGER",           "反方連貫性分"),
    ],
    "debater_scores — 辯員個人分數": [
        ("match_id",   "TEXT (PK, FK→scores)", "所屬場次"),
        ("judge_name", "TEXT (PK, FK→scores)", "評判姓名"),
        ("side",       "TEXT (PK)",            "'pro' | 'con'"),
        ("position",   "INTEGER (PK)",         "1–4，對應辯員位置"),
        ("score",      "INTEGER",              "該辯員得分"),
    ],
    "temp_scores — 評分暫存草稿": [
        ("match_id",   "TEXT (FK→matches)", "所屬場次"),
        ("judge_name", "TEXT",              "評判姓名"),
        ("team_side",  "TEXT",              "'pro' | 'con'"),
        ("data",       "TEXT",              "JSON 格式評分草稿（含 DataFrame）"),
        ("is_final",   "BOOLEAN",           "是否已正式提交"),
        ("updated_at", "TIMESTAMP",         "最後儲存時間"),
    ],
    "topics — 辯題庫": [
        ("topic",      "TEXT (PK)", "辯題內容"),
        ("author",     "TEXT",      "提出人（委員會帳號或 'admin'）"),
        ("category",   "TEXT",      "辯題類別"),
        ("difficulty", "INTEGER",   "難度 1=日常 2=一般 3=進階"),
    ],
    "topic_votes — 辯題徵集投票": [
        ("topic",      "TEXT (PK)",          "辯題內容"),
        ("author",     "TEXT (FK→accounts)", "提出人帳號"),
        ("status",     "TEXT",               "'pending' | 'passed' | 'rejected'"),
        ("created_at", "TIMESTAMP",          "提出時間"),
        ("deadline",   "DATE",               "投票截止日期"),
        ("threshold",  "INTEGER",            "入庫所需同意票數"),
        ("category",   "TEXT",               "辯題類別"),
        ("difficulty", "INTEGER",            "難度"),
    ],
    "topic_vote_ballots — 辯題投票選票": [
        ("topic",   "TEXT (PK, FK→topic_votes)", "對應辯題"),
        ("user_id", "TEXT (PK, FK→accounts)",    "投票人帳號"),
        ("vote",    "TEXT",                       "'agree' | 'against'"),
        ("reasons", "JSONB",                      "不同意原因列表（同意票為空陣列）"),
    ],
    "topic_depose_votes — 辯題罷免動議": [
        ("topic",            "TEXT (PK, FK→topics)",    "被罷免辯題"),
        ("mover",            "TEXT (FK→accounts)",      "提案人帳號"),
        ("status",           "TEXT",                    "'pending' | 'passed' | 'rejected'"),
        ("proposal_reasons", "JSONB",                   "提案理由列表"),
        ("created_at",       "TIMESTAMP",               "提案時間"),
        ("deadline",         "DATE",                    "投票截止日期"),
        ("threshold",        "INTEGER",                 "罷免所需同意票數"),
    ],
    "depose_vote_ballots — 罷免投票選票": [
        ("topic",   "TEXT (PK, FK→topic_depose_votes)", "對應罷免動議"),
        ("user_id", "TEXT (PK, FK→accounts)",           "投票人帳號"),
        ("vote",    "TEXT",                              "'agree' | 'against'"),
    ],
    "login_record — 登入紀錄": [
        ("id",         "SERIAL (PK)", "自動編號"),
        ("user_id",    "TEXT",        "登入帳號（委員會帳號或 'admin'）"),
        ("login_type", "TEXT",        "'committee' | 'admin' | 'score_review'"),
        ("login_time", "TIMESTAMP",   "登入時間（HKT）"),
    ],
    "noti — 站內通知": [
        ("notiid",    "INT (PK)",     "通知編號（見 assets/noti.md）"),
        ("notititle", "VARCHAR(255)", "通知標題"),
        ("userid",    "VARCHAR(50)",  "已閱讀的成員帳號"),
        ("seen_at",   "TIMESTAMP",    "閱讀時間"),
    ],
    "tg_notification_queue — Telegram 推送佇列": [
        ("id",                   "SERIAL (PK)", "自動編號"),
        ("noti_type",            "TEXT",        "'new_topic' | 'new_depose' | 'vote_result'"),
        ("payload",              "JSONB",       "通知內容（含推送所需全部資料）"),
        ("created_at",           "TIMESTAMP",   "建立時間"),
        ("processed",            "BOOLEAN",     "是否已由 Bot 處理"),
        ("processing_token",     "TEXT",        "防重複處理 token"),
        ("processing_started_at","TIMESTAMP",   "Bot 開始處理時間"),
        ("last_error",           "TEXT",        "最後錯誤訊息（若有）"),
    ],
}

with st.expander("📋 數據庫表結構參考", expanded=False):
    for table_label, columns in _SCHEMAS.items():
        st.markdown(f"**{table_label}**")
        st.dataframe(
            pd.DataFrame(columns, columns=["欄位", "類型", "說明"]),
            use_container_width=True,
            hide_index=True,
        )

if "sql_pending_confirm" not in st.session_state:
    st.session_state["sql_pending_confirm"] = False
if "sql_pending_query" not in st.session_state:
    st.session_state["sql_pending_query"] = ""


def _is_dangerous_no_where(sql: str) -> bool:
    """Return True if SQL contains UPDATE...SET or DELETE FROM without a WHERE clause."""
    upper = sql.upper()
    has_update = bool(re.search(r"\bUPDATE\b.+\bSET\b", upper, re.DOTALL))
    has_delete = bool(re.search(r"\bDELETE\b\s+FROM\b", upper))
    has_where = bool(re.search(r"\bWHERE\b", upper))
    return (has_update or has_delete) and not has_where


def _touches_system_config(sql: str) -> bool:
    """Return True if SQL tries to modify the system_config table."""
    upper = sql.upper()
    if not re.search(r"\bSYSTEM_CONFIG\b", upper):
        return False
    modifying_keywords = r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|SELECT)\b"
    return bool(re.search(modifying_keywords, upper))


def _run_query(sql: str):
    conn = get_connection()
    upper = sql.strip().upper()
    is_select = upper.startswith("SELECT") or upper.startswith("WITH")
    with conn.session as s:
        result = s.execute(text(sql))
        if is_select:
            rows = result.fetchall()
            cols = list(result.keys())
            s.commit()
            return "select", pd.DataFrame(rows, columns=cols)
        else:
            s.commit()
            return "dml", result.rowcount


sql_input = st.text_area("SQL Query", height=160, placeholder="SELECT * FROM topics LIMIT 10;")

col_run, col_clear = st.columns([1, 5])
with col_run:
    run_clicked = st.button("▶ 執行", type="primary", use_container_width=True)
with col_clear:
    if st.button("清除"):
        st.session_state["sql_pending_confirm"] = False
        st.session_state["sql_pending_query"] = ""
        st.rerun()

if run_clicked and sql_input.strip():
    sql = sql_input.strip()

    if _touches_system_config(sql):
        st.error("🚫 Access Denied: 'system_config' is not allowed to access")
    elif _is_dangerous_no_where(sql):
        st.session_state["sql_pending_confirm"] = True
        st.session_state["sql_pending_query"] = sql
        st.rerun()
    else:
        try:
            with st.spinner("執行中..."):
                kind, data = _run_query(sql)
            if kind == "select":
                st.success(f"查詢完成，共 {len(data)} 行")
                st.dataframe(data, use_container_width=True, hide_index=True)
            else:
                st.success(f"執行完成，影響 {data} 行")
        except Exception as e:
            st.error(f"執行失敗：{e}")

if st.session_state["sql_pending_confirm"]:
    pending_sql = st.session_state["sql_pending_query"]
    st.warning(
        "⚠️ **危險操作警告**\n\n"
        "偵測到此 SQL 語句含有 `UPDATE` 或 `DELETE` 操作，但**沒有 `WHERE` 條件**，"
        "將會影響整張表的所有資料，此操作**無法復原**！"
    )
    st.code(pending_sql, language="sql")
    confirmed = st.checkbox("我明白風險，確認執行此操作")
    col_confirm, col_cancel = st.columns(2)
    with col_confirm:
        if st.button("確認執行", type="primary", disabled=not confirmed, use_container_width=True):
            try:
                with st.spinner("執行中..."):
                    kind, data = _run_query(pending_sql)
                st.session_state["sql_pending_confirm"] = False
                st.session_state["sql_pending_query"] = ""
                if kind == "select":
                    st.success(f"查詢完成，共 {len(data)} 行")
                    st.dataframe(data, use_container_width=True, hide_index=True)
                else:
                    st.success(f"執行完成，影響 {data} 行")
            except Exception as e:
                st.error(f"執行失敗：{e}")
    with col_cancel:
        if st.button("取消", use_container_width=True):
            st.session_state["sql_pending_confirm"] = False
            st.session_state["sql_pending_query"] = ""
            st.rerun()
