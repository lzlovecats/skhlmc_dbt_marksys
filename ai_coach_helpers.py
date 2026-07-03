import streamlit as st
import logging
import json
import base64
from datetime import datetime
from zoneinfo import ZoneInfo

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
    execute_query,
    execute_query_count,
    get_system_config,
    get_active_user_count,
    DIFFICULTY_OPTIONS,
)
from schema import (
    CREATE_AI_FUND_TRANSACTIONS,
    CREATE_AI_FUND_USAGE_LOGS,
    TABLE_ACCOUNTS,
    TABLE_AI_FUND_TRANSACTIONS,
    TABLE_AI_FUND_USAGE_LOGS,
    TABLE_TOPICS,
    TABLE_DEBATERS,
)

logger = logging.getLogger(__name__)

POSITION_LABELS = {1: "主辯", 2: "一副", 3: "二副", 4: "結辯", 5: "三副"}

SPEECH_UNIT_MINUTES = 4
SPEECH_UNIT_WORDS = 1700
SPEECH_REVIEW_INPUT_TOKENS = 2500
SPEECH_REVIEW_OUTPUT_TOKENS = 1800
SPEECH_REVIEW_AUDIO_TOKENS = SPEECH_UNIT_MINUTES * 60 * 25
STRATEGY_INPUT_TOKENS = 1200
STRATEGY_OUTPUT_TOKENS = 2500
HKD_PER_USD = 7.80
WEB_RESEARCH_INPUT_TOKENS = 1500
WEB_RESEARCH_OUTPUT_TOKENS = 2500
OPENROUTER_WEB_SEARCH_USD_PER_CALL = 0.005

AI_FUND_TARGET_HKD_DEFAULT = 100.0
AI_FUND_LOW_BALANCE_HKD_DEFAULT = 20.0
AI_FUND_PAYMENT_INSTRUCTION_DEFAULT = "請向AI基金管理員查詢 FPS / 現金 / 轉賬安排，付款後在此提交入數紀錄。"

AI_FEATURE_LABELS = {
    "speech_review": "發言檢查",
    "strategy": "主線策劃",
    "web_research": "上網搵料",
    "fact_check": "Fact check易",
}

AI_FUND_TRANSACTION_LABELS = {
    "member_deposit": "成員入數",
    "provider_topup": "AI provider 充值 / 帳單",
    "refund": "退款",
    "adjustment": "手動調整",
}

AI_PROVIDER_LABELS = {
    "general": "整體AI基金",
    "openrouter": "OpenRouter",
    "gemini": "Gemini（舊）",
    "openai": "GPT / OpenAI（舊）",
    "other": "其他",
}

