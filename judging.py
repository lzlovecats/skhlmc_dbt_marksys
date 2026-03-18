import streamlit as st
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
from functions import (
    load_matches_from_db,
    load_draft_from_db,
    save_draft_to_db,
    execute_query_count,
    query_params
)
from scoring import (
    SPEECH_CRITERIA, speech_col, SPEECH_TOTAL_MAX,
    FREE_DEBATE_CRITERIA, free_debate_col, FREE_DEBATE_MAX,
    COHERENCE_MAX, GRAND_TOTAL
)

st.header("電子評分系統")

@st.dialog("最後確認")
def confirm_submit(pro, con, selected_match_id, judge_name, team_side, side_data):
    st.write("根據你的評分，兩隊總分為：")
    st.write(f"正方：{pro['final_total']} ／ {GRAND_TOTAL}")
    st.write(f"反方：{con['final_total']} ／ {GRAND_TOTAL}")
    if pro['final_total'] == con['final_total']:
        st.error("注意：兩隊總分相同！")
    elif pro['final_total'] > con['final_total']:
        st.success("勝方：正方")
    else:
        st.success("勝方：反方")
    st.warning("請仔細檢查分數，最後確定後將無法修改！")
    if st.button("最後確定", type="primary"):
        try:
            with st.spinner("正在上傳評分至雲端..."):
                save_draft_to_db(selected_match_id, judge_name, team_side, side_data)
                query = """
                INSERT INTO scores (
                    match_id, judge_name, pro_name, con_name, pro_total, con_total, mark_time,
                    pro1_m, pro2_m, pro3_m, pro4_m, con1_m, con2_m, con3_m, con4_m,
                    pro_free, con_free, pro_deduction, con_deduction, pro_coherence, con_coherence
                ) VALUES (
                    :match_id, :judge_name, :pro_name, :con_name,
                    :pro_total, :con_total, :mark_time,
                    :pro1_m, :pro2_m, :pro3_m, :pro4_m, :con1_m, :con2_m, :con3_m, :con4_m,
                    :pro_free, :con_free, :pro_deduction, :con_deduction, :pro_coherence, :con_coherence
                ) ON CONFLICT (match_id, judge_name) DO NOTHING
                """
                params = {
                    "match_id": selected_match_id, "judge_name": judge_name,
                    "pro_name": pro["team_name"], "con_name": con["team_name"],
                    "pro_total": pro["final_total"], "con_total": con["final_total"],
                    "mark_time": datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S"),
                    "pro1_m": pro["ind_scores"][0], "pro2_m": pro["ind_scores"][1], "pro3_m": pro["ind_scores"][2], "pro4_m": pro["ind_scores"][3],
                    "con1_m": con["ind_scores"][0], "con2_m": con["ind_scores"][1], "con3_m": con["ind_scores"][2], "con4_m": con["ind_scores"][3],
                    "pro_free": pro["total_b"], "con_free": con["total_b"],
                    "pro_deduction": pro["deduction"], "con_deduction": con["deduction"],
                    "pro_coherence": pro["coherence"], "con_coherence": con["coherence"]
                }
                rows_inserted = execute_query_count(query, params)

            if rows_inserted == 0:
                st.session_state["submission_message"] = {
                    "type": "error",
                    "content": "你已提交過評分！無法再次提交！",
                    "noti": "提交評分失敗（重覆提交）"}
            else:
                st.session_state["temp_scores"] = {"正方": None, "反方": None}
                st.session_state["submission_message"] = {
                    "type": "success",
                    "content": "已成功提交評分！感謝評判百忙之中抽空擔任評分工作 :>",
                    "noti": "🙌 已成功提交評分！"
                }
                st.session_state["judge_authenticated"] = False
            st.rerun()
        except Exception as e:
            st.error(f"儲存失敗: {e}")

@st.dialog("確認登出")
def confirm_logout_dialog():
    st.warning("登出後，本機暫存的評分進度將會清除，請確保已儲存至雲端。")
    if st.button("確認登出", type="primary"):
        st.session_state["last_judge_name"] = ""
        st.session_state["judge_authenticated"] = False
        st.session_state["temp_scores"] = {"正方": None, "反方": None}
        st.rerun()

