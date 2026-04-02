import streamlit as st
from datetime import datetime, timedelta
from functions import check_admin, get_connection, load_matches_from_db, save_match_to_db, draw_a_topic, draw_pro_con, execute_query, DIFFICULTY_OPTIONS
from schema import TABLE_MATCHES, TABLE_SCORE_DRAFTS, TABLE_SCORES
st.header("賽事資料輸入")

# Create time slots 15:00 – 18:00 in 10-minute steps
time_slots = []
_t = datetime.strptime("15:00", "%H:%M")
_end = datetime.strptime("18:00", "%H:%M")
while _t <= _end:
    time_slots.append(_t.strftime("%H:%M"))
    _t += timedelta(minutes=10)

# Create states if they don't exist
if "delete_confirm_id" not in st.session_state:
    st.session_state["delete_confirm_id"] = None

if "match_action_message" not in st.session_state:
    st.session_state["match_action_message"] = None

if "show_draw_side_ui" not in st.session_state:
    st.session_state.show_draw_side_ui = False

if "draw_result_dialog" not in st.session_state:
    st.session_state.draw_result_dialog = None

@st.dialog("抽站方結果")
def show_draw_result_dialog(pro_team, con_team):
    st.success(f"正方：{pro_team}")
    st.success(f"反方：{con_team}")
    st.info("抽籤結果已自動填入場次資料，請記得儲存！")
# Show msg
if st.session_state["match_action_message"]:
    action_message = st.session_state["match_action_message"]
    if action_message["type"] == "success":
        st.success(action_message["content"])
    elif action_message["type"] == "warning":
        st.warning(action_message["content"])
    st.session_state["match_action_message"] = None

# Authentication 
if not check_admin():
    st.stop()

# Get matches from db and store them to the state
if "all_matches" not in st.session_state:
    st.session_state["all_matches"] = load_matches_from_db()

# Add a new match and store to gsheet
new_match_id = st.text_input("輸入比賽場次")
if st.button("新增比賽場次"):
    if new_match_id:
        if new_match_id not in st.session_state["all_matches"]:
            new_match_data = {
                "match_id": new_match_id,
                "match_date": "",
                "match_time": "",
                "topic_text": "",
                "pro_team": "", "con_team": "",
                "pro_1": "", "pro_2": "", "pro_3": "", "pro_4": "",
                "con_1": "", "con_2": "", "con_3": "", "con_4": "", "access_code_hash": "", "review_password_hash": ""
            }
            st.session_state["all_matches"][new_match_id] = new_match_data
            save_match_to_db(new_match_data)
            st.success(f"已建立場次：{new_match_id}")
        else:
            st.warning("此場次已存在。")
    else:
        st.error("未輸入任何文字！")


