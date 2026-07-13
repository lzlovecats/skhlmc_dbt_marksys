import streamlit as st
import base64
import json
import math
import os
import secrets
from pathlib import Path
import httpx
import streamlit.components.v1 as components
from speech_recorder_component import render_speech_recorder
from auth import require_committee, sign_relay_token, committee_bearer_token
from functions import (
    load_matches_from_db,
    render_page_guidance,
    get_connection,
    DIFFICULTY_OPTIONS,
)
from schema import TABLE_TOPICS
from ai_coach_helpers import (
    review_speech,
    brainstorm_strategy,
    research_web,
    fact_check_claim,
    ensure_ai_fund_tables,
    estimate_ai_feature_usage,
    log_ai_fund_usage,
    is_successful_ai_result,
    get_ai_fund_summary,
    POSITION_LABELS,
    get_ai_model_settings,
    format_ai_model_label,
    format_ai_model_usage_note,
    format_usd_money,
    format_hkd_money,
    build_free_debate_live_prompt,
    build_full_mock_live_prompt,
    create_gemini_live_ephemeral_token,
    create_gemini_live_ephemeral_tokens,
    FREE_DEBATE_LIVE_MODEL_LABEL,
)
from debate_timing import (
    DEBATE_FORMATS,
    FREE_DEBATE_FORMATS,
    get_debate_timer_config,
    get_full_mock_sequence,
    full_mock_total_seconds,
    split_mock_into_sessions,
)
from prompts import LIVE_RUNTIME_PROMPTS, build_live_research_need_prompt


def _format_hkd(amount) -> str:
    return format_hkd_money(amount)


def _format_hkd_4dp(amount) -> str:
    return format_hkd_money(amount, decimals=4)


def _format_ai_estimate(feature: str, model_label: str, has_audio: bool = False, duration_minutes: float | None = None) -> str:
    usage = estimate_ai_feature_usage(
        feature,
        model_label,
        has_audio=has_audio,
        duration_minutes=duration_minutes,
    )
    search_note = "，按 1 次搜尋工具估算" if usage["search_calls"] else ""
    usd = usage.get("estimated_cost_usd", 0)
    hkd = usage["estimated_cost_hkd"]
    return f"估算成本：{format_usd_money(usd, decimals=4, escape_markdown=True)} ≈ {_format_hkd_4dp(hkd)} / 次{search_note}。"


def _azure_tts_configured() -> bool:
    return bool(st.secrets.get("AZURE_SPEECH_KEY") and st.secrets.get("AZURE_SPEECH_REGION"))


def _record_ai_usage(user_id: str, feature: str, model_label: str, result: str, has_audio: bool = False, usage: dict | None = None):
    success = is_successful_ai_result(result)
    try:
        log_ai_fund_usage(
            user_id=user_id,
            feature=feature,
            model_label=model_label,
            success=success,
            has_audio=has_audio,
            error_message="" if success else result,
            usage_override=usage if success else None,
        )
    except Exception as e:
        st.caption(f"AI 用量記錄未能寫入：{e}")


def _trim_live_research(text: str, max_chars: int = 4500) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0] + "\n（賽前資料摘要已截短）"


def _prepare_live_research(topic: str, user_side: str, ai_side: str, debate_format: str, mode_label: str, model_label: str, user_id: str) -> str:
    research_need = build_live_research_need_prompt(mode_label, user_side, ai_side, debate_format)
    result, actual_usage = research_web(
        topic=topic,
        research_need=research_need,
        model_label=model_label,
    )
    _record_ai_usage(user_id, "web_research", model_label, result, usage=actual_usage)
    if is_successful_ai_result(result):
        return _trim_live_research(result)
    st.caption(f"賽前搵料未能加入，會用辯題常識繼續：{result}")
    return ""


def _load_bell_src() -> str:
    try:
        bell_path = Path(__file__).parent / "assets" / "bell.mp3"
        return "data:audio/mpeg;base64," + base64.b64encode(bell_path.read_bytes()).decode()
    except FileNotFoundError:
        return ""


def _render_live_debate_component(
    token: str,
    model: str,
    system_prompt: str,
    duration_minutes: float = 2.5,
    bell_schedule=None,
    session_label: str = "自由辯論",
    segments=None,
    tokens=None,
    session_labels=None,
    ai_starts: bool = False,
    relay_user_id: str = "",
    relay_practice_kind: str = "solo_free",
    relay_practice_id: str = "",
    relay_max_seconds_by_token=None,
):
    template_path = Path(__file__).parent / "templates" / "live_debate.html"
    live_html = template_path.read_text(encoding="utf-8")
    if session_label != "自由辯論":
        live_html = live_html.replace("自由辯論", session_label)
    # 設定咗 LIVE_RELAY_WS_BASE（例如 wss://<render-domain>/gemini-live）時，瀏覽器
    # 會經 Render(Singapore) relay 連 Gemini Live，令香港等受限地區都用得；未設定
    # 時 fallback 直連 Google。
    try:
        relay_ws_base = st.secrets.get("LIVE_RELAY_WS_BASE", "") or ""
    except Exception:
        relay_ws_base = ""
    live_html = live_html.replace("__RELAY_WS_BASE__", json.dumps(relay_ws_base))
    # relay 模式下，為每粒 token 簽一個 HMAC，令 relay 只服務本 app 發出嘅 token
    # （見 auth.sign_relay_token / proxy._verify_relay_signature）。
    token_sigs = {}
    if relay_ws_base:
        try:
            seconds_by_token = relay_max_seconds_by_token or {}
            default_seconds = max(30, min(int(float(duration_minutes or 2.5) * 60), 30 * 60))
            for t in [token, *(tokens or [])]:
                if t and t not in token_sigs:
                    token_sigs[t] = sign_relay_token(
                        t, relay_user_id, relay_practice_kind,
                        int(seconds_by_token.get(t) or default_seconds),
                        relay_practice_id,
                    )
        except Exception as e:
            st.caption(f"Live relay 簽章未能產生：{e}")
    live_html = live_html.replace("__TOKEN_SIGS__", json.dumps(token_sigs))
    bell_src = _load_bell_src()
    live_html = live_html.replace("__LIVE_TOKEN__", json.dumps(token))
    live_html = live_html.replace("__LIVE_MODEL__", json.dumps(model))
    live_html = live_html.replace("__LIVE_PROMPT__", json.dumps(system_prompt, ensure_ascii=False))
    live_html = live_html.replace("__LIVE_MINUTES__", json.dumps(float(duration_minutes or 2.5)))
    live_html = live_html.replace("__BELL_SRC__", json.dumps(bell_src))
    live_html = live_html.replace("__BELL_SCHEDULE__", json.dumps(bell_schedule or [], ensure_ascii=False))
    live_html = live_html.replace("__MOCK_SEGMENTS__", json.dumps(segments or [], ensure_ascii=False))
    live_html = live_html.replace("__MOCK_TOKENS__", json.dumps(tokens or [], ensure_ascii=False))
    live_html = live_html.replace("__MOCK_SESSION_LABELS__", json.dumps(session_labels or [], ensure_ascii=False))
    live_html = live_html.replace("__AI_STARTS__", json.dumps(bool(ai_starts)))
    # 注入喺「自由辯論→session_label」替換之後，令 runtime prompt 唔會被該替換污染。
    live_html = live_html.replace("__LIVE_PROMPTS__", json.dumps(LIVE_RUNTIME_PROMPTS, ensure_ascii=False))
    height = 980 if segments else 860
    components.html(live_html, height=height, scrolling=True)


