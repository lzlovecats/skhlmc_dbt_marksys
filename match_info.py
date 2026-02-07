import streamlit as st
st.header("賽事資料輸入")

with st.form("第二屆季軍賽"):
    match_date = st.date_input("比賽日期")
    match_id = st.text_input("比賽場次")
    que = st.text_input("辯題")

    pro, con = st.columns(2)
    with pro:
        pro_team = st.text_input("正方隊名")
        pro_1 = st.text_input("正方主辯")
        pro_2 = st.text_input("正方一副")
        pro_3 = st.text_input("正方二副")
        pro_4 = st.text_input("正方結辯")
    with con:
        con_team = st.text_input("反方隊名")
        con_1 = st.text_input("反方主辯")
        con_2 = st.text_input("反方一副")
        con_3 = st.text_input("反方二副")
        con_4 = st.text_input("反方結辯")

    if st.form_submit_button("輸入比賽資料"):
        st.session_state["que"] = {"que": que, "pro": pro_team, "con": con_team, 
                                   "pro_1": pro_1, "pro_2": pro_2, "pro_3": pro_3, "pro_4": pro_4,
                                   "con_1": con_1, "con_2": con_2, "con_3": con_3, "con_4": con_4}
        st.success("資料已更新，請前往『電子分紙』頁面。")