if "auth_match_id" not in st.session_state:
    st.session_state["auth_match_id"] = None

if "judge_authenticated" not in st.session_state:
    st.session_state["judge_authenticated"] = False  # Authentication Success?

if "temp_scores" not in st.session_state:
    st.session_state["temp_scores"] = {"正方": None, "反方": None}  # Temp stores for Pro/Con (Local)

if "active_match_id" not in st.session_state:
    st.session_state["active_match_id"] = None

if "all_matches" not in st.session_state:
    st.session_state["all_matches"] = load_matches_from_db()  # All matches in db (Local)

if "submission_message" not in st.session_state:
    st.session_state["submission_message"] = None

if "last_judge_name" not in st.session_state:
    st.session_state["last_judge_name"] = ""

all_matches = st.session_state.get("all_matches", {})
if not all_matches:
    st.warning("目前沒有場次資料，請先由賽會人員輸入。")
    if st.button("🔄 重新載入場次"):
        st.session_state["all_matches"] = load_matches_from_db()
        st.rerun()
    st.stop()

selected_match_id = st.selectbox("請選擇比賽場次", options=list(all_matches.keys()))
current_match = all_matches[selected_match_id]

if st.session_state["active_match_id"] != selected_match_id:
    st.session_state["temp_scores"] = {"正方": None, "反方": None}
    st.session_state["active_match_id"] = selected_match_id
    st.session_state["draft_loaded"] = False

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

# Pre-fill judge name if available from session state
default_judge_name = st.session_state.get("last_judge_name", "")
judge_name_input = st.text_input("評判姓名", value=default_judge_name)
judge_name = judge_name_input.strip() if judge_name_input else ""

if st.button("登出評判帳戶"):
    confirm_logout_dialog()

if judge_name != st.session_state["last_judge_name"]:
    st.session_state["draft_loaded"] = False
    st.session_state["temp_scores"] = {"正方": None, "反方": None}
    st.session_state["last_judge_name"] = judge_name

if "draft_loaded" not in st.session_state:
    st.session_state["draft_loaded"] = False

if judge_name and selected_match_id and not st.session_state["draft_loaded"]:
    with st.spinner("正在檢查雲端暫存紀錄..."):
        drafts = load_draft_from_db(selected_match_id, judge_name)

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
roles = ["主辯", "一副", "二副", "結辯"]
if st.session_state["temp_scores"][team_side] is not None:
    df_a_source = st.session_state["temp_scores"][team_side]["raw_df_a"]
else:
    df_a_source = pd.DataFrame([
        {"辯位": role, "姓名": name, **{speech_col(c): 0 for c in SPEECH_CRITERIA}}
        for role, name in zip(roles, names)
    ])

edited_df_a = st.data_editor(
    df_a_source,
    column_config={
        "辯位": st.column_config.TextColumn(disabled=True),
        "姓名": st.column_config.TextColumn(disabled=True),
        **{speech_col(c): st.column_config.NumberColumn(min_value=0, max_value=c["max"], step=1, required=True)
           for c in SPEECH_CRITERIA}
    },
    hide_index=True,
    use_container_width=True,
    key=f"editor_a_{selected_match_id}_{team_side}"
)

individual_scores = sum(edited_df_a[speech_col(c)] * c["weight"] for c in SPEECH_CRITERIA)
total_score_a = individual_scores.sum()
st.markdown(f"總分：{total_score_a}/{SPEECH_TOTAL_MAX}")

# B
st.divider()
st.subheader("（乙）自由辯論")

if st.session_state["temp_scores"][team_side] is not None and "raw_df_b" in st.session_state["temp_scores"][team_side]:
    df_b = st.session_state["temp_scores"][team_side]["raw_df_b"]
else:
    df_b = pd.DataFrame([{free_debate_col(c): 0 for c in FREE_DEBATE_CRITERIA}])