def _room_api_base() -> str:
    """連線房間狀態位於 proxy 進程（與 Streamlit 不同進程），需經 HTTP 調用。
    容器內 proxy 監聽 $PORT；本地沒有 proxy 時可用 ROOM_API_BASE 覆寫。"""
    override = os.getenv("ROOM_API_BASE")
    if override:
        return override.rstrip("/")
    port = os.getenv("PORT", "8000")
    return f"http://127.0.0.1:{port}"


def _room_api_post(path: str, payload: dict, user_id: str):
    """以委員 Bearer token 調用 proxy 的房間 API，回傳 (ok, data_or_message)。"""
    try:
        token = committee_bearer_token(user_id)
    except Exception as e:
        return False, f"未能簽發委員 token：{e}"
    try:
        resp = httpx.post(
            f"{_room_api_base()}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except Exception as e:
        return False, f"未能連接房間服務：{e}"
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = resp.text[:200]
        return False, detail or f"房間服務錯誤（{resp.status_code}）"
    try:
        return True, resp.json()
    except Exception:
        return False, "房間服務回應無效"


def _room_api_get(path: str, user_id: str):
    try:
        token = committee_bearer_token(user_id)
        resp = httpx.get(
            f"{_room_api_base()}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except Exception as e:
        return False, f"未能連接房間服務：{e}"
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = resp.text[:200]
        return False, detail or f"房間服務錯誤（{resp.status_code}）"
    try:
        return True, resp.json()
    except Exception:
        return False, "房間服務回應無效"


def _render_room_debate_component(code: str, mode: str):
    template_path = Path(__file__).parent / "templates" / "room_debate.html"
    room_html = template_path.read_text(encoding="utf-8")
    try:
        room_ws_base = st.secrets.get("ROOM_WS_BASE", "") or ""
    except Exception:
        room_ws_base = ""
    room_html = room_html.replace("__ROOM_CODE__", json.dumps(code))
    room_html = room_html.replace("__ROOM_WS_BASE__", json.dumps(room_ws_base))
    room_html = room_html.replace("__MODE__", json.dumps(mode))
    room_html = room_html.replace("__BELL_SRC__", json.dumps(_load_bell_src()))
    components.html(room_html, height=900, scrolling=True)


def _render_speech_recorder(key: str, bell_schedule=None):
    return render_speech_recorder(
        key=key,
        bell_src=_load_bell_src(),
        bell_schedule=bell_schedule or [],
    )


st.header("✨AI 辯論易")

render_page_guidance(
    [
        "使用「練習發言」輸入文字稿或錄音，AI 會按照正式評分標準提供反饋。",
        "使用「主線策劃」模式，AI 可根據辯題及你的立場生成論點及應對策略。",
        "使用「搵料易」模式，AI 會即時搜尋網上資料並附上來源。",
        "使用「Fact Check易」模式，AI 會搜尋來源並核查陳述真偽。",
    ],
    title="首次使用指南",
)

user_id = require_committee()

ai_model_settings = get_ai_model_settings()
model_options = list(ai_model_settings["model_options"].keys())
if not model_options:
    st.error("未有可用 AI 模型，請聯絡開發人員檢查 AI Provider 設定。")
    st.stop()
default_model = ai_model_settings["default_model"]
if st.session_state.get("ai_model") not in model_options:
    st.session_state["ai_model"] = default_model
model_label = st.selectbox(
    "AI 模型",
    options=model_options,
    index=model_options.index(default_model),
    format_func=format_ai_model_label,
    key="ai_model",
)
model_config = ai_model_settings["model_options"][model_label]
st.caption(f"收費狀態：{model_config['pricing_label']}。{model_config['pricing_note']}")
with st.expander("模型限額及成本估算", expanded=True):
    st.markdown(format_ai_model_usage_note(model_label))
st.info("日常練習請用 Gemini 2.5 Flash，深入分析用 Gemini 3.1 Pro。重要稿件先用GPT-5.4 Mini。")
if model_config.get("is_premium"):
    st.warning("你正在使用高級模型。請確保不要濫用，避免資金用盡。")

if model_config["api_key"] not in st.secrets:
    st.warning(f"未設定 {model_config['api_key']}，呢個模型暫時未能使用。")

fund_summary_preview = get_ai_fund_summary() if ensure_ai_fund_tables() else {}
if (
    fund_summary_preview
    and fund_summary_preview["balance_hkd"] < fund_summary_preview["low_balance_hkd"]
):
    st.warning(
        "AI基金餘額偏低："
        f"{_format_hkd(fund_summary_preview['balance_hkd'])}。"
        "建議新增資金。"
    )
    st.page_link("ai_fund.py", label="前往 AI基金 管理入數 / 查閱用量", icon="💲")

_tab_options = ["strategy", "review", "fact_check", "research", "free_debate", "full_mock", "live_room"]


def format_ai_coach_tab(tab_name):
    if tab_name == "strategy":
        return "💡 主線策劃"
    if tab_name == "review":
        return "📝 練習發言"
    if tab_name == "fact_check":
        return "✅ Fact Check易"
    if tab_name == "research":
        return "🌐 搵料易"
    if tab_name == "free_debate":
        return "🎙️ 打Free De"
    if tab_name == "full_mock":
        return "🏟️ 打Mock"
    return "🎧 連線練習"


if hasattr(st, "segmented_control"):
    selected_tab = st.segmented_control(
        "頁面",
        options=_tab_options,
        default="strategy",
        format_func=format_ai_coach_tab,
        key="ai_coach_selected_tab",
        label_visibility="collapsed",
        width="stretch",
    )
else:
    selected_tab = st.radio(
        "頁面",
        options=_tab_options,
        format_func=format_ai_coach_tab,
        key="ai_coach_selected_tab",
        horizontal=True,
        label_visibility="collapsed",
    )

if selected_tab is None:
    selected_tab = "strategy"

# ─── Tab 1: 練習發言 ───────────────────────────────────────────────

if selected_tab == "review":
    if not model_config["supports_audio"]:
        st.warning("呢個模型不支援錄音分析。如需錄音分析，請選擇支援錄音嘅模型（如 Gemini 系列）。")

    source = st.radio(
        "辯題來源",
        ["手動輸入", "從系統場次載入"],
        horizontal=True,
        key="review_source",
    )

    selected_match_id = None
    manual_topic = None
    review_side = None

    if source == "從系統場次載入":
        all_matches = load_matches_from_db()
        if not all_matches:
            st.info("而家未有比賽場次。請選擇「手動輸入」。")
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
                "你的立場", ["正方", "反方"], horizontal=True, key="review_side"
            )
    else:
        manual_topic = st.text_input("辯題", key="review_manual_topic")
        review_side = st.radio(
            "你的立場", ["正方", "反方"], horizontal=True, key="review_side_manual"
        )

    is_manual_review = source == "手動輸入"
    position_options = [1, 2, 3, 4, 5] if is_manual_review else [1, 2, 3, 4]
    position = st.selectbox(
        "辯位",
        options=position_options,
        format_func=lambda x: POSITION_LABELS[x],
        key="review_position_manual" if is_manual_review else "review_position",
    )

    review_debate_format = st.selectbox(
        "賽制",
        options=DEBATE_FORMATS,
        key="review_debate_format",
    )
    review_timer_config = get_debate_timer_config(review_debate_format)
    review_stage_labels = dict(review_timer_config["timer_stages"])
    review_bell_stage = None
    review_mode = st.radio(
        "練習類型",
        ["台上發言", "台下發問", "交互答問"],
        horizontal=True,
        key="review_mode",
    )
    qa_warning = None
    qa_text_lines = []
    opposite_side = "反方" if review_side == "正方" else "正方"

    if review_mode == "台上發言":
        if review_debate_format == "星島":
            review_bell_stage = "deputy" if position == 4 else "main"
        else:
            review_bell_stage = "main" if position in (1, 4, 5) else "deputy"

    if review_mode == "台下發問":
        if review_debate_format == "聯中":
            review_bell_stage = st.radio(
                "台下計時",
                ["floor_question", "floor_prep", "floor_answer"],
                format_func=lambda x: review_stage_labels.get(x, x),
                horizontal=True,
                key="review_floor_timer_stage",
            )
        floor_mode = st.radio(
            "台下發問模式",
            ["我問，AI 答", "AI 問一條問題，我答"],
            horizontal=True,
            key="floor_question_mode",
        )
        qa_text_lines.append("## 台下發問練習")
        qa_text_lines.append(f"模式：{floor_mode}")
        qa_text_lines.append(f"你嘅角色係{opposite_side}辯員，請以{opposite_side}立場參與問答。")

        if floor_mode == "我問，AI 答":
            floor_question = st.text_area(
                "我嘅問題",
                height=120,
                placeholder="輸入你想向對方或 AI 提出嘅問題...",
                key="floor_user_question",
            )
            if floor_question:
                qa_text_lines.append(f"我嘅問題：{floor_question}")
                qa_text_lines.append("請以對方辯員身分回答呢條問題，再評估問題係咪清晰、尖銳、有追問空間。")
            else:
                qa_warning = "請輸入你想問 AI 嘅問題。"
        else:
            floor_ai_question = st.text_area(
                "AI / 對方問題（可留空，AI 會先問）",
                height=100,
                placeholder="如已有題目，可貼上問題；如留空，AI 會根據辯題先問一條問題。",
                key="floor_ai_question",
            )
            floor_user_answer = st.text_area(
                "我嘅回答（如想 AI 先問，可留空）",
                height=140,
                placeholder="輸入你對問題嘅回答...",
                key="floor_user_answer",
            )
            if floor_ai_question:
                qa_text_lines.append(f"AI / 對方問題：{floor_ai_question}")
            if floor_user_answer:
                qa_text_lines.append(f"我嘅回答：{floor_user_answer}")
                qa_text_lines.append("請評估我嘅回答，並指出點樣答得更直接、更有防守力。")
            elif floor_ai_question:
                qa_text_lines.append("我未提供回答；請重申呢條問題，提示我可以由咩方向作答，暫時毋須評分。")
            else:
                qa_text_lines.append(f"我未提供回答；請以{opposite_side}辯員身分，根據辯題向我提出一條台下發問問題，暫時毋須評分。")

    elif review_mode == "交互答問":
        if review_debate_format == "星島":
            review_bell_stage = st.radio(
                "交互計時",
                ["prep", "question", "answer"],
                format_func=lambda x: review_stage_labels.get(x, x),
                horizontal=True,
                key="review_exchange_timer_stage",
            )
        exchange_order = st.radio(
            "交互次序",
            ["我問，AI 答＋問，我再答", "AI 問，我答＋問，AI 再答"],
            horizontal=True,
            key="exchange_order",
        )
        qa_text_lines.append("## 交互答問練習")
        qa_text_lines.append(f"交互次序：{exchange_order}")
        qa_text_lines.append(f"你嘅角色係{opposite_side}辯員，請以{opposite_side}立場參與問答。")

        if exchange_order == "我問，AI 答＋問，我再答":
            exchange_user_question = st.text_area(
                "我嘅問題",
                height=110,
                placeholder="輸入你想先問嘅問題...",
                key="exchange_user_question",
            )
            exchange_user_final_answer = st.text_area(
                "我對 AI 追問嘅回答（可留空，AI 會先答＋追問）",
                height=130,
                placeholder="如你已經想練埋第二輪回答，可在此輸入。",
                key="exchange_user_final_answer",
            )
            if exchange_user_question:
                qa_text_lines.append(f"我嘅問題：{exchange_user_question}")
                qa_text_lines.append("請以對方辯員身分回答我嘅問題，然後追問我一條相關問題。")
            else:
                qa_warning = "請輸入你想先問 AI 嘅問題。"
            if exchange_user_final_answer:
                qa_text_lines.append(f"我對追問嘅回答：{exchange_user_final_answer}")
                qa_text_lines.append("請同時評估我嘅提問同回答。")
        else:
            exchange_ai_question = st.text_area(
                "AI / 對方問題（可留空，AI 會先問）",
                height=100,
                placeholder="如已有問題，可貼上；如留空，AI 會先問一條問題。",
                key="exchange_ai_question",
            )
            exchange_user_answer = st.text_area(
                "我嘅回答",
                height=120,
                placeholder="輸入你對第一條問題嘅回答...",
                key="exchange_user_answer",
            )
            exchange_user_follow_up = st.text_area(
                "我嘅追問",
                height=100,
                placeholder="輸入你回答後想反問 AI / 對方嘅問題...",
                key="exchange_user_follow_up",
            )
            if exchange_ai_question:
                qa_text_lines.append(f"AI / 對方問題：{exchange_ai_question}")
            if exchange_user_answer:
                qa_text_lines.append(f"我嘅回答：{exchange_user_answer}")
            if exchange_user_follow_up:
                qa_text_lines.append(f"我嘅追問：{exchange_user_follow_up}")
            if exchange_user_answer and exchange_user_follow_up:
                qa_text_lines.append("請以對方辯員身分回答我嘅追問，並評估我嘅回答同追問質素。")
            elif exchange_ai_question and not exchange_user_answer:
                qa_warning = "已有對方問題，請輸入你嘅回答。"
            else:
                qa_text_lines.append(f"我未完成回答及追問；請以{opposite_side}辯員身分，根據辯題向我提出一條交互答問問題，暫時毋須評分。")

    review_bell_schedule = review_timer_config["bell_schedules"].get(review_bell_stage or "", [])
    if review_bell_stage:
        st.caption(f"錄音計時：{review_stage_labels.get(review_bell_stage, review_bell_stage)}。")

    st.divider()

    speech_text = st.text_area(
        "輸入文字稿" if review_mode == "台上發言" else "補充文字稿（可選）",
        height=200,
        placeholder="輸入內容...",
        key="review_text",
    )
    audio_data = _render_speech_recorder("review_audio_recorder", review_bell_schedule)
    has_review_audio = bool(audio_data and audio_data.get("audio_base64"))
    st.caption(_format_ai_estimate("speech_review", model_label, has_audio=has_review_audio))

    review_text_parts = []
    if speech_text:
        review_text_parts.append(speech_text)
    if qa_text_lines:
        review_text_parts.append("\n".join(qa_text_lines))
    review_text_for_ai = "\n\n".join(review_text_parts)

    if st.button("分析發言", type="primary", width="stretch", key="review_submit"):
        if qa_warning:
            st.warning(qa_warning)
        elif not review_text_for_ai and not has_review_audio:
            st.warning("請輸入文字稿或錄音。")
        elif not review_side:
            st.warning("請選擇你的立場。")
        elif source == "手動輸入" and not manual_topic:
            st.warning("請輸入辯題。")
        else:
            audio_bytes = None
            if has_review_audio:
                audio_bytes = base64.b64decode(audio_data["audio_base64"])
                if len(audio_bytes) > 15 * 1024 * 1024:
                    st.error("錄音檔案過大（超過 15MB），請縮短錄音時間後重試。")
                    st.stop()

            with st.spinner("AI 分析中..."):
                result, actual_usage = review_speech(
                    text=review_text_for_ai or None,
                    audio_bytes=audio_bytes,
                    side=review_side,
                    position=position,
                    match_id=selected_match_id,
                    manual_topic=manual_topic,
                    model_label=model_label,
                )
                _record_ai_usage(
                    user_id,
                    "speech_review",
                    model_label,
                    result,
                    has_audio=audio_bytes is not None,
                    usage=actual_usage,
                )

            st.divider()
            st.subheader("分析結果")
            st.markdown(result)

# ─── Tab 2: 主線策劃 ───────────────────────────────────────────────

if selected_tab == "strategy":
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
            st.error(f"未能讀取辯題庫：{e}")
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
        "你的立場", ["正方", "反方"], horizontal=True, key="strategy_side"
    )
    strategy_debate_format = st.selectbox(
        "賽制", options=DEBATE_FORMATS, key="strategy_debate_format"
    )
    st.caption(_format_ai_estimate("strategy", model_label))

    if st.button("生成主線", type="primary", width="stretch", key="strategy_submit"):
        if not topic_text:
            st.warning("請輸入或選擇辯題。")
        else:
            with st.spinner("AI 策劃中..."):
                result, actual_usage = brainstorm_strategy(
                    topic=topic_text,
                    side=strategy_side,
                    debate_format=strategy_debate_format,
                    model_label=model_label,
                )
                _record_ai_usage(user_id, "strategy", model_label, result, usage=actual_usage)

            st.divider()
            st.subheader("策略建議")
            st.markdown(result)

            st.download_button(
                "下載策略建議",
                data=result,
                file_name="策略建議.txt",
                mime="text/plain",
                width="stretch",
            )

    # ─── Tab 3: 搵料易 ───────────────────────────────────────────────

