import streamlit as st
import pandas as pd
import json
from functions import check_score, get_connection
st.header("查閱評判分紙")

if not check_score():
    st.stop()

def get_score_data():
    try:
        conn = get_connection()
        df = conn.query("SELECT * FROM scores", ttl=0)
        return pd.DataFrame(df)
    except Exception as e:
        st.error(f"讀取評分失敗: {e}")
        return pd.DataFrame()
        
df_scores = get_score_data()

if df_scores is None or df_scores.empty:
    st.info("數據庫上未有任何評分紀錄。")
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
    st.write(f"**提交時間（HKT）：** {judge_record['mark_time']}")

st.divider()
st.write("### 評分詳情")

conn = get_connection()
temp_sheet = conn.query("SELECT * FROM temp_scores", ttl=0)

def display_team_scores(side_label, team_name, record, detail_a, detail_b):
    st.subheader(f"{side_label}：{team_name}")

    if not detail_a.empty:
        detail_a["總分（100）"] = detail_a["內容 (x4)"] * 4 + detail_a["辭鋒 (x3)"] * 3 + detail_a["組織 (x2)"] * 2 + detail_a["風度 (x1)"] * 1
    if not detail_b.empty:
        detail_b["總分（55）"] = detail_b.sum(axis=1)

    st.write("#### 甲：台上發言")
    st.dataframe(detail_a, use_container_width=True, hide_index=True)

    st.write("#### 乙：自由辯論")
    st.dataframe(detail_b, use_container_width=True, hide_index=True)

    if side_label == "正方":
        deduction_key, coherence_key, total = "pro_deduction", "pro_coherence", "pro_total"
    else:
        deduction_key, coherence_key, total = "con_deduction", "con_coherence", "con_total"

    st.write(f"扣分總和：{record[deduction_key]}")
    st.write(f"內容連貫：{record[coherence_key]} / 5 ")
    st.divider()
    st.metric(f"{side_label}總分", f"{record[total]}／460 ")


col_pro, col_con = st.columns(2)


pro_detail_a, pro_detail_b = pd.DataFrame(), pd.DataFrame()
con_detail_a, con_detail_b = pd.DataFrame(), pd.DataFrame()
for i, row in temp_sheet.iterrows():
    if str(row["match_id"]).strip() == str(selected_match).strip() and str(row["judge_name"]).strip() == str(selected_judge).strip():
        side = str(row["team_side"]).strip()
        detail_json = row["data"]
        try:
            data = detail_json if isinstance(detail_json, dict) else json.loads(detail_json)
            if side == "正方":
                if "raw_df_a" in data:
                    pro_detail_a = pd.read_json(data["raw_df_a"])
                if "raw_df_b" in data:
                    pro_detail_b = pd.read_json(data["raw_df_b"])
            elif side == "反方":
                if "raw_df_a" in data:
                    con_detail_a = pd.read_json(data["raw_df_a"])
                if "raw_df_b" in data:
                    con_detail_b = pd.read_json(data["raw_df_b"])
        except (json.JSONDecodeError, KeyError):
            continue

with col_pro:
    display_team_scores(
        "正方",
        judge_record['pro_name'],
        judge_record,
        pro_detail_a,
        pro_detail_b
    )

with col_con:
    display_team_scores(
        "反方",
        judge_record['con_name'],
        judge_record,
        con_detail_a,
        con_detail_b
    )