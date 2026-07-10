import streamlit as st

from auth import require_committee
from functions import render_page_guidance
from ai_coach_helpers import (
    ensure_ai_fund_tables,
    get_ai_fund_settings,
    is_ai_fund_treasurer,
    get_ai_fund_summary,
    create_ai_fund_transaction,
    update_ai_fund_transaction_status,
    get_ai_fund_transactions,
    get_ai_fund_usage_logs,
    get_ai_fund_usage_summary,
    save_ai_fund_public_settings,
    reset_ai_fund_usage_logs,
    get_google_ai_studio_balance,
    save_google_ai_studio_balance,
    get_openrouter_credit_balance,
    format_usd_money,
    format_hkd_money,
    HKD_PER_USD,
    AI_FUND_TRANSACTION_LABELS,
    AI_PROVIDER_LABELS,
    AI_FEATURE_LABELS,
)


def _format_hkd(amount) -> str:
    return format_hkd_money(amount)


def _format_hkd_4dp(amount) -> str:
    return format_hkd_money(amount, decimals=4)


def _prepare_transaction_display(df):
    if df.empty:
        return df
    display_df = df.copy()
    display_df["transaction_type"] = display_df["transaction_type"].map(
        lambda x: AI_FUND_TRANSACTION_LABELS.get(x, x)
    )
    if "provider" in display_df.columns:
        display_df["provider"] = display_df["provider"].map(
            lambda x: AI_PROVIDER_LABELS.get(x, x)
        )
    display_df["status"] = display_df["status"].map(
        {"pending": "待確認", "confirmed": "已確認", "rejected": "已拒絕"}
    ).fillna(display_df["status"])
    return display_df.rename(columns={
        "id": "編號",
        "transaction_type": "類型",
        "status": "狀態",
        "provider": "Provider",
        "amount_hkd": "金額(HKD)",
        "payment_method": "付款方式",
        "reference_no": "Reference",
        "note": "備註",
        "created_by": "提交者",
        "created_at": "提交時間",
        "confirmed_by": "確認者",
        "confirmed_at": "確認時間",
        "rejected_by": "拒絕者",
        "rejected_at": "拒絕時間",
        "status_note": "狀態備註",
    })


def _prepare_usage_display(df):
    if df.empty:
        return df
    display_df = df.copy()
    display_df["feature"] = display_df["feature"].map(
        lambda x: AI_FEATURE_LABELS.get(x, x)
    )
    display_df["provider"] = display_df["provider"].map(
        lambda x: AI_PROVIDER_LABELS.get(x, x)
    )
    display_df["status"] = display_df["status"].map(
        {"success": "成功", "failed": "失敗"}
    ).fillna(display_df["status"])
    return display_df.rename(columns={
        "id": "編號",
        "user_id": "用戶",
        "feature": "功能",
        "model_label": "模型",
        "provider": "Provider",
        "estimated_cost_usd": "估算成本(USD)",
        "estimated_cost_hkd": "估算成本(HKD)",
        "input_tokens": "Input tokens",
        "output_tokens": "Output tokens",
        "audio_tokens": "Audio tokens",
        "search_calls": "搜尋次數",
        "cost_source": "成本來源",
        "status": "狀態",
        "error_message": "錯誤訊息",
        "created_at": "時間",
    })


st.title("💲AI基金")
st.caption("正式現金帳以AI基金管理員確認的入數及 AI provider 付款紀錄為準；AI 用量成本只作估算。")

render_page_guidance(
    [
        "在「入數 / 交易」提交入數紀錄，待AI基金管理員確認後會計入AI基金。",
        "「AI 用量」顯示 AI 辯論易各功能的估算成本，只作參考。",
        "AI基金管理員可確認入數、記錄 provider 支出及更新基金設定。",
    ],
    title="AI基金使用指南",
)

user_id = require_committee()

if not ensure_ai_fund_tables():
    st.error("AI基金資料表尚未就緒，請聯絡開發者執行資料庫初始化。")
    st.stop()

fund_settings = get_ai_fund_settings()
is_treasurer = is_ai_fund_treasurer(user_id)
fund_summary = get_ai_fund_summary()

if not fund_settings["treasurers"]:
    st.warning("尚未設定AI基金管理員。請先到開發者設定指定AI基金管理員。")
elif is_treasurer:
    st.success("你是 AI基金管理員，可確認入數、記錄支出及更新AI基金設定。")

