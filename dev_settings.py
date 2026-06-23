import streamlit as st
import bcrypt
import datetime
from zoneinfo import ZoneInfo
from functions import get_connection, get_system_config, _verify_config_password, execute_query, hash_password, query_params, get_bypass_active_until
from schema import TABLE_ACCOUNTS, init_db

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


def _update_system_password(config_key, label):
    with st.form(f"change_{config_key}_form"):
        cur = st.text_input("目前密碼", type="password", key=f"{config_key}_cur")
        npw = st.text_input("新密碼", type="password", key=f"{config_key}_new")
        cpw = st.text_input("確認新密碼", type="password", key=f"{config_key}_confirm")
        sub = st.form_submit_button("更新密碼", type="primary")

    if sub:
        if not cur:
            st.warning("請輸入目前密碼")
        elif not npw:
            st.warning("請輸入新密碼")
        elif npw != cpw:
            st.error("兩次輸入的密碼不一致")
        else:
            stored = get_system_config(config_key)
            if stored is None:
                st.error(f"系統錯誤：未能讀取{label}密碼。")
            elif not _verify_config_password(cur, stored):
                st.error("目前密碼錯誤。")
            else:
                hashed = hash_password(npw)
                updated_at = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
                try:
                    execute_query(
                        "INSERT INTO system_config (key, value, updated_at) "
                        "VALUES (:key, :value, :updated_at) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                        {"key": config_key, "value": hashed, "updated_at": updated_at},
                    )
                    st.success(f"{label}密碼已成功更新！")
                except Exception as e:
                    st.error(f"更新失敗：{e}")


st.divider()
st.subheader("更改開發者密碼")
st.caption("此密碼用於登入本頁面。")
_update_system_password("developer_password", "開發者")

st.divider()
st.subheader("更改 SQL 存取密碼")
st.caption("此密碼用於資料庫管理控制台的二次驗證。")
_update_system_password("sql_password", "SQL 存取")

st.divider()
st.subheader("委員會帳戶管理")

with st.expander("建立新帳戶", expanded=False):
    with st.form("create_account_form"):
        new_uid = st.text_input("用戶名稱")
        new_acc_pw = st.text_input("初始密碼", type="password")
        create_btn = st.form_submit_button("建立帳戶", type="primary")

    if create_btn:
        if not new_uid.strip() or not new_acc_pw.strip():
            st.warning("請輸入用戶名稱及密碼。")
        else:
            existing = query_params(
                f"SELECT 1 FROM {TABLE_ACCOUNTS} WHERE user_id = :uid",
                {"uid": new_uid.strip()},
            )
            if not existing.empty:
                st.error("此用戶名稱已存在。")
            else:
                try:
                    execute_query(
                        f"INSERT INTO {TABLE_ACCOUNTS} (user_id, password_hash, account_status) "
                        "VALUES (:uid, :pw, 'inactive')",
                        {"uid": new_uid.strip(), "pw": hash_password(new_acc_pw.strip())},
                    )
                    st.success(f"帳戶「{new_uid.strip()}」已建立。")
                except Exception as e:
                    st.error(f"建立帳戶失敗：{e}")

with st.expander("重設帳戶密碼", expanded=False):
    accounts_df = query_params(f"SELECT user_id FROM {TABLE_ACCOUNTS} ORDER BY user_id")
    if accounts_df.empty:
        st.info("目前無帳戶。")
    else:
        with st.form("reset_account_pw_form"):
            reset_uid = st.selectbox("選擇帳戶", accounts_df["user_id"].tolist())
            reset_pw = st.text_input("新密碼", type="password")
            reset_btn = st.form_submit_button("重設密碼", type="primary")

        if reset_btn:
            if not reset_pw.strip():
                st.warning("請輸入新密碼。")
            else:
                try:
                    execute_query(
                        f"UPDATE {TABLE_ACCOUNTS} SET password_hash = :pw WHERE user_id = :uid",
                        {"pw": hash_password(reset_pw.strip()), "uid": reset_uid},
                    )
                    st.success(f"帳戶「{reset_uid}」密碼已重設。")
                except Exception as e:
                    st.error(f"重設密碼失敗：{e}")

