import streamlit as st
from datetime import datetime, time, timedelta
import gspread
from google.oauth2.service_account import Credentials
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

def get_connection():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=SCOPES
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key("1y8FFMVfp1to5iIVAhNUPvICr__REwslUJsr_TkK3QF8")
    return spreadsheet

def load_data_from_gsheet():
    try:
        spreadsheet = get_connection()
        sheet = spreadsheet.sheet1
        records = sheet.get_all_records()
        
        data_dict = {}
        for row in records:
            m_id = str(row["match_id"])
            if m_id:
                data_dict[m_id] = row
        return data_dict
    except Exception as e:
        st.error(f"連線錯誤: {e}")
        return {}

def save_match_to_gsheet(match_data):
    spreadsheet = get_connection()
    sheet = spreadsheet.sheet1
    try:
        match_ids = sheet.col_values(1)
        
        row_values = [
            match_data["match_id"],
            str(match_data["date"]),
            str(match_data["time"]),
            match_data["que"],
            match_data["pro"],
            match_data["con"],
            match_data["pro_1"], match_data["pro_2"], match_data["pro_3"], match_data["pro_4"],
            match_data["con_1"], match_data["con_2"], match_data["con_3"], match_data["con_4"]
        ]

        if match_data["match_id"] in match_ids:
            row_index = match_ids.index(match_data["match_id"]) + 1
            st.info("更新舊有紀錄中，請稍等。")
            sheet.delete_rows(row_index)
            sheet.append_row(row_values)
        else:
            sheet.append_row(row_values)
        
    except Exception as e:
        st.error(f"寫入失敗: {e}")

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
                "con_1": "", "con_2": "", "con_3": "", "con_4": ""
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

        access_code = st.text_input("評判入場密碼", value=current_data.get("access_code", ""))

        if st.form_submit_button("儲存場次資料"):
            match_data_prepare = {
                "match_id": selected_match,
                "date": match_date,
                "time": match_time,
                "que": que, 
                "pro": pro_team, "con": con_team, 
                "pro_1": pro_1, "pro_2": pro_2, "pro_3": pro_3, "pro_4": pro_4,
                "con_1": con_1, "con_2": con_2, "con_3": con_3, "con_4": con_4, "access_code": access_code}
            st.session_state["all_matches"][selected_match] = match_data_prepare
            save_match_to_gsheet(match_data_prepare)
            st.success(f"資料已儲存至Google Cloud！")