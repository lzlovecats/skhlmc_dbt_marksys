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
        
df_scores = get_score_data()

if df_scores is None or df_scores.empty:
    st.info("Google Cloud上未有任何評分紀錄。")
    st.stop()

all_matches = df_scores['match_id'].unique()
selected_match = st.selectbox("請選擇要查看的場次", options=all_matches)
match_results = df_scores[df_scores['match_id'] == selected_match]
all_judge = match_results['judge_name'].unique()
selected_judge = st.selectbox("請選擇評判", options=all_judge)

