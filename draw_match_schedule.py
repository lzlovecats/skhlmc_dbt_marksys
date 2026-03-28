import random

import pandas as pd
import streamlit as st

from functions import check_admin


def normalize_teams(raw_text):
    return [line.strip() for line in str(raw_text or "").splitlines() if line.strip()]


def build_draw_result(teams):
    shuffled = teams[:]
    random.shuffle(shuffled)

    summary_rows = []

    if len(shuffled) == 2:
        final_match = {"left": shuffled[0], "right": shuffled[1]}
        summary_rows.append({
            "階段": "決賽",
            "輪次": "決賽",
            "場次": "場次 1",
            "正方／甲方": shuffled[0],
            "反方／乙方": shuffled[1],
            "備註": "2 支隊伍直接進入決賽"
        })
        return {
            "teams": teams,
            "main_rounds": [],
            "main_byes": [],
            "loser_rounds": [],
            "final_match": final_match,
            "summary_rows": summary_rows,
            "direct_final": True,
        }

    main_rounds = []
    main_byes = []
    current_labels = shuffled[:]
    round_num = 1

    while len(current_labels) > 1:
        title = "第一輪正賽" if round_num == 1 else f"正賽第{round_num}輪"
        pairs = []
        bye_team = None

        if len(current_labels) % 2 == 1:
            bye_team = current_labels[-1]
            playing = current_labels[:-1]
        else:
            playing = current_labels[:]

        next_labels = []
        for i in range(0, len(playing), 2):
            match_no = i // 2 + 1
            left_team = playing[i]
            right_team = playing[i + 1]
            pairs.append({"match_no": match_no, "left": left_team, "right": right_team})
            summary_rows.append({
                "階段": "正賽",
                "輪次": title,
                "場次": f"場次 {match_no}",
                "正方／甲方": left_team,
                "反方／乙方": right_team,
                "備註": ""
            })

            if len(current_labels) == 2 and bye_team is None:
                next_labels.append("正賽冠軍")
            else:
                next_labels.append(f"{title}場次{match_no}勝方")

        if bye_team:
            main_byes.append({"round": round_num, "title": title, "team": bye_team})
            summary_rows.append({
                "階段": "正賽",
                "輪次": title,
                "場次": "輪空",
                "正方／甲方": bye_team,
                "反方／乙方": "",
                "備註": "直接晉級下一輪"
            })
            next_labels.append(bye_team)

        main_rounds.append({"round": round_num, "title": title, "pairs": pairs})
        current_labels = next_labels
        round_num += 1

    loser_rounds = []
    first_round_match_count = len(main_rounds[0]["pairs"])

    if first_round_match_count == 1:
        loser_rounds.append({
            "round": 1,
            "title": "負方賽",
            "pairs": [],
            "bye": None,
            "note": "第一輪只有一場正賽，該場敗方直接成為負方賽冠軍。"
        })
        summary_rows.append({
            "階段": "負方賽",
            "輪次": "負方賽",
            "場次": "直接晉級",
            "正方／甲方": "第一輪場次1負方",
            "反方／乙方": "",
            "備註": "直接成為負方賽冠軍"
        })
    else:
        current_labels = [f"第一輪場次{i}負方" for i in range(1, first_round_match_count + 1)]
        round_num = 1

        while len(current_labels) > 1:
            title = "負方賽" if round_num == 1 else f"負方賽第{round_num}輪"
            pairs = []
            bye_team = None

            if len(current_labels) % 2 == 1:
                bye_team = current_labels[-1]
                playing = current_labels[:-1]
            else:
                playing = current_labels[:]

            next_labels = []
            for i in range(0, len(playing), 2):
                match_no = i // 2 + 1
                left_team = playing[i]
                right_team = playing[i + 1]
                pairs.append({"match_no": match_no, "left": left_team, "right": right_team})
                summary_rows.append({
                    "階段": "負方賽",
                    "輪次": title,
                    "場次": f"場次 {match_no}",
                    "正方／甲方": left_team,
                    "反方／乙方": right_team,
                    "備註": ""
                })

                if len(current_labels) == 2 and bye_team is None:
                    next_labels.append("負方賽冠軍")
                else:
                    next_labels.append(f"{title}場次{match_no}勝方")

            if bye_team:
                summary_rows.append({
                    "階段": "負方賽",
                    "輪次": title,
                    "場次": "輪空",
                    "正方／甲方": bye_team,
                    "反方／乙方": "",
                    "備註": "直接晉級下一輪"
                })
                next_labels.append(bye_team)

            loser_rounds.append({
                "round": round_num,
                "title": title,
                "pairs": pairs,
                "bye": bye_team,
                "note": ""
            })
            current_labels = next_labels
            round_num += 1

    final_match = {"left": "正賽冠軍", "right": "負方賽冠軍"}
    summary_rows.append({
        "階段": "決賽",
        "輪次": "決賽",
        "場次": "場次 1",
        "正方／甲方": final_match["left"],
        "反方／乙方": final_match["right"],
        "備註": ""
    })

    return {
        "teams": teams,
        "main_rounds": main_rounds,
        "main_byes": main_byes,
        "loser_rounds": loser_rounds,
        "final_match": final_match,
        "summary_rows": summary_rows,
        "direct_final": False,
    }


st.header("抽取賽程")

if not check_admin():
    st.stop()

if "raw_input_text" not in st.session_state:
    st.session_state["raw_input_text"] = ""

