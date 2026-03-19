import streamlit as st
import random
from functions import check_admin

st.header("抽取賽程")

if not check_admin():
    st.stop()

if "draw_result" not in st.session_state:
    st.session_state["draw_result"] = None

# input teams
st.subheader("輸入參賽隊伍")
teams_input = st.text_area(
    "每行輸入一支隊伍名稱",
    placeholder="例如：\n思若秋澄\n請你食薯餅\n預設名稱一",
    height=160
)

col1, col2 = st.columns([1, 1])
with col1:
    draw_btn = st.button("🎲 抽取賽程", type="primary", use_container_width=True)
with col2:
    if st.button("🔄 重新抽取", use_container_width=True):
        st.session_state["draw_result"] = None
        st.rerun()

if draw_btn:
    teams = [t.strip() for t in teams_input.strip().splitlines() if t.strip()]
    if len(teams) < 2:
        st.error("至少需要 2 支隊伍。")
    elif len(teams) != len(set(teams)):
        st.error("隊伍名稱有重複，請檢查輸入。")
    else:
        shuffled = teams[:]
        random.shuffle(shuffled)

        # 處理輪空（奇數隊）
        bye_team = None
        if len(shuffled) % 2 == 1:
            bye_team = shuffled[-1]
            playing = shuffled[:-1]
        else:
            playing = shuffled[:]

        # 第一輪配對
        r1 = [(playing[i], playing[i + 1]) for i in range(0, len(playing), 2)]

        # 負方賽：按場次順序配對，多於兩輪則繼續配對直至剩一隊
        lb_rounds = []
        current_labels = [f"第一輪場次{i}負方" for i in range(1, len(r1) + 1)]
        round_num = 1

        while len(current_labels) > 1:
            pairs = []
            lb_bye = None
            if len(current_labels) % 2 == 1:
                lb_bye = current_labels[-1]
                playing_lb = current_labels[:-1]
            else:
                playing_lb = current_labels[:]

            next_labels = []
            for i in range(0, len(playing_lb), 2):
                pairs.append((playing_lb[i], playing_lb[i + 1]))
                next_labels.append(f"負方賽R{round_num}場次{i // 2 + 1}勝方")
            if lb_bye:
                next_labels.append(lb_bye)

            lb_rounds.append({"round": round_num, "pairs": pairs, "bye": lb_bye})
            current_labels = next_labels
            round_num += 1

        st.session_state["draw_result"] = {
            "teams": teams,
            "r1": r1,
            "bye_team": bye_team,
            "lb_rounds": lb_rounds,
            "main_count": len(r1) + (1 if bye_team else 0),
        }
        st.rerun()

# ── 顯示結果 ──────────────────────────────────────
if st.session_state["draw_result"]:
    result = st.session_state["draw_result"]
    r1 = result["r1"]
    bye_team = result["bye_team"]
    lb_rounds = result["lb_rounds"]
    main_count = result["main_count"]
    n_teams = len(result["teams"])

    st.divider()

    # 第一輪
    st.subheader("第一輪 正賽")
    for i, (a, b) in enumerate(r1, 1):
        with st.container(border=True):
            c1, c2, c3 = st.columns([2, 1, 2])
            with c1:
                st.markdown(f"**{a}**")
            with c2:
                st.markdown("<div style='text-align:center'>vs</div>", unsafe_allow_html=True)
            with c3:
                st.markdown(f"**{b}**")
        st.caption(f"場次 {i}")

    if bye_team:
        st.info(f"輪空（直接晉級正賽後段）：**{bye_team}**")

    # 負方賽
    st.divider()
    st.subheader("負方賽（第一輪落敗隊伍）")

    if len(r1) == 1:
        st.write("第一輪只有一場比賽，落敗隊伍直接以負方賽代表身份晉級決賽。")
    else:
        for lb in lb_rounds:
            round_label = "負方賽" if lb["round"] == 1 else f"負方賽 第{lb['round']}輪"
            st.write(f"**▸ {round_label}**")
            for j, (a, b) in enumerate(lb["pairs"], 1):
                st.write(f"　場次 {j}：{a}  　vs　  {b}")
            if lb["bye"]:
                st.caption(f"　輪空：{lb['bye']}")
        st.success("負方賽冠軍直接晉級決賽")

    # 決賽
    st.divider()
    st.subheader("決賽")
    with st.container(border=True):
        c1, c2, c3 = st.columns([2, 1, 2])
        with c1:
            st.markdown("**正賽冠軍**")
        with c2:
            st.markdown("<div style='text-align:center'>vs</div>", unsafe_allow_html=True)
        with c3:
            st.markdown("**負方賽冠軍**")

    # 流程總覽
    st.divider()
    st.subheader("賽程總流程")
    st.markdown(f"""
| 階段 | 詳情 |
|------|------|
| 第一輪正賽 | 共 {len(r1)} 場，{n_teams} 支隊伍{f"（{bye_team} 輪空）" if bye_team else ""} |
| 正賽後段 | 第一輪勝方{f"及輪空隊伍，" if bye_team else "，"}共 {main_count} 支隊伍繼續晉級 |
| 負方賽 | 第一輪 {len(r1)} 支敗方按場次順序配對，決出負方賽冠軍 |
| 決賽 | 正賽冠軍 vs 負方賽冠軍，勝方為總冠軍 |
""")
    st.caption("※ 負方賽冠軍直接晉級決賽，毋須打正賽後段。")
