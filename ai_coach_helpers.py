import streamlit as st
import logging
import json
import base64
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
from debate_timing import get_debate_timer_config, get_full_mock_sequence
from ai_model_config import DEFAULT_AI_MODEL, AI_MODEL_OPTIONS, NON_MANUAL_MODEL_OPTIONS
from prompts import (
    SPEECH_REVIEW_SYSTEM_PROMPT,
    QA_REVIEW_SYSTEM_PROMPT,
    build_strategy_prompt,
    build_strategy_user_prompt,
    WEB_RESEARCH_SYSTEM_PROMPT,
    FACT_CHECK_SYSTEM_PROMPT,
    build_web_research_user_prompt,
    build_fact_check_user_prompt,
    build_free_debate_live_prompt,
    build_full_mock_live_prompt,
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
FREE_DEBATE_LIVE_MODEL_LABEL = "Gemini 3.1 Flash Live Preview"
FREE_DEBATE_LIVE_MODEL = "gemini-3.1-flash-live-preview"
FREE_DEBATE_LIVE_DEFAULT_MINUTES = 10
FREE_DEBATE_LIVE_AUDIO_TOKENS_PER_SECOND = 32
FREE_DEBATE_LIVE_AI_REPLY_RATIO = 0.5
FREE_DEBATE_LIVE_TEXT_INPUT_PRICE_PER_MILLION = 0.50
FREE_DEBATE_LIVE_AUDIO_INPUT_PRICE_PER_MILLION = 3.00
FREE_DEBATE_LIVE_AUDIO_OUTPUT_PRICE_PER_MILLION = 12.00
# 完整 Mock：一次開波預先 log 嘅 billed 時長上限（分鐘）。ephemeral token 仍按全長，唔受此限。
# 一個 Gemini Live session 實際跑唔到成場 Mock，所以封頂避免一開波就記幾十分鐘。
# 待 session 分段完成後，改為按每個 session 實際時長 log。
FULL_MOCK_LIVE_BILLED_MINUTES_CAP = 15.0

AI_FUND_TARGET_HKD_DEFAULT = 100.0
AI_FUND_LOW_BALANCE_HKD_DEFAULT = 20.0
AI_FUND_PAYMENT_INSTRUCTION_DEFAULT = "請向AI基金管理員查詢 FPS / 現金 / 轉賬安排，付款後在此提交入數紀錄。"

AI_FEATURE_LABELS = {
    "speech_review": "練習發言",
    "strategy": "主線策劃",
    "web_research": "搵料易",
    "fact_check": "Fact Check易",
    "free_debate_live": "打Free De",
    "full_mock_live": "打完整Mock",
}

AI_FUND_TRANSACTION_LABELS = {
    "member_deposit": "成員入數",
    "provider_topup": "AI provider 充值 / 帳單",
    "refund": "退款",
    "adjustment": "手動調整",
}

AI_PROVIDER_LABELS = {
    "general": "整體AI基金",
    "gemini": "Gemini",
    "openrouter": "OpenRouter",
    "openai": "GPT",
    "other": "其他",
}

AI_ENABLED_PROVIDERS_CONFIG_KEY = "ai_enabled_providers"
AI_DEFAULT_MODEL_CONFIG_KEY = "ai_default_model"
GOOGLE_AI_STUDIO_BALANCE_USD_CONFIG_KEY = "google_ai_studio_balance_usd"
GOOGLE_AI_STUDIO_BALANCE_UPDATED_AT_CONFIG_KEY = "google_ai_studio_balance_updated_at"
GOOGLE_AI_STUDIO_BALANCE_UPDATED_BY_CONFIG_KEY = "google_ai_studio_balance_updated_by"

AI_MODEL_GENERATION_OPTIONS = {
    **NON_MANUAL_MODEL_OPTIONS,
    **AI_MODEL_OPTIONS,
}


def _get_model_config(model_label: str | None):
    return AI_MODEL_GENERATION_OPTIONS.get(
        model_label or DEFAULT_AI_MODEL,
        AI_MODEL_GENERATION_OPTIONS[DEFAULT_AI_MODEL],
    )


def format_ai_model_label(model_label: str) -> str:
    model_config = _get_model_config(model_label)
    return f"{model_label}（{model_config.get('selection_label', model_config['pricing_label'])}）"


def _format_usd(amount: float) -> str:
    if amount < 0.01:
        return f"US\\${amount:.3f}"
    if amount < 1:
        return f"US\\${amount:.2f}"
    return f"US\\${amount:.1f}"


def _escape_markdown_dollars(text: str) -> str:
    return text.replace("$", r"\$")


def format_usd_money(amount, decimals: int = 2, escape_markdown: bool = False) -> str:
    try:
        text = f"US${float(amount):,.{decimals}f}"
    except (TypeError, ValueError):
        text = f"US${0:,.{decimals}f}"
    return _escape_markdown_dollars(text) if escape_markdown else text


def format_hkd_money(amount, decimals: int = 2) -> str:
    try:
        return f"HKD {float(amount):,.{decimals}f}"
    except (TypeError, ValueError):
        return f"HKD {0:,.{decimals}f}"


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


def _format_gemini_grounded_response(response) -> str:
    text = response.text or "AI 未能生成回覆，請再試一次。"
    sources_by_index = {}

    candidates = _read_attr(response, "candidates") or []
    if not candidates:
        return text

    metadata = _read_attr(candidates[0], "grounding_metadata", "groundingMetadata")
    chunks = _read_attr(metadata, "grounding_chunks", "groundingChunks") or []
    supports = _read_attr(metadata, "grounding_supports", "groundingSupports") or []

    sorted_supports = sorted(
        supports,
        key=lambda s: _read_attr(_read_attr(s, "segment"), "end_index", "endIndex") or 0,
        reverse=True,
    )
    for support in sorted_supports:
        segment = _read_attr(support, "segment")
        end_index = _read_attr(segment, "end_index", "endIndex")
        chunk_indices = _read_attr(
            support, "grounding_chunk_indices", "groundingChunkIndices"
        ) or []
        if end_index is None or not chunk_indices or end_index > len(text):
            continue

        citation_links = []
        for chunk_index in chunk_indices:
            if chunk_index >= len(chunks):
                continue
            web = _read_attr(chunks[chunk_index], "web")
            url = _read_attr(web, "uri")
            title = _read_attr(web, "title") or f"來源 {chunk_index + 1}"
            if not url:
                continue
            citation_links.append(f"[{chunk_index + 1}]({url})")
            sources_by_index[chunk_index] = (title, url)
        if citation_links:
            text = (
                text[:end_index]
                + " "
                + ", ".join(citation_links)
                + text[end_index:]
            )

    return _append_source_list(
        text,
        [sources_by_index[i] for i in sorted(sources_by_index)],
    )


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


def _get_model_label_from_config(model_config) -> str:
    model_slug = model_config.get("model")
    for label, config in AI_MODEL_GENERATION_OPTIONS.items():
        if config.get("model") == model_slug:
            return label
    return DEFAULT_AI_MODEL


def _build_usage_record(
    feature: str,
    model_label: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    audio_tokens: int = 0,
    search_calls: int = 0,
    estimated_cost_usd: float | None = None,
    cost_source: str = "estimate",
    model_config=None,
) -> dict:
    if estimated_cost_usd is None:
        resolved_config = model_config or _get_model_config(model_label)
        estimated_cost_usd = _estimate_usage_cost(
            resolved_config,
            int(input_tokens or 0),
            int(output_tokens or 0),
            int(audio_tokens or 0),
        )
        if search_calls:
            estimated_cost_usd += (resolved_config.get("web_search_price_per_call") or 0) * int(search_calls)

    return {
        "feature": feature,
        "model_label": model_label,
        "provider": provider,
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "audio_tokens": int(audio_tokens or 0),
        "search_calls": int(search_calls or 0),
        "estimated_cost_usd": round(float(estimated_cost_usd or 0), 6),
        "estimated_cost_hkd": round(float(estimated_cost_usd or 0) * HKD_PER_USD, 4),
        "cost_source": cost_source,
    }




def _modality_audio_tokens(details) -> int:
    total = 0
    for detail in details or []:
        modality = str(_read_attr(detail, "modality", "Modality") or "").upper()
        if "AUDIO" in modality:
            total += int(_read_attr(detail, "token_count", "tokenCount") or 0)
    return total


def _usage_from_gemini_response(
    response,
    model_config,
    fallback_audio_tokens: int = 0,
    search_calls: int = 0,
) -> dict | None:
    usage = _read_attr(response, "usage_metadata", "usageMetadata")
    if not usage:
        return None

    prompt_tokens = int(_read_attr(usage, "prompt_token_count", "promptTokenCount") or 0)
    output_tokens = int(_read_attr(usage, "candidates_token_count", "candidatesTokenCount") or 0)
    output_tokens += int(_read_attr(usage, "thoughts_token_count", "thoughtsTokenCount") or 0)
    audio_tokens = _modality_audio_tokens(
        _read_attr(usage, "prompt_tokens_details", "promptTokensDetails")
    )
    if not audio_tokens and fallback_audio_tokens:
        audio_tokens = int(fallback_audio_tokens)
    input_tokens = max(0, prompt_tokens - audio_tokens)

    return _build_usage_record(
        feature="",
        model_label=_get_model_label_from_config(model_config),
        provider="gemini",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        audio_tokens=audio_tokens,
        search_calls=search_calls,
        cost_source="gemini_usage_metadata",
        model_config=model_config,
    )


def _usage_from_openrouter_response(response, model_config) -> dict | None:
    usage = _read_attr(response, "usage")
    if not usage:
        return None
    return _build_usage_record(
        feature="",
        model_label=_get_model_label_from_config(model_config),
        provider="openrouter",
        input_tokens=int(_read_attr(usage, "prompt_tokens", "promptTokens") or 0),
        output_tokens=int(_read_attr(usage, "completion_tokens", "completionTokens") or 0),
        cost_source="openrouter_response_usage",
        model_config=model_config,
    )


def _fetch_json(url: str, token: str, timeout: int = 5) -> tuple[dict | None, str | None]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def _usage_from_openrouter_generation(response, model_config) -> dict | None:
    generation_id = _read_attr(response, "id")
    if not generation_id or "OPENROUTER_API_KEY" not in st.secrets:
        return None
    url = "https://openrouter.ai/api/v1/generation?" + urllib.parse.urlencode({"id": generation_id})
    payload, error = _fetch_json(url, st.secrets["OPENROUTER_API_KEY"])
    if error or not payload:
        return None
    data = payload.get("data") or {}
    cost_usd = _as_float(data.get("total_cost"), None)
    if cost_usd is None:
        cost_usd = _as_float(data.get("usage"), None)
    if cost_usd is None:
        return None

    return _build_usage_record(
        feature="",
        model_label=_get_model_label_from_config(model_config),
        provider="openrouter",
        input_tokens=int(data.get("native_tokens_prompt") or data.get("tokens_prompt") or 0),
        output_tokens=int(
            (data.get("native_tokens_completion") or data.get("tokens_completion") or 0)
            + (data.get("native_tokens_reasoning") or 0)
        ),
        audio_tokens=int(data.get("num_input_audio_prompt") or 0),
        search_calls=1 if data.get("num_search_results") else 0,
        estimated_cost_usd=cost_usd,
        cost_source="openrouter_generation_stats",
        model_config=model_config,
    )


def _capture_openrouter_usage(response, model_config) -> dict | None:
    usage = _usage_from_openrouter_generation(response, model_config)
    if not usage:
        usage = _usage_from_openrouter_response(response, model_config)
    return usage


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
        f"**每次估算**：文字稿練習發言（{SPEECH_UNIT_MINUTES} 分鐘、約 {SPEECH_UNIT_WORDS} 字）約 {_format_usd(speech_text_cost)} / 次；主線策劃約 {_format_usd(strategy_cost)} / 次。",
    ]
    if model_config["supports_audio"]:
        lines.append(
            f"**錄音估算**：4 分鐘錄音檢查約 {_format_usd(speech_audio_cost)} / 次；音訊 tokens 只作粗略估算。"
        )
    if model_config["provider"] == "gemini":
        lines.append("Gemini 模型經 Google Gemini API 直連；估算按 paid tier / 超額價格，免費額度及實際用量可能不同。")
    else:
        lines.append("OpenRouter 模型經 OpenRouter 計費（USD）；搜尋工具可能另收 OpenRouter 或原生 provider 搜尋費。估算未必準確，實際用量會因回覆長度而變。")
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
    if st.session_state.get("_ai_fund_tables_ready") == "usage_actual_v3":
        return True
    try:
        execute_query(CREATE_AI_FUND_TRANSACTIONS)
        execute_query(CREATE_AI_FUND_USAGE_LOGS)
        execute_query(f"ALTER TABLE {TABLE_AI_FUND_TRANSACTIONS} ADD COLUMN IF NOT EXISTS provider TEXT")
        execute_query(f"ALTER TABLE {TABLE_AI_FUND_USAGE_LOGS} ADD COLUMN IF NOT EXISTS estimated_cost_usd NUMERIC(12, 6) DEFAULT 0")
        execute_query(f"ALTER TABLE {TABLE_AI_FUND_USAGE_LOGS} ADD COLUMN IF NOT EXISTS cost_source TEXT DEFAULT 'estimate'")
        execute_query(f"ALTER TABLE {TABLE_AI_FUND_USAGE_LOGS} DROP CONSTRAINT IF EXISTS {TABLE_AI_FUND_USAGE_LOGS}_feature_check")
        execute_query(f"ALTER TABLE {TABLE_AI_FUND_USAGE_LOGS} DROP CONSTRAINT IF EXISTS chk_ai_fund_usage_feature")
        execute_query(
            f"""
            ALTER TABLE {TABLE_AI_FUND_USAGE_LOGS}
            ADD CONSTRAINT chk_ai_fund_usage_feature
            CHECK (feature IN ('speech_review', 'strategy', 'web_research', 'fact_check', 'free_debate_live', 'full_mock_live'))
            """
        )
        st.session_state["_ai_fund_tables_ready"] = "usage_actual_v3"
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