if "draw_input_snapshot" not in st.session_state:
    st.session_state["draw_input_snapshot"] = None

if "draw_result" not in st.session_state:
    st.session_state["draw_result"] = None

if "draw_result_invalidated" not in st.session_state:
    st.session_state["draw_result_invalidated"] = False

st.subheader("輸入參賽隊伍")
st.caption("每行輸入一支隊伍名稱。修改名單後，舊抽籤結果會自動失效。")

teams_input = st.text_area(
    "每行輸入一支隊伍名稱",
    key="raw_input_text",
    placeholder="例如：\n思若秋澄\n請你食薯餅\n預設名稱一",
    height=180
)

current_teams = normalize_teams(teams_input)
snapshot_teams = st.session_state["draw_input_snapshot"]

if snapshot_teams is not None and current_teams != snapshot_teams:
    st.session_state["draw_result"] = None
    st.session_state["draw_input_snapshot"] = None
    st.session_state["draw_result_invalidated"] = True

if st.session_state["draw_result_invalidated"]:
    st.warning("隊伍名單已變更，請重新抽籤。")

with st.form("draw_schedule_form"):
    st.write(f"目前已輸入 **{len(current_teams)}** 支隊伍。")
    draw_btn = st.form_submit_button("🎲 抽取賽程", type="primary", use_container_width=True)

if draw_btn:
    if len(current_teams) < 2:
        st.error("至少需要 2 支隊伍。")
    elif len(current_teams) != len(set(current_teams)):
        st.error("隊伍名稱有重複，請檢查輸入。")
    else:
        st.session_state["draw_result"] = build_draw_result(current_teams)
        st.session_state["draw_input_snapshot"] = current_teams[:]
        st.session_state["draw_result_invalidated"] = False

if st.session_state["draw_result"]:
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("🔄 重新抽取", use_container_width=True):
            if len(current_teams) < 2:
                st.error("至少需要 2 支隊伍。")
            elif len(current_teams) != len(set(current_teams)):
                st.error("隊伍名稱有重複，請檢查輸入。")
            else:
                st.session_state["draw_result"] = build_draw_result(current_teams)
                st.session_state["draw_input_snapshot"] = current_teams[:]
                st.session_state["draw_result_invalidated"] = False
    with col2:
        if st.button("🧹 清空結果", use_container_width=True):
            st.session_state["draw_result"] = None
            st.session_state["draw_input_snapshot"] = None
            st.session_state["draw_result_invalidated"] = False

if st.session_state["draw_result"]:
    result = st.session_state["draw_result"]
    main_byes_by_round = {item["round"]: item["team"] for item in result["main_byes"]}

    st.divider()
    st.subheader("本次抽籤隊伍快照")
    snap_col1, snap_col2 = st.columns([1, 2])
    with snap_col1:
        st.metric("參賽隊伍總數", len(result["teams"]))
    with snap_col2:
        st.dataframe(
            pd.DataFrame({"隊伍名稱": result["teams"]}),
            use_container_width=True,
            hide_index=True
        )

    st.divider()
    st.subheader("正賽")
    if result["direct_final"]:
        st.info("本次只有 2 支隊伍，毋須進行正賽及負方賽，直接進入決賽。")
    else:
        for round_data in result["main_rounds"]:
            st.write(f"**{round_data['title']}**")
            for pair in round_data["pairs"]:
                with st.container(border=True):
                    c1, c2, c3 = st.columns([2, 1, 2])
                    with c1:
                        st.markdown(f"**{pair['left']}**")
                    with c2:
                        st.markdown("<div style='text-align:center'>vs</div>", unsafe_allow_html=True)
                    with c3:
                        st.markdown(f"**{pair['right']}**")
                st.caption(f"場次 {pair['match_no']}")
            if round_data["round"] in main_byes_by_round:
                st.info(f"輪空：**{main_byes_by_round[round_data['round']]}**（直接晉級下一輪）")

    st.divider()
    st.subheader("負方賽")
    if result["direct_final"]:
        st.write("本次只有 2 支隊伍，毋須進行負方賽。")
    else:
        for round_data in result["loser_rounds"]:
            st.write(f"**{round_data['title']}**")
            if round_data["note"]:
                st.info(round_data["note"])
            for pair in round_data["pairs"]:
                with st.container(border=True):
                    c1, c2, c3 = st.columns([2, 1, 2])
                    with c1:
                        st.markdown(f"**{pair['left']}**")
                    with c2:
                        st.markdown("<div style='text-align:center'>vs</div>", unsafe_allow_html=True)
                    with c3:
                        st.markdown(f"**{pair['right']}**")
                st.caption(f"場次 {pair['match_no']}")
            if round_data["bye"]:
                st.info(f"輪空：**{round_data['bye']}**（直接晉級下一輪）")

    st.divider()
    st.subheader("決賽")
    with st.container(border=True):
        c1, c2, c3 = st.columns([2, 1, 2])
        with c1:
            st.markdown(f"**{result['final_match']['left']}**")
        with c2:
            st.markdown("<div style='text-align:center'>vs</div>", unsafe_allow_html=True)
        with c3:
            st.markdown(f"**{result['final_match']['right']}**")

    st.divider()
    st.subheader("賽程總表")
    st.caption("以下為預覽結果，方便人工抄錄到「比賽場次管理」頁面。")
    st.dataframe(
        pd.DataFrame(result["summary_rows"]),
        use_container_width=True,
        hide_index=True
    )