fund_overview_tab, fund_deposit_tab, fund_usage_tab = st.tabs(
    ["總覽", "入數 / 交易", "AI 用量"]
)

with fund_overview_tab:
    col1, col2, col3 = st.columns(3)
    col1.metric("已確認現金餘額", _format_hkd(fund_summary["balance_hkd"]))
    col2.metric("待確認入數", _format_hkd(fund_summary["pending_deposits_hkd"]))
    col3.metric("最近 30 日 AI 用量（估算）", _format_hkd_4dp(fund_summary["recent_usage_hkd"]))

    if fund_summary["balance_hkd"] < fund_summary["low_balance_hkd"]:
        st.warning(
            f"餘額低於警戒線 {_format_hkd(fund_summary['low_balance_hkd'])}，建議補充資金。"
        )

    st.caption(
        f"目標金額：{_format_hkd(fund_summary['target_hkd'])}｜"
        f"建議補充：{_format_hkd(fund_summary['suggested_total_hkd'])}｜"
        f"按 {fund_summary['member_count']} 人計每人約 "
        f"{_format_hkd(fund_summary['suggested_per_member_hkd'])}"
    )

    with st.expander("Provider 餘額", expanded=True):
        google_ai_studio_balance = get_google_ai_studio_balance()
        provider_col1, provider_col2 = st.columns(2)
        with provider_col1:
            openrouter_balance = get_openrouter_credit_balance()
            if openrouter_balance.get("ok"):
                st.metric(
                    "OpenRouter 剩餘 credits",
                    format_usd_money(openrouter_balance["remaining_credits_usd"]),
                    help="由 OpenRouter credits API 即時讀取。",
                )
                st.caption(
                    f"Purchased：{format_usd_money(openrouter_balance['total_credits_usd'], escape_markdown=True)}｜"
                    f"Used：{format_usd_money(openrouter_balance['total_usage_usd'], escape_markdown=True)}｜"
                    f"約 {format_hkd_money(openrouter_balance['remaining_credits_usd'] * HKD_PER_USD)}"
                )
            else:
                st.metric("OpenRouter 剩餘 credits", "未能讀取")
                st.caption(openrouter_balance.get("message", "OpenRouter credits 讀取失敗。"))
        with provider_col2:
            if google_ai_studio_balance["balance_usd"] is None:
                st.metric("Google AI Studio 手動餘額", "未設定")
                st.caption("由 AI基金管理員從 AI Studio Billing 手動更新。")
            else:
                st.metric(
                    "Google AI Studio 手動餘額",
                    f"US${google_ai_studio_balance['balance_usd']:,.2f}",
                    help="手動輸入值，並非 Google API 即時回傳。",
                )
                st.caption(
                    f"約 {_format_hkd(google_ai_studio_balance['balance_hkd'])}｜"
                    f"更新：{google_ai_studio_balance['updated_at'] or '—'}｜"
                    f"{google_ai_studio_balance['updated_by'] or '—'}"
                )
            if is_treasurer:
                with st.form("google_ai_studio_balance_form"):
                    balance_default = google_ai_studio_balance["balance_usd"]
                    google_balance_usd = st.number_input(
                        "更新 Google AI Studio 餘額（USD）",
                        min_value=0.0,
                        value=float(balance_default or 0.0),
                        step=1.0,
                        format="%.4f",
                    )
                    submit_google_balance = st.form_submit_button("更新餘額")
                if submit_google_balance:
                    try:
                        save_google_ai_studio_balance(google_balance_usd, user_id)
                        st.success("Google AI Studio 餘額已更新。")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Google AI Studio 餘額更新失敗：{e}")

    with st.expander("付款指示"):
        st.text(fund_settings["payment_instruction"])

    if is_treasurer:
        with st.expander("AI基金管理員設定"):
            with st.form("ai_fund_settings_form"):
                target_hkd = st.number_input(
                    "目標金額（HKD）",
                    min_value=0.0,
                    value=float(fund_settings["target_hkd"]),
                    step=10.0,
                    format="%.2f",
                )
                low_balance_hkd = st.number_input(
                    "低餘額警戒線（HKD）",
                    min_value=0.0,
                    value=float(fund_settings["low_balance_hkd"]),
                    step=5.0,
                    format="%.2f",
                )
                payment_instruction = st.text_area(
                    "付款指示",
                    value=fund_settings["payment_instruction"],
                    height=120,
                )
                save_settings = st.form_submit_button("更新設定", type="primary")

            if save_settings:
                save_ai_fund_public_settings(
                    target_hkd=target_hkd,
                    low_balance_hkd=low_balance_hkd,
                    payment_instruction=payment_instruction,
                )
                st.success("AI基金設定已更新。")
                st.rerun()

            st.divider()
            st.markdown("##### 重置 AI 用量紀錄")
            st.caption("刪除所有 AI 用量估算紀錄。此操作不可復原，不影響現金帳交易。")
            reset_confirm = st.checkbox("我確認要重置所有 AI 用量紀錄", key="reset_usage_confirm")
            if st.button("重置用量紀錄", disabled=not reset_confirm, key="reset_usage_btn"):
                try:
                    deleted = reset_ai_fund_usage_logs()
                    st.success(f"已刪除 {deleted} 筆用量紀錄。")
                    st.rerun()
                except Exception as e:
                    st.error(f"重置失敗：{e}")

