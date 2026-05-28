import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from functions import (
    check_admin,
    ensure_registration_tables,
    execute_query,
    get_registration_settings,
    query_params,
    render_page_guidance,
)
from schema import TABLE_COMPETITION_REGISTRATION_SETTINGS, TABLE_COMPETITION_REGISTRATIONS


STATUS_LABELS = {
    "submitted": "已提交",
    "contacted": "已聯絡",
    "confirmed": "已確認",
    "withdrawn": "已退出",
}


def _format_dt(value):
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


st.header("比賽報名管理")
render_page_guidance(
    [
        "先設定第幾屆比賽及報名開始、截止時間，公開報名入口只會在開放時間顯示。",
        "可按屆數及狀態查看報名資料，並將報名狀態標記為已聯絡、已確認或已退出。",
        "如需保存名單，可使用 CSV 匯出；此頁不會直接修改隊伍及聯絡人資料。",
    ],
)

if not check_admin():
    st.stop()

if not ensure_registration_tables():
    st.error("未能建立或讀取報名資料表，請稍後再試或聯絡開發人員。")
    st.stop()

settings = get_registration_settings()
now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)

with st.container(border=True):
    st.subheader("報名時間設定")
    default_edition = settings["competition_edition"] if settings else 1
    default_start = settings["registration_start"] if settings else now_hk
    default_end = settings["registration_end"] if settings else now_hk + datetime.timedelta(days=14)

    with st.form("registration_settings_form"):
        competition_edition = st.number_input("比賽屆數", min_value=1, step=1, value=int(default_edition))
        start_col1, start_col2, end_col1, end_col2 = st.columns(4)
        with start_col1:
            start_date = st.date_input("報名開始日期", value=default_start.date())
        with start_col2:
            start_time = st.time_input("報名開始時間", value=default_start.time().replace(second=0, microsecond=0))
        with end_col1:
            end_date = st.date_input("報名截止日期", value=default_end.date())
        with end_col2:
            end_time = st.time_input("報名截止時間", value=default_end.time().replace(second=0, microsecond=0))

        save_settings = st.form_submit_button("儲存報名設定", type="primary", use_container_width=True)

    if save_settings:
        registration_start = datetime.datetime.combine(start_date, start_time)
        registration_end = datetime.datetime.combine(end_date, end_time)
        if registration_end <= registration_start:
            st.error("報名截止時間必須遲於開始時間。")
        else:
            execute_query(
                f"""
                INSERT INTO {TABLE_COMPETITION_REGISTRATION_SETTINGS} (
                    id,
                    competition_edition,
                    registration_start,
                    registration_end,
                    updated_at
                )
                VALUES (1, :competition_edition, :registration_start, :registration_end, :updated_at)
                ON CONFLICT (id) DO UPDATE SET
                    competition_edition = EXCLUDED.competition_edition,
                    registration_start = EXCLUDED.registration_start,
                    registration_end = EXCLUDED.registration_end,
                    updated_at = EXCLUDED.updated_at
                """,
                {
                    "competition_edition": int(competition_edition),
                    "registration_start": registration_start,
                    "registration_end": registration_end,
                    "updated_at": now_hk,
                },
            )
            st.success("報名設定已更新。")
            st.rerun()

if settings:
    st.info(
        f"目前設定：第 **{settings['competition_edition']}** 屆，"
        f"{_format_dt(settings['registration_start'])} 至 {_format_dt(settings['registration_end'])}（香港時間／HKT）"
    )
else:
    st.warning("尚未設定報名時間。公開報名入口暫不會顯示。")

st.divider()
st.subheader("報名資料")

edition_rows = query_params(
    f"""
    SELECT DISTINCT competition_edition
    FROM {TABLE_COMPETITION_REGISTRATIONS}
    ORDER BY competition_edition DESC
    """
)
edition_options = edition_rows["competition_edition"].astype(int).tolist() if not edition_rows.empty else []
if settings and settings["competition_edition"] not in edition_options:
    edition_options.insert(0, settings["competition_edition"])
if not edition_options:
    edition_options = [1]

filter_col1, filter_col2 = st.columns(2)
with filter_col1:
    selected_edition = st.selectbox("屆數", options=edition_options)
with filter_col2:
    selected_status = st.selectbox("狀態", options=["全部"] + list(STATUS_LABELS.keys()), format_func=lambda x: STATUS_LABELS.get(x, x))

where_sql = "WHERE competition_edition = :competition_edition"
params = {"competition_edition": int(selected_edition)}
if selected_status != "全部":
    where_sql += " AND status = :status"
    params["status"] = selected_status

registrations_df = query_params(
    f"""
    SELECT
        id,
        competition_edition,
        team_name,
        main_debater_name,
        first_deputy_name,
        second_deputy_name,
        closing_debater_name,
        contact_name,
        contact_class,
        contact_phone,
        status,
        submitted_at,
        updated_at
    FROM {TABLE_COMPETITION_REGISTRATIONS}
    {where_sql}
    ORDER BY submitted_at DESC, id DESC
    """,
    params,
)

if registrations_df.empty:
    st.info("目前未有符合條件的報名紀錄。")
    st.stop()

status_counts = registrations_df["status"].value_counts().to_dict()
metric_cols = st.columns(4)
for i, status_key in enumerate(STATUS_LABELS.keys()):
    metric_cols[i].metric(STATUS_LABELS[status_key], status_counts.get(status_key, 0))

display_df = registrations_df.copy()
display_df["status"] = display_df["status"].map(STATUS_LABELS).fillna(display_df["status"])
display_df = display_df.rename(columns={
    "id": "編號",
    "competition_edition": "屆數",
    "team_name": "隊名",
    "main_debater_name": "主辯",
    "first_deputy_name": "一副",
    "second_deputy_name": "二副",
    "closing_debater_name": "結辯",
    "contact_name": "聯絡人",
    "contact_class": "班別",
    "contact_phone": "聯絡電話",
    "status": "狀態",
    "submitted_at": "提交時間",
    "updated_at": "更新時間",
})
st.dataframe(display_df, use_container_width=True, hide_index=True)

csv_bytes = display_df.to_csv(index=False).encode("utf-8-sig")
st.download_button(
    "匯出 CSV",
    data=csv_bytes,
    file_name=f"competition_registrations_{selected_edition}.csv",
    mime="text/csv",
    use_container_width=True,
)

st.divider()
st.subheader("更新報名狀態")

id_options = registrations_df["id"].astype(int).tolist()
selected_id = st.selectbox(
    "選擇報名紀錄",
    options=id_options,
    format_func=lambda row_id: (
        f"{row_id} - "
        f"{registrations_df[registrations_df['id'] == row_id].iloc[0]['team_name']}"
    ),
)
selected_row = registrations_df[registrations_df["id"] == selected_id].iloc[0]
current_status = str(selected_row["status"])
new_status = st.selectbox(
    "新狀態",
    options=list(STATUS_LABELS.keys()),
    index=list(STATUS_LABELS.keys()).index(current_status) if current_status in STATUS_LABELS else 0,
    format_func=lambda x: STATUS_LABELS.get(x, x),
)

if st.button("更新狀態", type="primary", use_container_width=True):
    execute_query(
        f"""
        UPDATE {TABLE_COMPETITION_REGISTRATIONS}
        SET status = :status, updated_at = :updated_at
        WHERE id = :id
        """,
        {"status": new_status, "updated_at": now_hk, "id": int(selected_id)},
    )
    st.success("報名狀態已更新。")
    st.rerun()
