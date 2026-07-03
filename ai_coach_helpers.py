import streamlit as st
import logging

from scoring import (
    SPEECH_CRITERIA,
    FREE_DEBATE_CRITERIA,
    SPEECH_MAX_PER_DEBATER,
    FREE_DEBATE_MAX,
    COHERENCE_MAX,
    GRAND_TOTAL,
)
from functions import (
    load_matches_from_db,
    get_score_data,
    query_params,
    DIFFICULTY_OPTIONS,
)
from schema import TABLE_TOPICS, TABLE_DEBATERS

logger = logging.getLogger(__name__)

POSITION_LABELS = {1: "主辯", 2: "一副辯員", 3: "二副辯員", 4: "結辯"}

DEFAULT_AI_MODEL = "Gemini 2.5 Flash"
AI_MODEL_OPTIONS = {
    "Gemini 3.5 Flash": {
        "provider": "gemini",
        "model": "gemini-3.5-flash",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "pricing_label": "有免費額度",
        "pricing_note": "Gemini API Free Tier 提供免費 input/output tokens，超過免費額度或使用 paid tier 會收費。",
        "is_premium": False,
    },
    "Gemini 2.5 Flash": {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "pricing_label": "有免費額度",
        "pricing_note": "Gemini API Free Tier 提供免費 input/output tokens，超過免費額度或使用 paid tier 會收費。",
        "is_premium": False,
    },
    "Gemini 2.5 Pro": {
        "provider": "gemini",
        "model": "gemini-2.5-pro",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "pricing_label": "有免費額度",
        "pricing_note": "Gemini API Free Tier 提供免費 input/output tokens，超過免費額度或使用 paid tier 會收費。",
        "is_premium": True,
    },
    "Gemini 3.1 Pro Preview": {
        "provider": "gemini",
        "model": "gemini-3.1-pro-preview",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "pricing_label": "收費",
        "pricing_note": "官方 pricing 顯示 Free Tier 不適用，需使用 paid tier。",
        "is_premium": True,
    },
    "GPT-5.4 mini": {
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "api_key": "OPENAI_API_KEY",
        "supports_audio": False,
        "pricing_label": "收費",
        "pricing_note": "OpenAI API 按 token 計費，需可用 credits / budget。",
        "is_premium": False,
    },
    "GPT-5.4": {
        "provider": "openai",
        "model": "gpt-5.4",
        "api_key": "OPENAI_API_KEY",
        "supports_audio": False,
        "pricing_label": "收費",
        "pricing_note": "OpenAI API 按 token 計費，需可用 credits / budget。",
        "is_premium": True,
    },
}

_SCORING_RUBRIC = f"""## 評分標準（滿分 {GRAND_TOTAL} 分）

### A 部分：台上發言（每位辯員滿分 {SPEECH_MAX_PER_DEBATER} 分）
""" + "\n".join(
    f"- {c['key']}（×{c['weight']}，滿分 {c['weight'] * c['max']}）"
    for c in SPEECH_CRITERIA
) + f"""

### B 部分：自由辯論（每方滿分 {FREE_DEBATE_MAX} 分）
""" + "\n".join(
    f"- {c['key']}（{c['max']}分）"
    for c in FREE_DEBATE_CRITERIA
) + f"""

### C 部分：內容連貫（滿分 {COHERENCE_MAX} 分）
四位辯員論點的整體一致性和互相呼應。"""

SPEECH_REVIEW_SYSTEM_PROMPT = f"""你係聖呂中辯嘅辯論教練 AI。你嘅工作係分析辯論發言，根據以下評分標準畀出詳細反饋。

{_SCORING_RUBRIC}

## 你嘅任務
分析用戶嘅發言，針對上述各維度畀出：
1. 各維度嘅預估分數範圍（例如「內容：7-8/10」）
2. 優點（具體引用發言內容）
3. 需改善之處（具體、可操作嘅建議）
4. 整體評語

用繁體中文回覆。語氣要鼓勵但誠實。如果輸入係錄音，請同時評估語速、語調、停頓等辭鋒表現。"""

