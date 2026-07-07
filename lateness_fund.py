import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from functions import (
    check_committee_login,
    committee_cookie_manager,
    del_cookie,
    execute_query,
    execute_query_count,
    notify_committee_vote_event,
    query_params,
    render_committee_auth_bridge,
    render_page_guidance,
)
from schema import (
    CREATE_LATENESS_FUND_EXPENSES,
    CREATE_LATENESS_FUND_PERIODS,
    CREATE_LATENESS_FUND_RECORDS,
    TABLE_ACCOUNTS,
    TABLE_LATENESS_FUND_EXPENSES,
    TABLE_LATENESS_FUND_PERIODS,
    TABLE_LATENESS_FUND_RECORDS,
)


st.title("遲到罰款基金")

render_page_guidance(
    [
        "計算罰款公式：該成員於本年度第 N 次遲到 × 遲到分鐘。",
        "基金以年度（每年 9 月至翌年 8 月）劃分，設有Bal b/d及Bal c/d。",
    ],
    title="遲到罰款基金使用指南",
)


def _now_hk_timestamp() -> str:
    return datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")


def _today_hk() -> datetime.date:
    return datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).date()


def _format_hkd(amount) -> str:
    try:
        return f"HKD {float(amount):,.2f}"
    except (TypeError, ValueError):
        return "HKD 0.00"


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _fiscal_start_year(d: datetime.date) -> int:
    # 財政年度：每年 9 月至翌年 8 月。
    return d.year if d.month >= 9 else d.year - 1


def _fy_label(start_year) -> str:
    start_year = int(start_year)
    return f"{start_year}-{str((start_year + 1) % 100).zfill(2)}"


def _fy_range(start_year):
    start_year = int(start_year)
    return datetime.date(start_year, 9, 1), datetime.date(start_year + 1, 8, 31)


def _to_date(value):
    try:
        return pd.to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def _format_date(value) -> str:
    date_value = _to_date(value)
    return date_value.strftime("%d/%m/%Y") if date_value else ""