def get_ai_provider_options() -> list[str]:
    providers = []
    for model_config in AI_MODEL_OPTIONS.values():
        provider = model_config.get("provider", "")
        if provider and provider not in providers:
            providers.append(provider)
    return providers


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


def get_ai_model_settings() -> dict:
    provider_options = get_ai_provider_options()
    enabled_providers = [
        provider for provider in _parse_json_list(get_system_config(AI_ENABLED_PROVIDERS_CONFIG_KEY))
        if provider in provider_options
    ]
    if not enabled_providers:
        enabled_providers = provider_options

    model_options = {
        label: config
        for label, config in AI_MODEL_OPTIONS.items()
        if config.get("provider") in enabled_providers
    }
    if not model_options:
        enabled_providers = provider_options
        model_options = AI_MODEL_OPTIONS.copy()

    default_model = str(get_system_config(AI_DEFAULT_MODEL_CONFIG_KEY) or DEFAULT_AI_MODEL).strip()
    if default_model not in model_options:
        default_model = DEFAULT_AI_MODEL if DEFAULT_AI_MODEL in model_options else next(iter(model_options))

    return {
        "provider_options": provider_options,
        "enabled_providers": enabled_providers,
        "model_options": model_options,
        "default_model": default_model,
    }


def save_ai_model_settings(enabled_providers: list[str], default_model: str) -> None:
    provider_options = get_ai_provider_options()
    cleaned_providers = []
    for provider in enabled_providers:
        provider = str(provider).strip()
        if provider in provider_options and provider not in cleaned_providers:
            cleaned_providers.append(provider)
    if not cleaned_providers:
        raise ValueError("請至少啟用一個 AI Provider。")

    model_options = {
        label: config
        for label, config in AI_MODEL_OPTIONS.items()
        if config.get("provider") in cleaned_providers
    }
    if not model_options:
        raise ValueError("所選 Provider 沒有可用模型。")
    resolved_default_model = str(default_model or "").strip()
    if resolved_default_model not in model_options:
        resolved_default_model = next(iter(model_options))

    _save_system_config_value(
        AI_ENABLED_PROVIDERS_CONFIG_KEY,
        json.dumps(cleaned_providers, ensure_ascii=False),
    )
    _save_system_config_value(AI_DEFAULT_MODEL_CONFIG_KEY, resolved_default_model)


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