STRATEGY_SYSTEM_PROMPT = f"""你係聖呂中辯嘅辯論策略顧問 AI。你嘅工作係幫隊伍策劃比賽主線。

## 辯論賽制
- 每隊四位辯員：主辯（開場立論）、一副（補充論證）、二副（反駁對方）、結辯（總結陳詞）
- 自由辯論環節：雙方交替發言
- 評判根據內容、辭鋒、組織、風度評分

{_SCORING_RUBRIC}

## 你嘅任務
根據辯題同立場，提供：
1. **比賽主線**：一句話概括全隊嘅核心立場
2. **主要論點**（3-4 個），每個包含：論點陳述、支持論據、預期反駁及應對
3. **對方可能論點預判** + 反駁策略
4. **自由辯論策略建議**：建議嘅提問方向和防守要點
5. **各辯員分工建議**

用繁體中文回覆。"""


def _get_model_config(model_label: str | None):
    return AI_MODEL_OPTIONS.get(model_label or DEFAULT_AI_MODEL, AI_MODEL_OPTIONS[DEFAULT_AI_MODEL])


def format_ai_model_label(model_label: str) -> str:
    model_config = _get_model_config(model_label)
    return f"{model_label}（{model_config['pricing_label']}）"


def _get_gemini_modules():
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None, None, "❌ Gemini SDK 尚未安裝，請先更新 requirements.txt 並重新部署。"
    return genai, types, None


def _get_gemini_client():
    if "GEMINI_API_KEY" not in st.secrets:
        return None, "❌ AI 功能尚未設定，請聯絡開發人員設定 Gemini API Key。"
    genai, _, error = _get_gemini_modules()
    if error:
        return None, error
    if "_gemini_client" not in st.session_state:
        st.session_state["_gemini_client"] = genai.Client(
            api_key=st.secrets["GEMINI_API_KEY"]
        )
    return st.session_state["_gemini_client"], None


def _get_openai_client():
    if "OPENAI_API_KEY" not in st.secrets:
        return None, "❌ AI 功能尚未設定，請聯絡開發人員設定 OpenAI API Key。"
    try:
        from openai import OpenAI
    except ImportError:
        return None, "❌ OpenAI SDK 尚未安裝，請先更新 requirements.txt 並重新部署。"
    if "_openai_client" not in st.session_state:
        st.session_state["_openai_client"] = OpenAI(
            api_key=st.secrets["OPENAI_API_KEY"]
        )
    return st.session_state["_openai_client"], None


def _format_ai_error(provider: str, error: Exception) -> str:
    error_str = str(error)
    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "rate_limit" in error_str:
        return "⚠️ AI 使用量或速率已達上限，請稍後再試。"
    if "503" in error_str or "UNAVAILABLE" in error_str or "high demand" in error_str:
        return "⚠️ AI 服務暫時繁忙，請稍後再試。"
    if "401" in error_str or "403" in error_str or "API key" in error_str:
        return f"❌ {provider} API Key 無效或權限不足，請聯絡開發人員檢查設定。"
    logger.warning("%s API error: %s", provider, error)
    return f"❌ AI 服務暫時無法使用：{error}"


def _generate_gemini_response(model_config, system_prompt: str, user_parts) -> str:
    client, error = _get_gemini_client()
    if error:
        return error
    _, types, error = _get_gemini_modules()
    if error:
        return error
    try:
        response = client.models.generate_content(
            model=model_config["model"],
            contents=[types.Content(role="user", parts=user_parts)],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.7,
            ),
        )
        return response.text or "AI 未能生成回覆，請再試一次。"
    except Exception as e:
        return _format_ai_error("Gemini", e)


def _generate_openai_response(model_config, system_prompt: str, user_text: str) -> str:
    client, error = _get_openai_client()
    if error:
        return error
    try:
        response = client.responses.create(
            model=model_config["model"],
            instructions=system_prompt,
            input=user_text,
        )
        return response.output_text or "AI 未能生成回覆，請再試一次。"
    except Exception as e:
        return _format_ai_error("OpenAI", e)


