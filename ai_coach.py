import streamlit as st
import base64
import json
import math
import os
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
    get_ai_fund_settings,
    save_ai_fund_public_settings,
    is_ai_fund_treasurer,
    get_ai_fund_summary,
    create_ai_fund_transaction,
    update_ai_fund_transaction_status,
    get_ai_fund_transactions,
    get_ai_fund_usage_logs,
    get_ai_fund_usage_summary,
    POSITION_LABELS,
    AI_FEATURE_LABELS,
    AI_FUND_TRANSACTION_LABELS,
    AI_PROVIDER_LABELS,
    get_ai_model_settings,
    format_ai_model_label,
    format_ai_model_usage_note,
    get_openrouter_credit_balance,
    get_google_ai_studio_balance,
    save_google_ai_studio_balance,
    reset_ai_fund_usage_logs,
    format_usd_money,
    format_hkd_money,
    HKD_PER_USD,
    build_free_debate_live_prompt,
    build_full_mock_live_prompt,
    create_gemini_live_ephemeral_token,
    create_gemini_live_ephemeral_tokens,
    FREE_DEBATE_LIVE_MODEL_LABEL,
)
from debate_timing import (
    DEBATE_FORMATS,
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
            for t in [token, *(tokens or [])]:
                if t and t not in token_sigs:
                    token_sigs[t] = sign_relay_token(t)
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


def _prepare_transaction_display(df):
    if df.empty:
        return df
    display_df = df.copy()
    display_df["transaction_type"] = display_df["transaction_type"].map(
        lambda x: AI_FUND_TRANSACTION_LABELS.get(x, x)
    )
    if "provider" in display_df.columns:
        display_df["provider"] = display_df["provider"].map(
            lambda x: AI_PROVIDER_LABELS.get(x, x)
        )
    display_df["status"] = display_df["status"].map(
        {"pending": "待確認", "confirmed": "已確認", "rejected": "已拒絕"}
    ).fillna(display_df["status"])
    return display_df.rename(columns={
        "id": "編號",
        "transaction_type": "類型",
        "status": "狀態",
        "provider": "Provider",
        "amount_hkd": "金額(HKD)",
        "payment_method": "付款方式",
        "reference_no": "Reference",
        "note": "備註",
        "created_by": "提交者",
        "created_at": "提交時間",
        "confirmed_by": "確認者",
        "confirmed_at": "確認時間",
        "rejected_by": "拒絕者",
        "rejected_at": "拒絕時間",
        "status_note": "狀態備註",
    })


def _prepare_usage_display(df):
    if df.empty:
        return df
    display_df = df.copy()
    display_df["feature"] = display_df["feature"].map(
        lambda x: AI_FEATURE_LABELS.get(x, x)
    )
    display_df["provider"] = display_df["provider"].map(
        lambda x: AI_PROVIDER_LABELS.get(x, x)
    )
    display_df["status"] = display_df["status"].map(
        {"success": "成功", "failed": "失敗"}
    ).fillna(display_df["status"])
    return display_df.rename(columns={
        "id": "編號",
        "user_id": "用戶",
        "feature": "功能",
        "model_label": "模型",
        "provider": "Provider",
        "estimated_cost_usd": "估算成本(USD)",
        "estimated_cost_hkd": "估算成本(HKD)",
        "input_tokens": "Input tokens",
        "output_tokens": "Output tokens",
        "audio_tokens": "Audio tokens",
        "search_calls": "搜尋次數",
        "cost_source": "成本來源",
        "status": "狀態",
        "error_message": "錯誤訊息",
        "created_at": "時間",
    })

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

_tab_options = ["strategy", "review", "fact_check", "research", "free_debate", "full_mock", "live_room", "fund"]


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
    if tab_name == "live_room":
        return "🎧 連線練習"
    return "💲AI基金"


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

    if st.button("分析發言", type="primary", use_container_width=True, key="review_submit"):
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

    if st.button("生成主線", type="primary", use_container_width=True, key="strategy_submit"):
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
                use_container_width=True,
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

    if st.button("搵料易", type="primary", use_container_width=True, key="research_submit"):
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
                use_container_width=True,
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

    if st.button("Fact Check", type="primary", use_container_width=True, key="fact_check_submit"):
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
                use_container_width=True,
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
        options=[fmt for fmt in DEBATE_FORMATS if fmt != "基本法盃"],
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

    if st.button("開始Free De", type="primary", use_container_width=True, key="free_debate_live_create"):
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
                }
                try:
                    live_usage = estimate_ai_feature_usage(
                        "free_debate_live",
                        FREE_DEBATE_LIVE_MODEL_LABEL,
                        duration_minutes=live_token_minutes,
                    )
                    log_ai_fund_usage(
                        user_id=user_id,
                        feature="free_debate_live",
                        model_label=FREE_DEBATE_LIVE_MODEL_LABEL,
                        success=True,
                        usage_override=live_usage,
                    )
                except Exception as e:
                    st.caption(f"Live 用量估算未能寫入：{e}")
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

    if st.button("開始打Mock", type="primary", use_container_width=True, key="full_mock_live_create"):
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
                }
                # 逐節按計劃時長各記一條，summing 到真實全長（雙方段落兩邊都計，同 caption 一致）。
                for sess in mock_sessions:
                    sess_billed_minutes = full_mock_total_seconds(sess["segments"]) / 60
                    try:
                        sess_usage = estimate_ai_feature_usage(
                            "full_mock_live",
                            FREE_DEBATE_LIVE_MODEL_LABEL,
                            duration_minutes=sess_billed_minutes,
                        )
                        log_ai_fund_usage(
                            user_id=user_id,
                            feature="full_mock_live",
                            model_label=FREE_DEBATE_LIVE_MODEL_LABEL,
                            success=True,
                            usage_override=sess_usage,
                        )
                    except Exception as e:
                        st.caption(f"Mock 用量估算未能寫入：{e}")
                        break
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
        )
        if st.button("結束打Mock", key="full_mock_live_end"):
            del st.session_state["full_mock_live_session"]
            st.rerun()

