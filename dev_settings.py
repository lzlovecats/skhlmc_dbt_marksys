import streamlit as st
import bcrypt
import datetime
from zoneinfo import ZoneInfo
from functions import get_system_config, _verify_config_password, execute_query

st.header("開發者設定")

# ── Developer authentication ──────────────────────────────────────────────────

if "dev_logged_in" not in st.session_state:
    st.session_state["dev_logged_in"] = False

if not st.session_state["dev_logged_in"]:
    stored_dev_pw = get_system_config("developer_password")
    if stored_dev_pw is None:
        st.error(
            "尚未設定開發者密碼。\n\n"
            "請先在 `system_config` 加入以下紀錄，然後重新整理此頁：\n\n"
            "```sql\n"
            "INSERT INTO system_config (key, value, updated_at)\n"
            "VALUES ('developer_password', '<your password>', NOW()::TEXT);\n"
            "```\n\n"
            "首次設定時請先使用明文密碼，並於首次登入後立即改為加密版本。"
        )
        st.stop()

    st.subheader("開發者登入")
    dev_pwd = st.text_input("請輸入開發者密碼", type="password")
    if st.button("登入"):
        if _verify_config_password(dev_pwd, stored_dev_pw):
            st.session_state["dev_logged_in"] = True
            st.rerun()
        else:
            st.error("密碼錯誤。")
    st.stop()

# ── Settings (only shown after login) ────────────────────────────────────────

with st.sidebar:
    st.write("")
    if st.button("登出開發者帳戶", use_container_width=True):
        st.session_state["dev_logged_in"] = False
        st.rerun()

st.subheader("更改賽會人員登入密碼")
st.caption("此密碼用於所有需要賽會人員身份的頁面登入。")

with st.form("change_admin_pw_form"):
    new_pw = st.text_input("新密碼", type="password")
    new_pw_confirm = st.text_input("確認新密碼", type="password")
    submitted = st.form_submit_button("更新密碼", type="primary")

if submitted:
    if not new_pw:
        st.warning("請輸入新密碼")
    elif new_pw != new_pw_confirm:
        st.error("兩次輸入的密碼不一致")
    else:
        hashed = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
        updated_at = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
        try:
            execute_query(
                """
                INSERT INTO system_config (key, value, updated_at)
                VALUES ('admin_password', :value, :updated_at)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """,
                {"value": hashed, "updated_at": updated_at},
            )
            st.success("賽會人員密碼已成功更新！")
        except Exception as e:
            st.error(f"更新失敗：{e}")
