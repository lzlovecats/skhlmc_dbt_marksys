import streamlit as st
import streamlit.components.v1 as components
from urllib.parse import urlparse
from functions import get_registration_status, is_maintenance_mode, render_maintenance_notice, show_manual, show_rules

# Set up basic structure of the webpage
st.set_page_config(page_title="聖呂中辯電子賽務系統", layout="wide", page_icon="📑")


def render_pwa_install_listener():
    components.html(
        """
        <style>
        input, textarea, select {
            font-size: 16px !important;
        }
        </style>
        <script>
        (function () {
            const win = window.parent;
            let viewport = win.document.querySelector("meta[name='viewport']");
            if (!viewport) {
                viewport = win.document.createElement("meta");
                viewport.setAttribute("name", "viewport");
                win.document.head.appendChild(viewport);
            }
            viewport.setAttribute("content", "width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover");
            if (!win.document.getElementById("skh-mobile-input-zoom-fix")) {
                const style = win.document.createElement("style");
                style.id = "skh-mobile-input-zoom-fix";
                style.textContent = `
                    input, textarea, select { font-size: 16px !important; }

                    @media (max-width: 640px) {
                        [data-testid="stExpandSidebarButton"] {
                            position: fixed !important;
                            top: max(1.25rem, calc(env(safe-area-inset-top) + 0.75rem)) !important;
                            left: 0.75rem !important;
                            z-index: 999999 !important;
                        }

                        [data-testid="collapsedControl"],
                        [data-testid="stExpandSidebarButton"],
                        button[aria-label="Open sidebar"] {
                            width: 3rem !important;
                            height: 3rem !important;
                            min-width: 3rem !important;
                            min-height: 3rem !important;
                            padding: 0.65rem !important;
                            align-items: center !important;
                            justify-content: center !important;
                        }
                    }
                `;
                win.document.head.appendChild(style);
            }

            if (!win.__skhPwaInstallListenerReady) {
                win.__skhPwaInstallListenerReady = true;
                win.__skhPwaDeferredPrompt = null;
                win.__skhPwaInstalled = (
                    win.matchMedia("(display-mode: standalone)").matches ||
                    win.navigator.standalone === true
                );

                win.addEventListener("beforeinstallprompt", function (event) {
                    event.preventDefault();
                    win.__skhPwaDeferredPrompt = event;
                    win.dispatchEvent(new Event("skh-pwa-install-ready"));
                });

                win.addEventListener("appinstalled", function () {
                    win.__skhPwaInstalled = true;
                    win.__skhPwaDeferredPrompt = null;
                    win.dispatchEvent(new Event("skh-pwa-installed"));
                });

                win.__skhPromptPwaInstall = async function () {
                    if (!win.__skhPwaDeferredPrompt) {
                        return { available: false };
                    }
                    const promptEvent = win.__skhPwaDeferredPrompt;
                    win.__skhPwaDeferredPrompt = null;
                    promptEvent.prompt();
                    const choice = await promptEvent.userChoice;
                    return { available: true, outcome: choice && choice.outcome };
                };
            }
        })();
        </script>
        """,
        height=0,
        width=0,
    )


render_pwa_install_listener()

if is_maintenance_mode():
    st.title("聖呂中辯電子賽務系統")
    render_maintenance_notice()
    st.stop()

# Define pages
page_home = st.Page("home.py", title="主頁", icon="🏠", default=True)
page_judging = st.Page("judging.py", title="電子分紙")
page_match_mgmt = st.Page("match_info.py", title="比賽場次管理")
page_mgmt = st.Page("management.py", title="查閱比賽結果")
page_registration_admin = st.Page("registration_admin.py", title="比賽報名管理")
page_db_mgmt = st.Page("db_mgmt.py", title="資料庫管理控制台")
page_draw_schedule = st.Page("draw_match_schedule.py", title="抽取賽程")
page_score_sheet = st.Page("review.py", title="查閱比賽分紙")
page_video_replay = st.Page("video_replay.py", title="比賽片段重溫", url_path="video-replay")
page_match_photos = st.Page("match_photos.py", title="比賽圖片回顧", url_path="match-photos")
page_video_admin = st.Page("video_admin.py", title="比賽片段管理")
page_registration = st.Page("registration.py", title="比賽報名", url_path="registration")
page_open_db = st.Page("open_db.py", title="查閱辯題庫")
page_vote = st.Page("vote.py", title="辯題徵集、投票及罷免", url_path="vote")
page_dev_settings = st.Page("dev_settings.py", title="開發者設定")
page_admin_hub = st.Page("admin_hub.py", title="賽務管理易", url_path="admin-hub")
page_chairperson = st.Page("chairperson.py", title="主席主持易", url_path="chairperson")
page_team_roster = st.Page("team_roster.py", title="提交隊伍名單", url_path="team-roster")
page_ai_coach = st.Page("ai_coach.py", title="AI 辯論易", url_path="ai-coach")
page_lateness_fund = st.Page("lateness_fund.py", title="遲到罰款基金", url_path="lateness-fund")


def is_team_roster_page():
    try:
        path = urlparse(st.context.url).path.rstrip("/")
    except Exception:
        return False
    return path.endswith("/team-roster")


if is_team_roster_page():
    pg = st.navigation([page_team_roster], position="hidden")
    pg.run()
    st.stop()

registration_status = get_registration_status()
public_pages = [page_open_db]
if registration_status["is_open"]:
    public_pages.insert(0, page_registration)

# Arrange pages by user role
pg = st.navigation({
    "": [page_home],
    "評判": [page_judging],
    "參賽隊伍": [page_score_sheet],
    "一般人員": public_pages,
    "賽會人員": [page_admin_hub, page_chairperson, page_mgmt, page_db_mgmt],
    "內部委員會成員": [page_vote, page_ai_coach, page_video_replay, page_match_photos, page_lateness_fund],
    "開發者": [page_dev_settings],
})

# Show logout when admin logged in
if st.session_state.get("admin_logged_in"):
    with st.sidebar:
        st.write("")
        if st.button("登出賽會人員帳戶", use_container_width=True):
            st.session_state["admin_logged_in"] = False
            st.rerun()

# Show manual
with st.sidebar:
    if st.button("📖 閱讀使用手冊", use_container_width=True):
        show_manual()

with st.sidebar:
    if st.button("📋 查看賽規", use_container_width=True):
        show_rules()

# Show caption
with st.sidebar:
    st.caption("🛠️ 系統版本：3.6.7")
    st.caption("🛜 開發及維護：[lzlovecats](https://github.com/lzlovecats) @ 2026")

pg.run()
