import math


DEFAULT_AI_MODEL = "Gemini 2.5 Flash"

AI_MODEL_OPTIONS = {
    "Gemini 2.5 Flash": {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "supports_web_search": True,
        "pricing_label": "免費額度 / 收費",
        "selection_label": "日常練習",
        "pricing_note": "Provider: Google AI Studio，支援上網搜尋及錄音分析。",
        "paid_rate_note": "Input US$0.30 / 1M tokens（audio US$1.00 / 1M tokens），output US$2.50 / 1M tokens；Google Search 超額約 US$35 / 1,000 grounded prompts。",
        "input_price_per_million": 0.30,
        "audio_input_price_per_million": 1.00,
        "output_price_per_million": 2.50,
        "web_search_price_per_call": 0.035,
        "is_premium": False,
    },
    "Gemini 3.5 Flash": {
        "provider": "gemini",
        "model": "gemini-3.5-flash",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "supports_web_search": True,
        "pricing_label": "免費額度 / 收費",
        "selection_label": "高質快速",
        "pricing_note": "Provider: Google AI Studio，支援上網搜尋及錄音分析。",
        "paid_rate_note": "Input US$1.50 / 1M tokens，output US$9.00 / 1M tokens；Google Search 超額約 US$14 / 1,000 search queries。",
        "input_price_per_million": 1.50,
        "audio_input_price_per_million": 1.50,
        "output_price_per_million": 9.00,
        "web_search_price_per_call": 0.014,
        "is_premium": False,
    },
    "Gemini 3.1 Pro": {
        "provider": "gemini",
        "model": "gemini-3.1-pro-preview",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "supports_web_search": True,
        "pricing_label": "高級收費",
        "selection_label": "深入分析",
        "pricing_note": "Provider: Google AI Studio，支援上網搜尋及錄音分析。",
        "paid_rate_note": "Input US$2.00 / 1M tokens（prompt >200k：US$4.00），output US$12.00 / 1M tokens（prompt >200k：US$18.00）；Google Search 超額約 US$14 / 1,000 search queries。",
        "input_price_per_million": 2.00,
        "audio_input_price_per_million": 2.00,
        "output_price_per_million": 12.00,
        "web_search_price_per_call": 0.014,
        "is_premium": True,
    },
    "DeepSeek V4 Pro": {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-pro",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": False,
        "supports_web_search": True,
        "pricing_label": "收費",
        "selection_label": "平價推理",
        "pricing_note": "Provider: OpenRouter，支援上網搜尋，不支援錄音分析。",
        "paid_rate_note": "Input US$0.435 / 1M tokens，output US$0.87 / 1M tokens；OpenRouter fallback web search 約 US$0.005 / 次。",
        "input_price_per_million": 0.435,
        "audio_input_price_per_million": None,
        "output_price_per_million": 0.87,
        "web_search_price_per_call": 0.005,
        "is_premium": False,
    },
    "Haiku 4.5": {
        "provider": "openrouter",
        "model": "anthropic/claude-haiku-4.5",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": False,
        "supports_web_search": True,
        "pricing_label": "收費",
        "selection_label": "第二意見",
        "pricing_note": "Provider: OpenRouter，支援上網搜尋，不支援錄音分析。",
        "paid_rate_note": "Input US$1.00 / 1M tokens，output US$5.00 / 1M tokens；OpenRouter web search 約 US$0.01 / 次。",
        "input_price_per_million": 1.00,
        "audio_input_price_per_million": None,
        "output_price_per_million": 5.00,
        "web_search_price_per_call": 0.01,
        "is_premium": True,
    },
    "GPT-5.4 Mini": {
        "provider": "openrouter",
        "model": "openai/gpt-5.4-mini",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": False,
        "supports_web_search": True,
        "pricing_label": "收費",
        "selection_label": "重要先用",
        "pricing_note": "Provider: OpenRouter，支援上網搜尋，不支援錄音分析。",
        "paid_rate_note": "Input US$0.75 / 1M tokens，output US$4.50 / 1M tokens；OpenRouter web search 約 US$0.01 / 次。",
        "input_price_per_million": 0.75,
        "audio_input_price_per_million": None,
        "output_price_per_million": 4.50,
        "web_search_price_per_call": 0.01,
        "is_premium": True,
    },
}