if selected_tab == "research":
    research_topic = st.text_input("辯題", key="research_topic")
    research_need = st.text_area(
        "輸入要尋找的資料",
        height=160,
        placeholder="例如：香港青年精神健康近年數據、其他地區政策例子、有利正方的研究證據...",
        key="research_need",
    )
    st.caption(_format_ai_estimate("web_research", model_label))
    if not model_config.get("supports_web_search"):
        st.warning("呢個模型不支援上網搜尋。請選擇收費模型以使用此功能。")

    if st.button("搵料易", type="primary", width="stretch", key="research_submit"):
        if not research_topic:
            st.warning("請輸入辯題。")
        elif not research_need:
            st.warning("請簡輸入要尋找的資料。")
        else:
            with st.spinner("AI 搵料中..."):
                result, actual_usage = research_web(
                    topic=research_topic,
                    research_need=research_need,
                    model_label=model_label,
                )
                _record_ai_usage(user_id, "web_research", model_label, result, usage=actual_usage)

            st.divider()
            st.subheader("搵料結果")
            st.markdown(result)

            st.download_button(
                "下載搵料結果",
                data=result,
                file_name="搵料結果.txt",
                mime="text/plain",
                width="stretch",
            )

    # ─── Tab 4: Fact Check易 ────────────────────────────────────────────

