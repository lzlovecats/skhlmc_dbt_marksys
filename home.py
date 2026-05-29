import streamlit as st
from functions import get_connection, get_registration_status, is_maintenance_mode, query_params, render_maintenance_notice, show_manual, show_rules
from schema import (
    TABLE_ACCOUNTS,
    TABLE_LOGIN_RECORDS,
    TABLE_MATCHES,
    TABLE_SCORES,
    TABLE_TOPIC_VOTES,
    TABLE_TOPICS,
)

# ─── Session state ────────────────────────────────────────────────────────────

if "sys_status_results" not in st.session_state:
    st.session_state["sys_status_results"] = None


# ─── Status check helpers ─────────────────────────────────────────────────────

def _run_status_checks() -> dict:
    results = {
        "db_ok": False,
        "db_error": None,
        "table_counts": None,
        "config_admin_ok": False,
        "config_developer_ok": False,
        "pending_votes": None,
        "logins_24h": None,
        "errors": [],
    }

    # Check 1: DB connection
    try:
        conn = get_connection()
        conn.query("SELECT 1", ttl=0)
        results["db_ok"] = True
    except Exception as e:
        results["db_error"] = str(e)
        return results

    # Check 2: Table row counts
    try:
        counts = {}
        for table in (TABLE_ACCOUNTS, TABLE_MATCHES, TABLE_SCORES, TABLE_TOPICS):
            count_df = query_params(f"SELECT COUNT(*) AS cnt FROM {table}")
            counts[table] = int(count_df.iloc[0]["cnt"]) if not count_df.empty else 0
        results["table_counts"] = counts
    except Exception as e:
        results["errors"].append(f"表格計數失敗: {e}")

    # Check 3: system_config key existence
    try:
        config_df = query_params(
            "SELECT key FROM system_config WHERE key IN ('admin_password', 'developer_password')"
        )
        found_keys = set(config_df["key"].tolist()) if not config_df.empty else set()
        results["config_admin_ok"] = "admin_password" in found_keys
        results["config_developer_ok"] = "developer_password" in found_keys
    except Exception as e:
        results["errors"].append(f"系統設定查詢失敗: {e}")

    # Check 4: Pending topic votes
    try:
        pending_vote_df = query_params(f"SELECT COUNT(*) AS cnt FROM {TABLE_TOPIC_VOTES} WHERE status = 'pending'")
        results["pending_votes"] = int(pending_vote_df.iloc[0]["cnt"]) if not pending_vote_df.empty else 0
    except Exception as e:
        results["errors"].append(f"辯題投票查詢失敗: {e}")

    # Check 5: Login activity in last 24h
    try:
        login_df = query_params(
            f"SELECT COUNT(*) AS cnt FROM {TABLE_LOGIN_RECORDS} "
            "WHERE logged_in_at >= NOW() - INTERVAL '24 hours'"
        )
        results["logins_24h"] = int(login_df.iloc[0]["cnt"]) if not login_df.empty else 0
    except Exception as e:
        results["errors"].append(f"登入紀錄查詢失敗: {e}")

    return results