# The data factory may additionally use OpenRouter's explicit free-only router.
# Keep it factory-scoped: its random, rate-limited model selection is useful for
# producing human-reviewed candidates, but is not a suitable implicit choice
# for judging, coaching or other interactive features.
AI_FACTORY_FREE_MODEL_OPTIONS = {
    "OpenRouter Free": {
        "provider": "openrouter",
        "model": "openrouter/free",
        "api_key": "OPENROUTER_API_KEY",
        "supports_audio": False,
        "supports_web_search": False,
        "billing_mode": "free_only",
        "pricing_label": "免費",
        "selection_label": "免費候選",
        "pricing_note": (
            "Provider: OpenRouter Free Models Router；每次由可用免費模型中選擇，"
            "供應、速度及輸出質素可能不同。"
        ),
        "paid_rate_note": "Free-only route；input 及 output 單價均為 US$0。",
        "input_price_per_million": 0,
        "audio_input_price_per_million": 0,
        "output_price_per_million": 0,
        "web_search_price_per_call": 0,
        "is_premium": False,
    },
}

AI_FACTORY_MODEL_OPTIONS = {
    **AI_MODEL_OPTIONS,
    **AI_FACTORY_FREE_MODEL_OPTIONS,
}

NON_MANUAL_DEFAULT_AI_MODEL = "Gemini 3.5 Flash"

NON_MANUAL_MODEL_OPTIONS = {
    "Gemini 3.5 Flash": {
        "provider": "gemini",
        "model": "gemini-3.5-flash",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "supports_web_search": True,
        "pricing_label": "免費額度 / 收費",
        "selection_label": "預設高質",
        "pricing_note": "Provider: Google AI Studio，支援上網搜尋及錄音分析。",
        "paid_rate_note": "Input US$1.50 / 1M tokens，output US$9.00 / 1M tokens；Google Search 超額約 US$14 / 1,000 search queries。",
        "input_price_per_million": 1.50,
        "audio_input_price_per_million": 1.50,
        "output_price_per_million": 9.00,
        "web_search_price_per_call": 0.014,
        "is_premium": False,
    },
    "Gemini 2.5 Flash": {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "supports_web_search": True,
        "pricing_label": "免費額度 / 收費",
        "selection_label": "日常練習",
        "pricing_note": "Provider: Google AI Studio，支援上網搜尋及錄音分析。",
        "paid_rate_note": "Input US$0.30 / 1M tokens（audio US$1.00 / 1M tokens），output US$2.50 / 1M tokens；Google Search 超額約 US$35 / 1,000 grounded prompts。",
        "input_price_per_million": 0.30,
        "audio_input_price_per_million": 1.00,
        "output_price_per_million": 2.50,
        "web_search_price_per_call": 0.035,
        "is_premium": False,
    },
    "Gemini 2.5 Flash Lite": {
        "provider": "gemini",
        "model": "gemini-2.5-flash-lite",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "supports_web_search": True,
        "pricing_label": "免費額度 / 收費",
        "selection_label": "慳錢快速",
        "pricing_note": "Provider: Google AI Studio，支援上網搜尋及錄音分析。",
        "paid_rate_note": "Input US$0.10 / 1M tokens，output US$0.40 / 1M tokens；Google Search 費用按 Google AI Studio 實際帳單為準。",
        "input_price_per_million": 0.10,
        "audio_input_price_per_million": 0.10,
        "output_price_per_million": 0.40,
        "web_search_price_per_call": 0.035,
        "is_premium": False,
    },
}

# Non-interactive features must never choose their own model in an API module.
# Change a feature here and every caller, usage ledger entry and inventory page
# will resolve the same label/slug.
AI_FEATURE_MODEL_LABELS = {
    "vote_discussion": NON_MANUAL_DEFAULT_AI_MODEL,
    "vote_review": NON_MANUAL_DEFAULT_AI_MODEL,
    "vote_analysis": NON_MANUAL_DEFAULT_AI_MODEL,
    "room_judgement": "Gemini 3.5 Flash",
    "kiosk_match_review": "Gemini 3.5 Flash",
    "tts_review": "Gemini 2.5 Flash",
    "llm_review": "Gemini 2.5 Flash",
    "tts_script_analysis": "Gemini 2.5 Flash",
    "ai_training_eval": "Gemini 2.5 Flash",
}

