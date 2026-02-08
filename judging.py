import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from functions import get_connection

st.header("電子評分系統")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", 
          "https://www.googleapis.com/auth/drive"]

if "auth_match_id" not in st.session_state:
    st.session_state["auth_match_id"] = None

if "judge_authenticated" not in st.session_state:
    st.session_state["judge_authenticated"] = False

if "temp_scores" not in st.session_state:
    st.session_state["temp_scores"] = {"正方": None, "反方": None}

if "all_matches" not in st.session_state:
    st.session_state["all_matches"] = load_data_from_gsheet()

all_matches = st.session_state.get("all_matches", {})
if not all_matches:
    st.warning("目前沒有場次資料，請先由賽會人員輸入。")
    st.stop()

selected_match_id = st.selectbox("請選擇比賽場次", options=list(all_matches.keys()))
current_match = all_matches[selected_match_id]

if st.session_state["auth_match_id"] != selected_match_id:
    st.session_state["judge_authenticated"] = False

if not st.session_state["judge_authenticated"]:
    st.subheader("評判身分驗證")
    input_otp = st.text_input("請輸入由賽會提供的入場密碼", type="password")
    
    correct_otp = str(current_match.get("access_code", ""))
    if st.button("驗證入場"):
        if input_otp == correct_otp and correct_otp != "":
            st.session_state["judge_authenticated"] = True
            st.session_state["auth_match_id"] = selected_match_id
            st.rerun()
        elif correct_otp == "":
            st.error("該場次未開放評分，請向賽會人員查詢。")
            st.stop()
        else:
            st.error("密碼錯誤!")
            st.stop()
    else:
        st.stop()

st.success(f"已進入場次：{selected_match_id}")
motion = current_match.get("que", "（未輸入辯題）")
st.markdown(f"辯題：{motion}")
judge_name = st.text_input("評判姓名")

pro_team_name = current_match.get("pro", "未填寫")
con_team_name = current_match.get("con", "未填寫")

team_side = st.radio(
    "選擇評分隊伍", 
    ["正方", "反方"], 
    format_func=lambda x: f"{x} ({pro_team_name})" if x == "正方" else f"{x} ({con_team_name})",
    horizontal=True
)

#sync data from match_info
if team_side == "正方":
    names = [current_match.get("pro_1", ""), current_match.get("pro_2", ""), 
             current_match.get("pro_3", ""), current_match.get("pro_4", "")]
    team_name = current_match.get("pro", "正方")
else:
    names = [current_match.get("con_1", ""), current_match.get("con_2", ""), 
             current_match.get("con_3", ""), current_match.get("con_4", "")]
    team_name = current_match.get("con", "反方")

# A
st.subheader(f"（甲）台上發言 - {team_side}")
if st.session_state["temp_scores"][team_side] is not None:
    df_a_source = st.session_state["temp_scores"][team_side]["raw_df"]
else:
    df_a_source = pd.DataFrame([
        {"辯位": "主辯", "姓名": names[0], "內容 (x4)": 0, "辭鋒 (x3)": 0, "組織 (x2)": 0, "風度 (x1)": 0},
        {"辯位": "一副", "姓名": names[1], "內容 (x4)": 0, "辭鋒 (x3)": 0, "組織 (x2)": 0, "風度 (x1)": 0},
        {"辯位": "二副", "姓名": names[2], "內容 (x4)": 0, "辭鋒 (x3)": 0, "組織 (x2)": 0, "風度 (x1)": 0},
        {"辯位": "結辯", "姓名": names[3], "內容 (x4)": 0, "辭鋒 (x3)": 0, "組織 (x2)": 0, "風度 (x1)": 0},
    ])

edited_df_a = st.data_editor(
    df_a_source,
    column_config={
        "辯位": st.column_config.TextColumn(disabled=True),
        "姓名": st.column_config.TextColumn(disabled=True),
        "內容 (x4)": st.column_config.NumberColumn(min_value=0, max_value=10, step=1, required=True),
        "辭鋒 (x3)": st.column_config.NumberColumn(min_value=0, max_value=10, step=1, required=True),
        "組織 (x2)": st.column_config.NumberColumn(min_value=0, max_value=10, step=1, required=True),
        "風度 (x1)": st.column_config.NumberColumn(min_value=0, max_value=10, step=1, required=True),
    },
    hide_index=True,
    use_container_width=True,
    key=f"editor_a_{selected_match_id}_{team_side}"
)

