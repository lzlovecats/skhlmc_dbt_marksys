import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from functions import load_data_from_gsheet, get_connection, load_draft_from_gsheet, save_draft_to_gsheet
from extra_streamlit_components import CookieManager

st.header("電子評分系統")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", 
          "https://www.googleapis.com/auth/drive"]

cookie_manager = CookieManager(key="judging_cookies")

if "auth_match_id" not in st.session_state:
    st.session_state["auth_match_id"] = None

if "judge_authenticated" not in st.session_state:
    st.session_state["judge_authenticated"] = False  # Authentication Success?

if "temp_scores" not in st.session_state:
    st.session_state["temp_scores"] = {"正方": None, "反方": None}  # Temp stores for Pro/Con (Local)

if "active_match_id" not in st.session_state:
    st.session_state["active_match_id"] = None

if "all_matches" not in st.session_state:
    st.session_state["all_matches"] = load_data_from_gsheet()  # All matches in gsheet (Local)

if "submission_message" not in st.session_state:
    st.session_state["submission_message"] = None

if "last_judge_name" not in st.session_state:
    st.session_state["last_judge_name"] = ""

all_matches = st.session_state.get("all_matches", {})
if not all_matches:
    st.warning("目前沒有場次資料，請先由賽會人員輸入。")
    st.stop()

selected_match_id = st.selectbox("請選擇比賽場次", options=list(all_matches.keys()))
current_match = all_matches[selected_match_id]

if st.session_state["active_match_id"] != selected_match_id:
    st.session_state["temp_scores"] = {"正方": None, "反方": None}
    st.session_state["active_match_id"] = selected_match_id
    st.session_state["draft_loaded"] = False

# Auto-login check using cookies
if not st.session_state["judge_authenticated"]:
    saved_match_id = cookie_manager.get("match_id")
    saved_access_code = cookie_manager.get("access_code")
    saved_judge_name = cookie_manager.get("judge_name")
    
    if saved_match_id == selected_match_id and saved_access_code:
        correct_otp_from_sheet = str(current_match.get("access_code", ""))
        correct_otp = correct_otp_from_sheet[1:] if correct_otp_from_sheet.startswith("'") else correct_otp_from_sheet
        
        if saved_access_code == correct_otp:
            st.session_state["judge_authenticated"] = True
            st.session_state["auth_match_id"] = selected_match_id
            if saved_judge_name:
                st.session_state["last_judge_name"] = saved_judge_name
            st.rerun()

if st.session_state["auth_match_id"] != selected_match_id:
    st.session_state["judge_authenticated"] = False

if not st.session_state["judge_authenticated"]:
    st.subheader("評判身分驗證")
    input_otp = st.text_input("請輸入由賽會提供的入場密碼", type="password")
    
    correct_otp_from_sheet = str(current_match.get("access_code", ""))
    correct_otp = correct_otp_from_sheet[1:] if correct_otp_from_sheet.startswith("'") else correct_otp_from_sheet

    if st.button("驗證入場"):
        if input_otp == correct_otp and correct_otp_from_sheet != "":
            st.session_state["judge_authenticated"] = True
            st.session_state["auth_match_id"] = selected_match_id
            
            # Save cookies
            expires_at = datetime.now() + timedelta(days=1)
            cookie_manager.set("match_id", selected_match_id, expires_at=expires_at)
            cookie_manager.set("access_code", input_otp, expires_at=expires_at)
            
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

# Pre-fill judge name if available from session state (restored from cookie)
default_judge_name = st.session_state.get("last_judge_name", "")
judge_name_input = st.text_input("評判姓名", value=default_judge_name)
judge_name = judge_name_input.strip() if judge_name_input else ""

if judge_name != st.session_state["last_judge_name"]:
    st.session_state["draft_loaded"] = False
    st.session_state["temp_scores"] = {"正方": None, "反方": None}
    st.session_state["last_judge_name"] = judge_name
    # Update judge name in cookie
    expires_at = datetime.now() + timedelta(days=1)
    cookie_manager.set("judge_name", judge_name, expires_at=expires_at)

if "draft_loaded" not in st.session_state:
    st.session_state["draft_loaded"] = False