if selected_tab == "fact_check":
    statement = st.text_area(
        "輸入要核查的陳述",
        height=180,
        placeholder="例如：香港中學生每日平均睡眠時間少於 7 小時。",
        key="fact_check_statement",
    )
    st.caption(_format_ai_estimate("fact_check", model_label))
    if not model_config.get("supports_web_search"):
        st.warning("呢個模型不支援上網搜尋。請選擇收費模型以使用此功能。")

    if st.button("Fact Check", type="primary", width="stretch", key="fact_check_submit"):
        if not statement:
            st.warning("請輸入要核查的陳述。")
        else:
            with st.spinner("AI Fact Check 中..."):
                result, actual_usage = fact_check_claim(
                    statement=statement,
                    model_label=model_label,
                )
                _record_ai_usage(user_id, "fact_check", model_label, result, usage=actual_usage)

            st.divider()
            st.subheader("Fact Check 結果")
            st.markdown(result)

            st.download_button(
                "下載 Fact Check 結果",
                data=result,
                file_name="fact_check結果.txt",
                mime="text/plain",
                width="stretch",
            )

    # ─── Tab 5: 打Free De ─────────────────────────────────────────

if selected_tab == "free_debate":
    st.subheader("Gemini Live自由辯論練習")
    if model_config.get("provider") != "gemini":
        st.warning(
            f"而家模型為 {model_label}，不支援打Free De，"
            f"開始Free De 時會改用 {FREE_DEBATE_LIVE_MODEL_LABEL}。"
        )
    if not _azure_tts_configured():
        st.warning("未設定 Azure TTS，AI 讀音會 fallback 用 Gemini Live 原生聲音。")

    free_topic_source = st.radio(
        "辯題來源",
        ["手動輸入", "從辯題庫選擇"],
        horizontal=True,
        key="free_debate_topic_source",
    )
    free_topic = ""
    if free_topic_source == "從辯題庫選擇":
        try:
            conn = get_connection()
            topics_df = conn.query(
                f"SELECT topic_text, category, difficulty FROM {TABLE_TOPICS}",
                ttl=120,
            )
        except Exception as e:
            st.error(f"未能讀取辯題庫：{e}")
            topics_df = None

        if topics_df is not None and not topics_df.empty:
            topics_df["display"] = topics_df.apply(
                lambda r: f"[{r.get('category', '')}] {r['topic_text']}", axis=1
            )
            selected_display = st.selectbox(
                "選擇辯題",
                topics_df["display"].tolist(),
                key="free_debate_topic_select",
            )
            idx = topics_df["display"].tolist().index(selected_display)
            free_topic = topics_df.iloc[idx]["topic_text"]
            diff = topics_df.iloc[idx].get("difficulty")
            if diff and diff in DIFFICULTY_OPTIONS:
                st.caption(f"難度：{DIFFICULTY_OPTIONS[diff]}")
        else:
            st.info("辯題庫為空，請手動輸入辯題。")
            free_topic_source = "手動輸入"

    if free_topic_source == "手動輸入":
        free_topic = st.text_input("辯題", key="free_debate_live_topic")

    free_side = st.radio(
        "你的立場",
        ["正方", "反方"],
        horizontal=True,
        key="free_debate_live_side",
    )
    free_debate_format = st.selectbox(
        "賽制",
        options=FREE_DEBATE_FORMATS,
        key="free_debate_live_format",
    )
    if free_debate_format == "聯中":
        live_minutes = st.number_input(
            "每邊發言時間（分鐘）",
            min_value=0.5,
            max_value=10.0,
            value=5.0,
            step=0.5,
            key="free_debate_live_minutes",
        )
    else:
        live_minutes = 2.5
        if free_debate_format == "校園隨想":
            st.caption("校園隨想自由辯論時間固定為 2:30。")
    free_timer_config = get_debate_timer_config(
        free_debate_format,
        free_debate_minutes=live_minutes,
    )
    free_bell_schedule = free_timer_config["bell_schedules"].get("free", [])
    live_token_minutes = max(3, math.ceil(float(live_minutes) * 2 + 2))
    st.caption(
        _format_ai_estimate(
            "free_debate_live",
            FREE_DEBATE_LIVE_MODEL_LABEL,
            duration_minutes=live_token_minutes,
        )
    )

    if "GEMINI_API_KEY" not in st.secrets:
        st.warning("未設定 GEMINI_API_KEY，未能使用此功能。")

    if st.button("開始Free De", type="primary", width="stretch", key="free_debate_live_create"):
        if not free_topic.strip():
            st.warning("請先輸入辯題。")
        elif "GEMINI_API_KEY" not in st.secrets:
            st.error("未設定 GEMINI_API_KEY，未能使用此功能。")
        else:
            token_result = create_gemini_live_ephemeral_token(live_token_minutes)
            if not token_result.get("ok"):
                st.error(token_result.get("message", "開始Free De 失敗。"))
            else:
                ai_side = "反方" if free_side == "正方" else "正方"
                with st.spinner("AI 正在賽前搵料，準備攻防..."):
                    live_research = _prepare_live_research(
                        free_topic.strip(),
                        free_side,
                        ai_side,
                        free_debate_format,
                        "Free De",
                        model_label,
                        user_id,
                    )
                live_prompt = build_free_debate_live_prompt(free_topic.strip(), free_side, live_research)
                st.session_state["free_debate_live_session"] = {
                    **token_result,
                    "topic": free_topic.strip(),
                    "side": free_side,
                    "ai_side": ai_side,
                    "debate_format": free_debate_format,
                    "bell_schedule": free_bell_schedule,
                    "duration_minutes": float(live_minutes),
                    "prompt": live_prompt,
                    "practice_id": secrets.token_urlsafe(12),
                }
                st.success("請按下方「開始自由辯論」連線並允許麥克風權限。")

    live_session = st.session_state.get("free_debate_live_session")
    if live_session:
        st.info(
            f"辯題：{live_session['topic']}｜我方：{live_session['side']}｜"
            f"AI 方：{live_session.get('ai_side', '反方' if live_session['side'] == '正方' else '正方')}｜"
            f"賽制：{live_session.get('debate_format', '校園隨想')}｜"
            f"模型：{live_session['model_label']}｜建立時間：{live_session['created_at']}"
        )
        _render_live_debate_component(
            live_session["token"],
            live_session["model"],
            live_session["prompt"],
            live_session.get("duration_minutes", 10),
            live_session.get("bell_schedule"),
            ai_starts=live_session["side"] == "反方",
            relay_user_id=user_id,
            relay_practice_kind="solo_free",
            relay_practice_id=live_session["practice_id"],
            relay_max_seconds_by_token={
                live_session["token"]: max(
                    60, min(10 * 60, int(math.ceil(
                        float(live_session.get("duration_minutes", 10)) * 2 * 60
                    )))
                )
            },
        )
        if st.button("結束打Free De", key="free_debate_live_end"):
            del st.session_state["free_debate_live_session"]
            st.rerun()

