import streamlit as st
import pandas as pd
import gspread
from functions import check_score, get_connection, load_data_from_gsheet, save_match_to_gsheet
st.header("查閱評判分紙")

if not check_score():
    st.stop()

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

judge_record = match_results[match_results['judge_name'] == selected_judge].iloc[0]
col_info1, col_info2, col_info3 = st.columns(3)
with col_info1:
    st.write(f"**正方：** {judge_record['pro_name']}")
with col_info2:
    st.write(f"**反方：** {judge_record['con_name']}")
with col_info3:
    st.write(f"**提交時間：** {judge_record['mark_time']}")

st.divider()
st.write("### 評分詳情")

col_pro, col_con = st.columns(2)

with col_pro:
    st.subheader(f"正方：{judge_record['pro_name']}")
    st.write(f"主辯分數：{judge_record['pro1_m']} ")
    st.write(f"一副分數：{judge_record['pro2_m']} ")
    st.write(f"二副分數：{judge_record['pro3_m']} ")
    st.write(f"結辯分數：{judge_record['pro4_m']} ")
    st.write(f"正方扣分總和：{judge_record['pro_deduction']}")
    st.write(f"正方內容連貫：{judge_record['pro_coherence']} ")
    st.divider()
    st.metric("正方總分", f"{judge_record['pro_total']}／460 ")

with col_con:
    st.subheader(f"反方：{judge_record['con_name']}")
    st.write(f"主辯分數：{judge_record['con1_m']} ")
    st.write(f"一副分數：{judge_record['con2_m']} ")
    st.write(f"二副分數：{judge_record['con3_m']} ")
    st.write(f"結辯分數：{judge_record['con4_m']} ")
    st.write(f"反方扣分總和：{judge_record['con_deduction']}")
    st.write(f"反方內容連貫：{judge_record['con_coherence']} ")
    st.divider()
    st.metric("反方總分", f"{judge_record['con_total']} ／460")