# Select a match and edit info
if st.session_state["all_matches"]:
    match_options = list(st.session_state["all_matches"].keys())
    selected_match = st.selectbox("選擇比賽場次", options=match_options)
    current_data = st.session_state["all_matches"][selected_match]

    # "Draw a topic" button must be outside the form to prevent StreamlitAPIException.
    diff_filter = st.selectbox(
        "難度篩選",
        options=[0, 1, 2, 3],
        format_func=lambda x: "全部難度" if x == 0 else DIFFICULTY_OPTIONS[x],
        key=f"diff_filter_{selected_match}"
    )
    if st.button("抽辯題", key=f"draw_topic_{selected_match}"):
        drawn_topic = draw_a_topic(difficulty=diff_filter if diff_filter != 0 else None)
        if drawn_topic != "":
            st.success(f"已抽取辯題：{drawn_topic}")
            st.session_state["all_matches"][selected_match]["topic_text"] = drawn_topic
    else:
        st.info("按「抽辯題」以從辯題庫中抽取一條辯題。")

    if not st.session_state.show_draw_side_ui:
        if st.button("抽站方"):
            st.session_state.show_draw_side_ui = True
            st.rerun()
        st.info("按「抽站方」可以輸入兩隊名稱抽出站方。")

    if st.session_state.show_draw_side_ui:
        with st.container(border=True):
            st.subheader("抽站方")
            team1 = st.text_input("隊伍名稱1", key="team1_draw")
            team2 = st.text_input("隊伍名稱2", key="team2_draw")
            
            col1, col2 = st.columns(2)
            with col1:
                if st.button("確定抽籤"):
                    if team1 and team2:
                        pro_team, con_team = draw_pro_con(team1, team2)
                        st.session_state["all_matches"][selected_match]["pro_team"] = pro_team
                        st.session_state["all_matches"][selected_match]["con_team"] = con_team
                        st.session_state.draw_result_dialog = {"pro_team": pro_team, "con_team": con_team}
                        st.session_state.show_draw_side_ui = False
                        st.rerun()
                    else:
                        st.error("請輸入兩隊隊伍名稱。")
            with col2:
                if st.button("取消"):
                    st.session_state.show_draw_side_ui = False
                    st.rerun()

    if st.session_state.draw_result_dialog:
        draw_result = st.session_state.draw_result_dialog
        st.session_state.draw_result_dialog = None
        show_draw_result_dialog(draw_result["pro_team"], draw_result["con_team"])

    with st.form(key=f"form_{selected_match}"):

        default_date = datetime.now().date()
        saved_date_str = str(current_data.get("match_date", ""))
        if saved_date_str:
            try:
                default_date = datetime.strptime(saved_date_str, "%Y-%m-%d").date()
            except ValueError:
                pass
        
        saved_time_str = str(current_data.get("match_time", "16:00"))
        try:
            index = time_slots.index(saved_time_str)
        except ValueError:
            index = time_slots.index("16:00") if "16:00" in time_slots else 0

        match_date = st.date_input("比賽日期", value=default_date)
        match_time_str = st.selectbox("比賽時間", options=time_slots, index=index)
        match_time = datetime.strptime(match_time_str, "%H:%M").time()

        topic_text = st.text_input("辯題", value=current_data.get("topic_text", ""))

        pro, con = st.columns(2)
        with pro:
            st.markdown("### 正方資料")
            pro_team = st.text_input("正方隊名", value=current_data.get("pro_team", ""))
            pro_1 = st.text_input("正方主辯", value=current_data.get("pro_1", ""))
            pro_2 = st.text_input("正方一副", value=current_data.get("pro_2", ""))
            pro_3 = st.text_input("正方二副", value=current_data.get("pro_3", ""))
            pro_4 = st.text_input("正方結辯", value=current_data.get("pro_4", ""))
        
        with con:
            st.markdown("### 反方資料")
            con_team = st.text_input("反方隊名", value=current_data.get("con_team", ""))
            con_1 = st.text_input("反方主辯", value=current_data.get("con_1", ""))
            con_2 = st.text_input("反方一副", value=current_data.get("con_2", ""))
            con_3 = st.text_input("反方二副", value=current_data.get("con_3", ""))
            con_4 = st.text_input("反方結辯", value=current_data.get("con_4", ""))

        access_code_value = current_data.get("access_code_hash")
        has_access_code = access_code_value is not None and str(access_code_value).strip() != "" and str(access_code_value).strip().lower() != "nan"
        review_password_value = current_data.get("review_password_hash")
        has_review_password = review_password_value is not None and str(review_password_value).strip() != "" and str(review_password_value).strip().lower() != "nan"

        st.markdown("### 場次密碼")
        st.caption("留空代表保留現有密碼；輸入新密碼代表更新；如需移除，請勾選清除。")

        password_col1, password_col2 = st.columns(2)
        with password_col1:
            access_code = st.text_input("評判入場密碼", value="", placeholder="留空則保留現有密碼")
            st.caption("目前狀態：已設定" if has_access_code else "目前狀態：未設定")
            clear_access_code = st.checkbox("清除評判入場密碼", value=False, disabled=not has_access_code)

        with password_col2:
            review_password = st.text_input("查閱分紙密碼", value="", placeholder="留空則保留現有密碼")
            st.caption("目前狀態：已設定" if has_review_password else "目前狀態：未設定")
            clear_review_password = st.checkbox("清除查閱分紙密碼", value=False, disabled=not has_review_password)

        # Save edited info to db
        if st.form_submit_button("儲存場次資料"):
            if clear_access_code and access_code:
                st.error("如需清除評判入場密碼，請將密碼欄留空。")
                st.stop()
            if clear_review_password and review_password:
                st.error("如需清除查閱分紙密碼，請將密碼欄留空。")
                st.stop()
            match_data_prepare = {
                "match_id": selected_match,
                "match_date": match_date.strftime("%Y-%m-%d"),
                "match_time": match_time.strftime("%H:%M"),
                "topic_text": topic_text,
                "pro_team": pro_team, "con_team": con_team,
                "pro_1": pro_1, "pro_2": pro_2, "pro_3": pro_3, "pro_4": pro_4,
                "con_1": con_1, "con_2": con_2, "con_3": con_3, "con_4": con_4,
                "access_code_hash": access_code, "review_password_hash": review_password,
                "clear_access_code": clear_access_code, "clear_review_password": clear_review_password
            }
            save_match_to_db(match_data_prepare)
            st.session_state["all_matches"] = load_matches_from_db()
            st.session_state["match_action_message"] = {"type": "success", "content": f"場次「{selected_match}」資料已儲存至數據庫！"}
            st.rerun()
    
    # Delete a match
    st.divider()
    st.subheader("刪除場次")
    
    if st.session_state["delete_confirm_id"] != selected_match:
        if st.button(f"刪除場次：{selected_match}", type="primary", key="delete_match_btn"):
            st.session_state["delete_confirm_id"] = selected_match
            st.rerun()
    else:
        st.warning(f"確定刪除「{selected_match} 」？此動作無法復原！")
        col_del_1, col_del_2 = st.columns(2)
        with col_del_1:
            if st.button("確定刪除", type="primary", key="confirm_delete_btn"):
                try:
                    execute_query(f"DELETE FROM {TABLE_MATCHES} WHERE match_id = :match_id", {"match_id": selected_match})
                    execute_query(f"DELETE FROM {TABLE_SCORES} WHERE match_id = :match_id", {"match_id": selected_match})
                    execute_query(f"DELETE FROM {TABLE_SCORE_DRAFTS} WHERE match_id = :match_id", {"match_id": selected_match})

                    # Clean up session state and set final message
                    if selected_match in st.session_state["all_matches"]:
                        del st.session_state["all_matches"][selected_match]
                    st.session_state["match_action_message"] = {"type": "success", "content": f"已成功刪除場次 「{selected_match}」 及其所有相關評分記錄。"}
                    st.session_state["delete_confirm_id"] = None
                    st.rerun()
                except Exception as e:
                    st.error(f"刪除失敗: {e}")
        with col_del_2:
            if st.button("取消", type="secondary", key="cancel_delete_btn"):
                st.session_state["delete_confirm_id"] = None
                st.rerun()