ind_content = edited_df_a["內容 (x4)"] * 4
ind_delivery = edited_df_a["辭鋒 (x3)"] * 3
ind_org = edited_df_a["組織 (x2)"] * 2
ind_poise = edited_df_a["風度 (x1)"] * 1

individual_scores = ind_content + ind_delivery + ind_org + ind_poise
total_score_a = individual_scores.sum()
st.markdown(f"總分：{total_score_a}/400")
if team_side == "正方":
    pro1_m, pro2_m, pro3_m, pro4_m = [int(s) for s in individual_scores]
else:
    con1_m, con2_m, con3_m, con4_m = [int(s) for s in individual_scores]

st.divider()
st.subheader("（乙）自由辯論")
initial_data_b = [
    {"內容 (20)": 0, "辭鋒 (15)": 0, "組織 (10)": 0, "合作 (5)": 0, "風度 (5)": 0}
]
df_b = pd.DataFrame(initial_data_b)
edited_df_b = st.data_editor(
    df_b,
    column_config={
        "內容 (20)": st.column_config.NumberColumn(min_value=0, max_value=20, step=1, required=True),
        "辭鋒 (15)": st.column_config.NumberColumn(min_value=0, max_value=15, step=1, required=True),
        "組織 (10)": st.column_config.NumberColumn(min_value=0, max_value=10, step=1, required=True),
        "合作 (5)": st.column_config.NumberColumn(min_value=0, max_value=5, step=1, required=True),
        "風度 (5)": st.column_config.NumberColumn(min_value=0, max_value=5, step=1, required=True),
    },
    hide_index=True,
    use_container_width=True
)
total_score_b = edited_df_b.sum().sum()
st.markdown(f"總分：{total_score_b}/55")

st.divider()
st.subheader("（丙）扣分及內容連貫")
col1, col2 = st.columns(2)
with col1:
    deduction = st.number_input("扣分總和", min_value=0, step=1)
with col2:
    coherence = st.number_input("內容連貫 (5)", min_value=0, max_value=5, step=1)

final_total = total_score_a + total_score_b - deduction + coherence

st.markdown("---")
st.title(f"總分：{final_total} / 460")

s_pro = "已暫存✅" if st.session_state["temp_scores"]["正方"] else "未評分❌"
s_con = "已暫存✅" if st.session_state["temp_scores"]["反方"] else "未評分❌"
st.write(f"**評分進度：**")
st.write(f"正方：{s_pro}")
st.write(f"反方：{s_con}")

if st.button(f"暫存{team_side}評分"):
    if not judge_name:
        st.error("請輸入評判姓名！")
    else:
        side_data = {
            "team_name": team_name,
            "total_a": int(total_score_a),
            "total_b": int(total_score_b),
            "deduction": int(deduction),
            "coherence": int(coherence),
            "final_total": int(final_total),
            "ind_scores": [int(s) for s in individual_scores],
            "raw_df": edited_df_a
        }
        st.session_state["temp_scores"][team_side] = side_data
        st.success(f"已暫存 {team_side} ({team_name}) 分數。")
        st.rerun()

if st.session_state["temp_scores"]["正方"] and st.session_state["temp_scores"]["反方"]:
    st.warning("⚠️ 兩隊評分已完成。")
    if st.button("正式提交評分", type="primary"):
        try:
            ss = get_connection()
            score_sheet = ss.worksheet("Score") 
            
            pro = st.session_state["temp_scores"]["正方"]
            con = st.session_state["temp_scores"]["反方"]
            
            merged_row = [
                selected_match_id,
                judge_name,
                pro["team_name"],
                con["team_name"],
                pro["final_total"],
                con["final_total"],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                pro["ind_scores"][0], pro["ind_scores"][1], pro["ind_scores"][2], pro["ind_scores"][3],
                con["ind_scores"][0], con["ind_scores"][1], con["ind_scores"][2], con["ind_scores"][3],
            ]
            
            score_sheet.append_row(merged_row)
            st.session_state["temp_scores"] = {"正方": None, "反方": None}
            st.success("已成功提交評分！")
            st.session_state["judge_authenticated"] = False
        except Exception as e:
            st.error(f"儲存失敗: {e}")
    