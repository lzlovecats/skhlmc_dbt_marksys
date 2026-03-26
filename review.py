import streamlit as st
import pandas as pd
import json
import io
from functions import get_connection, get_score_data, query_params, _log_login, normalize_judge_name
from scoring import SPEECH_CRITERIA, speech_col, FREE_DEBATE_MAX, COHERENCE_MAX, GRAND_TOTAL

st.header("查閱評判分紙")

if "score_unlocked_matches" not in st.session_state:
    st.session_state["score_unlocked_matches"] = set()

# Load matches that have scores (joined to get review_password)
matches_with_scores = query_params("""
    SELECT DISTINCT m.match_id, m.review_password
    FROM matches m
    INNER JOIN scores s ON m.match_id = s.match_id
    ORDER BY m.match_id
""")

if matches_with_scores.empty:
    st.info("數據庫上未有任何評分紀錄。")
    st.stop()

all_match_ids = matches_with_scores["match_id"].tolist()
selected_match = st.selectbox("請選擇要查看的場次", options=all_match_ids)

match_row = matches_with_scores[matches_with_scores["match_id"] == selected_match].iloc[0]
review_password = match_row["review_password"]

# Per-match password gate
if selected_match not in st.session_state["score_unlocked_matches"]:
    st.subheader("查閱比賽分紙登入")
    if not review_password:
        st.warning("此場次未設定查閱密碼，請聯絡賽會人員。")
        st.stop()
    pwd = st.text_input("請輸入由賽會人員提供的密碼", type="password", key=f"pwd_{selected_match}")
    if st.button("登入"):
        if pwd == review_password:
            st.session_state["score_unlocked_matches"].add(selected_match)
            _log_login("score_review", "score_review")
            st.rerun()
        else:
            st.error("密碼錯誤")
    st.stop()

# Authenticated — show scores for selected match
df_scores = get_score_data()

if df_scores is None or df_scores.empty:
    st.info("數據庫上未有任何評分紀錄。")
    st.stop()

match_results = df_scores[df_scores['match_id'] == selected_match]
if match_results.empty:
    st.info("此場次未有評分紀錄。")
    st.stop()


def compute_best_debater(match_id, records):
    debaters_row = query_params(
        "SELECT side, position, name FROM debaters WHERE match_id = :match_id",
        {"match_id": match_id}
    )
    if not debaters_row.empty:
        debater_names = {
            (str(r["side"]).strip(), int(r["position"])): str(r["name"]).strip()
            for _, r in debaters_row.iterrows()
        }
        def _label(pos, side, position):
            name = debater_names.get((side, position), "")
            return f"{pos}（{name}）" if name else pos
        role_map = {
            "pro1_m": _label("正方主辯", "pro", 1),
            "pro2_m": _label("正方一副", "pro", 2),
            "pro3_m": _label("正方二副", "pro", 3),
            "pro4_m": _label("正方結辯", "pro", 4),
            "con1_m": _label("反方主辯", "con", 1),
            "con2_m": _label("反方一副", "con", 2),
            "con3_m": _label("反方二副", "con", 3),
            "con4_m": _label("反方結辯", "con", 4),
        }
    else:
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

    rank_cols = ["pro1_m", "pro2_m", "pro3_m", "pro4_m", "con1_m", "con2_m", "con3_m", "con4_m"]
    scores_only = records[rank_cols].apply(pd.to_numeric, errors="coerce")
    if scores_only.isna().any().any():
        return None, None

    all_ranks = []
    for _, row in scores_only.iterrows():
        all_ranks.append(row.rank(ascending=False, method='min'))

    df_ranks = pd.DataFrame(all_ranks)
    total_rank_sum = df_ranks.sum()

    best_debater_results = []
    for col_id in rank_cols:
        best_debater_results.append({
            "辯位": role_map.get(col_id, col_id),
            "名次總和": int(total_rank_sum[col_id]),
            "平均得分": round(scores_only[col_id].mean(), 2)
        })

    df_final_best = pd.DataFrame(best_debater_results).sort_values(
        by=["名次總和", "平均得分"],
        ascending=[True, False]
    )
    return df_final_best, df_final_best.iloc[0]


all_judge = match_results['judge_name'].unique()
selected_judge = st.selectbox("請選擇評判", options=all_judge)

judge_record = match_results[match_results['judge_name'] == selected_judge].iloc[0]
with st.container(border=True):
    st.write(f"**正方：** {judge_record['pro_name']}")
    st.write(f"**反方：** {judge_record['con_name']}")
    st.write(f"**提交時間（HKT）：** {judge_record['mark_time']}")