with fund_deposit_tab:
    st.markdown("#### 成員入數")
    with st.form("ai_fund_deposit_form"):
        deposit_amount = st.number_input(
            "入數金額（HKD）",
            min_value=0.0,
            step=10.0,
            format="%.2f",
            key="ai_fund_deposit_amount",
        )
        payment_method = st.selectbox(
            "付款方式",
            ["FPS", "現金", "Alipay", "PayMe", "其他"],
            key="ai_fund_deposit_method",
        )
        reference_no = st.text_input("Reference / 交易編號（如有）", key="ai_fund_deposit_ref")
        deposit_note = st.text_area("備註（如有）", height=80, key="ai_fund_deposit_note")
        submit_deposit = st.form_submit_button("提交入數紀錄", type="primary")

    if submit_deposit:
        if deposit_amount <= 0:
            st.warning("請輸入大於 0 的入數金額。")
        else:
            create_ai_fund_transaction(
                user_id=user_id,
                transaction_type="member_deposit",
                amount_hkd=deposit_amount,
                provider="general",
                payment_method=payment_method,
                reference_no=reference_no,
                note=deposit_note,
                status="pending",
            )
            st.success("入數紀錄已提交，待AI基金管理員確認後會計入AI基金。")
            st.rerun()

    if is_treasurer:
        st.divider()
        st.markdown("#### AI基金管理員操作")
        tx_df_for_pending = get_ai_fund_transactions(user_id=user_id, treasurer=True, limit=200)
        pending_df = tx_df_for_pending[
            (tx_df_for_pending["status"] == "pending")
            & (tx_df_for_pending["transaction_type"] == "member_deposit")
        ] if not tx_df_for_pending.empty else tx_df_for_pending

        with st.expander(f"待確認入數（{0 if pending_df.empty else len(pending_df)}）", expanded=True):
            if pending_df.empty:
                st.caption("而家沒有待確認入數。")
            else:
                for _, row in pending_df.iterrows():
                    tx_id = int(row["id"])
                    with st.container(border=True):
                        st.markdown(
                            f"**#{tx_id}**｜{row['created_by']}｜"
                            f"{_format_hkd(row['amount_hkd'])}｜{row.get('payment_method') or '—'}"
                        )
                        st.caption(
                            f"Reference：{row.get('reference_no') or '—'}｜"
                            f"提交時間：{row.get('created_at')}"
                        )
                        if row.get("note"):
                            st.caption(f"備註：{row['note']}")
                        status_note = st.text_input(
                            "確認 / 拒絕備註（可選）",
                            key=f"ai_fund_status_note_{tx_id}",
                        )
                        btn_col1, btn_col2 = st.columns(2)
                        with btn_col1:
                            if st.button("確認入數", key=f"confirm_ai_fund_{tx_id}", width="stretch"):
                                updated = update_ai_fund_transaction_status(
                                    tx_id,
                                    "confirmed",
                                    user_id,
                                    status_note=status_note,
                                )
                                st.success("已確認入數。" if updated else "此入數已被處理。")
                                st.rerun()
                        with btn_col2:
                            if st.button("拒絕入數", key=f"reject_ai_fund_{tx_id}", width="stretch"):
                                updated = update_ai_fund_transaction_status(
                                    tx_id,
                                    "rejected",
                                    user_id,
                                    status_note=status_note,
                                )
                                st.warning("已拒絕入數。" if updated else "此入數已被處理。")
                                st.rerun()

        with st.expander("記錄 provider 支出 / 退款 / 調整", expanded=False):
            with st.form("ai_fund_treasurer_tx_form"):
                treasurer_tx_type = st.selectbox(
                    "交易類型",
                    ["provider_topup", "refund", "adjustment"],
                    format_func=lambda x: AI_FUND_TRANSACTION_LABELS.get(x, x),
                )
                treasurer_provider = st.selectbox(
                    "Provider / 分類",
                    ["gemini", "openrouter", "other"],
                    format_func=lambda x: AI_PROVIDER_LABELS.get(x, x),
                )
                treasurer_amount = st.number_input(
                    "金額（HKD）",
                    value=0.0,
                    step=10.0,
                    format="%.2f",
                    help="充值 / 支出及退款請輸入正數；手動調整可輸入正數或負數。",
                )
                treasurer_method = st.text_input("付款方式 / Provider", placeholder="例如：OpenRouter、FPS")
                treasurer_ref = st.text_input("Reference / 帳單編號（如有）")
                treasurer_note = st.text_area("原因 / 備註", height=100)
                submit_treasurer_tx = st.form_submit_button("新增已確認交易", type="primary")

            if submit_treasurer_tx:
                if treasurer_tx_type != "adjustment" and treasurer_amount <= 0:
                    st.warning("充值 / 支出及退款金額必須大於 0。")
                elif treasurer_tx_type == "adjustment" and treasurer_amount == 0:
                    st.warning("手動調整金額不能為 0。")
                elif not treasurer_note.strip():
                    st.warning("請填寫原因 / 備註。")
                else:
                    create_ai_fund_transaction(
                        user_id=user_id,
                        transaction_type=treasurer_tx_type,
                        amount_hkd=treasurer_amount,
                        provider=treasurer_provider,
                        payment_method=treasurer_method,
                        reference_no=treasurer_ref,
                        note=treasurer_note,
                        status="confirmed",
                    )
                    st.success("已新增交易紀錄。")
                    st.rerun()

    st.divider()
    st.markdown("#### 交易紀錄" if is_treasurer else "#### 我的入數紀錄")
    tx_df = get_ai_fund_transactions(user_id=user_id, treasurer=is_treasurer, limit=80)
    if tx_df.empty:
        st.info("暫無交易紀錄。")
    else:
        tx_display = _prepare_transaction_display(tx_df)
        st.dataframe(tx_display, width="stretch", hide_index=True)
        st.download_button(
            "下載交易紀錄 CSV",
            data=tx_display.to_csv(index=False).encode("utf-8-sig"),
            file_name="ai基金交易紀錄.csv",
            mime="text/csv",
            width="stretch",
        )

