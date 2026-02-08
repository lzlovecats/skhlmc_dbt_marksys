import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from functions import check_admin, get_connection, load_data_from_gsheet, save_match_to_gsheet
st.header("賽事結果統計")

def get_score_data():
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

if not check_admin():
        st.stop()

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
col3.metric("平票", f"{draws} 票")

if pro_votes > con_votes:
    winner_text = f"🏆勝方：正方 ({match_results['pro_name'].iloc[0]})"
    st.success(winner_text)
elif con_votes > pro_votes:
    winner_text = f"🏆勝方：反方 ({match_results['con_name'].iloc[0]})"
    st.error(winner_text)
else:
    st.warning("票數相同，依賽規需要重新運作自由辯論環節。")

role_map = {
    "pro1_m": "正方主辯",
    "pro2_m": "正方一副",
    "pro3_m": "正方二副",
    "pro4_m": "正方結辯",
    "con1_m": "反方主辯",
    "con2_m": "反方一副",
    "con3_m": "反方二副",
    "con4_m": "反方結辯",
}

all_ranks = []
rank_cols = ["pro1_m", "pro2_m", "pro3_m", "pro4_m", "con1_m", "con2_m", "con3_m", "con4_m"]
for index, row in match_results.iterrows():
    scores = row[rank_cols].astype(int)
    ranks = scores.rank(ascending=False, method='min')
    all_ranks.append(ranks)
df_ranks = pd.DataFrame(all_ranks)
total_rank_sum = df_ranks.sum()

best_debater_results = []
for col_id in rank_cols:
    best_debater_results.append({
        "辯位": role_map.get(col_id, col_id),
        "名次總和": int(total_rank_sum[col_id]),
        "平均得分": round(match_results[col_id].mean(), 2)
    })

df_final_best = pd.DataFrame(best_debater_results).sort_values(
    by=["名次總和", "平均得分"], 
    ascending=[True, False]
)

st.dataframe(df_final_best, use_container_width=True, hide_index=True)

best_one = df_final_best.iloc[0]
st.info(f"本場最佳辯論員：**{best_one['辯位']}** (名次總和：{best_one['名次總和']} | 平均分：{best_one['平均得分']})")




