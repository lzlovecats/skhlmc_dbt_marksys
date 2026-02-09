import streamlit as st
import pandas as pd
import gspread
from functions import check_admin, get_connection, load_data_from_gsheet, save_match_to_gsheet
st.header("查閱評判分紙")

def get_score_data():
    try:
        spreadsheet = get_connection()
        score_sheet = spreadsheet.worksheet("Score")
        data = score_sheet.get_all_records()
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"讀取評分失敗: {e}")
        return None