with fund_usage_tab:
    st.markdown("#### AI 用量估算")
    usage_df = get_ai_fund_usage_logs(user_id=user_id, treasurer=is_treasurer, limit=50)
    if usage_df.empty:
        st.info("暫無 AI 用量紀錄。")
    else:
        usage_display = _prepare_usage_display(usage_df)
        st.dataframe(usage_display, width="stretch", hide_index=True)
        st.download_button(
            "下載最近用量 CSV",
            data=usage_display.to_csv(index=False).encode("utf-8-sig"),
            file_name="ai用量估算紀錄.csv",
            mime="text/csv",
            width="stretch",
        )

    usage_summary_df = get_ai_fund_usage_summary()
    if not usage_summary_df.empty:
        if not is_treasurer:
            usage_summary_df = usage_summary_df[usage_summary_df["user_id"] == user_id]
        if not usage_summary_df.empty:
            usage_summary_display = usage_summary_df.copy()
            usage_summary_display["provider"] = usage_summary_display["provider"].map(
                lambda x: AI_PROVIDER_LABELS.get(x, x)
            )
            usage_summary_display["feature"] = usage_summary_display["feature"].map(
                lambda x: AI_FEATURE_LABELS.get(x, x)
            )
            usage_summary_display = usage_summary_display.rename(columns={
                "month": "月份",
                "user_id": "用戶",
                "provider": "Provider",
                "feature": "功能",
                "model_label": "模型",
                "uses": "使用次數",
                "estimated_cost_hkd": "估算成本(HKD)",
            })
            st.markdown("#### 按月 / 用戶 / 功能統計")
            st.dataframe(usage_summary_display, width="stretch", hide_index=True)
            st.download_button(
                "下載用量統計 CSV",
                data=usage_summary_display.to_csv(index=False).encode("utf-8-sig"),
                file_name="ai用量統計.csv",
                mime="text/csv",
                width="stretch",
            )
