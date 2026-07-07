import streamlit as st
import bcrypt
import datetime
from zoneinfo import ZoneInfo
import json
from functions import get_connection, get_system_config, _verify_config_password, execute_query, hash_password, query_params, get_bypass_active_until, _parse_bypass_data, ensure_push_subscriptions_table, get_vapid_public_key, notify_committee_vote_event
from schema import TABLE_ACCOUNTS, TABLE_PUSH_SUBSCRIPTIONS, init_db, run_migrations
from ai_coach_helpers import (
    AI_MODEL_OPTIONS,
    AI_PROVIDER_LABELS,
    format_ai_model_label,
    get_ai_fund_account_options,
    get_ai_fund_settings,
    get_ai_model_settings,
    save_ai_model_settings,
    save_ai_fund_treasurers,
)

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
st.subheader("手動推送通知")
st.caption("向已在辯題投票頁啟用通知的委員發送 PWA 背景通知。")

if not get_vapid_public_key():
    st.warning("尚未設定 VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY，未能發送 Web Push 通知。")
elif not ensure_push_subscriptions_table():
    st.warning("未能確認 push_subscriptions 資料表，請先檢查資料庫連線。")
else:
    active_subs = query_params(
        f"SELECT user_id, COUNT(*) AS device_count FROM {TABLE_PUSH_SUBSCRIPTIONS} "
        "WHERE is_active = TRUE GROUP BY user_id ORDER BY user_id"
    )
    subscribed_users = active_subs["user_id"].tolist() if not active_subs.empty else []
    if active_subs.empty:
        st.info("目前未有委員啟用通知。")
    else:
        total_devices = int(active_subs["device_count"].sum())
        st.caption(f"目前可推送：{len(subscribed_users)} 位委員，{total_devices} 個裝置。")

    target_mode = st.radio(
        "推送對象",
        options=["所有已訂閱委員", "指定委員"],
        horizontal=True,
    )
    target_user = None
    if target_mode == "指定委員":
        if subscribed_users:
            target_user = st.selectbox("選擇委員", options=subscribed_users)
        else:
            st.info("目前沒有可選委員。")

    with st.form("manual_push_notification_form"):
        push_title = st.text_input("通知標題", value="聖呂中辯")
        push_body = st.text_area("通知內容", placeholder="請輸入要推送的內容。")
        push_url = st.text_input("點擊後開啟路徑", value="/vote")
        confirm_push = st.checkbox("我確認要立即發送此通知")
        send_push = st.form_submit_button("發送推送通知", type="primary")

    if send_push:
        if not push_title.strip():
            st.warning("請輸入通知標題。")
        elif not push_body.strip():
            st.warning("請輸入通知內容。")
        elif target_mode == "指定委員" and not target_user:
            st.warning("請選擇委員。")
        elif not confirm_push:
            st.warning("請先確認要立即發送。")
        else:
            tag = f"manual-push-{datetime.datetime.now(ZoneInfo('Asia/Hong_Kong')).strftime('%Y%m%d%H%M%S')}"
            sent = notify_committee_vote_event(
                push_title.strip(),
                push_body.strip(),
                target_user=target_user if target_mode == "指定委員" else None,
                tag=tag,
                url=push_url.strip() or "/vote",
            )
            if sent:
                st.success(f"已發送通知至 {sent} 個裝置。")
            else:
                st.warning("未有通知成功發送。請確認目標委員已啟用通知，並檢查 VAPID 設定。")

st.divider()
st.subheader("AI Provider / Model 設定")
st.caption("控制 AI 辯論易可選 Provider 及預設模型。API Key 仍需在 Streamlit secrets 設定，不會存入資料庫。")

ai_model_settings = get_ai_model_settings()
provider_options = ai_model_settings["provider_options"]
provider_key_names = {}
for model_config in AI_MODEL_OPTIONS.values():
    provider_key_names.setdefault(model_config.get("provider", ""), model_config.get("api_key", ""))

if not provider_options:
    st.info("目前未有可設定的 AI Provider。")
else:
    selected_ai_providers = st.multiselect(
        "啟用 Provider",
        options=provider_options,
        default=ai_model_settings["enabled_providers"],
        format_func=lambda x: AI_PROVIDER_LABELS.get(x, x),
        key="ai_enabled_provider_select",
    )
    provider_model_options = [
        model_label
        for model_label, model_config in AI_MODEL_OPTIONS.items()
        if model_config.get("provider") in selected_ai_providers
    ]
    if not provider_model_options:
        st.warning("請至少啟用一個有模型的 Provider。")
        selected_default_model = ""
    else:
        current_default_model = ai_model_settings["default_model"]
        default_index = (
            provider_model_options.index(current_default_model)
            if current_default_model in provider_model_options
            else 0
        )
        selected_default_model = st.selectbox(
            "預設模型",
            options=provider_model_options,
            index=default_index,
            format_func=format_ai_model_label,
            key="ai_default_model_select",
        )

    st.write("**API Key 狀態**")
    for provider in provider_options:
        key_name = provider_key_names.get(provider, "")
        key_status = "已設定" if key_name and key_name in st.secrets else "未設定"
        st.caption(
            f"{AI_PROVIDER_LABELS.get(provider, provider)}："
            f"{key_name or '未指定 key'}（{key_status}）"
        )

    if st.button("更新 AI Provider 設定", type="primary"):
        try:
            save_ai_model_settings(selected_ai_providers, selected_default_model)
            st.success("AI Provider 設定已更新。")
            st.rerun()
        except Exception as e:
            st.error(f"更新失敗：{e}")

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
st.subheader("AI基金管理員設定")
st.caption("AI基金管理員可在 AI 辯論易的「💲AI基金」分頁確認入數、記錄 provider 支出及更新AI基金設定。")