def _render_status_results(results: dict):
    if results["db_ok"]:
        st.success("資料庫連線正常")
    else:
        st.error(f"資料庫連線失敗: {results['db_error']}")
        return

    if results["table_counts"] is not None:
        counts = results["table_counts"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("帳戶數", counts.get("accounts", "—"))
        c2.metric("比賽場次", counts.get("matches", "—"))
        c3.metric("評分紀錄", counts.get("scores", "—"))
        c4.metric("辯題庫", counts.get("topics", "—"))

    c5, c6 = st.columns(2)
    pending_vote_count = results["pending_votes"]
    c5.metric("待表決辯題", pending_vote_count if pending_vote_count is not None else "—")
    login_count_24h = results["logins_24h"]
    c6.metric("24 小時內登入次數", login_count_24h if login_count_24h is not None else "—")

    if results["config_admin_ok"]:
        st.success("賽會人員密碼已設定")
    else:
        st.warning("尚未設定賽會人員密碼")
    if results["config_developer_ok"]:
        st.success("開發者密碼已設定")
    else:
        st.warning("尚未設定開發者密碼")

    if results["errors"]:
        st.warning("部分檢查未能完成：\n" + "\n".join(f"- {e}" for e in results["errors"]))


# ─── Page header ──────────────────────────────────────────────────────────────

st.title("聖呂中辯電子分紙系統")
st.caption("請根據你的身份選擇對應功能")
if is_maintenance_mode():
    render_maintenance_notice()
    st.stop()

registration_status = get_registration_status()
if registration_status["is_open"]:
    settings = registration_status["settings"]
    with st.container(border=True):
        st.markdown(f"### 第 {settings['competition_edition']} 屆比賽現正接受報名")
        st.write("報名步驟：填寫隊伍資料 → 確認聯絡方法 → 提交報名")
        c1, c2 = st.columns([3, 1])
        with c1:
            st.caption(
                f"截止時間（香港時間／HKT）：{settings['registration_end'].strftime('%Y-%m-%d %H:%M')}"
            )
        with c2:
            st.page_link("registration.py", label="前往比賽報名", icon="📝")

# ─── Role cards — 2-column grid ───────────────────────────────────────────────

col_left, col_right = st.columns(2)

with col_left:
    with st.container(border=True):
        st.markdown("### ⚖️ 評判")
        st.write("填寫電子分紙，提交比賽評分。")
        st.page_link("judging.py", label="前往電子分紙", icon="📝")

    with st.container(border=True):
        st.markdown("### 🌐 一般人員")
        st.write("瀏覽公開辯題庫及相關統計。")
        st.page_link("open_db.py", label="查閱辯題庫", icon="📚")

with col_right:
    with st.container(border=True):
        st.markdown("### 🏆 參賽隊伍")
        st.write("查閱所參與比賽的評判評分紙。")
        st.page_link("review.py", label="查閱比賽分紙", icon="📄")

    with st.container(border=True):
        st.markdown("### 🎛️ 賽會人員")
        st.write("管理比賽報名、比賽場次、賽果、資料庫控制台及抽取賽程。")
        st.page_link("registration_admin.py", label="比賽報名管理", icon="🗂️")
        st.page_link("match_info.py", label="比賽場次管理", icon="📋")
        st.page_link("management.py", label="查閱比賽結果", icon="📊")
        st.page_link("db_mgmt.py", label="資料庫管理控制台", icon="🖥️")
        st.page_link("draw_match_schedule.py", label="抽取賽程", icon="🎲")

with st.container(border=True):
    st.markdown("### 🗳️ 內部委員會成員")
    st.write("提出辯題、參與投票、提出罷免動議及管理個人帳戶。")
    st.page_link("vote.py", label="辯題徵集、投票及罷免", icon="🗳️")

st.divider()
with st.container(border=True):
    st.markdown("### 📚 支援資料")
    st.caption("如需了解操作流程或比賽規則，可在此開啟完整說明。")
    support_col1, support_col2 = st.columns(2)
    with support_col1:
        if st.button("📖 閱讀使用手冊", use_container_width=True, key="home_show_manual"):
            show_manual()
    with support_col2:
        if st.button("📋 查看賽規", use_container_width=True, key="home_show_rules"):
            show_rules()

# ─── System status check — collapsed at bottom ────────────────────────────────

st.divider()
with st.expander("🔧 系統狀態檢查", expanded=False):
    if st.button("執行系統狀態檢查", use_container_width=True):
        with st.spinner("正在檢查系統狀態..."):
            st.session_state["sys_status_results"] = _run_status_checks()

    results = st.session_state.get("sys_status_results")
    if results is not None:
        _render_status_results(results)
