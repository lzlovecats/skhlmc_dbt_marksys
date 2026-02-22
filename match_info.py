import streamlit as st
from datetime import datetime, timedelta
from functions import check_admin, get_connection, load_matches_from_db, save_match_to_db, draw_a_topic, draw_pro_con, execute_query
st.header("賽事資料輸入")

# Create time slots
time_slots = []
start_t = datetime.strptime("15:30", "%H:%M")
end_t = datetime.strptime("18:00", "%H:%M")

while start_t <= end_t:
    time_slots.append(start_t.strftime("%H:%M"))
    start_t += timedelta(minutes=10)

# Create states if they don't exist
if "delete_confirm_id" not in st.session_state:
    st.session_state["delete_confirm_id"] = None

if "match_action_message" not in st.session_state:
    st.session_state["match_action_message"] = None

if "show_draw_side_ui" not in st.session_state:
    st.session_state.show_draw_side_ui = False

if "draw_result" not in st.session_state:
    st.session_state.draw_result = {"pro_team": None, "con_team": None, "show": False}
# Show msg
if st.session_state["match_action_message"]:
    msg = st.session_state["match_action_message"]
    if msg["type"] == "success":
        st.success(msg["content"])
    elif msg["type"] == "warning":
        st.warning(msg["content"])
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
                "date": "",
                "time": "",
                "que": "",
                "pro": "", "con": "",
                "pro_1": "", "pro_2": "", "pro_3": "", "pro_4": "",
                "con_1": "", "con_2": "", "con_3": "", "con_4": "", "access_code": ""
            }
            st.session_state["all_matches"][new_match_id] = new_match_data
            save_match_to_db(new_match_data)
            st.success(f"已建立場次：{new_match_id}")
        else:
            st.warning("此場次已存在。")
    else:
        st.error("未輸入任何文字！")

if st.session_state.draw_result["show"]:
    st.success(f"正方：{st.session_state.draw_result['pro_team']}")
    st.success(f"反方：{st.session_state.draw_result['con_team']}")
    st.session_state.draw_result["show"] = False

# Select a match and edit info
if st.session_state["all_matches"]:
    match_options = list(st.session_state["all_matches"].keys())
    selected_match = st.selectbox("選擇比賽場次", options=match_options)
    current_data = st.session_state["all_matches"][selected_match]

    # "Draw a topic" button must be outside the form to prevent StreamlitAPIException.
    if st.button("抽辯題", key=f"draw_topic_{selected_match}"):
        drawed_topic = draw_a_topic()
        if drawed_topic != "":
            st.success(f"已抽取辯題：{drawed_topic}")
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
                        st.session_state["all_matches"][selected_match]["pro"] = pro_team
                        st.session_state["all_matches"][selected_match]["con"] = con_team
                        st.session_state.draw_result = {"pro_team": pro_team, "con_team": con_team, "show": True}
                        st.session_state.show_draw_side_ui = False
                        st.rerun()
                    else:
                        st.error("請輸入兩隊隊伍名稱。")
            with col2:
                if st.button("取消"):
                    st.session_state.show_draw_side_ui = False
                    st.rerun()

    with st.form(key=f"form_{selected_match}"):

        default_date = datetime.now().date()
        saved_date_str = str(current_data.get("date", ""))
        if saved_date_str:
            try:
                default_date = datetime.strptime(saved_date_str, "%Y-%m-%d").date()
            except ValueError:
                pass
        
        default_time = datetime.now().time()
        saved_time_str = str(current_data.get("time", "16:00"))
        try:
            index = time_slots.index(saved_time_str)
        except ValueError:
            index = time_slots.index("16:00") if "16:00" in time_slots else 0

        match_date = st.date_input("比賽日期", value=default_date)
        match_time_str = st.selectbox("比賽時間", options=time_slots, index=index)
        match_time = datetime.strptime(match_time_str, "%H:%M").time()

        que = st.text_input("辯題", value=current_data.get("que", ""), key=f"que_{selected_match}")

        pro, con = st.columns(2)
        with pro:
            st.markdown("### 正方資料")
            pro_team = st.text_input("正方隊名", value=current_data.get("pro", ""))
            pro_1 = st.text_input("正方主辯", value=current_data.get("pro_1", ""))
            pro_2 = st.text_input("正方一副", value=current_data.get("pro_2", ""))
            pro_3 = st.text_input("正方二副", value=current_data.get("pro_3", ""))
            pro_4 = st.text_input("正方結辯", value=current_data.get("pro_4", ""))
        
        with con:
            st.markdown("### 反方資料")
            con_team = st.text_input("反方隊名", value=current_data.get("con", ""))
            con_1 = st.text_input("反方主辯", value=current_data.get("con_1", ""))
            con_2 = st.text_input("反方一副", value=current_data.get("con_2", ""))
            con_3 = st.text_input("反方二副", value=current_data.get("con_3", ""))
            con_4 = st.text_input("反方結辯", value=current_data.get("con_4", ""))

        current_access_code_from_sheet = str(current_data.get("access_code", ""))
        display_access_code = current_access_code_from_sheet[1:] if current_access_code_from_sheet.startswith("'") else current_access_code_from_sheet
        access_code = st.text_input("評判入場密碼", value=display_access_code)

        # Save edited info to db
        if st.form_submit_button("儲存場次資料"):
            match_data_prepare = {
                "match_id": selected_match,
                "date": match_date.strftime("%Y-%m-%d"),
                "time": match_time.strftime("%H:%M"),
                "que": que, 
                "pro": pro_team, "con": con_team, 
                "pro_1": pro_1, "pro_2": pro_2, "pro_3": pro_3, "pro_4": pro_4,
                "con_1": con_1, "con_2": con_2, "con_3": con_3, "con_4": con_4, "access_code": access_code}
            st.session_state["all_matches"][selected_match] = match_data_prepare
            save_match_to_db(match_data_prepare)
            st.success(f"資料已儲存至數據庫！")
    
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
                    execute_query("DELETE FROM matches WHERE match_id = :match_id", {"match_id": selected_match})
                    execute_query("DELETE FROM scores WHERE match_id = :match_id", {"match_id": selected_match})
                    execute_query("DELETE FROM temp_scores WHERE match_id = :match_id", {"match_id": selected_match})

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