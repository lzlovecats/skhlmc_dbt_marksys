import streamlit as st
from datetime import datetime, time, timedelta
import gspread
from google.oauth2.service_account import Credentials
from functions import check_admin, get_connection, load_data_from_gsheet, save_match_to_gsheet
st.header("賽事資料輸入")

time_slots = []
start_t = datetime.strptime("15:00", "%H:%M")
end_t = datetime.strptime("18:00", "%H:%M")

while start_t <= end_t:
    time_slots.append(start_t.strftime("%H:%M"))
    start_t += timedelta(minutes=15)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

if "delete_confirm_id" not in st.session_state:
    st.session_state["delete_confirm_id"] = None

if "match_action_message" not in st.session_state:
    st.session_state["match_action_message"] = None

if st.session_state["match_action_message"]:
    msg = st.session_state["match_action_message"]
    if msg["type"] == "success":
        st.success(msg["content"])
    elif msg["type"] == "warning":
        st.warning(msg["content"])
    st.session_state["match_action_message"] = None

if not check_admin():
    st.stop()

if "all_matches" not in st.session_state:
    st.session_state["all_matches"] = load_data_from_gsheet()

new_match_id = st.text_input("輸入比賽場次")
if st.button("新增比賽場次"):
    if new_match_id:
        if new_match_id not in st.session_state["all_matches"]:
            new_match_data = {
                "match_id": new_match_id,
                "date": "",
                "time": "",
                "que": "",
                "pro": "", "con": "",
                "pro_1": "", "pro_2": "", "pro_3": "", "pro_4": "",
                "con_1": "", "con_2": "", "con_3": "", "con_4": "", "access_code": ""
            }
            st.session_state["all_matches"][new_match_id] = new_match_data
            save_match_to_gsheet(new_match_data)
            st.success(f"已建立場次：{new_match_id}")
        else:
            st.warning("此場次已存在。")
    else:
        st.error("未輸入任何文字！")

if st.session_state["all_matches"]:
    match_options = list(st.session_state["all_matches"].keys())
    selected_match = st.selectbox("選擇比賽場次", options=match_options)
    current_data = st.session_state["all_matches"][selected_match]

    with st.form(key=f"form_{selected_match}"):

        default_date = datetime.now().date()
        saved_date_str = str(current_data.get("date", ""))
        if saved_date_str:
            try:
                default_date = datetime.strptime(saved_date_str, "%Y-%m-%d").date()
            except ValueError:
                pass
        
        default_time = datetime.now().time()
        saved_time_str = str(current_data.get("time", "16:00"))     
        if saved_time_str not in time_slots:
            index = 4
        else:
            index = time_slots.index(saved_time_str)
        
        match_date = st.date_input("比賽日期", value=default_date)
        match_time_str = st.selectbox("比賽時間", options=time_slots, index=index)
        match_time = datetime.strptime(match_time_str, "%H:%M").time()


        que = st.text_input("辯題", value=current_data.get("que", ""))

        pro, con = st.columns(2)
        with pro:
            st.markdown("### 正方資料")
            pro_team = st.text_input("正方隊名", value=current_data.get("pro", ""))
            pro_1 = st.text_input("正方主辯", value=current_data.get("pro_1", ""))
            pro_2 = st.text_input("正方一副", value=current_data.get("pro_2", ""))
            pro_3 = st.text_input("正方二副", value=current_data.get("pro_3", ""))
            pro_4 = st.text_input("正方結辯", value=current_data.get("pro_4", ""))
        
        with con:
            st.markdown("### 反方資料")
            con_team = st.text_input("反方隊名", value=current_data.get("con", ""))
            con_1 = st.text_input("反方主辯", value=current_data.get("con_1", ""))
            con_2 = st.text_input("反方一副", value=current_data.get("con_2", ""))
            con_3 = st.text_input("反方二副", value=current_data.get("con_3", ""))
            con_4 = st.text_input("反方結辯", value=current_data.get("con_4", ""))

        access_code = str(st.text_input("評判入場密碼", value=current_data.get("access_code", "")))

        if st.form_submit_button("儲存場次資料"):
            match_data_prepare = {
                "match_id": selected_match,
                "date": match_date.strftime("%Y-%m-%d"),
                "time": match_time.strftime("%H:%M"),
                "que": que, 
                "pro": pro_team, "con": con_team, 
                "pro_1": pro_1, "pro_2": pro_2, "pro_3": pro_3, "pro_4": pro_4,
                "con_1": con_1, "con_2": con_2, "con_3": con_3, "con_4": con_4, "access_code": access_code}
            st.session_state["all_matches"][selected_match] = match_data_prepare
            save_match_to_gsheet(match_data_prepare)
            st.success(f"資料已儲存至Google Cloud！")

    st.divider()
    st.subheader("刪除場次")
    
    if st.session_state["delete_confirm_id"] != selected_match:
        if st.button(f"刪除場次：{selected_match}", type="primary", key="delete_match_btn"):
            st.session_state["delete_confirm_id"] = selected_match
            st.rerun()
    else:
        st.warning(f"刪除場次 「{selected_match} 」後將無法復原！確定繼續刪除？")
        col_del_1, col_del_2 = st.columns(2)
        with col_del_1:
            if st.button("確定刪除", type="primary", key="confirm_delete_btn"):
                try:
                    ss = get_connection()
                    ws = ss.worksheet("Match")
                    col_values = ws.col_values(1)
                    if selected_match in col_values:
                        row_index = col_values.index(selected_match) + 1
                        ws.delete_rows(row_index)
                        del st.session_state["all_matches"][selected_match]
                        st.session_state["match_action_message"] = {"type": "success", "content": f"已刪除場次：{selected_match}"}
                    else:
                        st.session_state["match_action_message"] = {"type": "warning", "content": "Google Cloud中找不到此場次(可能已被刪除)。"}
                        if selected_match in st.session_state["all_matches"]:
                            del st.session_state["all_matches"][selected_match]
                    
                    st.session_state["delete_confirm_id"] = None
                    st.rerun()
                except Exception as e:
                    st.error(f"刪除失敗: {e}")
        with col_del_2:
            if st.button("取消", type="secondary", key="cancel_delete_btn"):
                st.session_state["delete_confirm_id"] = None
                st.rerun()