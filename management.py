import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
st.header("賽事結果統計")

def get_score_data():
    from match_info import get_connection
    try:
        ss_client = get_connection()
        spreadsheet = gspread.authorize(Credentials.from_service_account_info(
            st.secrets["gcp_service_account"], 
            scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        )).open_by_key("1y8FFMVfp1to5iIVAhNUPvICr__REwslUJsr_TkK3QF8")
        
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
st.write(f"### 場次 {selected_match} 評分狀況")
st.write(f"目前已有 **{len(match_results)}** 位評判提交分數。")

pro_votes = (match_results['pro_total'] > match_results['con_total']).sum()
con_votes = (match_results['con_total'] > match_results['pro_total']).sum()
draws = (match_results['pro_total'] == match_results['con_total']).sum()

st.subheader("勝負判定")
col1, col2, col3 = st.columns(3)
col1.metric("正方得票", f"{pro_votes} 票")
col2.metric("反方得票", f"{con_votes} 票")
col3.metric("打和票數", f"{draws} 票")

if pro_votes > con_votes:
    winner_text = f"🏆勝方：正方 ({match_results['pro_name'].iloc[0]})"
    st.success(winner_text)
elif con_votes > pro_votes:
    winner_text = f"🏆勝方：反方 ({match_results['con_name'].iloc[0]})"
    st.error(winner_text)
else:
    st.warning("票數相同，主席將依賽規重新運作自由辯論環節。")