edited_df_b = st.data_editor(
    df_b,
    column_config={
        free_debate_col(c): st.column_config.NumberColumn(min_value=0, max_value=c["max"], step=1, required=True)
        for c in FREE_DEBATE_CRITERIA
    },
    hide_index=True,
    use_container_width=True,
    key=f"editor_b_{selected_match_id}_{team_side}"
)
total_score_b = edited_df_b.sum().sum()
st.markdown(f"總分：{total_score_b}/{FREE_DEBATE_MAX}")

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
    coherence = st.number_input(f"內容連貫 ({COHERENCE_MAX})", min_value=0, max_value=COHERENCE_MAX, step=1, value=existing_cohere, key=f"cohere_{selected_match_id}_{team_side}")

final_total = total_score_a + total_score_b - deduction + coherence

st.markdown("---")
st.title(f"總分：{final_total} / {GRAND_TOTAL}")

st.write("**評分進度：**")
prog_col1, prog_col2 = st.columns(2)
with prog_col1:
    if st.session_state["temp_scores"]["正方"]:
        st.success(f"正方 ({pro_team_name})：已暫存 ✅")
    else:
        st.warning(f"正方 ({pro_team_name})：未評分 ✖️")
with prog_col2:
    if st.session_state["temp_scores"]["反方"]:
        st.success(f"反方 ({con_team_name})：已暫存 ✅")
    else:
        st.warning(f"反方 ({con_team_name})：未評分 ✖️")

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
        existing_submit = query_params(
            "SELECT * FROM scores WHERE match_id = :match_id AND judge_name = :judge_name",
            {"match_id": selected_match_id, "judge_name": judge_name}
        )
        if not existing_submit.empty:
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
            success = save_draft_to_db(selected_match_id, judge_name, team_side, side_data)

        cols_a = [speech_col(c) for c in SPEECH_CRITERIA]
        cols_b = [free_debate_col(c) for c in FREE_DEBATE_CRITERIA]
        has_zeros = (edited_df_a[cols_a] == 0).any().any() or (edited_df_b[cols_b] == 0).any().any()

        if success:
            if has_zeros:
                st.session_state["submission_message"] = {
                "type": "warning",
                "content": f"已暫存 {team_side} ({team_name}) 分數至雲端 。注意：有評分細項為 0 分！",
                "noti": f"警告：{team_side}有評分細項為 0 分！"}
            else:
                other_side = "反方" if team_side == "正方" else "正方"
                st.session_state["submission_message"] = {
                "type": "success",
                "content": f"已暫存 {team_side} ({team_name}) 分數至雲端。請記得切換至「{other_side}」繼續評分！",
                "noti": f"雲端備份成功：{team_side}，請切換至{other_side}繼續！"}
        else:
            if has_zeros:
                st.session_state["submission_message"] = {
                    "type": "warning",
                    "content": f"已暫存 {team_side} ({team_name}) 分數至本機。注意：有評分細項為 0 分！",
                    "noti": f"警告：{team_side}有評分細項為 0 分！"
                    }
            else:
                other_side = "反方" if team_side == "正方" else "正方"
                st.session_state["submission_message"] = {
                    "type": "success",
                    "content": f"已暫存 {team_side} ({team_name}) 分數至本機。請記得切換至「{other_side}」繼續評分！",
                    "noti": f"成功暫存 {team_side} 分數，請切換至{other_side}繼續！"}
        st.rerun()

if st.session_state["temp_scores"]["正方"] and st.session_state["temp_scores"]["反方"]:
    st.success("🎉 兩隊評分已完成！（尚未上傳評分）")
    st.warning("⚠️ 請注意！正式提交分紙後將無法修改分數！請確認所有資料輸入正確！")
    if st.button("正式提交評分", type="primary"):
        try:
            if not judge_name:
                st.error("請輸入評判姓名！")
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

            pro = st.session_state["temp_scores"]["正方"]
            con = st.session_state["temp_scores"]["反方"]

            confirm_submit(pro, con, selected_match_id, judge_name, team_side, side_data)
        except Exception as e:
            st.error(f"儲存失敗: {e}")