def ensure_lateness_fund_tables() -> bool:
    if st.session_state.get("_lateness_fund_tables_ready"):
        return True
    try:
        execute_query(CREATE_LATENESS_FUND_RECORDS)
        execute_query(f"ALTER TABLE {TABLE_LATENESS_FUND_RECORDS} ADD COLUMN IF NOT EXISTS member_user_id TEXT")
        execute_query(f"DROP INDEX IF EXISTS idx_lateness_fund_records_member_date")
        execute_query(f"ALTER TABLE {TABLE_LATENESS_FUND_RECORDS} DROP COLUMN IF EXISTS member_name")
        execute_query(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'fk_lateness_fund_record_member'
                ) THEN
                    ALTER TABLE {TABLE_LATENESS_FUND_RECORDS}
                    ADD CONSTRAINT fk_lateness_fund_record_member
                    FOREIGN KEY (member_user_id) REFERENCES {TABLE_ACCOUNTS}(user_id)
                    ON DELETE SET NULL;
                END IF;
            END $$;
            """
        )
        execute_query(CREATE_LATENESS_FUND_EXPENSES)
        execute_query(CREATE_LATENESS_FUND_PERIODS)
        execute_query(
            f"CREATE INDEX IF NOT EXISTS idx_lateness_fund_records_member_user_date "
            f"ON {TABLE_LATENESS_FUND_RECORDS}(member_user_id, late_date)"
        )
        execute_query(
            f"CREATE INDEX IF NOT EXISTS idx_lateness_fund_expenses_date "
            f"ON {TABLE_LATENESS_FUND_EXPENSES}(expense_date)"
        )
        st.session_state["_lateness_fund_tables_ready"] = True
        return True
    except Exception as e:
        st.caption(f"遲到罰款基金資料表初始化失敗：{e}")
        return False


def get_member_options() -> list[str]:
    account_df = query_params(
        f"""
        SELECT user_id
        FROM {TABLE_ACCOUNTS}
        WHERE user_id NOT IN ('admin', 'developer', '')
          AND COALESCE(account_disabled, FALSE) = FALSE
        ORDER BY user_id
        """
    )
    if account_df.empty:
        return []
    return [str(user_id).strip() for user_id in account_df["user_id"].tolist() if str(user_id).strip()]


def get_late_records():
    # ROW_NUMBER() 按（成員, 財政年度）重新起計，令罰款倍數每年重設，
    # 對應 Excel 每張年度分頁以 COUNTIF 由該年首行起計的做法。
    fiscal_expr = (
        "(CASE WHEN EXTRACT(MONTH FROM late_date) >= 9 "
        "THEN EXTRACT(YEAR FROM late_date) "
        "ELSE EXTRACT(YEAR FROM late_date) - 1 END)"
    )
    return query_params(
        f"""
        WITH ranked_records AS (
            SELECT
                id,
                late_date,
                member_user_id,
                late_minutes,
                COALESCE(paid_amount, 0) AS paid_amount,
                note,
                created_by,
                created_at,
                updated_at,
                {fiscal_expr}::int AS fiscal_start_year,
                ROW_NUMBER() OVER (
                    PARTITION BY member_user_id, {fiscal_expr}
                    ORDER BY late_date, id
                ) AS late_no
            FROM {TABLE_LATENESS_FUND_RECORDS}
        )
        SELECT
            *,
            late_no * late_minutes AS penalty_amount,
            COALESCE(paid_amount, 0) - (late_no * late_minutes) AS record_balance
        FROM ranked_records
        ORDER BY late_date DESC, id DESC
        """
    )


def get_expenses():
    return query_params(
        f"""
        SELECT id, expense_date, amount_hkd, note, created_by, created_at
        FROM {TABLE_LATENESS_FUND_EXPENSES}
        ORDER BY expense_date DESC, id DESC
        """
    )


def get_period_opening(year_label: str) -> float:
    df = query_params(
        f"SELECT opening_balance FROM {TABLE_LATENESS_FUND_PERIODS} WHERE year_label = :y",
        {"y": year_label},
    )
    if df.empty:
        return 0.0
    return _as_float(df.iloc[0]["opening_balance"])


def set_period_opening(year_label: str, amount: float) -> None:
    execute_query(
        f"""
        INSERT INTO {TABLE_LATENESS_FUND_PERIODS} (year_label, opening_balance, updated_at)
        VALUES (:y, :amt, :now)
        ON CONFLICT (year_label) DO UPDATE SET
            opening_balance = EXCLUDED.opening_balance,
            updated_at = EXCLUDED.updated_at
        """,
        {"y": year_label, "amt": float(amount), "now": _now_hk_timestamp()},
    )


def build_member_summary(year_records: pd.DataFrame) -> pd.DataFrame:
    if year_records.empty:
        return pd.DataFrame()
    grouped = (
        year_records.groupby("member_user_id")
        .agg(
            late_count=("id", "count"),
            total_late_minutes=("late_minutes", "sum"),
            penalty_amount=("penalty_amount", "sum"),
            paid_amount=("paid_amount", "sum"),
        )
        .reset_index()
    )
    grouped["balance"] = grouped["paid_amount"] - grouped["penalty_amount"]
    grouped = grouped.sort_values(["total_late_minutes", "member_user_id"], ascending=[False, True])
    grouped["late_rank"] = grouped["total_late_minutes"].rank(method="dense", ascending=False).astype(int)
    return grouped[
        ["late_rank", "member_user_id", "late_count", "total_late_minutes", "penalty_amount", "paid_amount", "balance"]
    ]


def prepare_records_display(df):
    if df.empty:
        return df
    display_df = df.copy()
    if "fiscal_start_year" in display_df.columns:
        display_df = display_df.drop(columns=["fiscal_start_year"])
    display_df["late_date"] = pd.to_datetime(display_df["late_date"]).dt.strftime("%d/%m/%Y")
    display_df["penalty_amount"] = display_df["penalty_amount"].map(_format_hkd)
    display_df["paid_amount"] = display_df["paid_amount"].map(_format_hkd)
    display_df["record_balance"] = display_df["record_balance"].map(_format_hkd)
    return display_df.rename(columns={
        "id": "ID",
        "late_date": "日期",
        "member_user_id": "帳戶",
        "late_minutes": "遲到分鐘",
        "late_no": "本年度第幾次",
        "penalty_amount": "應繳罰款",
        "paid_amount": "已繳金額",
        "record_balance": "本次結餘",
        "note": "備註",
        "created_by": "記錄人",
        "created_at": "記錄時間",
        "updated_at": "更新時間",
    })


def prepare_summary_display(df):
    if df.empty:
        return df
    display_df = df.copy()
    display_df["penalty_amount"] = display_df["penalty_amount"].map(_format_hkd)
    display_df["paid_amount"] = display_df["paid_amount"].map(_format_hkd)
    display_df["balance"] = display_df["balance"].map(_format_hkd)
    return display_df.rename(columns={
        "late_rank": "排名",
        "member_user_id": "帳戶",
        "late_count": "遲到次數",
        "total_late_minutes": "累計遲到分鐘",
        "penalty_amount": "應繳罰款",
        "paid_amount": "已繳金額",
        "balance": "結餘",
    })


def prepare_expenses_display(df):
    if df.empty:
        return df
    display_df = df.copy()
    display_df["expense_date"] = pd.to_datetime(display_df["expense_date"]).dt.strftime("%d/%m/%Y")
    display_df["amount_hkd"] = display_df["amount_hkd"].map(_format_hkd)
    return display_df.rename(columns={
        "id": "ID",
        "expense_date": "支出日期",
        "amount_hkd": "支出金額",
        "note": "備註",
        "created_by": "記錄人",
        "created_at": "記錄時間",
    })


if not check_committee_login():
    st.stop()

user_id = st.session_state["committee_user"]
if user_id == "admin":
    st.error("賽會人員帳戶不能使用此頁面。請改用內部委員會成員帳戶登入。")
    if st.button("登出賽會人員帳戶", use_container_width=True):
        st.session_state["committee_user"] = None
        del_cookie(committee_cookie_manager(), "committee_user")
        render_committee_auth_bridge(clear=True)
        st.rerun()
    st.stop()

if not ensure_lateness_fund_tables():
    st.error("遲到罰款基金資料表尚未就緒，請聯絡開發者執行資料庫初始化。")
    st.stop()

records_df = get_late_records()
expenses_df = get_expenses()

# 正規化日期，方便按年度篩選。
if not records_df.empty:
    records_df["late_date_d"] = pd.to_datetime(records_df["late_date"]).dt.date
if not expenses_df.empty:
    expenses_df["expense_date_d"] = pd.to_datetime(expenses_df["expense_date"]).dt.date

# 組合可選年度：紀錄年度 + 支出年度 + 本年度。
record_years = set(int(y) for y in records_df["fiscal_start_year"].tolist()) if not records_df.empty else set()
expense_years = (
    {_fiscal_start_year(d) for d in expenses_df["expense_date_d"].tolist() if d}
    if not expenses_df.empty
    else set()
)
current_year = _fiscal_start_year(_today_hk())
all_years = sorted(record_years | expense_years | {current_year}, reverse=True)

selected_year = st.selectbox(
    "選擇年度",
    options=all_years,
    format_func=_fy_label,
)
selected_label = _fy_label(selected_year)
year_start, year_end = _fy_range(selected_year)

# 年度範圍內的紀錄及支出。
if not records_df.empty:
    year_records = records_df[
        (records_df["late_date_d"] >= year_start) & (records_df["late_date_d"] <= year_end)
    ].copy()
else:
    year_records = records_df.copy()
if not expenses_df.empty:
    year_expenses = expenses_df[
        (expenses_df["expense_date_d"] >= year_start) & (expenses_df["expense_date_d"] <= year_end)
    ].copy()
else:
    year_expenses = expenses_df.copy()

opening_balance = get_period_opening(selected_label)
year_penalties = _as_float(year_records["penalty_amount"].sum()) if not year_records.empty else 0.0
year_received = _as_float(year_records["paid_amount"].sum()) if not year_records.empty else 0.0
year_expense_total = _as_float(year_expenses["amount_hkd"].sum()) if not year_expenses.empty else 0.0
year_outstanding = year_penalties - year_received
closing_balance = opening_balance + year_received - year_expense_total

st.caption(f"目前年度：{selected_label}（{year_start:%d/%m/%Y} 至 {year_end:%d/%m/%Y}）")
metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
metric_col1.metric("Bal b/d", _format_hkd(opening_balance))
metric_col2.metric("本年度已收罰款", _format_hkd(year_received))
metric_col3.metric("本年度支出", _format_hkd(year_expense_total))
metric_col4.metric("Bal c/d", _format_hkd(closing_balance))

metric2_col1, metric2_col2, metric2_col3 = st.columns(3)
metric2_col1.metric("本年度應收罰款", _format_hkd(year_penalties))
metric2_col2.metric("本年度未收罰款", _format_hkd(year_outstanding))
metric2_col3.metric("本年度罰款次數", int(len(year_records)))

with st.expander("年度結餘設定", expanded=False):
    st.caption("Bal b/d為本年度開始時基金持有的現金，Bal c/d ＝Bal b/d ＋ 本年度已收罰款 － 本年度支出。")
    bd_input = st.number_input(
        f"{selected_label} Bal b/d（HKD）",
        value=float(opening_balance),
        step=1.0,
        format="%.2f",
        key=f"bd_input_{selected_label}",
    )
    bd_col1, bd_col2 = st.columns(2)
    with bd_col1:
        if st.button("儲存Bal b/d", use_container_width=True):
            set_period_opening(selected_label, bd_input)
            st.success(f"已更新 {selected_label} 年度 Bal b/d。")
            st.rerun()
    with bd_col2:
        prev_label = _fy_label(selected_year - 1)
        if st.button(f"沿用上一年度（{prev_label}）結餘", use_container_width=True):
            prev_start, prev_end = _fy_range(selected_year - 1)
            if not records_df.empty:
                prev_records = records_df[
                    (records_df["late_date_d"] >= prev_start) & (records_df["late_date_d"] <= prev_end)
                ]
            else:
                prev_records = records_df
            if not expenses_df.empty:
                prev_expenses = expenses_df[
                    (expenses_df["expense_date_d"] >= prev_start) & (expenses_df["expense_date_d"] <= prev_end)
                ]
            else:
                prev_expenses = expenses_df
            prev_received = _as_float(prev_records["paid_amount"].sum()) if not prev_records.empty else 0.0
            prev_expense_total = _as_float(prev_expenses["amount_hkd"].sum()) if not prev_expenses.empty else 0.0
            prev_closing = get_period_opening(prev_label) + prev_received - prev_expense_total
            set_period_opening(selected_label, prev_closing)
            st.success(f"已將 {prev_label} 年結餘額 {_format_hkd(prev_closing)} 結轉為 {selected_label} Bal b/d。")
            st.rerun()

overview_tab, input_tab, history_tab = st.tabs(["總覽", "新增紀錄", "紀錄管理"])

with overview_tab:
    st.markdown(f"#### 成員統計（{selected_label} 年度）")
    summary_df = build_member_summary(year_records)
    if summary_df.empty:
        st.info("本年度暫無遲到紀錄。")
    else:
        st.dataframe(prepare_summary_display(summary_df), use_container_width=True, hide_index=True)

        with st.container(border=True):
            st.markdown("##### 發送遲到罰款通知")
            outstanding_df = summary_df[summary_df["balance"] < 0]
            outstanding_ids = outstanding_df["member_user_id"].tolist()
            st.caption(
                f"本年度有結欠的委員：{len(outstanding_ids)} 人"
                + (f"（{'、'.join(outstanding_ids)}）" if outstanding_ids else "")
            )
            notify_target = st.radio(
                "發送對象",
                options=["outstanding", "all"],
                format_func=lambda v: "只發送給有結欠的委員" if v == "outstanding" else "發送給所有委員",
                horizontal=True,
                key="lateness_notify_target",
            )
            custom_notify_msg = st.text_input(
                "通知內容（可留空使用預設）",
                key="lateness_notify_msg",
                placeholder="例：請盡快找數！",
            )
            if st.button("發送通知", type="primary", use_container_width=True, key="lateness_notify_send"):
                custom_text = custom_notify_msg.strip()
                if notify_target == "outstanding":
                    targets = {
                        str(row["member_user_id"]): -_as_float(row["balance"])
                        for _, row in outstanding_df.iterrows()
                    }
                else:
                    targets = {uid: None for uid in get_member_options()}

                if not targets:
                    st.info(
                        "本年度暫無有結欠的委員，毋須發送通知。"
                        if notify_target == "outstanding"
                        else "暫時未有可通知的委員帳戶。"
                    )
                else:
                    title = "💰 遲到罰款提醒"
                    if outstanding_ids:
                        all_default_body = (
                            f"{selected_label} 年度尚有結欠遲到罰款的委員："
                            f"{'、'.join(outstanding_ids)}，請盡快繳交。"
                        )
                    else:
                        all_default_body = f"{selected_label} 年度暫無委員結欠遲到罰款。"
                    sent_count = 0
                    notified_members = 0
                    for target_uid, owed in targets.items():
                        if custom_text:
                            body = custom_text
                        elif owed is not None:
                            body = f"你於 {selected_label} 年度尚欠遲到罰款 {_format_hkd(owed)}，請盡快繳交！"
                        else:
                            body = all_default_body
                        count = notify_committee_vote_event(
                            title,
                            body,
                            target_user=target_uid,
                            tag="lateness-fund-reminder",
                            url="/lateness-fund",
                        )
                        if count:
                            sent_count += count
                            notified_members += 1

                    if sent_count:
                        st.success(f"已向 {notified_members} 位委員發送通知（合共 {sent_count} 部裝置）。")
                    else:
                        st.warning("未能發送通知，對象可能尚未開啟推送通知。")

        member_ids = summary_df["member_user_id"].tolist()
        selected_member = st.selectbox("搜尋帳戶", member_ids)
        member_row = summary_df[summary_df["member_user_id"] == selected_member].iloc[0]
        member_col1, member_col2, member_col3, member_col4 = st.columns(4)
        member_col1.metric("累計遲到分鐘", int(member_row["total_late_minutes"] or 0))
        member_col2.metric("遲到次數", int(member_row["late_count"] or 0))
        member_col3.metric("應繳罰款", _format_hkd(member_row["penalty_amount"]))
        member_col4.metric("餘額", _format_hkd(member_row["balance"]))

        member_records = year_records[year_records["member_user_id"] == selected_member]
        st.dataframe(
            prepare_records_display(member_records),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown(f"#### 基金支出（{selected_label} 年度）")
    if year_expenses.empty:
        st.caption("本年度暫無基金支出。")
    else:
        st.dataframe(
            prepare_expenses_display(year_expenses.drop(columns=["expense_date_d"], errors="ignore")),
            use_container_width=True,
            hide_index=True,
        )

with input_tab:
    st.markdown("#### 新增遲到紀錄")
    member_options = get_member_options()
    late_col1, late_col2 = st.columns(2)
    with late_col1:
        late_date = st.date_input("日期", value=_today_hk(), key="lateness_late_date")
        if member_options:
            member_user_id = st.selectbox("帳戶", member_options, key="lateness_member_user_id")
        else:
            member_user_id = ""
            st.warning("暫時未有可選帳戶，請先建立內部委員會帳戶。")
    with late_col2:
        late_minutes = st.number_input("遲到分鐘", min_value=1, value=1, step=1, key="lateness_late_minutes")
        paid_amount = st.number_input("已繳金額（HKD）", min_value=0.0, value=0.0, step=1.0, format="%.2f", key="lateness_paid_amount")
        late_note = st.text_input("備註（如有）", key="lateness_note")

    member_user_id = str(member_user_id).strip()

    # 預覽：計算該成員於此紀錄所屬年度的第幾次遲到。
    entry_date = late_date if isinstance(late_date, datetime.date) else _today_hk()
    entry_start, entry_end = _fy_range(_fiscal_start_year(entry_date))
    if member_user_id and not records_df.empty:
        prior_dates = records_df[
            (records_df["member_user_id"] == member_user_id)
            & (records_df["late_date_d"] >= entry_start)
            & (records_df["late_date_d"] <= entry_end)
        ]
        previous_late_count = int(len(prior_dates))
    else:
        previous_late_count = 0
    next_late_no = previous_late_count + 1
    preview_penalty = next_late_no * int(late_minutes or 0)
    st.caption(
        f"按現有紀錄計算，今次是 {member_user_id or '該帳戶'} 於 {_fy_label(_fiscal_start_year(entry_date))} 年度第 "
        f"{next_late_no} 次遲到，應繳 {_format_hkd(preview_penalty)}。"
    )

    if st.button("新增遲到紀錄", type="primary", use_container_width=True, disabled=not member_options):
        if not member_user_id:
            st.warning("請選擇帳戶。")
        else:
            execute_query(
                f"""
                INSERT INTO {TABLE_LATENESS_FUND_RECORDS} (
                    late_date, member_user_id, late_minutes, paid_amount, note, created_by, created_at
                )
                VALUES (
                    :late_date, :member_user_id, :late_minutes, :paid_amount, :note, :created_by, :created_at
                )
                """,
                {
                    "late_date": late_date,
                    "member_user_id": member_user_id,
                    "late_minutes": int(late_minutes),
                    "paid_amount": float(paid_amount),
                    "note": late_note.strip(),
                    "created_by": user_id,
                    "created_at": _now_hk_timestamp(),
                },
            )
            st.success("已新增遲到紀錄。")
            st.rerun()

    st.divider()
    st.markdown("#### 新增基金支出")
    with st.form("lateness_expense_form"):
        expense_date = st.date_input("支出日期", value=_today_hk())
        expense_amount = st.number_input("支出金額（HKD）", min_value=0.0, value=0.0, step=1.0, format="%.2f")
        expense_note = st.text_input("支出用途 / 備註")
        submit_expense = st.form_submit_button("新增支出紀錄", type="primary")

    if submit_expense:
        if expense_amount <= 0:
            st.warning("請輸入大於 0 的支出金額。")
        else:
            execute_query(
                f"""
                INSERT INTO {TABLE_LATENESS_FUND_EXPENSES} (
                    expense_date, amount_hkd, note, created_by, created_at
                )
                VALUES (
                    :expense_date, :amount_hkd, :note, :created_by, :created_at
                )
                """,
                {
                    "expense_date": expense_date,
                    "amount_hkd": float(expense_amount),
                    "note": expense_note.strip(),
                    "created_by": user_id,
                    "created_at": _now_hk_timestamp(),
                },
            )
            st.success("已新增支出紀錄。")
            st.rerun()

with history_tab:
    st.markdown(f"#### 遲到紀錄（{selected_label} 年度）")
    if year_records.empty:
        st.info("本年度暫無遲到紀錄。")
    else:
        records_display = prepare_records_display(year_records.drop(columns=["late_date_d"], errors="ignore"))
        st.dataframe(records_display, use_container_width=True, hide_index=True)
        st.download_button(
            "下載本年度遲到紀錄 CSV",
            data=records_display.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"遲到罰款基金_遲到紀錄_{selected_label}.csv",
            mime="text/csv",
            use_container_width=True,
        )

        st.markdown("#### 更新已繳金額")
        record_options = {
            int(row["id"]): f"#{int(row['id'])}｜{row['member_user_id']}｜{_format_date(row['late_date'])}｜第 {int(row['late_no'])} 次｜應繳 {_format_hkd(row['penalty_amount'])}"
            for _, row in year_records.iterrows()
        }
        selected_record_id = st.selectbox(
            "選擇紀錄",
            list(record_options.keys()),
            format_func=lambda record_id: record_options.get(record_id, str(record_id)),
        )
        selected_record = year_records[year_records["id"] == selected_record_id].iloc[0]
        updated_paid_amount = st.number_input(
            "已繳金額（HKD）",
            min_value=0.0,
            value=float(selected_record["paid_amount"] or 0),
            step=1.0,
            format="%.2f",
            key=f"lateness_update_paid_{selected_record_id}",
        )
        if st.button("更新已繳金額", use_container_width=True):
            updated = execute_query_count(
                f"""
                UPDATE {TABLE_LATENESS_FUND_RECORDS}
                SET paid_amount = :paid_amount,
                    updated_at = :updated_at
                WHERE id = :record_id
                """,
                {
                    "record_id": int(selected_record_id),
                    "paid_amount": float(updated_paid_amount),
                    "updated_at": _now_hk_timestamp(),
                },
            )
            st.success("已更新已繳金額。" if updated else "找不到要更新的紀錄。")
            st.rerun()

    st.divider()
    st.markdown(f"#### 支出紀錄（{selected_label} 年度）")
    if year_expenses.empty:
        st.info("本年度暫無支出紀錄。")
    else:
        expenses_display = prepare_expenses_display(year_expenses.drop(columns=["expense_date_d"], errors="ignore"))
        st.dataframe(expenses_display, use_container_width=True, hide_index=True)
        st.download_button(
            "下載本年度支出紀錄 CSV",
            data=expenses_display.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"遲到罰款基金_支出紀錄_{selected_label}.csv",
            mime="text/csv",
            use_container_width=True,
        )
