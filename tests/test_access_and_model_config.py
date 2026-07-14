import pytest

from account_access import (
    AI_COMMENT_ACCOUNT_ID,
    ADMIN_ACCOUNT_ID,
    DEVELOPER_ACCOUNT_ID,
    KIOSK_ACCOUNT_ID,
    NON_MEMBER_ACCOUNT_IDS,
    NON_MEMBER_ACCOUNT_DB_KEYS,
    account_id_can_be_created,
    account_can_access,
    is_non_member_account,
    sql_account_id_literals,
)
from ai_model_config import (
    AI_FEATURE_MODEL_FALLBACK_LABELS,
    AI_FEATURE_MODEL_LABELS,
    AI_MODEL_OPTIONS,
    AZURE_TTS_PROVIDER,
    CUSTOM_LLM_OPTION,
    CUSTOM_TTS_PROVIDER,
    DEFAULT_TTS_PROVIDER,
    GEMINI_LIVE_MODEL,
    GEMINI_LIVE_MODEL_LABEL,
    GEMINI_LIVE_PROVIDER,
    RAG_EMBEDDING_MODEL,
    RAG_EMBEDDING_VERSION,
    TTS_PROVIDER_OPTIONS,
    TTS_PROVIDER_SECRET,
    get_feature_model,
    get_tts_provider_config,
    model_slugs_for_feature,
    resolve_interactive_model_settings,
)


def test_special_accounts_are_centrally_denied_from_member_login_and_vote():
    for account_id in (
        ADMIN_ACCOUNT_ID,
        DEVELOPER_ACCOUNT_ID,
        KIOSK_ACCOUNT_ID,
        AI_COMMENT_ACCOUNT_ID,
    ):
        assert is_non_member_account(account_id.upper())
        assert not account_can_access(account_id, "committee_login")
        assert not account_can_access(account_id, "vote")

    assert account_can_access("member01", "committee_login")
    assert account_can_access("member01", "vote")
    assert not account_can_access("member01", "unknown-page")


def test_kiosk_policy_is_allowlisted_but_can_still_use_ai_coach():
    assert account_can_access(KIOSK_ACCOUNT_ID, "kiosk")
    assert account_can_access(KIOSK_ACCOUNT_ID.upper(), "kiosk")
    assert account_can_access(KIOSK_ACCOUNT_ID, "ai_coach")
    assert account_can_access(KIOSK_ACCOUNT_ID, "ai_room")
    assert account_can_access(KIOSK_ACCOUNT_ID, "tts")
    assert not account_can_access("member01", "kiosk")
    assert not account_can_access(ADMIN_ACCOUNT_ID, "ai_coach")


def test_system_accounts_are_denied_from_other_authenticated_member_surfaces():
    for page in ("member_profile", "funds", "video_replay", "projector"):
        assert account_can_access("member01", page)
        for account_id in NON_MEMBER_ACCOUNT_IDS:
            assert not account_can_access(account_id.swapcase(), page)


def test_only_the_exact_canonical_kiosk_reserved_id_can_be_provisioned():
    assert account_id_can_be_created("member01")
    assert account_id_can_be_created(KIOSK_ACCOUNT_ID)
    for reserved in ("admin", "ADMIN", "Developer", "Gemini", "GEMINI", "KIOSK"):
        assert not account_id_can_be_created(reserved)


def test_static_view_sql_literals_are_escaped_and_complete():
    literals = sql_account_id_literals(NON_MEMBER_ACCOUNT_IDS)
    assert literals == "'admin', 'developer', 'kiosk', 'Gemini'"
    assert sql_account_id_literals(NON_MEMBER_ACCOUNT_DB_KEYS) == (
        "'admin', 'developer', 'kiosk', 'gemini'"
    )
    assert sql_account_id_literals(("a'b",)) == "'a''b'"


def test_every_ai_feature_resolves_from_the_central_model_file():
    for feature in AI_FEATURE_MODEL_LABELS:
        label, config = get_feature_model(feature)
        assert label == AI_FEATURE_MODEL_LABELS[feature]
        assert config["model"]
        assert config["provider"]

    _label, kiosk = get_feature_model("kiosk_match_review")
    assert kiosk["supports_audio"] is True
    assert model_slugs_for_feature("room_judgement")
    assert GEMINI_LIVE_MODEL.startswith("gemini-")
    assert GEMINI_LIVE_MODEL_LABEL == "Gemini Live"
    assert GEMINI_LIVE_PROVIDER == "gemini"
    assert RAG_EMBEDDING_MODEL.startswith("gemini-embedding-")
    assert RAG_EMBEDDING_VERSION.startswith(RAG_EMBEDDING_MODEL + "@")
    assert AI_FEATURE_MODEL_FALLBACK_LABELS["room_judgement"][0] == (
        AI_FEATURE_MODEL_LABELS["room_judgement"]
    )


def test_interactive_provider_settings_resolve_to_an_eligible_default():
    all_providers, default_model = resolve_interactive_model_settings(None, None)
    assert set(all_providers) == {"gemini", "openrouter"}
    assert default_model in AI_MODEL_OPTIONS

    providers, default_model = resolve_interactive_model_settings(
        ["openrouter"], "Gemini 2.5 Flash",
    )
    assert providers == ("openrouter",)
    assert AI_MODEL_OPTIONS[default_model]["provider"] == "openrouter"

    providers, default_model = resolve_interactive_model_settings(
        ["gemini"], "Gemini 3.1 Pro",
    )
    assert providers == ("gemini",)
    assert default_model == "Gemini 3.1 Pro"


def test_deployment_selected_llm_tts_and_voice_have_central_selectors():
    assert CUSTOM_LLM_OPTION["label"] == "自家辯論 LLM"
    assert CUSTOM_LLM_OPTION["model_secret"] == "CUSTOM_LLM_MODEL"
    assert CUSTOM_LLM_OPTION["registry_model_type"] == "llm"

    assert TTS_PROVIDER_SECRET == "TTS_PROVIDER"
    assert set(TTS_PROVIDER_OPTIONS) == {
        AZURE_TTS_PROVIDER,
        CUSTOM_TTS_PROVIDER,
    }
    provider, azure = get_tts_provider_config("")
    assert provider == DEFAULT_TTS_PROVIDER == "azure"
    assert azure["default_voice"] == "zh-HK-HiuMaanNeural"
    assert get_tts_provider_config("unexpected")[0] == "azure"
    provider, custom = get_tts_provider_config(" CUSTOM ")
    assert provider == "custom"
    assert custom["model_secret"] == "CUSTOM_TTS_MODEL_VERSION"
    assert custom["registry_model_type"] == "tts"


@pytest.mark.parametrize(
    "feature",
    ("kiosk_match_review", "tts_review", "llm_review", "tts_script_analysis"),
)
def test_gemini_api_features_reject_a_non_gemini_central_selection(monkeypatch, feature):
    monkeypatch.setitem(AI_FEATURE_MODEL_LABELS, feature, "GPT-5.4 Mini")
    assert AI_MODEL_OPTIONS["GPT-5.4 Mini"]["provider"] == "openrouter"
    with pytest.raises(ValueError, match="provider='gemini'"):
        get_feature_model(feature)