AI_FEATURE_MODEL_FALLBACK_LABELS = {
    "room_judgement": (
        AI_FEATURE_MODEL_LABELS["room_judgement"],
        "Gemini 2.5 Flash",
        "Gemini 2.5 Flash Lite",
    ),
}

AI_FEATURE_REQUIREMENTS = {
    # These callers use the Gemini generateContent/file APIs directly.  Keeping
    # the provider constraint beside the selected label prevents a future
    # config-only model swap from sending an OpenRouter slug to a Gemini URL.
    "room_judgement": {"provider": "gemini"},
    "kiosk_match_review": {"provider": "gemini", "supports_audio": True},
    "tts_review": {"provider": "gemini", "supports_audio": True},
    "llm_review": {"provider": "gemini"},
    "tts_script_analysis": {"provider": "gemini"},
}

# Provider-specific models which do not use the normal text/audio generation
# selector still live in this same file.
GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"
GEMINI_LIVE_MODEL_LABEL = "Gemini Live"
GEMINI_LIVE_PROVIDER = "gemini"
RAG_EMBEDDING_MODEL = "gemini-embedding-2"
RAG_EMBEDDING_VERSION = "gemini-embedding-2@2026-04"
LOCAL_TTS_TRAINING_ENGINE = "GPT-SoVITS"

# Deployment-selected models/providers are dynamic values rather than fixed
# public model slugs, but their selectors and defaults are still model choices.
# Keep those names here too so API/proxy modules never invent a second source
# of truth for a custom LLM, custom TTS checkpoint or Azure voice.
CUSTOM_LLM_OPTION = {
    "label": "自家辯論 LLM",
    "provider": "custom",
    "base_url_secret": "CUSTOM_LLM_BASE_URL",
    "model_secret": "CUSTOM_LLM_MODEL",
    "api_key_secret": "CUSTOM_LLM_API_KEY",
    "registry_model_type": "llm",
    "supports_audio": False,
    "supports_web_search": False,
    "input_price_per_million": 0,
    "output_price_per_million": 0,
    "web_search_price_per_call": 0,
    "pricing_note": "自家OpenAI-compatible endpoint。",
    "paid_rate_note": "成本由本地／GPU服務承擔。",
    "selection_label": "自家模型",
    "pricing_label": "自家",
    "is_premium": False,
}

TTS_PROVIDER_SECRET = "TTS_PROVIDER"
AZURE_TTS_PROVIDER = "azure"
CUSTOM_TTS_PROVIDER = "custom"
DEFAULT_TTS_PROVIDER = AZURE_TTS_PROVIDER
TTS_PROVIDER_OPTIONS = {
    AZURE_TTS_PROVIDER: {
        "speech_key_secret": "AZURE_SPEECH_KEY",
        "region_secret": "AZURE_SPEECH_REGION",
        "voice_secret": "AZURE_TTS_VOICE",
        "default_voice": "zh-HK-HiuMaanNeural",
        "rate_secret": "AZURE_TTS_RATE",
        "default_rate": "0%",
        "output_format_secret": "AZURE_TTS_OUTPUT_FORMAT",
        "default_output_format": "audio-24khz-48kbitrate-mono-mp3",
        "accounting_model_label": "Azure Speech TTS",
        "price_per_million_characters_secret": (
            "AZURE_TTS_PRICE_PER_MILLION_CHARACTERS_USD"
        ),
        # Azure Speech pricing varies by region, contract and voice tier.  This
        # is a documented estimate-only fallback, never an assertion about the
        # provider invoice.  Deployments should set the secret above to their
        # current effective rate and reconcile against the Azure bill.
        "default_price_per_million_characters_usd": 16.0,
        "pricing_default_note": (
            "Estimate-only default of US$16 per million characters; override "
            "AZURE_TTS_PRICE_PER_MILLION_CHARACTERS_USD with the deployment's "
            "current Azure Speech rate."
        ),
    },
    CUSTOM_TTS_PROVIDER: {
        "url_secret": "CUSTOM_TTS_URL",
        "api_key_secret": "CUSTOM_TTS_API_KEY",
        "model_secret": "CUSTOM_TTS_MODEL_VERSION",
        "registry_model_type": "tts",
        "accounting_model_label": "Custom TTS",
        "price_per_million_characters_secret": (
            "CUSTOM_TTS_PRICE_PER_MILLION_CHARACTERS_USD"
        ),
        # No third-party character fee can be inferred for a self-hosted model.
        # Configure the deployment-specific marginal rate when applicable.
        "default_price_per_million_characters_usd": 0.0,
        "pricing_default_note": (
            "Default is zero for self-hosted TTS; override "
            "CUSTOM_TTS_PRICE_PER_MILLION_CHARACTERS_USD when the deployment "
            "has a measurable per-character cost."
        ),
    },
}


