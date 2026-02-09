import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",  "https://www.googleapis.com/auth/drive"]

def check_admin():
    if "admin_logged_in" not in st.session_state:
        st.session_state["admin_logged_in"] = False
        
    if not st.session_state["admin_logged_in"]:
        st.subheader("賽會人員登入")
        pwd = st.text_input("請輸入賽會人員密碼", type="password")
        if st.button("登入"):
            if pwd == st.secrets["admin_password"]:
                st.session_state["admin_logged_in"] = True
                st.rerun()
            else:
                st.error("密碼錯誤")
        return False
    return True
    
def check_score():
    if "score_logged_in" not in st.session_state:
        st.session_state["score_logged_in"] = False
        
    if not st.session_state["score_logged_in"]:
        st.subheader("查閱比賽分紙登入")
        pwd = st.text_input("請輸入由賽會人員提供的密碼", type="password")
        if st.button("登入"):
            if pwd == st.secrets["score_password"]:
                st.session_state["score_logged_in"] = True
                st.rerun()
            else:
                st.error("密碼錯誤")
        return False
    return True
    
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
            match_data["con_1"], match_data["con_2"], match_data["con_3"], match_data["con_4"], match_data.get("access_code", "")
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