pro_total = judge_record['pro_total']
con_total = judge_record['con_total']
if pro_total > con_total:
    winner_label = f"正方 ({judge_record['pro_name']})"
elif con_total > pro_total:
    winner_label = f"反方 ({judge_record['con_name']})"
else:
    winner_label = "平局"
sum_col1, sum_col2 = st.columns(2)
sum_col1.metric(f"正方總分（{judge_record['pro_name']}）", f"{pro_total} / {GRAND_TOTAL}")
sum_col2.metric(f"反方總分（{judge_record['con_name']}）", f"{con_total} / {GRAND_TOTAL}")
st.metric("本張分紙勝方", winner_label)

best_table, best_one = compute_best_debater(selected_match, match_results)
if best_one is not None:
    st.info(f"本場最佳辯論員：**{best_one['辯位']}** (名次總和：{best_one['名次總和']} | 平均分：{best_one['平均得分']})")
    with st.expander("查看最佳辯論員排名"):
        st.dataframe(best_table, use_container_width=True, hide_index=True)
else:
    st.caption("最佳辯論員資料暫時不可用。")

st.divider()
st.write("### 評分詳情")
st.caption("手機或 iPad 可於下方分頁切換正反雙方分紙。")

conn = get_connection()
temp_sheet = conn.query(
    """
    SELECT *
    FROM temp_scores
    WHERE match_id = :mid
      AND judge_name = :jname
      AND COALESCE(is_final, FALSE) = TRUE
    ORDER BY updated_at DESC
    """,
    params={"mid": str(selected_match), "jname": normalize_judge_name(str(selected_judge))},
    ttl=0,
)

def display_team_scores(side_label, team_name, record, detail_a, detail_b):
    st.subheader(f"{side_label}：{team_name}")

    st.write("#### 甲：台上發言")
    if detail_a.empty:
        st.caption("（詳細評分資料不可用）")
    else:
        detail_a["總分（100）"] = sum(detail_a[speech_col(c)] * c["weight"] for c in SPEECH_CRITERIA)
        st.dataframe(detail_a, use_container_width=True, hide_index=True)

    st.write("#### 乙：自由辯論")
    if detail_b.empty:
        st.caption("（詳細評分資料不可用）")
    else:
        detail_b[f"總分（{FREE_DEBATE_MAX}）"] = detail_b.sum(axis=1)
        st.dataframe(detail_b, use_container_width=True, hide_index=True)

    if side_label == "正方":
        deduction_key, coherence_key, total = "pro_deduction", "pro_coherence", "pro_total"
    else:
        deduction_key, coherence_key, total = "con_deduction", "con_coherence", "con_total"

    st.write(f"扣分總和：{record[deduction_key]}")
    st.write(f"內容連貫：{record[coherence_key]} / {COHERENCE_MAX} ")
    st.divider()
    st.metric(f"{side_label}總分", f"{record[total]}／{GRAND_TOTAL} ")


pro_detail_a, pro_detail_b = pd.DataFrame(), pd.DataFrame()
con_detail_a, con_detail_b = pd.DataFrame(), pd.DataFrame()
loaded_final_sides = set()
for i, row in temp_sheet.iterrows():
    side = str(row["team_side"]).strip()
    detail_json = row["data"]
    try:
        data = detail_json if isinstance(detail_json, dict) else json.loads(detail_json)
        if side == "正方":
            if "raw_df_a" in data:
                pro_detail_a = pd.read_json(io.StringIO(data["raw_df_a"]))
            if "raw_df_b" in data:
                pro_detail_b = pd.read_json(io.StringIO(data["raw_df_b"]))
            loaded_final_sides.add("正方")
        elif side == "反方":
            if "raw_df_a" in data:
                con_detail_a = pd.read_json(io.StringIO(data["raw_df_a"]))
            if "raw_df_b" in data:
                con_detail_b = pd.read_json(io.StringIO(data["raw_df_b"]))
            loaded_final_sides.add("反方")
    except (json.JSONDecodeError, KeyError):
        continue

missing_final_sides = [side for side in ["正方", "反方"] if side not in loaded_final_sides]
if missing_final_sides:
    st.error(f"此評判的最終分紙細項資料不完整（缺少：{'、'.join(missing_final_sides)}），請聯絡賽會人員。")

tab_pro, tab_con = st.tabs([f"正方：{judge_record['pro_name']}", f"反方：{judge_record['con_name']}"])

with tab_pro:
    display_team_scores(
        "正方",
        judge_record['pro_name'],
        judge_record,
        pro_detail_a,
        pro_detail_b
    )

with tab_con:
    display_team_scores(
        "反方",
        judge_record['con_name'],
        judge_record,
        con_detail_a,
        con_detail_b
    )