def get_tts_provider_config(provider=None):
    """Return the selected TTS provider and its central runtime selectors.

    Unknown or blank deployment values retain the previous safe behaviour of
    falling back to Azure rather than accidentally calling an arbitrary path.
    """
    selected = str(provider or "").strip().lower()
    if selected not in TTS_PROVIDER_OPTIONS:
        selected = DEFAULT_TTS_PROVIDER
    return selected, TTS_PROVIDER_OPTIONS[selected]


def resolve_tts_accounting_config(
    provider=None, *, price_per_million_characters_usd=None
):
    """Return stable TTS provider metadata and one configurable estimate rate.

    The caller reads the named deployment secret from the same secret source as
    the provider credentials, then passes that value here.  Keeping the secret
    name, documented fallback and validation beside the provider selector avoids
    scattering volatile pricing assumptions through request handlers.
    """
    selected, provider_config = get_tts_provider_config(provider)
    default_rate = float(
        provider_config.get("default_price_per_million_characters_usd") or 0
    )
    raw_rate = price_per_million_characters_usd
    configured = raw_rate not in (None, "")
    try:
        rate = float(raw_rate) if configured else default_rate
    except (TypeError, ValueError, OverflowError):
        rate = default_rate
        configured = False
    if not math.isfinite(rate) or rate < 0:
        rate = default_rate
        configured = False
    return selected, {
        "provider": selected,
        "model_label": str(
            provider_config.get("accounting_model_label") or f"{selected} TTS"
        ),
        "billing_unit": "characters",
        "price_per_million_characters_usd": rate,
        "price_secret": str(
            provider_config.get("price_per_million_characters_secret") or ""
        ),
        "cost_source": (
            "configured_character_rate"
            if configured
            else "documented_default_character_rate"
        ),
        "pricing_note": str(provider_config.get("pricing_default_note") or ""),
    }


def build_tts_usage_metadata(
    provider,
    text,
    *,
    price_per_million_characters_usd=None,
    model_label="",
    operation_id="",
    operation_stage="synthesis",
):
    """Build non-content TTS ledger metadata for one attempted provider call.

    ``text`` must be the post-lexicon text actually submitted to the provider.
    The text itself is never returned or stored; only its Unicode character
    count is retained.  Failed calls can use the same metadata so an attempt
    which the provider may still bill does not silently disappear.
    """
    selected, accounting = resolve_tts_accounting_config(
        provider,
        price_per_million_characters_usd=price_per_million_characters_usd,
    )
    characters = len(str(text or ""))
    rate = float(accounting["price_per_million_characters_usd"])
    return {
        "provider": selected,
        "model_label": str(model_label or accounting["model_label"]),
        "billable_characters": characters,
        "estimated_cost_usd": characters * rate / 1_000_000,
        "cost_source": accounting["cost_source"],
        "operation_id": str(operation_id or ""),
        "operation_stage": str(operation_stage or "synthesis"),
    }


