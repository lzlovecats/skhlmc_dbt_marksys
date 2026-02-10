import streamlit as st
import gspread
import json
import pandas as pd
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
        sheet = spreadsheet.worksheet("Match")
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
    sheet = spreadsheet.worksheet("Match")
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
        
def delete_match_from_gsheet(match_id):
    spreadsheet = get_connection()
    sheet = spreadsheet.worksheet("Match")

def save_draft_to_gsheet(match_id, judge_name, team_side, score_data):
    try:
        spreadsheet = get_connection()
        worksheet = spreadsheet.worksheet("Temp")

        data_to_save = score_data.copy()
        
        if "raw_df_a" in data_to_save:
            data_to_save["raw_df_a"] = data_to_save["raw_df_a"].to_json()
        if "raw_df_b" in data_to_save:
            data_to_save["raw_df_b"] = data_to_save["raw_df_b"].to_json()
            
        json_str = json.dumps(data_to_save, ensure_ascii=False)

        # Find and delete all existing drafts for this specific judge/match/side
        all_values = worksheet.get_all_values()
        rows_to_delete = []
        for i, row in enumerate(all_values):
            if i == 0: continue  # Skip header
            if (len(row) >= 3 and
                str(row[0]) == str(match_id) and
                str(row[1]) == str(judge_name) and
                str(row[2]) == str(team_side)):
                rows_to_delete.append(i + 1)

        if rows_to_delete:
            for row_num in sorted(rows_to_delete, reverse=True):
                worksheet.delete_rows(row_num)

        # Append the new, updated draft
        worksheet.append_row([str(match_id), str(judge_name), str(team_side), json_str])
            
        return True
    except Exception as e:
        st.error(f"無法上傳暫存資料至Google Cloud: {e}")
        return False
    
def load_draft_from_gsheet(match_id, judge_name):
    try:
        spreadsheet = get_connection()
        worksheet = spreadsheet.worksheet("Temp")
            
        all_values = worksheet.get_all_values()
        result = {"正方": None, "反方": None}
        
        for i, row in enumerate(all_values):
            if i == 0: continue  # Skip header
            if len(row) < 4: continue # Ensure row has enough columns
            
            if (str(row[0]) == str(match_id) and 
                str(row[1]) == str(judge_name)):
                
                side = row[2]
                json_str = row[3]
                
                if json_str:
                    try:
                        data = json.loads(json_str)
                        if "raw_df_a" in data:
                            data["raw_df_a"] = pd.read_json(data["raw_df_a"])
                        if "raw_df_b" in data:
                            data["raw_df_b"] = pd.read_json(data["raw_df_b"])
                        result[side] = data
                    except:
                        pass
        return result
    except Exception as e:
        return {"正方": None, "反方": None}