# ─── Tab 7: 連線練習 ───────────────────────────────────────────────

if selected_tab == "live_room":
    st.subheader("🎧 連線練習（Beta）")
    st.caption(
        "與其他委員即時連線練習。模式 A：真人對真人（1 對 1），AI 擔任評判；"
        "模式 B：多人（1–4）一起對 AI 練習。使用房間代碼加入同一場練習。"
    )

    active_room = st.session_state.get("live_room")
    if active_room:
        mode_label = "多人對 AI" if active_room["mode"] == "B" else "真人對真人"
        st.success(
            f"你已在房間 **{active_room['code']}**（{mode_label}）。"
            "請將房間代碼分享給其他委員，對方在「加入房間」輸入即可。"
        )
        if active_room["mode"] == "B":
            st.info("模式 B 的 AI 對手功能開發中（Phase 2）；目前可先測試多人連線、輪流發言及計時。")
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
                ["真人對真人（1 對 1）", "多人對 AI（1–4 人）"],
                key="live_room_create_mode",
            )
            mode = "A" if create_mode.startswith("真人") else "B"
            room_format = st.selectbox(
                "賽制",
                options=[fmt for fmt in DEBATE_FORMATS if fmt != "基本法盃"],
                key="live_room_format",
            )
            structure_label = st.radio(
                "形式", ["自由辯論", "完整 Mock"], horizontal=True, key="live_room_structure"
            )
            structure = "free" if structure_label == "自由辯論" else "mock"
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
                room_topic = st.text_input("辯題（可選）", key="live_room_topic")
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
                        st.caption("校園隨想自由辯論係每邊 2:30。")
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
                payload["capacity"] = st.slider(
                    "隊員人數上限", min_value=1, max_value=4, value=4, key="live_room_cap"
                )
                st.caption("模式 B 的 AI 對手會於 Phase 2 接入；目前建立後可先測試多人連線。")

            if st.button("建立房間", type="primary", key="live_room_create_btn"):
                ok, data = _room_api_post("/api/room/create", payload, user_id)
                if ok and isinstance(data, dict) and data.get("code"):
                    st.session_state["live_room"] = {"code": data["code"], "mode": mode}
                    st.rerun()
                else:
                    st.error(data if isinstance(data, str) else "建立房間失敗")

# ─── Tab 8: AI基金 ─────────────────────────────────────────────────