def _build_match_context(match_id: str) -> str:
    all_matches = load_matches_from_db()
    if match_id not in all_matches:
        return ""
    m = all_matches[match_id]
    topic = m.get("topic_text", "")
    pro_team = m.get("pro_team", "")
    con_team = m.get("con_team", "")

    lines = [
        "## 比賽資料",
        f"- 場次：{match_id}",
    ]
    if topic:
        lines.append(f"- 辯題：{topic}")
    if pro_team:
        pro_names = ", ".join(
            filter(None, [m.get(f"pro_{i}", "") for i in range(1, 5)])
        )
        lines.append(f"- 正方：{pro_team}（{pro_names}）" if pro_names else f"- 正方：{pro_team}")
    if con_team:
        con_names = ", ".join(
            filter(None, [m.get(f"con_{i}", "") for i in range(1, 5)])
        )
        lines.append(f"- 反方：{con_team}（{con_names}）" if con_names else f"- 反方：{con_team}")

    scores = get_score_data(match_id)
    if scores is not None and not scores.empty:
        pro_avg = scores["pro_total_score"].mean()
        con_avg = scores["con_total_score"].mean()
        lines.append(f"\n## 歷史評分參考")
        lines.append(f"- 正方平均總分：{pro_avg:.1f} / {GRAND_TOTAL}")
        lines.append(f"- 反方平均總分：{con_avg:.1f} / {GRAND_TOTAL}")

    return "\n".join(lines)


def _build_topic_context(topic_text: str) -> str:
    if not topic_text:
        return ""
    try:
        df = query_params(
            f"SELECT category, difficulty FROM {TABLE_TOPICS} WHERE topic_text = :topic",
            {"topic": topic_text},
        )
    except Exception:
        return ""
    if df.empty:
        return ""
    row = df.iloc[0]
    cat = row.get("category", "")
    diff = DIFFICULTY_OPTIONS.get(row.get("difficulty"), "")
    parts = []
    if cat:
        parts.append(f"類別：{cat}")
    if diff:
        parts.append(f"難度：{diff}")
    return "辯題資料：" + "，".join(parts) if parts else ""


def review_speech(
    text: str | None,
    audio_bytes: bytes | None,
    side: str,
    position: int,
    match_id: str | None = None,
    manual_topic: str | None = None,
    model_label: str | None = None,
) -> str:
    model_config = _get_model_config(model_label)
    if audio_bytes and not model_config["supports_audio"]:
        return "⚠️ 錄音分析目前只支援 Gemini 模型。請改用 Gemini 模型，或貼上文字稿後再使用 OpenAI 模型。"

    position_label = POSITION_LABELS.get(position, "")
    user_parts = []
    user_text_lines = [f"我嘅辯位：{side}{position_label}"]

    if match_id:
        context = _build_match_context(match_id)
        if context:
            user_text_lines.append(context)
    elif manual_topic:
        user_text_lines.append(f"辯題：{manual_topic}")
        user_text_lines.append(f"立場：{side}")

    if text:
        user_text_lines.append(f"\n## 我嘅演辭內容\n{text}")

    if model_config["provider"] == "openai":
        return _generate_openai_response(
            model_config,
            SPEECH_REVIEW_SYSTEM_PROMPT,
            "\n".join(user_text_lines),
        )

    _, types, error = _get_gemini_modules()
    if error:
        return error

    user_parts.append(types.Part.from_text(text="\n".join(user_text_lines)))

    if audio_bytes:
        if not text:
            user_parts[0] = types.Part.from_text(
                text="\n".join(user_text_lines) + "\n\n以下係我嘅演辭錄音，請分析："
            )
        user_parts.append(
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")
        )

    return _generate_gemini_response(
        model_config,
        SPEECH_REVIEW_SYSTEM_PROMPT,
        user_parts,
    )


def brainstorm_strategy(
    topic: str,
    side: str,
    model_label: str | None = None,
) -> str:
    model_config = _get_model_config(model_label)

    user_lines = [f"辯題：{topic}", f"立場：{side}"]
    topic_ctx = _build_topic_context(topic)
    if topic_ctx:
        user_lines.append(topic_ctx)
    user_lines.append("\n請為以上辯題和立場提供完整的比賽策略。")

    if model_config["provider"] == "openai":
        return _generate_openai_response(
            model_config,
            STRATEGY_SYSTEM_PROMPT,
            "\n".join(user_lines),
        )
    _, types, error = _get_gemini_modules()
    if error:
        return error
    return _generate_gemini_response(
        model_config,
        STRATEGY_SYSTEM_PROMPT,
        [types.Part.from_text(text="\n".join(user_lines))],
    )