# ─── Tab 6: 打Mock ─────────────────────────────────────────────

if selected_tab == "full_mock":
    st.subheader("Gemini Live Mock練習")
    if model_config.get("provider") != "gemini":
        st.warning(
            f"目前模型為 {model_label}，不支援打Mock功能，"
            f"開始時會改用 {FREE_DEBATE_LIVE_MODEL_LABEL}。"
        )
    if not _azure_tts_configured():
        st.warning("未設定 Azure TTS，AI 讀音會 fallback 用 Gemini Live 原生聲音。")

    mock_topic_source = st.radio(
        "辯題來源",
        ["手動輸入", "從辯題庫選擇"],
        horizontal=True,
        key="full_mock_topic_source",
    )
    mock_topic = ""
    if mock_topic_source == "從辯題庫選擇":
        try:
            conn = get_connection()
            topics_df = conn.query(
                f"SELECT topic_text, category, difficulty FROM {TABLE_TOPICS}",
                ttl=120,
            )
        except Exception as e:
            st.error(f"未能讀取辯題庫：{e}")
            topics_df = None

        if topics_df is not None and not topics_df.empty:
            topics_df["display"] = topics_df.apply(
                lambda r: f"[{r.get('category', '')}] {r['topic_text']}", axis=1
            )
            selected_display = st.selectbox(
                "選擇辯題",
                topics_df["display"].tolist(),
                key="full_mock_topic_select",
            )
            idx = topics_df["display"].tolist().index(selected_display)
            mock_topic = topics_df.iloc[idx]["topic_text"]
            diff = topics_df.iloc[idx].get("difficulty")
            if diff and diff in DIFFICULTY_OPTIONS:
                st.caption(f"難度：{DIFFICULTY_OPTIONS[diff]}")
        else:
            st.info("辯題庫為空，請手動輸入辯題。")
            mock_topic_source = "手動輸入"

    if mock_topic_source == "手動輸入":
        mock_topic = st.text_input("辯題", key="full_mock_topic")

    mock_side = st.radio(
        "你的立場",
        ["正方", "反方"],
        horizontal=True,
        key="full_mock_side",
    )
    mock_debate_format = st.selectbox(
        "賽制",
        options=DEBATE_FORMATS,
        key="full_mock_format",
    )
    mock_free_minutes = None
    if mock_debate_format == "聯中":
        mock_free_minutes = st.number_input(
            "自由辯論每邊（分鐘）",
            min_value=2.0,
            max_value=10.0,
            value=5.0,
            step=0.5,
            key="full_mock_free_minutes",
        )
    mock_segments = get_full_mock_sequence(mock_debate_format, free_debate_minutes=mock_free_minutes)
    mock_sessions = split_mock_into_sessions(mock_segments)
    mock_total_seconds = full_mock_total_seconds(mock_segments)
    mock_total_minutes = mock_total_seconds / 60
    st.caption(
        f"Mock 流程（{mock_debate_format}）：共 {len(mock_segments)} 段，全長約 {mock_total_minutes:.0f} 分鐘，"
        f"分 {len(mock_sessions)} 節連線（每節 ≤ 15 分鐘，自動接力）。逐段跟賽制響叮。"
    )
    with st.expander("查看完整流程次序"):
        st.markdown(
            "\n".join(f"{i}. {seg['label']}" for i, seg in enumerate(mock_segments, start=1))
        )
    st.caption(
        _format_ai_estimate(
            "full_mock_live",
            FREE_DEBATE_LIVE_MODEL_LABEL,
            duration_minutes=mock_total_minutes,
        )
        + f"（分 {len(mock_sessions)} 節逐節記錄）"
    )

    if "GEMINI_API_KEY" not in st.secrets:
        st.warning("未設定 GEMINI_API_KEY，未能使用此功能。")

    if st.button("開始打Mock", type="primary", width="stretch", key="full_mock_live_create"):
        if not mock_topic.strip():
            st.warning("請先輸入辯題。")
        elif "GEMINI_API_KEY" not in st.secrets:
            st.error("未設定 GEMINI_API_KEY，未能使用此功能。")
        else:
            token_result = create_gemini_live_ephemeral_tokens(len(mock_sessions), mock_total_minutes)
            if not token_result.get("ok"):
                st.error(token_result.get("message", "開始失敗"))
            else:
                ai_side = "反方" if mock_side == "正方" else "正方"
                with st.spinner("AI 正在賽前搵料，準備攻防..."):
                    mock_research = _prepare_live_research(
                        mock_topic.strip(),
                        mock_side,
                        ai_side,
                        mock_debate_format,
                        "Mock",
                        model_label,
                        user_id,
                    )
                mock_prompt = build_full_mock_live_prompt(
                    mock_topic.strip(),
                    mock_side,
                    mock_debate_format,
                    free_debate_minutes=mock_free_minutes,
                    research_brief=mock_research,
                )
                # 展平段落並標記所屬 session index，畀 component 逐節換 WS 用。
                flat_segments = []
                for si, sess in enumerate(mock_sessions):
                    for seg in sess["segments"]:
                        flat_segments.append({**seg, "session": si})
                st.session_state["full_mock_live_session"] = {
                    **token_result,
                    "topic": mock_topic.strip(),
                    "side": mock_side,
                    "ai_side": ai_side,
                    "debate_format": mock_debate_format,
                    "segments": flat_segments,
                    "session_labels": [s["label"] for s in mock_sessions],
                    "duration_minutes": mock_total_minutes,
                    "prompt": mock_prompt,
                    "practice_id": secrets.token_urlsafe(12),
                    "session_planned_seconds": [s["planned_seconds"] for s in mock_sessions],
                }
                st.success("請按下方「開始Mock」連線並允許麥克風權限。")

    mock_session = st.session_state.get("full_mock_live_session")
    if mock_session:
        st.info(
            f"辯題：{mock_session['topic']}｜我方：{mock_session['side']}｜"
            f"AI 方：{mock_session.get('ai_side', '反方' if mock_session['side'] == '正方' else '正方')}｜"
            f"賽制：{mock_session.get('debate_format', '校園隨想')}｜"
            f"模型：{mock_session['model_label']}｜建立時間：{mock_session['created_at']}"
        )
        mock_tokens = mock_session.get("tokens") or []
        _render_live_debate_component(
            mock_tokens[0] if mock_tokens else "",
            mock_session["model"],
            mock_session["prompt"],
            mock_session.get("duration_minutes", 25),
            session_label="Mock",
            segments=mock_session.get("segments"),
            tokens=mock_tokens,
            session_labels=mock_session.get("session_labels"),
            relay_user_id=user_id,
            relay_practice_kind="solo_mock",
            relay_practice_id=mock_session["practice_id"],
            relay_max_seconds_by_token={
                token_value: max(60, min(30 * 60, int(planned) + 120))
                for token_value, planned in zip(
                    mock_tokens, mock_session.get("session_planned_seconds") or []
                )
            },
        )
        if st.button("結束打Mock", key="full_mock_live_end"):
            del st.session_state["full_mock_live_session"]
            st.rerun()