def resolve_interactive_model_settings(enabled_providers=None, default_model=None):
    """Resolve the developer-controlled AI Coach provider/model settings.

    Missing or malformed legacy values preserve the historical runtime by
    enabling every known provider.  A default which is unknown or belongs to a
    disabled provider falls back deterministically to the normal default, then
    to the first model belonging to an enabled provider.
    """
    known_providers = tuple(dict.fromkeys(
        str(config.get("provider") or "").strip()
        for config in AI_MODEL_OPTIONS.values()
        if str(config.get("provider") or "").strip()
    ))
    raw_providers = (
        enabled_providers
        if isinstance(enabled_providers, (list, tuple, set))
        else ()
    )
    selected_providers = tuple(dict.fromkeys(
        str(provider or "").strip()
        for provider in raw_providers
        if str(provider or "").strip() in known_providers
    ))
    if not selected_providers:
        selected_providers = known_providers

    eligible_labels = tuple(
        label for label, config in AI_MODEL_OPTIONS.items()
        if config.get("provider") in selected_providers
    )
    requested = str(default_model or "").strip()
    if requested not in eligible_labels:
        requested = (
            DEFAULT_AI_MODEL
            if DEFAULT_AI_MODEL in eligible_labels
            else eligible_labels[0]
        )
    return selected_providers, requested

# Backwards-compatible public name used by the room runtime and existing tests.
ROOM_JUDGEMENT_MODEL_LABELS = AI_FEATURE_MODEL_FALLBACK_LABELS["room_judgement"]

# Official third-judge choices deliberately reuse the same central model list
# as AI辯論易.  The UI defaults to Gemini 3.5 Flash, while competition staff
# may select any listed model and switch to a different one for the sole retry.
OFFICIAL_AI_JUDGE_DEFAULT_MODEL = "Gemini 3.5 Flash"
OFFICIAL_AI_JUDGE_MODEL_LABELS = tuple(AI_MODEL_OPTIONS)


def model_slugs_for_labels(labels):
    return tuple(
        NON_MANUAL_MODEL_OPTIONS[label]["model"]
        for label in labels
        if label in NON_MANUAL_MODEL_OPTIONS
    )


def _validate_feature_model(feature_key, label, config):
    for requirement, expected in AI_FEATURE_REQUIREMENTS.get(feature_key, {}).items():
        actual = config.get(requirement)
        if actual != expected:
            raise ValueError(
                f"AI feature {feature_key} requires {requirement}={expected!r}; "
                f"{label} has {actual!r}"
            )


def get_feature_model(feature):
    """Return ``(label, config)`` for one centrally selected AI feature."""
    feature_key = str(feature or "").strip()
    try:
        label = AI_FEATURE_MODEL_LABELS[feature_key]
    except KeyError as exc:
        raise KeyError(f"Unknown AI model feature: {feature_key}") from exc
    config = NON_MANUAL_MODEL_OPTIONS.get(label) or AI_MODEL_OPTIONS.get(label)
    if not config:
        raise KeyError(f"AI feature {feature_key} selects unknown model: {label}")
    _validate_feature_model(feature_key, label, config)
    return label, config


def get_official_ai_judge_model(label):
    """Resolve one model shared with the existing AI debate feature."""
    selected = str(label or "").strip()
    if selected not in OFFICIAL_AI_JUDGE_MODEL_LABELS:
        raise ValueError("正式 AI 評判只可使用系統提供的 AI 模型。")
    return selected, dict(AI_MODEL_OPTIONS[selected])


def model_slugs_for_feature(feature):
    """Ordered provider model slugs, including centrally defined fallbacks."""
    feature_key = str(feature or "").strip()
    labels = AI_FEATURE_MODEL_FALLBACK_LABELS.get(feature_key)
    if not labels:
        labels = (get_feature_model(feature_key)[0],)
    slugs = []
    for label in labels:
        config = NON_MANUAL_MODEL_OPTIONS.get(label) or AI_MODEL_OPTIONS.get(label)
        if not config:
            raise KeyError(f"AI feature {feature} selects unknown model: {label}")
        _validate_feature_model(feature_key, label, config)
        slugs.append(config["model"])
    return tuple(slugs)


def get_model_by_slug(model_slug):
    """Resolve one centrally registered model slug to its label and pricing.

    Runtime fallback loops carry provider slugs on the wire.  Accounting callers
    use this reverse lookup so model labels and rates do not get duplicated in
    transport modules.
    """
    requested = str(model_slug or "").strip()
    for options in (NON_MANUAL_MODEL_OPTIONS, AI_MODEL_OPTIONS):
        for label, config in options.items():
            if str(config.get("model") or "") == requested:
                return label, config
    raise KeyError(f"Unknown central AI model slug: {requested}")