if selected_tab == "fund":
    st.subheader("AI基金")
    st.caption("正式現金帳以AI基金管理員確認的入數及 AI provider 付款紀錄為準；AI 用量成本只作估算。")

    if not ensure_ai_fund_tables():
        st.error("AI基金資料表尚未就緒，請聯絡開發者執行資料庫初始化。")
        st.stop()

    fund_settings = get_ai_fund_settings()
    is_treasurer = is_ai_fund_treasurer(user_id)
    fund_summary = get_ai_fund_summary()

    if not fund_settings["treasurers"]:
        st.warning("尚未設定AI基金管理員。請先到開發者設定指定AI基金管理員。")
    elif is_treasurer:
        st.success("你是 AI基金管理員，可確認入數、記錄支出及更新AI基金設定。")

    fund_overview_tab, fund_deposit_tab, fund_usage_tab = st.tabs(
        ["總覽", "入數 / 交易", "AI 用量"]
    )

    with fund_overview_tab:
        col1, col2, col3 = st.columns(3)
        col1.metric("已確認現金餘額", _format_hkd(fund_summary["balance_hkd"]))
        col2.metric("待確認入數", _format_hkd(fund_summary["pending_deposits_hkd"]))
        col3.metric("最近 30 日 AI 用量（估算）", _format_hkd_4dp(fund_summary["recent_usage_hkd"]))

        if fund_summary["balance_hkd"] < fund_summary["low_balance_hkd"]:
            st.warning(
                f"餘額低於警戒線 {_format_hkd(fund_summary['low_balance_hkd'])}，建議補充資金。"
            )

        st.caption(
            f"目標金額：{_format_hkd(fund_summary['target_hkd'])}｜"
            f"建議補充：{_format_hkd(fund_summary['suggested_total_hkd'])}｜"
            f"按 {fund_summary['member_count']} 人計每人約 "
            f"{_format_hkd(fund_summary['suggested_per_member_hkd'])}"
        )

        with st.expander("Provider 餘額", expanded=True):
            google_ai_studio_balance = get_google_ai_studio_balance()
            provider_col1, provider_col2 = st.columns(2)
            with provider_col1:
                openrouter_balance = get_openrouter_credit_balance()
                if openrouter_balance.get("ok"):
                    st.metric(
                        "OpenRouter 剩餘 credits",
                        format_usd_money(openrouter_balance["remaining_credits_usd"]),
                        help="由 OpenRouter credits API 即時讀取。",
                    )
                    st.caption(
                        f"Purchased：{format_usd_money(openrouter_balance['total_credits_usd'], escape_markdown=True)}｜"
                        f"Used：{format_usd_money(openrouter_balance['total_usage_usd'], escape_markdown=True)}｜"
                        f"約 {format_hkd_money(openrouter_balance['remaining_credits_usd'] * HKD_PER_USD)}"
                    )
                else:
                    st.metric("OpenRouter 剩餘 credits", "未能讀取")
                    st.caption(openrouter_balance.get("message", "OpenRouter credits 讀取失敗。"))
            with provider_col2:
                if google_ai_studio_balance["balance_usd"] is None:
                    st.metric("Google AI Studio 手動餘額", "未設定")
                    st.caption("由 AI基金管理員從 AI Studio Billing 手動更新。")
                else:
                    st.metric(
                        "Google AI Studio 手動餘額",
                        f"US${google_ai_studio_balance['balance_usd']:,.2f}",
                        help="手動輸入值，並非 Google API 即時回傳。",
                    )
                    st.caption(
                        f"約 {_format_hkd(google_ai_studio_balance['balance_hkd'])}｜"
                        f"更新：{google_ai_studio_balance['updated_at'] or '—'}｜"
                        f"{google_ai_studio_balance['updated_by'] or '—'}"
                    )
                if is_treasurer:
                    with st.form("google_ai_studio_balance_form"):
                        balance_default = google_ai_studio_balance["balance_usd"]
                        google_balance_usd = st.number_input(
                            "更新 Google AI Studio 餘額（USD）",
                            min_value=0.0,
                            value=float(balance_default or 0.0),
                            step=1.0,
                            format="%.4f",
                        )
                        submit_google_balance = st.form_submit_button("更新餘額")
                    if submit_google_balance:
                        try:
                            save_google_ai_studio_balance(google_balance_usd, user_id)
                            st.success("Google AI Studio 餘額已更新。")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Google AI Studio 餘額更新失敗：{e}")

        with st.expander("付款指示"):
            st.text(fund_settings["payment_instruction"])

        if is_treasurer:
            with st.expander("AI基金管理員設定"):
                with st.form("ai_fund_settings_form"):
                    target_hkd = st.number_input(
                        "目標金額（HKD）",
                        min_value=0.0,
                        value=float(fund_settings["target_hkd"]),
                        step=10.0,
                        format="%.2f",
                    )
                    low_balance_hkd = st.number_input(
                        "低餘額警戒線（HKD）",
                        min_value=0.0,
                        value=float(fund_settings["low_balance_hkd"]),
                        step=5.0,
                        format="%.2f",
                    )
                    payment_instruction = st.text_area(
                        "付款指示",
                        value=fund_settings["payment_instruction"],
                        height=120,
                    )
                    save_settings = st.form_submit_button("更新設定", type="primary")

                if save_settings:
                    save_ai_fund_public_settings(
                        target_hkd=target_hkd,
                        low_balance_hkd=low_balance_hkd,
                        payment_instruction=payment_instruction,
                    )
                    st.success("AI基金設定已更新。")
                    st.rerun()

                st.divider()
                st.markdown("##### 重置 AI 用量紀錄")
                st.caption("刪除所有 AI 用量估算紀錄。此操作不可復原，不影響現金帳交易。")
                reset_confirm = st.checkbox("我確認要重置所有 AI 用量紀錄", key="reset_usage_confirm")
                if st.button("重置用量紀錄", disabled=not reset_confirm, key="reset_usage_btn"):
                    try:
                        deleted = reset_ai_fund_usage_logs()
                        st.success(f"已刪除 {deleted} 筆用量紀錄。")
                        st.rerun()
                    except Exception as e:
                        st.error(f"重置失敗：{e}")

    with fund_deposit_tab:
        st.markdown("#### 成員入數")
        with st.form("ai_fund_deposit_form"):
            deposit_amount = st.number_input(
                "入數金額（HKD）",
                min_value=0.0,
                step=10.0,
                format="%.2f",
                key="ai_fund_deposit_amount",
            )
            payment_method = st.selectbox(
                "付款方式",
                ["FPS", "現金", "Alipay", "PayMe", "其他"],
                key="ai_fund_deposit_method",
            )
            reference_no = st.text_input("Reference / 交易編號（如有）", key="ai_fund_deposit_ref")
            deposit_note = st.text_area("備註（如有）", height=80, key="ai_fund_deposit_note")
            submit_deposit = st.form_submit_button("提交入數紀錄", type="primary")

        if submit_deposit:
            if deposit_amount <= 0:
                st.warning("請輸入大於 0 的入數金額。")
            else:
                create_ai_fund_transaction(
                    user_id=user_id,
                    transaction_type="member_deposit",
                    amount_hkd=deposit_amount,
                    provider="general",
                    payment_method=payment_method,
                    reference_no=reference_no,
                    note=deposit_note,
                    status="pending",
                )
                st.success("入數紀錄已提交，待AI基金管理員確認後會計入AI基金。")
                st.rerun()

        if is_treasurer:
            st.divider()
            st.markdown("#### AI基金管理員操作")
            tx_df_for_pending = get_ai_fund_transactions(user_id=user_id, treasurer=True, limit=200)
            pending_df = tx_df_for_pending[
                (tx_df_for_pending["status"] == "pending")
                & (tx_df_for_pending["transaction_type"] == "member_deposit")
            ] if not tx_df_for_pending.empty else tx_df_for_pending

            with st.expander(f"待確認入數（{0 if pending_df.empty else len(pending_df)}）", expanded=True):
                if pending_df.empty:
                    st.caption("而家沒有待確認入數。")
                else:
                    for _, row in pending_df.iterrows():
                        tx_id = int(row["id"])
                        with st.container(border=True):
                            st.markdown(
                                f"**#{tx_id}**｜{row['created_by']}｜"
                                f"{_format_hkd(row['amount_hkd'])}｜{row.get('payment_method') or '—'}"
                            )
                            st.caption(
                                f"Reference：{row.get('reference_no') or '—'}｜"
                                f"提交時間：{row.get('created_at')}"
                            )
                            if row.get("note"):
                                st.caption(f"備註：{row['note']}")
                            status_note = st.text_input(
                                "確認 / 拒絕備註（可選）",
                                key=f"ai_fund_status_note_{tx_id}",
                            )
                            btn_col1, btn_col2 = st.columns(2)
                            with btn_col1:
                                if st.button("確認入數", key=f"confirm_ai_fund_{tx_id}", use_container_width=True):
                                    updated = update_ai_fund_transaction_status(
                                        tx_id,
                                        "confirmed",
                                        user_id,
                                        status_note=status_note,
                                    )
                                    st.success("已確認入數。" if updated else "此入數已被處理。")
                                    st.rerun()
                            with btn_col2:
                                if st.button("拒絕入數", key=f"reject_ai_fund_{tx_id}", use_container_width=True):
                                    updated = update_ai_fund_transaction_status(
                                        tx_id,
                                        "rejected",
                                        user_id,
                                        status_note=status_note,
                                    )
                                    st.warning("已拒絕入數。" if updated else "此入數已被處理。")
                                    st.rerun()

            with st.expander("記錄 provider 支出 / 退款 / 調整", expanded=False):
                with st.form("ai_fund_treasurer_tx_form"):
                    treasurer_tx_type = st.selectbox(
                        "交易類型",
                        ["provider_topup", "refund", "adjustment"],
                        format_func=lambda x: AI_FUND_TRANSACTION_LABELS.get(x, x),
                    )
                    treasurer_provider = st.selectbox(
                        "Provider / 分類",
                        ["gemini", "openrouter", "other"],
                        format_func=lambda x: AI_PROVIDER_LABELS.get(x, x),
                    )
                    treasurer_amount = st.number_input(
                        "金額（HKD）",
                        value=0.0,
                        step=10.0,
                        format="%.2f",
                        help="充值 / 支出及退款請輸入正數；手動調整可輸入正數或負數。",
                    )
                    treasurer_method = st.text_input("付款方式 / Provider", placeholder="例如：OpenRouter、FPS")
                    treasurer_ref = st.text_input("Reference / 帳單編號（如有）")
                    treasurer_note = st.text_area("原因 / 備註", height=100)
                    submit_treasurer_tx = st.form_submit_button("新增已確認交易", type="primary")

                if submit_treasurer_tx:
                    if treasurer_tx_type != "adjustment" and treasurer_amount <= 0:
                        st.warning("充值 / 支出及退款金額必須大於 0。")
                    elif treasurer_tx_type == "adjustment" and treasurer_amount == 0:
                        st.warning("手動調整金額不能為 0。")
                    elif not treasurer_note.strip():
                        st.warning("請填寫原因 / 備註。")
                    else:
                        create_ai_fund_transaction(
                            user_id=user_id,
                            transaction_type=treasurer_tx_type,
                            amount_hkd=treasurer_amount,
                            provider=treasurer_provider,
                            payment_method=treasurer_method,
                            reference_no=treasurer_ref,
                            note=treasurer_note,
                            status="confirmed",
                        )
                        st.success("已新增交易紀錄。")
                        st.rerun()

        st.divider()
        st.markdown("#### 交易紀錄" if is_treasurer else "#### 我的入數紀錄")
        tx_df = get_ai_fund_transactions(user_id=user_id, treasurer=is_treasurer, limit=80)
        if tx_df.empty:
            st.info("暫無交易紀錄。")
        else:
            tx_display = _prepare_transaction_display(tx_df)
            st.dataframe(tx_display, use_container_width=True, hide_index=True)
            st.download_button(
                "下載交易紀錄 CSV",
                data=tx_display.to_csv(index=False).encode("utf-8-sig"),
                file_name="ai基金交易紀錄.csv",
                mime="text/csv",
                use_container_width=True,
            )

    with fund_usage_tab:
        st.markdown("#### AI 用量估算")
        usage_df = get_ai_fund_usage_logs(user_id=user_id, treasurer=is_treasurer, limit=50)
        if usage_df.empty:
            st.info("暫無 AI 用量紀錄。")
        else:
            usage_display = _prepare_usage_display(usage_df)
            st.dataframe(usage_display, use_container_width=True, hide_index=True)
            st.download_button(
                "下載最近用量 CSV",
                data=usage_display.to_csv(index=False).encode("utf-8-sig"),
                file_name="ai用量估算紀錄.csv",
                mime="text/csv",
                use_container_width=True,
            )

        usage_summary_df = get_ai_fund_usage_summary()
        if not usage_summary_df.empty:
            if not is_treasurer:
                usage_summary_df = usage_summary_df[usage_summary_df["user_id"] == user_id]
            if not usage_summary_df.empty:
                usage_summary_display = usage_summary_df.copy()
                usage_summary_display["provider"] = usage_summary_display["provider"].map(
                    lambda x: AI_PROVIDER_LABELS.get(x, x)
                )
                usage_summary_display["feature"] = usage_summary_display["feature"].map(
                    lambda x: AI_FEATURE_LABELS.get(x, x)
                )
                usage_summary_display = usage_summary_display.rename(columns={
                    "month": "月份",
                    "user_id": "用戶",
                    "provider": "Provider",
                    "feature": "功能",
                    "model_label": "模型",
                    "uses": "使用次數",
                    "estimated_cost_hkd": "估算成本(HKD)",
                })
                st.markdown("#### 按月 / 用戶 / 功能統計")
                st.dataframe(usage_summary_display, use_container_width=True, hide_index=True)
                st.download_button(
                    "下載用量統計 CSV",
                    data=usage_summary_display.to_csv(index=False).encode("utf-8-sig"),
                    file_name="ai用量統計.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
