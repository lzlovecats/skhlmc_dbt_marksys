import streamlit as st
from functions import (
    check_committee_login,
    committee_cookie_manager,
    del_cookie,
    load_matches_from_db,
    render_page_guidance,
    get_connection,
    DIFFICULTY_OPTIONS,
)
from schema import TABLE_TOPICS
from ai_coach_helpers import (
    review_speech,
    brainstorm_strategy,
    POSITION_LABELS,
    AI_MODEL_OPTIONS,
    DEFAULT_AI_MODEL,
    format_ai_model_label,
)

st.header("✨AI 辯論易")

render_page_guidance(
    [
        "使用「發言檢查」模式輸入文字稿或錄音，AI 會按照正式評分標準提供反饋。",
        "使用「主線策劃」模式，AI 可根據辯題及立場生成論點及應對策略。",
        "可選擇 Gemini 或 OpenAI 模型；錄音分析目前只支援 Gemini 模型。",
        "請節約使用高級或收費模型；一般草擬及練習可先使用 Flash 模型。",
        "可選擇從系統場次載入比賽資料，或手動輸入外部比賽嘅辯題。",
        "此功能使用 AI 生成，僅供參考，不代表評判觀點。",
    ],
    title="首次使用指南",
)

if not check_committee_login():
    st.stop()

user_id = st.session_state["committee_user"]

if user_id == "admin":
    st.error("賽會人員帳戶不能使用此頁面。請改用內部委員會成員帳戶登入。")
    if st.button("登出"):
        st.session_state["committee_user"] = None
        del_cookie(committee_cookie_manager(), "committee_user")
        st.rerun()
    st.stop()

model_options = list(AI_MODEL_OPTIONS.keys())
model_label = st.selectbox(
    "AI 模型",
    options=model_options,
    index=model_options.index(DEFAULT_AI_MODEL),
    format_func=format_ai_model_label,
    key="ai_model",
)
model_config = AI_MODEL_OPTIONS[model_label]
st.caption(f"收費狀態：{model_config['pricing_label']}。{model_config['pricing_note']}")
st.info("請節約使用高級或收費模型；一般草擬及練習建議先用 Flash 模型，複雜策略或重要稿件先用 Pro / GPT 模型。")
if model_config.get("is_premium"):
    st.warning("你正在使用高級模型。請確認今次任務需要較高成本模型後再提交。")
if model_config["api_key"] not in st.secrets:
    st.warning(f"未設定 {model_config['api_key']}，此模型暫時無法使用。")
if not model_config["supports_audio"]:
    st.caption("此模型只會分析文字稿；如需錄音分析，請選擇 Gemini 模型。")

tab_review, tab_strategy = st.tabs(["📝 發言檢查", "💡 主線策劃"])

# ─── Tab 1: 發言檢查 ───────────────────────────────────────────────

with tab_review:
    source = st.radio(
        "比賽資料來源",
        ["從系統場次載入", "手動輸入（外部比賽）"],
        horizontal=True,
        key="review_source",
    )

    selected_match_id = None
    manual_topic = None
    review_side = None

    if source == "從系統場次載入":
        all_matches = load_matches_from_db()
        if not all_matches:
            st.info("目前未有比賽場次。請選擇「手動輸入」。")
        else:
            match_options = list(all_matches.keys())
            selected_match_id = st.selectbox(
                "選擇場次", match_options, key="review_match"
            )
            m = all_matches[selected_match_id]
            if m.get("topic_text"):
                st.caption(f"辯題：{m['topic_text']}")
            col_teams = st.columns(2)
            with col_teams[0]:
                if m.get("pro_team"):
                    st.caption(f"正方：{m['pro_team']}")
            with col_teams[1]:
                if m.get("con_team"):
                    st.caption(f"反方：{m['con_team']}")

            review_side = st.radio(
                "立場", ["正方", "反方"], horizontal=True, key="review_side"
            )
    else:
        manual_topic = st.text_input("辯題", key="review_manual_topic")
        review_side = st.radio(
            "立場", ["正方", "反方"], horizontal=True, key="review_side_manual"
        )

    position = st.selectbox(
        "辯位",
        options=[1, 2, 3, 4],
        format_func=lambda x: POSITION_LABELS[x],
        key="review_position",
    )

    st.divider()

    speech_text = st.text_area(
        "輸入文字稿",
        height=200,
        placeholder="輸入內容...",
        key="review_text",
    )
    audio_data = st.audio_input("錄音）", key="review_audio")

    if st.button("分析發言", type="primary", use_container_width=True, key="review_submit"):
        if not speech_text and audio_data is None:
            st.warning("請輸入文字稿或錄音。")
        elif not review_side:
            st.warning("請選擇立場。")
        elif source == "手動輸入（外部比賽）" and not manual_topic:
            st.warning("請輸入辯題。")
        else:
            audio_bytes = None
            if audio_data is not None:
                audio_bytes = audio_data.read()
                if len(audio_bytes) > 15 * 1024 * 1024:
                    st.error("錄音檔案過大（超過 15MB），請縮短錄音時間後重試。")
                    st.stop()

            with st.spinner("AI 分析中..."):
                result = review_speech(
                    text=speech_text or None,
                    audio_bytes=audio_bytes,
                    side=review_side,
                    position=position,
                    match_id=selected_match_id,
                    manual_topic=manual_topic,
                    model_label=model_label,
                )

            st.divider()
            st.subheader("分析結果")
            st.markdown(result)

# ─── Tab 2: 主線策劃 ───────────────────────────────────────────────

with tab_strategy:
    topic_source = st.radio(
        "辯題來源",
        ["手動輸入", "從辯題庫選擇"],
        horizontal=True,
        key="strategy_topic_source",
    )

    topic_text = ""

    if topic_source == "從辯題庫選擇":
        try:
            conn = get_connection()
            topics_df = conn.query(
                f"SELECT topic_text, category, difficulty FROM {TABLE_TOPICS}",
                ttl=120,
            )
        except Exception as e:
            st.error(f"無法讀取辯題庫：{e}")
            topics_df = None

        if topics_df is not None and not topics_df.empty:
            topics_df["display"] = topics_df.apply(
                lambda r: f"[{r.get('category', '')}] {r['topic_text']}", axis=1
            )
            selected_display = st.selectbox(
                "選擇辯題",
                topics_df["display"].tolist(),
                key="strategy_topic_select",
            )
            idx = topics_df["display"].tolist().index(selected_display)
            topic_text = topics_df.iloc[idx]["topic_text"]
            diff = topics_df.iloc[idx].get("difficulty")
            if diff and diff in DIFFICULTY_OPTIONS:
                st.caption(f"難度：{DIFFICULTY_OPTIONS[diff]}")
        else:
            st.info("辯題庫為空，請手動輸入辯題。")
            topic_source = "手動輸入"

    if topic_source == "手動輸入":
        topic_text = st.text_input("輸入辯題", key="strategy_manual_topic")

    strategy_side = st.radio(
        "立場", ["正方", "反方"], horizontal=True, key="strategy_side"
    )

    if st.button("生成主線", type="primary", use_container_width=True, key="strategy_submit"):
        if not topic_text:
            st.warning("請輸入或選擇辯題。")
        else:
            with st.spinner("AI 策劃中..."):
                result = brainstorm_strategy(
                    topic=topic_text,
                    side=strategy_side,
                    model_label=model_label,
                )

            st.divider()
            st.subheader("策略建議")
            st.markdown(result)

            st.download_button(
                "下載策略建議",
                data=result,
                file_name="策略建議.txt",
                mime="text/plain",
                use_container_width=True,
            )