with st.expander("指定AI基金管理員", expanded=False):
    account_options = get_ai_fund_account_options()
    current_treasurers = [
        user_id for user_id in get_ai_fund_settings()["treasurers"]
        if user_id in account_options
    ]
    if not account_options:
        st.info("目前未有可選委員帳戶。")
    else:
        selected_treasurers = st.multiselect(
            "選擇AI基金管理員帳戶",
            options=account_options,
            default=current_treasurers,
        )
        if st.button("更新AI基金管理員名單", type="primary"):
            save_ai_fund_treasurers(selected_treasurers)
            st.success("AI基金管理員名單已更新。")
            st.rerun()

st.divider()
st.subheader("資料庫結構初始化")
st.caption("執行 init_db 以確保所有資料表、視圖及索引已建立，並套用結構升級（migrations）。適用於首次部署或結構更新後。")
if st.button("執行 init_db", type="primary"):
    try:
        conn = get_connection()
        init_db(conn)
        migration_log = run_migrations(conn)
        st.success("資料庫結構初始化完成。")
        if migration_log:
            with st.expander("結構升級（migrations）結果", expanded=True):
                for line in migration_log:
                    (st.error if line.startswith("ERR") else st.caption)(line)
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
st.caption("為指定的非活躍委員臨時開放提案權限，直至指定時間自動失效。")

hk_now = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
bypass_data = _parse_bypass_data()

active_bypasses = {}
for uid, ts in bypass_data.items():
    try:
        dt = datetime.datetime.strptime(str(ts).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
        if dt > hk_now:
            active_bypasses[uid] = dt
    except (ValueError, TypeError):
        continue

if active_bypasses:
    st.write("**目前已開放的委員：**")
    for uid, dt in active_bypasses.items():
        col_info, col_btn = st.columns([3, 1])
        with col_info:
            st.write(f"• **{uid}**（至 {dt.strftime('%Y-%m-%d %H:%M')}）")
        with col_btn:
            if st.button("撤銷", key=f"revoke_{uid}"):
                bypass_data.pop(uid, None)
                updated_at = hk_now.strftime("%Y-%m-%d %H:%M:%S")
                execute_query(
                    "INSERT INTO system_config (key, value, updated_at) "
                    "VALUES ('bypass_active_check_until', :value, :updated_at) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                    {"value": json.dumps(bypass_data, ensure_ascii=False), "updated_at": updated_at},
                )
                st.success(f"已撤銷「{uid}」的臨時提案權限。")
                st.rerun()
    if st.button("全部撤銷"):
        execute_query("DELETE FROM system_config WHERE key = 'bypass_active_check_until'")
        st.success("已撤銷所有臨時提案權限。")
        st.rerun()
else:
    st.info("目前沒有委員獲臨時提案權限。")

st.write("**新增臨時提案權限：**")
all_accounts = query_params(
    f"SELECT user_id FROM {TABLE_ACCOUNTS} WHERE account_status = 'inactive' ORDER BY user_id"
)
if all_accounts.empty:
    st.info("目前無非活躍委員帳戶。")
else:
    bypass_users = st.multiselect("選擇委員", options=all_accounts["user_id"].tolist(), key="bypass_user_select")
    col_date, col_time = st.columns(2)
    with col_date:
        bypass_date = st.date_input("到期日期", value=hk_now.date() + datetime.timedelta(days=7), min_value=hk_now.date())
    with col_time:
        bypass_time = st.time_input("到期時間", value=datetime.time(23, 59))
    if st.button("啟用臨時提案權限"):
        if not bypass_users:
            st.warning("請選擇至少一位委員。")
        else:
            until_str = f"{bypass_date.strftime('%Y-%m-%d')} {bypass_time.strftime('%H:%M')}"
            chosen_dt = datetime.datetime.strptime(until_str, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
            if chosen_dt <= hk_now:
                st.error("到期時間必須在未來。")
            else:
                for uid in bypass_users:
                    bypass_data[uid] = until_str
                updated_at = hk_now.strftime("%Y-%m-%d %H:%M:%S")
                execute_query(
                    "INSERT INTO system_config (key, value, updated_at) "
                    "VALUES ('bypass_active_check_until', :value, :updated_at) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                    {"value": json.dumps(bypass_data, ensure_ascii=False), "updated_at": updated_at},
                )
                st.success(f"已為 {', '.join(bypass_users)} 啟用臨時提案權限，至 {until_str} 届滿自動失效。")
                st.rerun()