with st.expander("刪除帳戶", expanded=False):
    accounts_df2 = query_params(f"SELECT user_id FROM {TABLE_ACCOUNTS} ORDER BY user_id")
    if accounts_df2.empty:
        st.info("目前無帳戶。")
    else:
        del_uid = st.selectbox("選擇要刪除的帳戶", accounts_df2["user_id"].tolist(), key="del_account_select")
        st.warning(f"刪除帳戶「{del_uid}」後無法復原，相關投票紀錄等會一併刪除。")
        confirmed = st.checkbox("我確認要刪除此帳戶", key="del_account_confirm")
        if st.button("刪除帳戶", type="primary", disabled=not confirmed):
            try:
                execute_query(
                    f"DELETE FROM {TABLE_ACCOUNTS} WHERE user_id = :uid",
                    {"uid": del_uid},
                )
                st.success(f"帳戶「{del_uid}」已刪除。")
                st.rerun()
            except Exception as e:
                st.error(f"刪除帳戶失敗：{e}")

st.divider()
st.subheader("資料庫結構初始化")
st.caption("執行 init_db 以確保所有資料表、視圖及索引已建立。適用於首次部署或結構更新後。")
if st.button("執行 init_db", type="primary"):
    try:
        conn = get_connection()
        init_db(conn)
        st.success("資料庫結構初始化完成。")
    except Exception as e:
        st.error(f"初始化失敗：{e}")

st.divider()
st.subheader("維護模式")
st.caption("開啟維護模式後，所有頁面將顯示維護中訊息，僅開發者設定頁面可正常登入。")
current_maint = get_system_config("maintenance_mode")
is_maint_on = str(current_maint).strip().lower() in ("true", "1", "yes", "on") if current_maint else False
st.info(f"目前維護模式狀態：**{'開啟' if is_maint_on else '關閉'}**")
if is_maint_on:
    if st.button("關閉維護模式"):
        updated_at = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
        execute_query(
            "INSERT INTO system_config (key, value, updated_at) "
            "VALUES ('maintenance_mode', 'false', :updated_at) "
            "ON CONFLICT (key) DO UPDATE SET value = 'false', updated_at = EXCLUDED.updated_at",
            {"updated_at": updated_at},
        )
        st.success("維護模式已關閉。")
        st.rerun()
else:
    if st.button("開啟維護模式"):
        updated_at = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
        execute_query(
            "INSERT INTO system_config (key, value, updated_at) "
            "VALUES ('maintenance_mode', 'true', :updated_at) "
            "ON CONFLICT (key) DO UPDATE SET value = 'true', updated_at = EXCLUDED.updated_at",
            {"updated_at": updated_at},
        )
        st.success("維護模式已開啟。所有頁面將顯示維護中訊息。")
        st.rerun()

st.divider()
st.subheader("臨時開放提案")
st.caption("開啟後，非活躍委員亦可提出新辯題及罷免動議，直至指定時間自動失效。")

bypass_until = get_bypass_active_until()
hk_now = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
bypass_on = bypass_until is not None and hk_now < bypass_until

if bypass_on:
    st.info(f"目前狀態：**開啟中**（至 {bypass_until.strftime('%Y-%m-%d %H:%M')}）")
    if st.button("立即關閉"):
        updated_at = hk_now.strftime("%Y-%m-%d %H:%M:%S")
        execute_query(
            "DELETE FROM system_config WHERE key = 'bypass_active_check_until'",
        )
        st.success("已關閉臨時開放提案。")
        st.rerun()
else:
    st.info("目前狀態：**已關閉**")
    col_date, col_time = st.columns(2)
    with col_date:
        bypass_date = st.date_input("到期日期", value=hk_now.date() + datetime.timedelta(days=7), min_value=hk_now.date())
    with col_time:
        bypass_time = st.time_input("到期時間", value=datetime.time(23, 59))
    if st.button("啟用臨時開放提案"):
        until_str = f"{bypass_date.strftime('%Y-%m-%d')} {bypass_time.strftime('%H:%M')}"
        chosen_dt = datetime.datetime.strptime(until_str, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
        if chosen_dt <= hk_now:
            st.error("到期時間必須在未來。")
        else:
            updated_at = hk_now.strftime("%Y-%m-%d %H:%M:%S")
            execute_query(
                "INSERT INTO system_config (key, value, updated_at) "
                "VALUES ('bypass_active_check_until', :value, :updated_at) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                {"value": until_str, "updated_at": updated_at},
            )
            st.success(f"已啟用臨時開放提案，至 {until_str} 届滿自動失效。")
            st.rerun()