if judge_name and selected_match_id and not st.session_state["draft_loaded"]:
    with st.spinner("正在檢查雲端暫存紀錄..."):
        drafts = load_draft_from_gsheet(selected_match_id, judge_name)
        
        if drafts["正方"] or drafts["反方"]:
            if st.session_state["temp_scores"]["正方"] is None and drafts["正方"]:
                 st.session_state["temp_scores"]["正方"] = drafts["正方"]
                 st.toast("已恢復正方雲端暫存分數。", icon="☁️")
                 
            if st.session_state["temp_scores"]["反方"] is None and drafts["反方"]:
                 st.session_state["temp_scores"]["反方"] = drafts["反方"]
                 st.toast("已恢復反方雲端暫存分數。", icon="☁️")
    
    st.session_state["draft_loaded"] = True

pro_team_name = current_match.get("pro", "未填寫")
con_team_name = current_match.get("con", "未填寫")

team_side = st.radio(
    "選擇評分隊伍", 
    ["正方", "反方"], 
    format_func=lambda x: f"{x} ({pro_team_name})" if x == "正方" else f"{x} ({con_team_name})",
    horizontal=True
)

if st.session_state["temp_scores"][team_side] and "last_saved" in st.session_state["temp_scores"][team_side]:
    try:
        last_saved_str = st.session_state["temp_scores"][team_side]["last_saved"]
        last_saved_dt = datetime.fromisoformat(last_saved_str)
        diff = datetime.now() - last_saved_dt
        minutes = int(diff.total_seconds() / 60)
        st.caption(f"上一次儲存 {team_side} 分數：{minutes} 分鐘前")
    except:
        pass

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
    df_a_source = st.session_state["temp_scores"][team_side]["raw_df_a"]
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

# B
st.divider()
st.subheader("（乙）自由辯論")

if st.session_state["temp_scores"][team_side] is not None and "raw_df_b" in st.session_state["temp_scores"][team_side]:
    df_b = st.session_state["temp_scores"][team_side]["raw_df_b"]
else:
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
    use_container_width=True,
    key=f"editor_b_{selected_match_id}_{team_side}"
)
total_score_b = edited_df_b.sum().sum()
st.markdown(f"總分：{total_score_b}/55")

# C
st.divider()
st.subheader("（丙）扣分及內容連貫")

existing_deduct = 0
existing_cohere = 0

if st.session_state["temp_scores"][team_side] is not None:
    existing_deduct = st.session_state["temp_scores"][team_side].get("deduction", 0)
    existing_cohere = st.session_state["temp_scores"][team_side].get("coherence", 0)

col1, col2 = st.columns(2)
with col1:
    deduction = st.number_input("扣分總和", min_value=0, step=1, value=existing_deduct, key=f"deduct_{selected_match_id}_{team_side}")
with col2:
    coherence = st.number_input("內容連貫 (5)", min_value=0, max_value=5, step=1, value=existing_cohere, key=f"cohere_{selected_match_id}_{team_side}")

final_total = total_score_a + total_score_b - deduction + coherence

st.markdown("---")
st.title(f"總分：{final_total} / 460")

s_pro = "已暫存☑️" if st.session_state["temp_scores"]["正方"] else "未評分✖️"
s_con = "已暫存☑️" if st.session_state["temp_scores"]["反方"] else "未評分✖️"
st.write(f"**評分進度：**")
st.write(f"正方：{s_pro}")
st.write(f"反方：{s_con}")

if st.session_state["submission_message"]:
    msg = st.session_state["submission_message"]
    if msg["type"] == "warning":
        st.warning(msg["content"])
        if "noti" in msg:
            st.toast(msg["noti"], icon="⚠️")
    elif msg["type"] == "success":
        st.success(msg["content"])
        if "noti" in msg:
            st.toast(msg["noti"], icon="✅")
    elif msg["type"] == "error":
        st.error(msg["content"])
        if "noti" in msg:
            st.toast(msg["noti"], icon="❌")
    st.session_state["submission_message"] = None

