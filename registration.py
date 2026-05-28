import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from functions import execute_query, get_registration_status, query_params, render_page_guidance
from schema import TABLE_COMPETITION_REGISTRATIONS


st.header("比賽報名")
render_page_guidance(
    [
        "請於報名時間內填寫隊伍資料及聯絡人資料。",
        "每隊只需提交一次報名，同一屆不可使用相同隊名重覆報名。",
        "提交後如需更改資料，請直接聯絡賽會人員。",
    ],
)

registration_status = get_registration_status()
settings = registration_status["settings"]

if not registration_status["is_open"]:
    st.warning(registration_status["message"])
    if settings and settings.get("registration_start") and settings.get("registration_end"):
        st.write(f"第 **{settings['competition_edition']}** 屆比賽報名時間：")
        st.write(f"開始：{settings['registration_start'].strftime('%Y-%m-%d %H:%M')}")
        st.write(f"截止：{settings['registration_end'].strftime('%Y-%m-%d %H:%M')}")
    st.stop()

edition = settings["competition_edition"]
registration_start = settings["registration_start"]
registration_end = settings["registration_end"]

with st.container(border=True):
    st.subheader(f"第 {edition} 屆比賽報名")
    st.write("報名流程：填寫隊伍資料 → 確認聯絡方法 → 提交報名")
    st.write("請至側邊欄查閱賽規。如有疑問，請WhatsApp 52698715。")
    st.caption(
        f"報名時間（香港時間／HKT）："
        f"{registration_start.strftime('%Y-%m-%d %H:%M')} 至 {registration_end.strftime('%Y-%m-%d %H:%M')}"
    )

if st.session_state.get("registration_submitted"):
    submitted_team = st.session_state["registration_submitted"]
    st.success(f"已收到「{submitted_team}」的報名。賽會人員稍後會按聯絡資料跟進。")
    if st.button("提交另一隊報名", use_container_width=True):
        st.session_state["registration_submitted"] = None
        st.rerun()
    st.stop()

with st.form("competition_registration_form"):
    st.subheader("隊伍資訊")
    team_name = st.text_input("隊名")

    debater_col1, debater_col2 = st.columns(2)
    with debater_col1:
        main_debater_name = st.text_input("主辯姓名")
        first_deputy_name = st.text_input("一副姓名")
    with debater_col2:
        second_deputy_name = st.text_input("二副姓名")
        closing_debater_name = st.text_input("結辯姓名")

    st.subheader("聯絡人資料")
    contact_col1, contact_col2, contact_col3 = st.columns(3)
    with contact_col1:
        contact_name = st.text_input("聯絡人姓名")
    with contact_col2:
        contact_class = st.text_input("聯絡人班別")
    with contact_col3:
        contact_phone = st.text_input("聯絡電話號碼")

    submitted = st.form_submit_button("提交報名", type="primary", use_container_width=True)

if submitted:
    form_data = {
        "team_name": team_name.strip(),
        "main_debater_name": main_debater_name.strip(),
        "first_deputy_name": first_deputy_name.strip(),
        "second_deputy_name": second_deputy_name.strip(),
        "closing_debater_name": closing_debater_name.strip(),
        "contact_name": contact_name.strip(),
        "contact_class": contact_class.strip(),
        "contact_phone": contact_phone.strip(),
    }
    missing_fields = [label for label, value in {
        "隊名": form_data["team_name"],
        "主辯姓名": form_data["main_debater_name"],
        "一副姓名": form_data["first_deputy_name"],
        "二副姓名": form_data["second_deputy_name"],
        "結辯姓名": form_data["closing_debater_name"],
        "聯絡人姓名": form_data["contact_name"],
        "聯絡人班別": form_data["contact_class"],
        "聯絡電話號碼": form_data["contact_phone"],
    }.items() if not value]

    if missing_fields:
        st.error("請填寫所有必填資料：" + "、".join(missing_fields))
        st.stop()

    latest_status = get_registration_status()
    if not latest_status["is_open"]:
        st.error("報名時間已關閉，未能提交報名。")
        st.stop()

    duplicate = query_params(
        f"""
        SELECT 1
        FROM {TABLE_COMPETITION_REGISTRATIONS}
        WHERE competition_edition = :competition_edition
          AND team_name = :team_name
        """,
        {"competition_edition": edition, "team_name": form_data["team_name"]},
    )
    if not duplicate.empty:
        st.error("此隊名已於本屆提交報名，請勿重覆提交。")
        st.stop()

    now_hk = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
    try:
        execute_query(
            f"""
            INSERT INTO {TABLE_COMPETITION_REGISTRATIONS} (
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
            )
            VALUES (
                :competition_edition,
                :team_name,
                :main_debater_name,
                :first_deputy_name,
                :second_deputy_name,
                :closing_debater_name,
                :contact_name,
                :contact_class,
                :contact_phone,
                'submitted',
                :submitted_at,
                :updated_at
            )
            """,
            {
                "competition_edition": edition,
                **form_data,
                "submitted_at": now_hk,
                "updated_at": now_hk,
            },
        )
        st.session_state["registration_submitted"] = form_data["team_name"]
        st.rerun()
    except Exception as e:
        error_text = str(e)
        if "duplicate key" in error_text.lower() or "unique" in error_text.lower():
            st.error("此隊名已於本屆提交報名，請勿重覆提交。")
        else:
            st.error(f"提交報名失敗：{e}")
