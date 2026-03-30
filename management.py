import streamlit as st
from functions import check_admin, get_score_data, get_best_debater_results, query_params
st.header("賽事結果統計")

if not check_admin():
        st.stop()

df_scores = get_score_data()

if df_scores is None or df_scores.empty:
    st.info("系統中未有任何評分紀錄。")
    st.stop()

df_scores['match_id'] = df_scores['match_id'].astype(str)
all_matches = df_scores['match_id'].unique()
selected_match = st.selectbox("請選擇要查看的場次", options=all_matches)

match_results = df_scores[df_scores['match_id'] == selected_match]
st.write(f"### 場次 {selected_match} 評分狀況")
st.write(f"目前已有 **{len(match_results)}** 位評判提交分數。")

pro_votes = (match_results['pro_total'] > match_results['con_total']).sum()
con_votes = (match_results['con_total'] > match_results['pro_total']).sum()
draws = (match_results['pro_total'] == match_results['con_total']).sum()

st.subheader("勝負判定")
match_topic_row = query_params(
    "SELECT topic FROM MATCHES WHERE match_id = :match_id",
    {"match_id": selected_match}
)
match_topic = match_topic_row.iloc[0]["topic"] if not match_topic_row.empty else "（未有辯題資料）"
st.write(f"辯題：{match_topic}")
col1, col2, col3 = st.columns(3)
col1.metric(f"正方({match_results['pro_name'].iloc[0]})得票", f"{pro_votes} 票")
col2.metric(f"反方 ({match_results['con_name'].iloc[0]})得票", f"{con_votes} 票")
col3.metric("平票", f"{draws} 票")

if pro_votes > con_votes:
    winner_text = f"🏆勝方：正方 ({match_results['pro_name'].iloc[0]})"
    st.info(winner_text)
elif con_votes > pro_votes:
    winner_text = f"🏆勝方：反方 ({match_results['con_name'].iloc[0]})"
    st.info(winner_text)
else:
    st.warning("票數相同，依賽規需要重新運作自由辯論環節。")

df_final_best, best_one = get_best_debater_results(selected_match, match_results)
if df_final_best is not None and best_one is not None:
    st.dataframe(df_final_best, use_container_width=True, hide_index=True)
    st.info(f"本場最佳辯論員：**{best_one['辯位']}** (名次總和：{best_one['名次總和']} | 平均分：{best_one['平均得分']})")
else:
    st.warning("最佳辯論員資料暫時不可用。")

# Score anomaly detection
if len(match_results) >= 3:
    st.divider()
    st.subheader("評分異常偵測")
    for side, label in [("pro_total", "正方"), ("con_total", "反方")]:
        mean_val = match_results[side].mean()
        std_val = match_results[side].std()
        if std_val > 0:
            for _, row in match_results.iterrows():
                if abs(row[side] - mean_val) > 2 * std_val:
                    st.warning(f"⚠️ {row['judge_name']} 的{label}評分 ({row[side]}) 偏離其他評判 (平均: {mean_val:.1f}, 標準差: {std_val:.1f})")
    if not any(
        abs(row[side] - match_results[side].mean()) > 2 * match_results[side].std()
        for side in ["pro_total", "con_total"]
        if match_results[side].std() > 0
        for _, row in match_results.iterrows()
    ):
        st.success("所有評判評分無明顯異常。")