if st.button(f"暫存{team_side}評分"):
    if not judge_name:
        st.error("請輸入評判姓名！")
    else:
        existing_submit = get_connection().worksheet("Score").get_all_values()
        for i, row in enumerate(existing_submit):
            if i == 0: continue  # Skip header
            if row[0] == selected_match_id and row[1] == judge_name:
                st.error("你已提交過評分！無法修改評分！")
                st.stop()

        side_data = {
            "team_name": team_name,
            "total_a": int(total_score_a),
            "total_b": int(total_score_b),
            "deduction": int(deduction),
            "coherence": int(coherence),
            "final_total": int(final_total),
            "ind_scores": [int(s) for s in individual_scores],
            "raw_df_a": edited_df_a,
            "raw_df_b": edited_df_b,
            "last_saved": datetime.now().isoformat()
        }
        st.session_state["temp_scores"][team_side] = side_data

        with st.spinner("正在上傳暫存資料至雲端..."):
            success = save_draft_to_gsheet(selected_match_id, judge_name, team_side, side_data)
        
        cols_a = ["內容 (x4)", "辭鋒 (x3)", "組織 (x2)", "風度 (x1)"]
        cols_b = ["內容 (20)", "辭鋒 (15)", "組織 (10)", "合作 (5)", "風度 (5)"]
        has_zeros = (edited_df_a[cols_a] == 0).any().any() or (edited_df_b[cols_b] == 0).any().any()

        if success:
            if has_zeros:
                st.session_state["submission_message"] = {
                "type": "warning",
                "content": f"已暫存 {team_side} ({team_name}) 分數至雲端 。注意：有評分細項為 0 分！",
                "noti": f"警告：{team_side}有評分細項為 0 分！"}
            else:
                st.session_state["submission_message"] = {
                "type": "success",
                "content": f"已暫存 {team_side} ({team_name}) 分數至雲端。",
                "noti": f"雲端備份成功：{team_side}"}
        else:
            if has_zeros:
                st.session_state["submission_message"] = {
                    "type": "warning",
                    "content": f"已暫存 {team_side} ({team_name}) 分數至本機。注意：有評分細項為 0 分！",
                    "noti": f"警告：{team_side}有評分細項為 0 分！"
                    }
            else:
                st.session_state["submission_message"] = {
                    "type": "success",
                    "content": f"已暫存 {team_side} ({team_name}) 分數至本機。",
                    "noti": f"成功暫存 {team_side} 分數。"}
        st.rerun()

if st.session_state["temp_scores"]["正方"] and st.session_state["temp_scores"]["反方"]:
    st.success("🎉 兩隊評分已完成！（尚未上傳評分）")
    st.warning("⚠️ 請注意！正式提交分紙後將無法修改分數！請確認所有資料輸入正確！")
    if st.button("正式提交評分", type="primary"):
        try:
            if not judge_name:
                st.error("請輸入評判姓名！")
                st.stop()

            ss = get_connection()
            score_sheet = ss.worksheet("Score") 
            
            side_data = {
            "team_name": team_name,
            "total_a": int(total_score_a),
            "total_b": int(total_score_b),
            "deduction": int(deduction),
            "coherence": int(coherence),
            "final_total": int(final_total),
            "ind_scores": [int(s) for s in individual_scores],
            "raw_df_a": edited_df_a,
            "raw_df_b": edited_df_b,
            "last_saved": datetime.now().isoformat()
            }
            st.session_state["temp_scores"][team_side] = side_data

            pro = st.session_state["temp_scores"]["正方"]
            con = st.session_state["temp_scores"]["反方"]
            
            merged_row = [
                selected_match_id,
                judge_name,
                pro["team_name"],
                con["team_name"],
                pro["final_total"],
                con["final_total"],
                (datetime.now() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
                pro["ind_scores"][0], pro["ind_scores"][1], pro["ind_scores"][2], pro["ind_scores"][3],
                con["ind_scores"][0], con["ind_scores"][1], con["ind_scores"][2], con["ind_scores"][3],
                pro["total_b"], con["total_b"],
                pro["deduction"], con["deduction"],
                pro["coherence"], con["coherence"]
            ]
            
            existing_submit = get_connection().worksheet("Score").get_all_values()
            for i, row in enumerate(existing_submit):
                if i == 0: continue  # Skip header
                if row[0] == selected_match_id and row[1] == judge_name:
                    st.session_state["submission_message"] = {
                        "type": "error",
                        "content": "你已提交過評分！無法再次提交！",
                        "noti": "提交評分失敗（重覆提交）"}
                    st.rerun()
            with st.spinner("正在上傳評分至雲端..."):
                save_final_draft = save_draft_to_gsheet(selected_match_id, judge_name, team_side, side_data)
                score_sheet.append_row(merged_row)
            st.session_state["temp_scores"] = {"正方": None, "反方": None}
            
            # Clear cookies after successful submission
            cookie_manager.delete("match_id")
            cookie_manager.delete("access_code")
            cookie_manager.delete("judge_name")

            st.balloons()
            st.success("已成功提交評分！")
            st.toast("感謝評判百忙之中抽空擔任評分工作 :>", icon="🙌")
            st.session_state["judge_authenticated"] = False
        except Exception as e:
            st.error(f"儲存失敗: {e}")
