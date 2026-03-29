import streamlit as st
import re
from functions import return_user_manual, return_rules

# Set up basic structure of the webpage
st.set_page_config(page_title="聖呂中辯電子分紙系統", layout="wide", page_icon="📑")


def extract_markdown_section(content, heading_level, target_heading):
    prefix = "#" * heading_level
    m = re.search(
        rf"^{re.escape(prefix)}\s+{re.escape(target_heading)}\s*$.*?(?=^{re.escape(prefix)}\s|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    return m.group(0).strip() if m else None

@st.dialog("聖呂中辯電子分紙系統：用戶使用手冊", width="large")
def show_manual():
    role = st.radio(
        "請先選擇你的身份：",
        ["評判", "賽會人員", "比賽隊伍", "一般人員", "內部委員會成員"],
        horizontal=True,
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
    target = role_section_map[role]
    section_text = extract_markdown_section(manual_content, 3, target)
    if section_text:
        st.markdown(section_text)
    else:
        st.markdown(manual_content)

@st.dialog("校園隨想辯論比賽：賽規", width="large")
def show_rules():
    role = st.radio(
        "請先選擇你的身份：",
        ["評判", "賽會人員", "參賽隊伍"],
        horizontal=True,
    )
    st.divider()

    role_section_map = {
        "評判": "一、評判",
        "賽會人員": "二、賽會人員",
        "參賽隊伍": "三、參賽隊伍",
    }

    rules_content = return_rules()
    # Always show the disclaimer first
    disclaimer_end = rules_content.find("---")
    if disclaimer_end != -1:
        st.markdown(rules_content[: disclaimer_end + 3])
        body = rules_content[disclaimer_end + 3 :]
    else:
        body = rules_content

    target = role_section_map[role]
    section_text = extract_markdown_section(body, 2, target)
    if section_text:
        st.markdown(section_text)
    else:
        st.markdown(body)

# Define pages
page_judging = st.Page("judging.py", title="電子分紙（評判用）")
page_match_mgmt = st.Page("match_info.py", title="比賽場次管理（賽會人員用）")
page_mgmt = st.Page("management.py", title="查閱比賽結果（賽會人員用）")

# Hided this page start from V2.1.0, Reason: Not many people need this function, and it may cause security issues if not used properly. 
# Will consider to reopen this page in the future if there are enough demand.
page_db_mgmt = st.Page("db_mgmt.py", title="辯題庫管理（賽會人員用）")  

page_draw_schedule = st.Page("draw_match_schedule.py", title="抽取賽程（賽會人員用）")
page_score_sheet = st.Page("review.py", title="查閱比賽分紙（比賽隊伍用）")
page_open_db = st.Page("open_db.py", title="查閱辯題庫（一般人員用）")
page_vote = st.Page("vote.py", title="辯題徵集、投票及罷免系統（內部用）", url_path="vote")

# Arrange pages
pg = st.navigation([page_judging, page_match_mgmt, page_mgmt, page_draw_schedule, page_score_sheet, page_open_db, page_vote])

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
    st.caption("🛠️ 系統版本：2.11.7")
    st.caption("🛜 Developed by lzlovecats @ 2026")

pg.run()