def get_google_ai_studio_balance() -> dict:
    balance_raw = get_system_config(GOOGLE_AI_STUDIO_BALANCE_USD_CONFIG_KEY)
    balance_usd = None if balance_raw in (None, "") else _as_float(balance_raw, None)
    return {
        "balance_usd": balance_usd,
        "balance_hkd": None if balance_usd is None else balance_usd * HKD_PER_USD,
        "updated_at": get_system_config(GOOGLE_AI_STUDIO_BALANCE_UPDATED_AT_CONFIG_KEY) or "",
        "updated_by": get_system_config(GOOGLE_AI_STUDIO_BALANCE_UPDATED_BY_CONFIG_KEY) or "",
    }


def save_google_ai_studio_balance(balance_usd: float, user_id: str) -> None:
    amount = float(balance_usd)
    if amount < 0:
        raise ValueError("Google AI Studio 餘額不能為負數。")
    updated_at = _now_hk_timestamp()
    _save_system_config_value(GOOGLE_AI_STUDIO_BALANCE_USD_CONFIG_KEY, f"{amount:.4f}")
    _save_system_config_value(GOOGLE_AI_STUDIO_BALANCE_UPDATED_AT_CONFIG_KEY, updated_at)
    _save_system_config_value(GOOGLE_AI_STUDIO_BALANCE_UPDATED_BY_CONFIG_KEY, str(user_id or ""))