DEFAULT_AI_MODEL = "Gemma 4 31B"
AI_MODEL_OPTIONS = {
    "Gemma 4 31B": {
        "provider": "openrouter",
        "model": "google/gemma-4-31b-it:free",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": False,
        "pricing_label": "免費",
        "pricing_note": "OpenRouter 免費模型，無需扣基金。⚠️ 免費模型可能會將輸入內容用於訓練。",
        "paid_rate_note": "免費。",
        "input_price_per_million": 0,
        "audio_input_price_per_million": None,
        "output_price_per_million": 0,
        "is_premium": False,
    },
    "GPT-OSS-120B": {
        "provider": "openrouter",
        "model": "openai/gpt-oss-120b:free",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": False,
        "pricing_label": "免費",
        "pricing_note": "OpenRouter 免費模型，無需扣基金。⚠️ 免費模型可能會將輸入內容用於訓練。",
        "paid_rate_note": "免費。",
        "input_price_per_million": 0,
        "audio_input_price_per_million": None,
        "output_price_per_million": 0,
        "is_premium": False,
    },
    "Gemini 2.5 Flash": {
        "provider": "openrouter",
        "model": "google/gemini-2.5-flash",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": True,
        "pricing_label": "收費",
        "pricing_note": "經 OpenRouter 按 token 計費，最低成本收費模型。",
        "paid_rate_note": "Input US$0.30 / 1M tokens，output US$2.50 / 1M tokens。",
        "input_price_per_million": 0.30,
        "audio_input_price_per_million": 1.00,
        "output_price_per_million": 2.50,
        "is_premium": False,
    },
    "GPT-5.4 mini": {
        "provider": "openrouter",
        "model": "openai/gpt-5.4-mini",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": False,
        "pricing_label": "收費",
        "pricing_note": "經 OpenRouter 按 token 計費。",
        "paid_rate_note": "Input US$0.75 / 1M tokens，output US$4.50 / 1M tokens。",
        "input_price_per_million": 0.75,
        "audio_input_price_per_million": None,
        "output_price_per_million": 4.50,
        "is_premium": False,
    },
    "Gemini 2.5 Pro": {
        "provider": "openrouter",
        "model": "google/gemini-2.5-pro",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": True,
        "pricing_label": "收費",
        "pricing_note": "經 OpenRouter 按 token 計費。",
        "paid_rate_note": "Input US$1.25 / 1M tokens，output US$10.00 / 1M tokens。",
        "input_price_per_million": 1.25,
        "audio_input_price_per_million": 1.25,
        "output_price_per_million": 10.00,
        "is_premium": True,
    },
    "Gemini 3.5 Flash": {
        "provider": "openrouter",
        "model": "google/gemini-3.5-flash",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": True,
        "pricing_label": "收費",
        "pricing_note": "經 OpenRouter 按 token 計費。",
        "paid_rate_note": "Input US$1.50 / 1M tokens，output US$9.00 / 1M tokens。",
        "input_price_per_million": 1.50,
        "audio_input_price_per_million": 1.50,
        "output_price_per_million": 9.00,
        "is_premium": False,
    },
    "Gemini 3.1 Pro Preview": {
        "provider": "openrouter",
        "model": "google/gemini-3.1-pro-preview",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": True,
        "pricing_label": "收費",
        "pricing_note": "經 OpenRouter 按 token 計費。",
        "paid_rate_note": "Input US$2.00 / 1M tokens，output US$12.00 / 1M tokens。",
        "input_price_per_million": 2.00,
        "audio_input_price_per_million": 2.00,
        "output_price_per_million": 12.00,
        "is_premium": True,
    },
    "GPT-5.4": {
        "provider": "openrouter",
        "model": "openai/gpt-5.4",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": False,
        "pricing_label": "收費",
        "pricing_note": "經 OpenRouter 按 token 計費。",
        "paid_rate_note": "Input US$2.50 / 1M tokens，output US$15.00 / 1M tokens。",
        "input_price_per_million": 2.50,
        "audio_input_price_per_million": None,
        "output_price_per_million": 15.00,
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

用繁體中文回覆。語氣要鼓勵但誠實。如果輸入係錄音，請同時評估語速、語調、停頓等辭鋒表現。
部分賽制設有三副辯員（第五位），負責額外補充論證或專責反駁。"""

QA_REVIEW_SYSTEM_PROMPT = """你係聖呂中辯嘅辯論教練 AI。你嘅工作係幫學生練習辯論問答環節（台下發問或交互答問）。

## 辯論賽制背景
- 每隊四位辯員：主辯（開場立論）、一副（補充論證）、二副（反駁對方）、結辯（總結陳詞）
- 部分賽制設有三副辯員（第五位），負責額外補充論證或專責反駁
- 台下發問：一方向另一方提問，對方即時回應
- 交互答問：雙方輪流問答，考驗即時反應同邏輯能力

## 你嘅任務
按輸入指定嘅次序扮演對方辯員回答或追問。回覆要清楚分開「AI 示範回應 / 追問」同「對用戶表現嘅評語」兩部分。

如果用戶只要求你先提出問題或先作答，先完成該步，暫時毋須評分。

如果用戶已提供內容，請根據以下維度評估：

### 對提問嘅評估
- 清晰度：問題是否明確、對方能否理解
- 尖銳度：能否直指對方論點弱點
- 追問空間：無論對方點答都有得追問
- 防避難度：對方是否容易避開或轉移話題

### 對回答嘅評估
- 直接程度：有冇正面回應問題，定係顧左右而言他
- 防守力：能否守住本方立場、化解對方攻擊
- 扣題能力：回答能否扣回辯題同本方主線
- 反擊意識：有冇喺回答中反守為攻

畀出整體表現評語同具體改善建議，唔需要逐項打分。

用繁體中文回覆。語氣要鼓勵但誠實。"""

STRATEGY_SYSTEM_PROMPT = f"""你係聖呂中辯嘅辯論策略顧問 AI。你嘅工作係幫隊伍策劃比賽主線。

## 辯論賽制
- 每隊四位辯員：主辯（開場立論）、一副（補充論證）、二副（反駁對方）、結辯（總結陳詞）
- 部分賽制設有三副辯員（第五位），負責額外補充論證或專責反駁
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

WEB_RESEARCH_SYSTEM_PROMPT = """你係聖呂中辯嘅辯論資料搜集助手。你嘅工作係即時上網搜尋資料，幫用戶為辯題搵最新、可核查、可引用嘅資料。

## 要求
- 必須使用網上搜尋工具，唔好只靠模型記憶。
- 優先使用官方、政府、學術、國際組織、主流新聞或具公信力機構來源。
- 每一項重要資料或數據都要附上可點擊出處連結，方便用戶 fact check。
- 如資料有年份、地區、定義或統計口徑限制，要清楚標明。
- 如搵唔到可靠來源，要直接講「未能找到可靠來源」，唔好估。
- 用繁體中文回覆，適合辯論備賽使用。

## 回覆格式
1. **搜尋方向**
2. **可引用資料**：每點包含資料、點樣用於辯論、出處
3. **可能有爭議或要小心嘅地方**
4. **可核查來源清單**"""

FACT_CHECK_SYSTEM_PROMPT = """你係聖呂中辯嘅 Fact check 助手。你嘅工作係即時上網搜尋資料，核查用戶輸入嘅陳述係真、假、過時、誤導，定係未能證實。

## 要求
- 必須使用網上搜尋工具，唔好只靠模型記憶。
- 優先使用原始來源、官方數據、研究報告、法例文件、國際組織或可信新聞來源。
- 將陳述拆成可以逐項核查嘅 claim。
- 每項核查都要附上可點擊出處連結，方便用戶自行 fact check。
- 如果證據不足，要標示「未能證實」，唔好硬判真偽。
- 用繁體中文回覆。

## 回覆格式
1. **總體判斷**：真確 / 大致真確 / 部分真確但誤導 / 未能證實 / 錯誤
2. **逐項核查**：原陳述、核查結果、證據、出處
3. **修正版陳述**：如原句有問題，提供較準確講法
4. **可核查來源清單**"""


def _get_model_config(model_label: str | None):
    return AI_MODEL_OPTIONS.get(model_label or DEFAULT_AI_MODEL, AI_MODEL_OPTIONS[DEFAULT_AI_MODEL])


def format_ai_model_label(model_label: str) -> str:
    model_config = _get_model_config(model_label)
    return f"{model_label}（{model_config['pricing_label']}）"


def _format_usd(amount: float) -> str:
    if amount < 0.01:
        return f"US\\${amount:.3f}"
    if amount < 1:
        return f"US\\${amount:.2f}"
    return f"US\\${amount:.1f}"


def _escape_markdown_dollars(text: str) -> str:
    return text.replace("$", r"\$")


def _today_hk() -> str:
    try:
        return datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _read_attr(obj, *names):
    if obj is None:
        return None
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _safe_link_title(title: str | None, fallback: str) -> str:
    text = str(title or fallback).strip() or fallback
    return text.replace("[", "(").replace("]", ")")


def _append_source_list(text: str, sources: list[tuple[str, str]]) -> str:
    seen = set()
    source_lines = []
    for title, url in sources:
        if not url or url in seen:
            continue
        seen.add(url)
        source_lines.append(
            f"{len(source_lines) + 1}. [{_safe_link_title(title, url)}]({url})"
        )
    if not source_lines:
        return text
    return text.rstrip() + "\n\n## 可核查來源\n" + "\n".join(source_lines)


def _estimate_usage_cost(
    model_config,
    input_tokens: int,
    output_tokens: int,
    audio_tokens: int = 0,
) -> float:
    input_price = model_config.get("input_price_per_million") or 0
    audio_price = model_config.get("audio_input_price_per_million") or input_price
    output_price = model_config.get("output_price_per_million") or 0
    return (
        (input_tokens * input_price)
        + (audio_tokens * audio_price)
        + (output_tokens * output_price)
    ) / 1_000_000


def format_ai_model_usage_note(model_label: str) -> str:
    model_config = _get_model_config(model_label)
    speech_text_cost = _estimate_usage_cost(
        model_config,
        SPEECH_REVIEW_INPUT_TOKENS,
        SPEECH_REVIEW_OUTPUT_TOKENS,
    )
    speech_audio_cost = _estimate_usage_cost(
        model_config,
        SPEECH_REVIEW_INPUT_TOKENS,
        SPEECH_REVIEW_OUTPUT_TOKENS,
        SPEECH_REVIEW_AUDIO_TOKENS if model_config["supports_audio"] else 0,
    )
    strategy_cost = _estimate_usage_cost(
        model_config,
        STRATEGY_INPUT_TOKENS,
        STRATEGY_OUTPUT_TOKENS,
    )

    lines = [
        f"**收費單價**：{_escape_markdown_dollars(model_config['paid_rate_note'])}",
        f"**每次估算**：文字稿發言檢查（{SPEECH_UNIT_MINUTES} 分鐘、約 {SPEECH_UNIT_WORDS} 字）約 {_format_usd(speech_text_cost)} / 次；主線策劃約 {_format_usd(strategy_cost)} / 次。",
    ]
    if model_config["supports_audio"]:
        lines.append(
            f"**錄音估算**：4 分鐘錄音檢查約 {_format_usd(speech_audio_cost)} / 次；音訊 tokens 只作粗略估算。"
        )
    lines.append("所有模型經 OpenRouter 統一計費（USD）。估算未必準確，實際用量會因回覆長度而變。")
    return "\n\n".join(lines)


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _now_hk_timestamp() -> str:
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")


def ensure_ai_fund_tables() -> bool:
    if st.session_state.get("_ai_fund_tables_ready") == "provider_v1":
        return True
    try:
        execute_query(CREATE_AI_FUND_TRANSACTIONS)
        execute_query(CREATE_AI_FUND_USAGE_LOGS)
        execute_query(f"ALTER TABLE {TABLE_AI_FUND_TRANSACTIONS} ADD COLUMN IF NOT EXISTS provider TEXT")
        st.session_state["_ai_fund_tables_ready"] = "provider_v1"
        return True
    except Exception as e:
        logger.warning("ensure_ai_fund_tables failed: %s", e)
        return False


def _parse_json_list(raw_value) -> list[str]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def normalize_ai_provider(provider: str | None) -> str:
    text = str(provider or "").strip().lower()
    if text == "openrouter":
        return "openrouter"
    if text in ("gemini", "google"):
        return "gemini"
    if text in ("openai", "gpt", "chatgpt"):
        return "openai"
    if text == "general":
        return "general"
    return "other"


def _save_system_config_value(config_key: str, value: str) -> None:
    execute_query(
        "INSERT INTO system_config (key, value, updated_at) "
        "VALUES (:key, :value, :updated_at) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
        {"key": config_key, "value": value, "updated_at": _now_hk_timestamp()},
    )


def get_ai_fund_settings() -> dict:
    treasurers = _parse_json_list(get_system_config("ai_fund_treasurers"))
    target_hkd = _as_float(get_system_config("ai_fund_target_hkd"), AI_FUND_TARGET_HKD_DEFAULT)
    low_balance_hkd = _as_float(
        get_system_config("ai_fund_low_balance_hkd"),
        AI_FUND_LOW_BALANCE_HKD_DEFAULT,
    )
    payment_instruction = (
        get_system_config("ai_fund_payment_instruction")
        or AI_FUND_PAYMENT_INSTRUCTION_DEFAULT
    )
    return {
        "treasurers": treasurers,
        "target_hkd": target_hkd,
        "low_balance_hkd": low_balance_hkd,
        "payment_instruction": payment_instruction,
    }


def save_ai_fund_treasurers(treasurers: list[str]) -> None:
    cleaned = [str(user_id).strip() for user_id in treasurers if str(user_id).strip()]
    _save_system_config_value("ai_fund_treasurers", json.dumps(cleaned, ensure_ascii=False))


def save_ai_fund_public_settings(
    target_hkd: float,
    low_balance_hkd: float,
    payment_instruction: str,
) -> None:
    _save_system_config_value("ai_fund_target_hkd", f"{float(target_hkd):.2f}")
    _save_system_config_value("ai_fund_low_balance_hkd", f"{float(low_balance_hkd):.2f}")
    _save_system_config_value(
        "ai_fund_payment_instruction",
        payment_instruction.strip() or AI_FUND_PAYMENT_INSTRUCTION_DEFAULT,
    )


def is_ai_fund_treasurer(user_id: str | None) -> bool:
    if not user_id:
        return False
    return str(user_id).strip() in get_ai_fund_settings()["treasurers"]


def get_ai_fund_account_options() -> list[str]:
    df = query_params(
        f"""
        SELECT user_id
        FROM {TABLE_ACCOUNTS}
        WHERE user_id NOT IN ('admin', 'developer', '')
        ORDER BY user_id
        """
    )
    if df.empty:
        return []
    return [str(user_id).strip() for user_id in df["user_id"].tolist() if str(user_id).strip()]


def _confirmed_balance_sql() -> str:
    return f"""
        SELECT COALESCE(SUM(
            CASE
                WHEN transaction_type = 'member_deposit' THEN amount_hkd
                WHEN transaction_type = 'provider_topup' THEN -amount_hkd
                WHEN transaction_type = 'refund' THEN amount_hkd
                WHEN transaction_type = 'adjustment' THEN amount_hkd
                ELSE 0
            END
        ), 0) AS amount
        FROM {TABLE_AI_FUND_TRANSACTIONS}
        WHERE status = 'confirmed'
    """


def _transaction_provider_case_sql() -> str:
    return """
        CASE
            WHEN provider IN ('openrouter', 'gemini', 'openai', 'general', 'other') THEN provider
            WHEN LOWER(COALESCE(payment_method, '')) LIKE '%openrouter%' THEN 'openrouter'
            WHEN LOWER(COALESCE(payment_method, '')) LIKE '%gemini%'
              OR LOWER(COALESCE(payment_method, '')) LIKE '%google%' THEN 'gemini'
            WHEN LOWER(COALESCE(payment_method, '')) LIKE '%openai%'
              OR LOWER(COALESCE(payment_method, '')) LIKE '%gpt%'
              OR LOWER(COALESCE(payment_method, '')) LIKE '%chatgpt%' THEN 'openai'
            ELSE 'other'
        END
    """


def _provider_amount_map(df) -> dict:
    amounts = {"openrouter": 0.0, "gemini": 0.0, "openai": 0.0, "other": 0.0}
    if df.empty:
        return amounts
    for _, row in df.iterrows():
        provider = normalize_ai_provider(row.get("provider"))
        if provider == "general":
            provider = "other"
        amounts[provider] = _as_float(row.get("amount")) if provider not in amounts else amounts[provider] + _as_float(row.get("amount"))
    return amounts


def get_ai_fund_summary() -> dict:
    if not ensure_ai_fund_tables():
        return {}

    settings = get_ai_fund_settings()
    month_start = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-01 00:00:00")

    balance_df = query_params(_confirmed_balance_sql())
    balance_hkd = _as_float(balance_df.iloc[0]["amount"]) if not balance_df.empty else 0.0

    pending_df = query_params(
        f"""
        SELECT COALESCE(SUM(amount_hkd), 0) AS amount
        FROM {TABLE_AI_FUND_TRANSACTIONS}
        WHERE status = 'pending' AND transaction_type = 'member_deposit'
        """
    )
    pending_deposits_hkd = _as_float(pending_df.iloc[0]["amount"]) if not pending_df.empty else 0.0

    usage_df = query_params(
        f"""
        SELECT COALESCE(SUM(estimated_cost_hkd), 0) AS amount
        FROM {TABLE_AI_FUND_USAGE_LOGS}
        WHERE status = 'success' AND created_at >= :month_start
        """,
        {"month_start": month_start},
    )
    monthly_usage_hkd = _as_float(usage_df.iloc[0]["amount"]) if not usage_df.empty else 0.0

    usage_provider_df = query_params(
        f"""
        SELECT COALESCE(provider, 'other') AS provider,
               COALESCE(SUM(estimated_cost_hkd), 0) AS amount
        FROM {TABLE_AI_FUND_USAGE_LOGS}
        WHERE status = 'success' AND created_at >= :month_start
        GROUP BY COALESCE(provider, 'other')
        """,
        {"month_start": month_start},
    )
    monthly_usage_by_provider = _provider_amount_map(usage_provider_df)

    provider_case = _transaction_provider_case_sql()
    topup_df = query_params(
        f"""
        SELECT COALESCE(SUM(amount_hkd), 0) AS amount
        FROM {TABLE_AI_FUND_TRANSACTIONS}
        WHERE status = 'confirmed'
          AND transaction_type = 'provider_topup'
          AND created_at >= :month_start
        """,
        {"month_start": month_start},
    )
    monthly_provider_topup_hkd = _as_float(topup_df.iloc[0]["amount"]) if not topup_df.empty else 0.0

    topup_provider_df = query_params(
        f"""
        SELECT {provider_case} AS provider,
               COALESCE(SUM(amount_hkd), 0) AS amount
        FROM {TABLE_AI_FUND_TRANSACTIONS}
        WHERE status = 'confirmed'
          AND transaction_type = 'provider_topup'
          AND created_at >= :month_start
        GROUP BY {provider_case}
        """,
        {"month_start": month_start},
    )
    monthly_provider_topup_by_provider = _provider_amount_map(topup_provider_df)

    try:
        active_member_count, _ = get_active_user_count()
    except Exception:
        active_member_count = 0
    member_count = active_member_count
    if member_count <= 0:
        member_count = len(get_ai_fund_account_options())

    suggested_total_hkd = max(0.0, settings["target_hkd"] - balance_hkd)
    suggested_per_member_hkd = suggested_total_hkd / member_count if member_count > 0 else 0.0

    return {
        "balance_hkd": balance_hkd,
        "pending_deposits_hkd": pending_deposits_hkd,
        "monthly_usage_hkd": monthly_usage_hkd,
        "monthly_usage_by_provider": monthly_usage_by_provider,
        "monthly_provider_topup_hkd": monthly_provider_topup_hkd,
        "monthly_provider_topup_by_provider": monthly_provider_topup_by_provider,
        "target_hkd": settings["target_hkd"],
        "low_balance_hkd": settings["low_balance_hkd"],
        "member_count": member_count,
        "suggested_total_hkd": suggested_total_hkd,
        "suggested_per_member_hkd": suggested_per_member_hkd,
    }


def create_ai_fund_transaction(
    user_id: str,
    transaction_type: str,
    amount_hkd: float,
    provider: str = "general",
    payment_method: str = "",
    reference_no: str = "",
    note: str = "",
    status: str = "pending",
) -> None:
    if not ensure_ai_fund_tables():
        raise RuntimeError("AI基金資料表尚未就緒。")
    if transaction_type not in AI_FUND_TRANSACTION_LABELS:
        raise ValueError("不支援的交易類型。")
    if status not in ("pending", "confirmed"):
        raise ValueError("不支援的交易狀態。")
    amount = float(amount_hkd)
    if transaction_type != "adjustment" and amount <= 0:
        raise ValueError("金額必須大於 0。")
    if transaction_type == "adjustment" and amount == 0:
        raise ValueError("調整金額不能為 0。")

    resolved_provider = normalize_ai_provider(provider)
    confirmed_by = user_id if status == "confirmed" else None
    confirmed_at = _now_hk_timestamp() if status == "confirmed" else None
    execute_query(
        f"""
        INSERT INTO {TABLE_AI_FUND_TRANSACTIONS} (
            transaction_type, status, provider, amount_hkd, payment_method, reference_no,
            note, created_by, created_at, confirmed_by, confirmed_at
        )
        VALUES (
            :transaction_type, :status, :provider, :amount_hkd, :payment_method, :reference_no,
            :note, :created_by, :created_at, :confirmed_by, :confirmed_at
        )
        """,
        {
            "transaction_type": transaction_type,
            "status": status,
            "provider": resolved_provider,
            "amount_hkd": amount,
            "payment_method": payment_method.strip(),
            "reference_no": reference_no.strip(),
            "note": note.strip(),
            "created_by": user_id,
            "created_at": _now_hk_timestamp(),
            "confirmed_by": confirmed_by,
            "confirmed_at": confirmed_at,
        },
    )


def update_ai_fund_transaction_status(
    transaction_id: int,
    status: str,
    user_id: str,
    status_note: str = "",
) -> int:
    if not ensure_ai_fund_tables():
        return 0
    if status == "confirmed":
        return execute_query_count(
            f"""
            UPDATE {TABLE_AI_FUND_TRANSACTIONS}
            SET status = 'confirmed',
                confirmed_by = :user_id,
                confirmed_at = :updated_at,
                status_note = :status_note
            WHERE id = :transaction_id AND status = 'pending'
            """,
            {
                "transaction_id": int(transaction_id),
                "user_id": user_id,
                "updated_at": _now_hk_timestamp(),
                "status_note": status_note.strip(),
            },
        )
    if status == "rejected":
        return execute_query_count(
            f"""
            UPDATE {TABLE_AI_FUND_TRANSACTIONS}
            SET status = 'rejected',
                rejected_by = :user_id,
                rejected_at = :updated_at,
                status_note = :status_note
            WHERE id = :transaction_id AND status = 'pending'
            """,
            {
                "transaction_id": int(transaction_id),
                "user_id": user_id,
                "updated_at": _now_hk_timestamp(),
                "status_note": status_note.strip(),
            },
        )
    return 0


def get_ai_fund_transactions(user_id: str | None = None, treasurer: bool = False, limit: int = 80):
    if not ensure_ai_fund_tables():
        return query_params("SELECT 1 WHERE FALSE")
    where_clause = ""
    params = {"limit": int(limit)}
    if not treasurer:
        where_clause = "WHERE created_by = :user_id"
        params["user_id"] = user_id
    return query_params(
        f"""
        SELECT
            id,
            transaction_type,
            status,
            COALESCE(provider, 'other') AS provider,
            amount_hkd,
            payment_method,
            reference_no,
            note,
            created_by,
            created_at,
            confirmed_by,
            confirmed_at,
            rejected_by,
            rejected_at,
            status_note
        FROM {TABLE_AI_FUND_TRANSACTIONS}
        {where_clause}
        ORDER BY created_at DESC, id DESC
        LIMIT :limit
        """,
        params,
    )


def get_ai_fund_usage_logs(user_id: str | None = None, treasurer: bool = False, limit: int = 50):
    if not ensure_ai_fund_tables():
        return query_params("SELECT 1 WHERE FALSE")
    where_clause = ""
    params = {"limit": int(limit)}
    if not treasurer:
        where_clause = "WHERE user_id = :user_id"
        params["user_id"] = user_id
    return query_params(
        f"""
        SELECT
            id,
            user_id,
            feature,
            model_label,
            provider,
            estimated_cost_hkd,
            input_tokens,
            output_tokens,
            audio_tokens,
            search_calls,
            status,
            error_message,
            created_at
        FROM {TABLE_AI_FUND_USAGE_LOGS}
        {where_clause}
        ORDER BY created_at DESC, id DESC
        LIMIT :limit
        """,
        params,
    )


def get_ai_fund_usage_summary():
    if not ensure_ai_fund_tables():
        return query_params("SELECT 1 WHERE FALSE")
    return query_params(
        f"""
        SELECT
            TO_CHAR(created_at, 'YYYY-MM') AS month,
            user_id,
            COALESCE(provider, 'other') AS provider,
            feature,
            model_label,
            COUNT(*) AS uses,
            ROUND(SUM(estimated_cost_hkd)::numeric, 4) AS estimated_cost_hkd
        FROM {TABLE_AI_FUND_USAGE_LOGS}
        WHERE status = 'success'
        GROUP BY month, user_id, COALESCE(provider, 'other'), feature, model_label
        ORDER BY month DESC, estimated_cost_hkd DESC
        """
    )


def estimate_ai_feature_usage(
    feature: str,
    model_label: str | None,
    has_audio: bool = False,
) -> dict:
    model_config = _get_model_config(model_label)
    if feature == "speech_review":
        input_tokens = SPEECH_REVIEW_INPUT_TOKENS
        output_tokens = SPEECH_REVIEW_OUTPUT_TOKENS
        audio_tokens = SPEECH_REVIEW_AUDIO_TOKENS if has_audio and model_config["supports_audio"] else 0
        search_calls = 0
        usd = _estimate_usage_cost(model_config, input_tokens, output_tokens, audio_tokens)
    elif feature == "strategy":
        input_tokens = STRATEGY_INPUT_TOKENS
        output_tokens = STRATEGY_OUTPUT_TOKENS
        audio_tokens = 0
        search_calls = 0
        usd = _estimate_usage_cost(model_config, input_tokens, output_tokens)
    elif feature in ("web_research", "fact_check"):
        input_tokens = WEB_RESEARCH_INPUT_TOKENS
        output_tokens = WEB_RESEARCH_OUTPUT_TOKENS
        audio_tokens = 0
        search_calls = 1
        usd = (
            _estimate_usage_cost(model_config, input_tokens, output_tokens)
            + OPENROUTER_WEB_SEARCH_USD_PER_CALL
        )
    else:
        input_tokens = output_tokens = audio_tokens = search_calls = 0
        usd = 0.0

    return {
        "feature": feature,
        "model_label": model_label or DEFAULT_AI_MODEL,
        "provider": model_config.get("provider", ""),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "audio_tokens": audio_tokens,
        "search_calls": search_calls,
        "estimated_cost_usd": round(usd, 4),
        "estimated_cost_hkd": round(usd * HKD_PER_USD, 4),
    }


def is_successful_ai_result(result: str | None) -> bool:
    if not result:
        return False
    text = str(result).lstrip()
    return not text.startswith(("⚠️", "❌"))


def log_ai_fund_usage(
    user_id: str,
    feature: str,
    model_label: str | None,
    success: bool,
    has_audio: bool = False,
    error_message: str = "",
) -> None:
    if not ensure_ai_fund_tables():
        return
    usage = estimate_ai_feature_usage(feature, model_label, has_audio=has_audio)
    estimated_cost_hkd = usage["estimated_cost_hkd"] if success else 0
    execute_query(
        f"""
        INSERT INTO {TABLE_AI_FUND_USAGE_LOGS} (
            user_id, feature, model_label, provider, estimated_cost_hkd,
            input_tokens, output_tokens, audio_tokens, search_calls,
            status, error_message, created_at
        )
        VALUES (
            :user_id, :feature, :model_label, :provider, :estimated_cost_hkd,
            :input_tokens, :output_tokens, :audio_tokens, :search_calls,
            :status, :error_message, :created_at
        )
        """,
        {
            "user_id": user_id,
            "feature": feature,
            "model_label": usage["model_label"],
            "provider": usage["provider"],
            "estimated_cost_hkd": estimated_cost_hkd,
            "input_tokens": usage["input_tokens"] if success else 0,
            "output_tokens": usage["output_tokens"] if success else 0,
            "audio_tokens": usage["audio_tokens"] if success else 0,
            "search_calls": usage["search_calls"] if success else 0,
            "status": "success" if success else "failed",
            "error_message": error_message[:500] if error_message else "",
            "created_at": _now_hk_timestamp(),
        },
    )


def _get_openrouter_client():
    if "OPENROUTER_API_KEY" not in st.secrets:
        return None, "❌ 未設定 OpenRouter API Key，請聯絡開發人員。"
    try:
        from openai import OpenAI
    except ImportError:
        return None, "❌ OpenAI SDK 尚未安裝，請先更新 requirements.txt 並重新部署。"
    if "_openrouter_client" not in st.session_state:
        st.session_state["_openrouter_client"] = OpenAI(
            api_key=st.secrets["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
        )
    return st.session_state["_openrouter_client"], None


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


def _generate_response(model_config, system_prompt: str, user_text: str) -> str:
    client, error = _get_openrouter_client()
    if error:
        return error
    try:
        response = client.chat.completions.create(
            model=model_config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            temperature=0.7,
        )
        return response.choices[0].message.content or "AI 未能生成回覆，請再試一次。"
    except Exception as e:
        return _format_ai_error("OpenRouter", e)


def _generate_audio_response(model_config, system_prompt: str, user_text: str, audio_bytes: bytes) -> str:
    client, error = _get_openrouter_client()
    if error:
        return error
    try:
        content = [{"type": "text", "text": user_text}]
        content.append({
            "type": "input_audio",
            "input_audio": {
                "data": base64.b64encode(audio_bytes).decode(),
                "format": "wav",
            },
        })
        response = client.chat.completions.create(
            model=model_config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            temperature=0.7,
        )
        return response.choices[0].message.content or "AI 未能生成回覆，請再試一次。"
    except Exception as e:
        return _format_ai_error("OpenRouter", e)


def _format_web_search_response(response) -> str:
    text = response.choices[0].message.content or "AI 未能生成回覆，請再試一次。"
    annotations = []
    message = response.choices[0].message
    if hasattr(message, "annotations") and message.annotations:
        annotations = message.annotations
    elif hasattr(message, "content") and isinstance(message.content, list):
        for part in message.content:
            if hasattr(part, "annotations"):
                annotations.extend(part.annotations or [])

    sources_by_url = {}
    sorted_annotations = sorted(
        annotations,
        key=lambda a: _read_attr(a, "end_index", "endIndex") or 0,
        reverse=True,
    )
    for annotation in sorted_annotations:
        if _read_attr(annotation, "type") != "url_citation":
            continue
        url = _read_attr(annotation, "url")
        title = _read_attr(annotation, "title") or url
        end_index = _read_attr(annotation, "end_index", "endIndex")
        if not url:
            continue
        sources_by_url[url] = (title, url)
        if end_index is not None and end_index <= len(text):
            source_no = list(sources_by_url).index(url) + 1
            text = text[:end_index] + f" [{source_no}]({url})" + text[end_index:]

    return _append_source_list(text, list(sources_by_url.values()))


def _generate_web_response(model_config, system_prompt: str, user_text: str) -> str:
    client, error = _get_openrouter_client()
    if error:
        return error
    try:
        response = client.chat.completions.create(
            model=model_config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            tools=[{
                "type": "openrouter:web_search",
                "parameters": {"search_context_size": "medium"},
            }],
            temperature=0.3,
        )
        return _format_web_search_response(response)
    except Exception as e:
        return _format_ai_error("OpenRouter", e)


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
        return "⚠️ 此模型不支援錄音分析。請選擇支援錄音嘅模型，或貼上文字稿再試。"

    position_label = POSITION_LABELS.get(position, "")
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

    is_qa_mode = text and ("## 台下發問練習" in text or "## 交互答問練習" in text)
    system_prompt = QA_REVIEW_SYSTEM_PROMPT if is_qa_mode else SPEECH_REVIEW_SYSTEM_PROMPT

    user_text = "\n".join(user_text_lines)

    if audio_bytes:
        if not text:
            user_text += "\n\n以下係我嘅演辭錄音，請分析："
        return _generate_audio_response(model_config, system_prompt, user_text, audio_bytes)

    return _generate_response(model_config, system_prompt, user_text)


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

    return _generate_response(model_config, STRATEGY_SYSTEM_PROMPT, "\n".join(user_lines))


def research_web(
    topic: str,
    research_need: str,
    model_label: str | None = None,
) -> str:
    model_config = _get_model_config(model_label)
    user_text = f"""今日日期：{_today_hk()}

辯題：{topic}

想搵嘅資料：
{research_need}

請即時上網搜尋最新、可核查資料。每一項可引用資料都要附上來源連結，並標明資料年份、地區或口徑限制。"""

    return _generate_web_response(
        model_config,
        WEB_RESEARCH_SYSTEM_PROMPT,
        user_text,
    )


def fact_check_claim(
    statement: str,
    model_label: str | None = None,
) -> str:
    model_config = _get_model_config(model_label)
    user_text = f"""今日日期：{_today_hk()}

需要核查嘅陳述：
{statement}

請即時上網搜尋可靠來源，逐項驗證以上陳述嘅真偽。每個判斷都要附上來源連結。"""

    return _generate_web_response(
        model_config,
        FACT_CHECK_SYSTEM_PROMPT,
        user_text,
    )
