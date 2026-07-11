import streamlit as st
import streamlit.components.v1 as components
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


def _is_install_mode():
    install_value = st.query_params.get("install", "")
    if isinstance(install_value, list):
        install_value = install_value[0] if install_value else ""
    return str(install_value) == "1"


def _render_mobile_styles():
    st.markdown(
        """
        <style>
        .skh-install-assistant {
            border: 1px solid rgba(148, 163, 184, 0.35);
            border-radius: 8px;
            padding: 1rem;
            margin: 0.5rem 0 1rem;
            background: rgba(15, 23, 42, 0.72);
        }
        .skh-install-kicker {
            color: #5eead4;
            font-size: 0.82rem;
            font-weight: 700;
            margin-bottom: 0.25rem;
        }
        .skh-install-title {
            font-size: 1.1rem;
            font-weight: 700;
            line-height: 1.35;
            margin-bottom: 0.35rem;
        }
        .skh-install-copy,
        .skh-install-status,
        .skh-ios-steps {
            color: rgba(226, 232, 240, 0.9);
            font-size: 0.92rem;
            line-height: 1.55;
        }
        .skh-install-actions {
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
            align-items: center;
            margin: 0.8rem 0 0.35rem;
        }
        .skh-install-button {
            min-height: 2.6rem;
            border: 0;
            border-radius: 8px;
            padding: 0 1rem;
            color: #0f172a;
            background: #5eead4;
            font-weight: 700;
            cursor: pointer;
        }
        .skh-install-button:disabled {
            cursor: not-allowed;
            opacity: 0.62;
        }
        .skh-ios-steps {
            display: none;
            margin: 0.6rem 0 0;
            padding-left: 1.25rem;
        }
        .skh-pwa-ios .skh-ios-steps {
            display: block;
        }
        .skh-pwa-ios .skh-android-only {
            display: none !important;
        }
        .skh-pwa-installed .skh-install-assistant {
            display: none;
        }

        @media (max-width: 640px) {
            .block-container {
                padding-left: 1rem;
                padding-right: 1rem;
            }
            div[data-testid="stHorizontalBlock"] {
                gap: 0.75rem;
            }
            div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
                min-width: 100% !important;
                width: 100% !important;
                flex: 1 1 100% !important;
            }
            div[data-testid="stPageLink"] a,
            div[data-testid="stButton"] button {
                min-height: 2.75rem;
                align-items: center;
            }
            h1 {
                font-size: 1.65rem !important;
                line-height: 1.25 !important;
            }
            h3 {
                font-size: 1.08rem !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_install_assistant():
    st.markdown(
        """
        <div id="skh-install-assistant" class="skh-install-assistant">
            <div class="skh-install-kicker">手機版捷徑</div>
            <div class="skh-install-title">將系統加到手機主畫面</div>
            <div class="skh-install-copy">之後按下主畫面圖示會直接進入系統主頁，再自行選擇功能。</div>
            <div class="skh-install-actions skh-android-only">
                <button id="skh-install-button" class="skh-install-button" type="button" style="display:none;">安裝手機版</button>
            </div>
            <ol class="skh-ios-steps">
                <li>用 Safari 開啟此頁。</li>
                <li>按下底部分享按鈕 ⬆️。</li>
                <li>選擇「加入主畫面」，再按下「加入」。</li>
            </ol>
            <div id="skh-install-status" class="skh-install-status">正在檢查手機瀏覽器安裝方式。</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    components.html(
        """
        <script>
        (function () {
            const win = window.parent;
            const doc = win.document;
            const ua = win.navigator.userAgent || "";
            const isStandalone = (
                win.matchMedia("(display-mode: standalone)").matches ||
                win.navigator.standalone === true
            );
            const isIOS = /iPad|iPhone|iPod/.test(ua) ||
                (ua.includes("Macintosh") && "ontouchend" in doc);
            const isAndroid = /Android/i.test(ua);

            doc.body.classList.toggle("skh-pwa-installed", isStandalone);
            doc.body.classList.toggle("skh-pwa-ios", isIOS);
            doc.body.classList.toggle("skh-pwa-android", isAndroid);

            function init(attempt) {
                const assistant = doc.getElementById("skh-install-assistant");
                const button = doc.getElementById("skh-install-button");
                const status = doc.getElementById("skh-install-status");

                if (!assistant || !status) {
                    if (attempt < 20) {
                        setTimeout(function () { init(attempt + 1); }, 150);
                    }
                    return;
                }
                if (isStandalone) {
                    assistant.hidden = true;
                    return;
                }

                function setStatus(text) {
                    status.textContent = text;
                }

                function refreshInstallButton() {
                    if (!button) {
                        return;
                    }
                    if (isAndroid && win.__skhPwaDeferredPrompt) {
                        button.style.display = "inline-flex";
                        button.disabled = false;
                        setStatus("如瀏覽器彈出確認視窗，按安裝即可。");
                    } else if (isAndroid) {
                        button.style.display = "none";
                        setStatus("如未見安裝按鈕，請按 Chrome 右上角選單，再選 Install app 或 Add to Home screen。");
                    } else if (isIOS) {
                        button.style.display = "none";
                        setStatus("iPhone/iPad 需要經 Safari 分享選單加入主畫面。");
                    } else {
                        button.style.display = "none";
                        setStatus("可使用瀏覽器選單將此系統加入桌面或主畫面。");
                    }
                }

                if (button && !button.dataset.skhInstallBound) {
                    button.dataset.skhInstallBound = "1";
                    button.addEventListener("click", async function () {
                        button.disabled = true;
                        const result = win.__skhPromptPwaInstall ?
                            await win.__skhPromptPwaInstall() :
                            { available: false };
                        if (!result.available) {
                            setStatus("瀏覽器暫時未開放安裝提示，請改用選單加入主畫面。");
                            refreshInstallButton();
                            return;
                        }
                        if (result.outcome === "accepted") {
                            setStatus("已開始安裝。完成後可從手機主畫面開啟。");
                        } else {
                            setStatus("已取消安裝，可稍後再按安裝。");
                            refreshInstallButton();
                        }
                    });
                }

                if (!win.__skhPwaAssistantEventsBound) {
                    win.__skhPwaAssistantEventsBound = true;
                    win.addEventListener("skh-pwa-install-ready", refreshInstallButton);
                    win.addEventListener("skh-pwa-installed", function () {
                        assistant.hidden = true;
                    });
                }
                refreshInstallButton();
                setTimeout(refreshInstallButton, 1200);
            }

            init(0);
        })();
        </script>
        """,
        height=0,
        width=0,
    )


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

_render_mobile_styles()

st.title("聖呂中辯電子賽務系統")
st.caption("請根據你的身份選擇對應功能")
if _is_install_mode():
    _render_install_assistant()
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
        st.write("整合管理報名、場次、片段及賽程，並提供主席主持工具。")
        st.page_link("admin_hub.py", label="賽務管理易", icon="🗂️")
        st.page_link("chairperson.py", label="主席主持易", icon="🎤")
        st.page_link("management.py", label="查閱比賽結果", icon="📊")
        st.page_link("db_mgmt.py", label="資料庫管理控制台", icon="🖥️")

with st.container(border=True):
    st.markdown("### 🗳️ 內部委員會成員")
    st.write("提出辯題、參與投票、提出罷免動議、重溫比賽片段及圖片、使用 AI 辯論教練、提交 AI 訓練資料、管理 AI 基金及遲到罰款、回報 Bug 及管理個人帳戶。")
    st.page_link("vote.py", label="辯題徵集、投票及罷免", icon="🗳️")
    st.page_link("ai_coach.py", label="AI 辯論易", icon="✨")
    st.page_link("ai_training.py", label="聖呂中辯AI訓練", icon="🎙️")
    st.page_link("video_replay.py", label="比賽片段重溫", icon="🎬")
    st.page_link("match_photos.py", label="比賽圖片回顧", icon="🖼️")
    st.page_link("ai_fund.py", label="AI基金", icon="💲")
    st.page_link("lateness_fund.py", label="遲到罰款基金", icon="💰")
    st.page_link("bug_report.py", label="Bug回報", icon="🛠️")

st.divider()
with st.expander("📚 支援資料", expanded=False):
    st.caption("如需了解操作流程或比賽規則，可在此開啟完整說明。")
    support_col1, support_col2 = st.columns(2)
    with support_col1:
        if st.button("📖 閱讀使用手冊", width="stretch", key="home_show_manual"):
            show_manual()
    with support_col2:
        if st.button("📋 查看賽規", width="stretch", key="home_show_rules"):
            show_rules()

# ─── System status check — collapsed at bottom ────────────────────────────────

st.divider()
with st.expander("🔧 系統狀態檢查", expanded=False):
    if st.button("執行系統狀態檢查", width="stretch"):
        with st.spinner("正在檢查系統狀態..."):
            st.session_state["sys_status_results"] = _run_status_checks()

    results = st.session_state.get("sys_status_results")
    if results is not None:
        _render_status_results(results)
