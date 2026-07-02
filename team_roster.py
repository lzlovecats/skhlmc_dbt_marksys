import streamlit as st

from functions import get_roster_link_by_token, render_page_guidance, save_team_roster_by_token


def _query_param(name):
    value = st.query_params.get(name, "")
    if isinstance(value, list):
        return value[0] if value else ""
    return value


def _has_submitted(value):
    if value is None:
        return False
    text = str(value).strip().lower()
    return text not in ("", "nan", "nat", "none")


st.header("提交隊伍名單")
render_page_guidance(
    [
        "請使用賽會人員提供的專屬連結填寫隊名及辯員姓名。",
        "每個連結只可提交一次，提交後如需修改，請聯絡賽會人員重開填寫。",
        "請確認資料無誤後才提交。",
    ],
)

token = _query_param("token").strip()
if not token:
    st.error("此連結缺少提交代碼，請使用賽會人員提供的完整連結。")
    st.stop()

roster_link = get_roster_link_by_token(token)
if roster_link is None:
    st.error("此提交連結無效或已被重新生成，請向賽會人員索取最新連結。")
    st.stop()

side = str(roster_link["side"]).strip()
side_label = "正方" if side == "pro" else "反方"
team_key = "pro_team" if side == "pro" else "con_team"

with st.container(border=True):
    st.subheader("場次資料")
    st.write(f"**比賽場次：** {roster_link['match_id']}")
    st.write(f"**填寫方：** {side_label}")
    if roster_link.get("match_date") or roster_link.get("match_time"):
        st.write(f"**比賽時間：** {roster_link.get('match_date') or '未設定'} {roster_link.get('match_time') or ''}")
    if roster_link.get("topic_text"):
        st.write(f"**辯題：** {roster_link['topic_text']}")

if _has_submitted(roster_link.get("submitted_at")):
    st.success("此連結已提交名單。")
    st.info("如需更改隊名或辯員姓名，請聯絡賽會人員重開填寫。")
    st.stop()

with st.form("team_roster_form"):
    st.subheader(f"{side_label}資料")
    team_name = st.text_input(f"{side_label}隊名", value=roster_link.get(team_key, "") or "")

    col1, col2 = st.columns(2)
    with col1:
        debater_1 = st.text_input(f"{side_label}主辯", value=roster_link.get("debater_1", "") or "")
        debater_2 = st.text_input(f"{side_label}一副", value=roster_link.get("debater_2", "") or "")
    with col2:
        debater_3 = st.text_input(f"{side_label}二副", value=roster_link.get("debater_3", "") or "")
        debater_4 = st.text_input(f"{side_label}結辯", value=roster_link.get("debater_4", "") or "")

    submitted = st.form_submit_button("提交名單", type="primary", use_container_width=True)

if submitted:
    form_data = {
        "team_name": team_name.strip(),
        "debater_1": debater_1.strip(),
        "debater_2": debater_2.strip(),
        "debater_3": debater_3.strip(),
        "debater_4": debater_4.strip(),
    }
    missing_fields = [label for label, value in {
        "隊名": form_data["team_name"],
        "主辯": form_data["debater_1"],
        "一副": form_data["debater_2"],
        "二副": form_data["debater_3"],
        "結辯": form_data["debater_4"],
    }.items() if not value]

    if missing_fields:
        st.error("請填寫所有必填資料：" + "、".join(missing_fields))
        st.stop()

    result = save_team_roster_by_token(token, form_data)
    if result["ok"]:
        st.success("名單已成功提交。")
        st.info("提交後不可自行修改；如資料有誤，請聯絡賽會人員。")
    elif result["reason"] == "submitted":
        st.error("此連結已提交名單，不能重覆提交。")
    else:
        st.error("此提交連結無效或已被重新生成，請向賽會人員索取最新連結。")