def is_ai_fund_treasurer(user_id: str | None) -> bool:
    if not user_id:
        return False
    return str(user_id).strip() in get_ai_fund_settings()["treasurers"]


def reset_ai_fund_usage_logs() -> int:
    if not ensure_ai_fund_tables():
        raise RuntimeError("AI基金資料表尚未就緒。")
    result = execute_query(f"DELETE FROM {TABLE_AI_FUND_USAGE_LOGS}")
    return result.rowcount if hasattr(result, "rowcount") else 0


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
    recent_start = (datetime.now(ZoneInfo("Asia/Hong_Kong")) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

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
        WHERE status = 'success' AND created_at >= :recent_start
        """,
        {"recent_start": recent_start},
    )
    recent_usage_hkd = _as_float(usage_df.iloc[0]["amount"]) if not usage_df.empty else 0.0

    usage_provider_df = query_params(
        f"""
        SELECT COALESCE(provider, 'other') AS provider,
               COALESCE(SUM(estimated_cost_hkd), 0) AS amount
        FROM {TABLE_AI_FUND_USAGE_LOGS}
        WHERE status = 'success' AND created_at >= :recent_start
        GROUP BY COALESCE(provider, 'other')
        """,
        {"recent_start": recent_start},
    )
    recent_usage_by_provider = _provider_amount_map(usage_provider_df)

    provider_case = _transaction_provider_case_sql()
    topup_df = query_params(
        f"""
        SELECT COALESCE(SUM(amount_hkd), 0) AS amount
        FROM {TABLE_AI_FUND_TRANSACTIONS}
        WHERE status = 'confirmed'
          AND transaction_type = 'provider_topup'
          AND created_at >= :recent_start
        """,
        {"recent_start": recent_start},
    )
    recent_provider_topup_hkd = _as_float(topup_df.iloc[0]["amount"]) if not topup_df.empty else 0.0

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
        "recent_usage_hkd": recent_usage_hkd,
        "recent_usage_by_provider": recent_usage_by_provider,
        "recent_provider_topup_hkd": recent_provider_topup_hkd,
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
            estimated_cost_usd,
            estimated_cost_hkd,
            input_tokens,
            output_tokens,
            audio_tokens,
            search_calls,
            cost_source,
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
    duration_minutes: float | None = None,
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
        web_search_usd = model_config.get("web_search_price_per_call") or 0
        usd = (
            _estimate_usage_cost(model_config, input_tokens, output_tokens)
            + web_search_usd
        )
    elif feature in ("free_debate_live", "full_mock_live"):
        minutes = float(duration_minutes or FREE_DEBATE_LIVE_DEFAULT_MINUTES)
        input_tokens = 0
        audio_tokens = int(minutes * 60 * FREE_DEBATE_LIVE_AUDIO_TOKENS_PER_SECOND)
        output_tokens = int(
            minutes
            * 60
            * FREE_DEBATE_LIVE_AI_REPLY_RATIO
            * FREE_DEBATE_LIVE_AUDIO_TOKENS_PER_SECOND
        )
        search_calls = 0
        usd = (
            (audio_tokens * FREE_DEBATE_LIVE_AUDIO_INPUT_PRICE_PER_MILLION)
            + (output_tokens * FREE_DEBATE_LIVE_AUDIO_OUTPUT_PRICE_PER_MILLION)
        ) / 1_000_000
        return {
            "feature": feature,
            "model_label": FREE_DEBATE_LIVE_MODEL_LABEL,
            "provider": "gemini",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "audio_tokens": audio_tokens,
            "search_calls": search_calls,
            "estimated_cost_usd": round(usd, 6),
            "estimated_cost_hkd": round(usd * HKD_PER_USD, 4),
            "cost_source": "live_estimate",
        }
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
        "cost_source": "estimate",
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
    usage_override: dict | None = None,
) -> None:
    if not ensure_ai_fund_tables():
        return
    estimate = estimate_ai_feature_usage(feature, model_label, has_audio=has_audio)
    if success and usage_override:
        usage = estimate.copy()
        usage["input_tokens"] = usage_override.get("input_tokens", estimate["input_tokens"])
        usage["output_tokens"] = usage_override.get("output_tokens", estimate["output_tokens"])
        usage["audio_tokens"] = usage_override.get("audio_tokens", estimate["audio_tokens"])
        usage["search_calls"] = usage_override.get("search_calls") or estimate["search_calls"]
        usage["estimated_cost_usd"] = usage_override.get("estimated_cost_usd", estimate["estimated_cost_usd"])
        usage["estimated_cost_hkd"] = usage_override.get("estimated_cost_hkd", estimate["estimated_cost_hkd"])
        usage["cost_source"] = usage_override.get("cost_source", "actual")
    else:
        usage = estimate
    estimated_cost_hkd = usage["estimated_cost_hkd"] if success else 0
    estimated_cost_usd = usage.get("estimated_cost_usd", 0) if success else 0
    execute_query(
        f"""
        INSERT INTO {TABLE_AI_FUND_USAGE_LOGS} (
            user_id, feature, model_label, provider, estimated_cost_usd, estimated_cost_hkd,
            input_tokens, output_tokens, audio_tokens, search_calls,
            cost_source, status, error_message, created_at
        )
        VALUES (
            :user_id, :feature, :model_label, :provider, :estimated_cost_usd, :estimated_cost_hkd,
            :input_tokens, :output_tokens, :audio_tokens, :search_calls,
            :cost_source, :status, :error_message, :created_at
        )
        """,
        {
            "user_id": user_id,
            "feature": feature,
            "model_label": usage["model_label"],
            "provider": usage["provider"],
            "estimated_cost_usd": estimated_cost_usd,
            "estimated_cost_hkd": estimated_cost_hkd,
            "input_tokens": usage["input_tokens"] if success else 0,
            "output_tokens": usage["output_tokens"] if success else 0,
            "audio_tokens": usage["audio_tokens"] if success else 0,
            "search_calls": usage["search_calls"] if success else 0,
            "cost_source": usage.get("cost_source", "estimate") if success else "failed",
            "status": "success" if success else "failed",
            "error_message": error_message[:500] if error_message else "",
            "created_at": _now_hk_timestamp(),
        },
    )


def _get_gemini_modules():
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None, None, "❌ Gemini SDK 尚未安裝，請先更新 requirements.txt 並重新部署。"
    return genai, types, None


def _get_gemini_client():
    if "GEMINI_API_KEY" not in st.secrets:
        return None, "❌ 未設定 Gemini API Key，請聯絡開發人員。"
    genai, _, error = _get_gemini_modules()
    if error:
        return None, error
    if "_gemini_client" not in st.session_state:
        st.session_state["_gemini_client"] = genai.Client(
            api_key=st.secrets["GEMINI_API_KEY"]
        )
    return st.session_state["_gemini_client"], None


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


def get_openrouter_credit_balance() -> dict:
    if "OPENROUTER_MANAGEMENT_KEY" not in st.secrets:
        return {
            "ok": False,
            "message": "未設定 OPENROUTER_MANAGEMENT_KEY，未能讀取 OpenRouter credits。",
        }
    payload, error = _fetch_json(
        "https://openrouter.ai/api/v1/credits",
        st.secrets["OPENROUTER_MANAGEMENT_KEY"],
    )
    if error or not payload:
        return {
            "ok": False,
            "message": f"OpenRouter credits 讀取失敗：{error or 'empty response'}",
        }
    data = payload.get("data") or {}
    total_credits = _as_float(data.get("total_credits"))
    total_usage = _as_float(data.get("total_usage"))
    return {
        "ok": True,
        "total_credits_usd": total_credits,
        "total_usage_usd": total_usage,
        "remaining_credits_usd": total_credits - total_usage,
    }


def create_gemini_live_ephemeral_token(duration_minutes: float = 10) -> dict:
    if "GEMINI_API_KEY" not in st.secrets:
        return {"ok": False, "message": "❌ 未設定 Gemini API Key，未能開始即時練習。"}
    genai, _, error = _get_gemini_modules()
    if error:
        return {"ok": False, "message": error}
    try:
        token_minutes = max(3, math.ceil(float(duration_minutes)))
        client = genai.Client(
            api_key=st.secrets["GEMINI_API_KEY"],
            http_options={"api_version": "v1alpha"},
        )
        now = datetime.now(timezone.utc)
        expire = now + timedelta(minutes=token_minutes + 2)
        token = client.auth_tokens.create(
            config={
                "uses": 1,
                "expire_time": expire,
                "new_session_expire_time": expire,
                "http_options": {"api_version": "v1alpha"},
            }
        )
        token_name = _read_attr(token, "name")
        if not token_name:
            return {"ok": False, "message": "❌ Gemini 未有回傳 ephemeral token。"}
        return {
            "ok": True,
            "token": token_name,
            "model": FREE_DEBATE_LIVE_MODEL,
            "model_label": FREE_DEBATE_LIVE_MODEL_LABEL,
            "duration_minutes": token_minutes,
            "created_at": _now_hk_timestamp(),
        }
    except Exception as e:
        return {"ok": False, "message": _format_ai_error("Gemini Live", e)}


def create_gemini_live_ephemeral_tokens(count: int, total_minutes: float) -> dict:
    """一次過 mint 多粒 ephemeral token，畀完整 Mock 逐節接力用。

    每粒 uses:1；expiry 覆蓋成場 Mock（＋緩衝），令最後一節嘅 token 都仲有效。
    任何一粒失敗即整體 fail，唔開殘缺 Mock。
    """
    if "GEMINI_API_KEY" not in st.secrets:
        return {"ok": False, "message": "❌ 未設定 Gemini API Key，未能開始即時練習。"}
    genai, _, error = _get_gemini_modules()
    if error:
        return {"ok": False, "message": error}
    try:
        count = max(1, int(count))
        expire_minutes = max(3, math.ceil(float(total_minutes))) + 5
        client = genai.Client(
            api_key=st.secrets["GEMINI_API_KEY"],
            http_options={"api_version": "v1alpha"},
        )
        now = datetime.now(timezone.utc)
        expire = now + timedelta(minutes=expire_minutes)
        tokens = []
        for _ in range(count):
            token = client.auth_tokens.create(
                config={
                    "uses": 1,
                    "expire_time": expire,
                    "new_session_expire_time": expire,
                    "http_options": {"api_version": "v1alpha"},
                }
            )
            token_name = _read_attr(token, "name")
            if not token_name:
                return {"ok": False, "message": "❌ Gemini 未有回傳 ephemeral token。"}
            tokens.append(token_name)
        return {
            "ok": True,
            "tokens": tokens,
            "model": FREE_DEBATE_LIVE_MODEL,
            "model_label": FREE_DEBATE_LIVE_MODEL_LABEL,
            "created_at": _now_hk_timestamp(),
        }
    except Exception as e:
        return {"ok": False, "message": _format_ai_error("Gemini Live", e)}


def _format_ai_error(provider: str, error: Exception) -> str:
    error_str = str(error)
    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "rate_limit" in error_str:
        return "⚠️ AI 使用量或速率已達上限，請稍後再試。"
    if "503" in error_str or "UNAVAILABLE" in error_str or "high demand" in error_str:
        return "⚠️ AI 服務暫時繁忙，請稍後再試。"
    if "location is not supported" in error_str.lower() or "unsupported user location" in error_str.lower():
        return "❌ Gemini Live 地區限制：你目前所在地區暫時不支援 Live API。請開啟 VPN 轉換到受支援地區後再重試。"
    if "401" in error_str or "403" in error_str or "API key" in error_str:
        return f"❌ {provider} API Key 無效或權限不足，請聯絡開發人員檢查設定。"
    logger.warning("%s API error: %s", provider, error)
    return f"❌ AI 服務暫時無法使用：{error}"


def _generate_response(model_config, system_prompt: str, user_text: str) -> tuple[str, dict | None]:
    if model_config["provider"] == "gemini":
        return _generate_gemini_text(model_config, system_prompt, user_text)
    return _generate_openrouter_text(model_config, system_prompt, user_text)


def generate_general_ai_reply(system_prompt: str, user_text: str, model_label: str | None = None) -> tuple[str, dict | None]:
    model_config = _get_model_config(model_label)
    return _generate_response(model_config, system_prompt, user_text)


def _generate_gemini_text(model_config, system_prompt: str, user_text: str) -> tuple[str, dict | None]:
    client, error = _get_gemini_client()
    if error:
        return error, None
    _, types, error = _get_gemini_modules()
    if error:
        return error, None
    try:
        response = client.models.generate_content(
            model=model_config["model"],
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_text)])],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.7,
            ),
        )
        usage = _usage_from_gemini_response(response, model_config)
        return response.text or "AI 未能生成回覆，請再試一次。", usage
    except Exception as e:
        return _format_ai_error("Gemini", e), None


def _generate_openrouter_text(model_config, system_prompt: str, user_text: str) -> tuple[str, dict | None]:
    client, error = _get_openrouter_client()
    if error:
        return error, None
    try:
        response = client.chat.completions.create(
            model=model_config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            temperature=0.7,
        )
        usage = _capture_openrouter_usage(response, model_config)
        return response.choices[0].message.content or "AI 未能生成回覆，請再試一次。", usage
    except Exception as e:
        return _format_ai_error("OpenRouter", e), None


def _generate_audio_response(model_config, system_prompt: str, user_text: str, audio_bytes: bytes) -> tuple[str, dict | None]:
    if model_config["provider"] == "gemini":
        return _generate_gemini_audio(model_config, system_prompt, user_text, audio_bytes)
    return _generate_openrouter_audio(model_config, system_prompt, user_text, audio_bytes)


def _generate_gemini_audio(model_config, system_prompt: str, user_text: str, audio_bytes: bytes) -> tuple[str, dict | None]:
    client, error = _get_gemini_client()
    if error:
        return error, None
    _, types, error = _get_gemini_modules()
    if error:
        return error, None
    try:
        user_parts = [types.Part.from_text(text=user_text)]
        user_parts.append(types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"))
        response = client.models.generate_content(
            model=model_config["model"],
            contents=[types.Content(role="user", parts=user_parts)],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.7,
            ),
        )
        usage = _usage_from_gemini_response(
            response,
            model_config,
            fallback_audio_tokens=SPEECH_REVIEW_AUDIO_TOKENS,
        )
        return response.text or "AI 未能生成回覆，請再試一次。", usage
    except Exception as e:
        return _format_ai_error("Gemini", e), None


def _generate_openrouter_audio(model_config, system_prompt: str, user_text: str, audio_bytes: bytes) -> tuple[str, dict | None]:
    client, error = _get_openrouter_client()
    if error:
        return error, None
    try:
        content = [
            {"type": "text", "text": user_text},
            {
                "type": "input_audio",
                "input_audio": {
                    "data": base64.b64encode(audio_bytes).decode(),
                    "format": "wav",
                },
            },
        ]
        response = client.chat.completions.create(
            model=model_config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            temperature=0.7,
        )
        usage = _capture_openrouter_usage(response, model_config)
        return response.choices[0].message.content or "AI 未能生成回覆，請再試一次。", usage
    except Exception as e:
        return _format_ai_error("OpenRouter", e), None


def _generate_web_response(model_config, system_prompt: str, user_text: str) -> tuple[str, dict | None]:
    if model_config["provider"] == "gemini":
        return _generate_gemini_web(model_config, system_prompt, user_text)
    return _generate_openrouter_web(model_config, system_prompt, user_text)


def _generate_gemini_web(model_config, system_prompt: str, user_text: str) -> tuple[str, dict | None]:
    client, error = _get_gemini_client()
    if error:
        return error, None
    _, types, error = _get_gemini_modules()
    if error:
        return error, None
    try:
        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        response = client.models.generate_content(
            model=model_config["model"],
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_text)])],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.3,
                tools=[grounding_tool],
            ),
        )
        usage = _usage_from_gemini_response(response, model_config, search_calls=1)
        return _format_gemini_grounded_response(response), usage
    except Exception as e:
        return _format_ai_error("Gemini", e), None


def _format_openrouter_web_response(response) -> str:
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


def _generate_openrouter_web(model_config, system_prompt: str, user_text: str) -> tuple[str, dict | None]:
    client, error = _get_openrouter_client()
    if error:
        return error, None
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
        usage = _capture_openrouter_usage(response, model_config)
        return _format_openrouter_web_response(response), usage
    except Exception as e:
        return _format_ai_error("OpenRouter", e), None


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
) -> tuple[str, dict | None]:
    model_config = _get_model_config(model_label)
    if audio_bytes and not model_config["supports_audio"]:
        return "⚠️ 呢個模型不支援錄音分析。請選擇支援錄音嘅模型，或貼上文字稿再試。", None

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
    debate_format: str = "校園隨想",
    model_label: str | None = None,
) -> tuple[str, dict | None]:
    model_config = _get_model_config(model_label)
    topic_ctx = _build_topic_context(topic)

    return _generate_response(
        model_config,
        build_strategy_prompt(debate_format),
        build_strategy_user_prompt(topic, side, debate_format, topic_ctx),
    )


def research_web(
    topic: str,
    research_need: str,
    model_label: str | None = None,
) -> tuple[str, dict | None]:
    model_config = _get_model_config(model_label)

    return _generate_web_response(
        model_config,
        WEB_RESEARCH_SYSTEM_PROMPT,
        build_web_research_user_prompt(_today_hk(), topic, research_need),
    )


def fact_check_claim(
    statement: str,
    model_label: str | None = None,
) -> tuple[str, dict | None]:
    model_config = _get_model_config(model_label)

    return _generate_web_response(
        model_config,
        FACT_CHECK_SYSTEM_PROMPT,
        build_fact_check_user_prompt(_today_hk(), statement),
    )