# ─── Tab 7: 連線練習 ───────────────────────────────────────────────

if selected_tab == "live_room":
    st.subheader("🎧 連線練習")
    st.caption(
        "與其他委員即時連線練習。模式 A：真人對真人（1 對 1），AI 擔任評判；"
        "模式 B：多人一起對 AI 練習（完整 Mock 按賽制要求 3 或 4 位隊員）。使用房間代碼加入同一場練習。"
    )

    active_room = st.session_state.get("live_room")
    if active_room:
        mode_label = "多人對 AI" if active_room["mode"] == "B" else "真人對真人"
        st.success(
            f"你已在房間 **{active_room['code']}**（{mode_label}）。"
            "請將房間代碼分享給其他委員，對方在「加入房間」輸入即可。"
        )
        if active_room["mode"] == "B":
            st.info("多人對 AI：隊員輪流「按一下開始，發言完畢再按一下停止」向 AI 發言，AI 會扮演另一方即時攻防，全房一齊聽到。")
        _render_room_debate_component(active_room["code"], active_room["mode"])
        if st.button("離開房間", key="live_room_leave"):
            _room_api_post(f"/api/room/{active_room['code']}/leave", {}, user_id)
            del st.session_state["live_room"]
            st.rerun()
    else:
        action = st.radio(
            "請選擇操作", ["建立房間", "加入房間"], horizontal=True, key="live_room_action"
        )

        if action == "加入房間":
            join_code = st.text_input("房間代碼", key="live_room_join_code", max_chars=8)
            if st.button("加入房間", type="primary", key="live_room_join_btn"):
                code = (join_code or "").strip().upper()
                if not code:
                    st.warning("請輸入房間代碼。")
                else:
                    ok, data = _room_api_get(f"/api/room/{code}", user_id)
                    if not ok:
                        st.error(data if isinstance(data, str) else "加入失敗")
                    else:
                        st.session_state["live_room"] = {
                            "code": data["code"], "mode": data.get("mode", "A")
                        }
                        st.rerun()

        else:  # 建立房間
            create_mode = st.radio(
                "模式",
                ["真人對真人（1 對 1）", "多人對 AI"],
                key="live_room_create_mode",
            )
            mode = "A" if create_mode.startswith("真人") else "B"
            structure_label = st.radio(
                "形式", ["自由辯論", "完整 Mock"], horizontal=True, key="live_room_structure"
            )
            structure = "free" if structure_label == "自由辯論" else "mock"
            format_options = FREE_DEBATE_FORMATS if structure == "free" else DEBATE_FORMATS
            room_format = st.selectbox(
                "賽制",
                options=format_options,
                key=f"live_room_format_{structure}",
            )
            if mode == "B" and structure == "mock":
                st.caption(
                    "完整 Mock（多人對 AI）：隊員輪流負責我方各段發言（用語音轉文字記錄），"
                    "AI 會在對方段落自動代入發言；主持用「下一段」推進流程。"
                )
            elif mode == "B":
                st.caption("自由辯論（多人對 AI）：隊員輪流發言，AI 扮演另一方即時攻防。")
            if structure == "free" and room_format not in FREE_DEBATE_FORMATS:
                st.warning(f"{room_format}不設自由辯論；請改選「完整 Mock」。")
            room_topic_source = st.radio(
                "辯題來源",
                ["手動輸入", "從辯題庫選擇"],
                horizontal=True,
                key="live_room_topic_source",
            )
            room_topic = ""
            if room_topic_source == "從辯題庫選擇":
                try:
                    conn = get_connection()
                    topics_df = conn.query(
                        f"SELECT topic_text, category, difficulty FROM {TABLE_TOPICS}",
                        ttl=120,
                    )
                except Exception as e:
                    st.error(f"未能讀取辯題庫：{e}")
                    topics_df = None

                if topics_df is not None and not topics_df.empty:
                    topics_df["display"] = topics_df.apply(
                        lambda r: f"[{r.get('category', '')}] {r['topic_text']}", axis=1
                    )
                    selected_display = st.selectbox(
                        "選擇辯題",
                        topics_df["display"].tolist(),
                        key="live_room_topic_select",
                    )
                    idx = topics_df["display"].tolist().index(selected_display)
                    room_topic = topics_df.iloc[idx]["topic_text"]
                    diff = topics_df.iloc[idx].get("difficulty")
                    if diff and diff in DIFFICULTY_OPTIONS:
                        st.caption(f"難度：{DIFFICULTY_OPTIONS[diff]}")
                else:
                    st.info("辯題庫暫時沒有辯題，請手動輸入。")
                    room_topic_source = "手動輸入"

            if room_topic_source == "手動輸入":
                room_topic = st.text_input("辯題", key="live_room_topic")
            room_minutes = 2.5
            if structure == "free":
                if room_format == "聯中":
                    room_minutes = st.number_input(
                        "自由辯論每邊時間（分鐘）",
                        min_value=0.5, max_value=10.0, value=5.0, step=0.5,
                        key="live_room_minutes",
                    )
                else:
                    room_minutes = 2.5
                    if room_format == "校園隨想":
                        st.caption("校園隨想自由辯論為每邊 2:30。")
            elif room_format == "聯中":
                room_minutes = st.number_input(
                    "Mock 自由辯論每邊時間（分鐘）",
                    min_value=2.0, max_value=10.0, value=5.0, step=0.5,
                    key="live_room_mock_free_minutes",
                )

            payload = {
                "mode": mode,
                "debate_format": room_format,
                "structure": structure,
                "topic": (room_topic or "").strip(),
                "free_minutes": float(room_minutes),
            }
            if mode == "A":
                payload["side"] = st.radio(
                    "你的立場", ["正方", "反方"], horizontal=True, key="live_room_side"
                )
            else:
                payload["human_side"] = st.radio(
                    "你的立場（AI 代表另一方）",
                    ["正方", "反方"], horizontal=True, key="live_room_hside",
                )
                if structure == "mock":
                    mock_capacity = 3 if room_format == "星島" else 4
                    payload["capacity"] = mock_capacity
                    st.caption(f"完整 Mock 必須 {mock_capacity} 位隊員全部入房，並先選好辯位，才可開始。")
                else:
                    payload["capacity"] = st.slider(
                        "隊員人數上限", min_value=1, max_value=4, value=4, key="live_room_cap"
                    )
                if "GEMINI_API_KEY" not in st.secrets:
                    st.warning("未設定 GEMINI_API_KEY，暫時無法建立多人對 AI 房間。")

            if st.button("建立房間", type="primary", key="live_room_create_btn"):
                if not payload["topic"]:
                    st.warning("請手動輸入辯題，或從辯題庫選擇辯題。")
                elif structure == "free" and room_format not in FREE_DEBATE_FORMATS:
                    st.warning(f"{room_format}不設自由辯論；請改選「完整 Mock」。")
                else:
                    proceed = True
                    if mode == "B":
                        # Server (proxy) owns the shared Gemini Live session; mint an
                        # ephemeral token here (needs GEMINI_API_KEY / st.secrets) and
                        # pass it in the create payload — it never reaches any browser.
                        if "GEMINI_API_KEY" not in st.secrets:
                            st.error("未設定 GEMINI_API_KEY，未能啟動 AI 對手。")
                            proceed = False
                        else:
                            token_minutes = 20 if structure == "mock" else 14
                            token_result = create_gemini_live_ephemeral_token(token_minutes)
                            if not token_result.get("ok"):
                                st.error(token_result.get("message", "AI 連線 token 產生失敗。"))
                                proceed = False
                            else:
                                if structure == "mock":
                                    ai_prompt = build_full_mock_live_prompt(
                                        payload["topic"], payload["human_side"], room_format,
                                        free_debate_minutes=(
                                            room_minutes if room_format == "聯中" else None
                                        ),
                                    )
                                else:
                                    ai_prompt = build_free_debate_live_prompt(
                                        payload["topic"], payload["human_side"], ""
                                    )
                                payload["gemini"] = {
                                    "tokens": [token_result["token"]],
                                    "model": token_result["model"],
                                    "prompt": ai_prompt,
                                }
                                try:
                                    usage = estimate_ai_feature_usage(
                                        "free_debate_live",
                                        FREE_DEBATE_LIVE_MODEL_LABEL,
                                        duration_minutes=token_minutes,
                                    )
                                    log_ai_fund_usage(
                                        user_id=user_id,
                                        feature="free_debate_live",
                                        model_label=FREE_DEBATE_LIVE_MODEL_LABEL,
                                        success=True,
                                        usage_override=usage,
                                    )
                                except Exception as e:
                                    st.caption(f"AI 用量估算未能寫入：{e}")
                    if proceed:
                        ok, data = _room_api_post("/api/room/create", payload, user_id)
                        if ok and isinstance(data, dict) and data.get("code"):
                            st.session_state["live_room"] = {"code": data["code"], "mode": mode}
                            st.rerun()
                        else:
                            st.error(data if isinstance(data, str) else "建立房間失敗")
