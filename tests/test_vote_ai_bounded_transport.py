"""Vote AI must use the shared timeout and response-bounded provider path."""

import asyncio
import inspect

import pytest

from api import vote_api
from core import ai_provider
from core import vote_ai
from system_limits import AI_PROVIDER_RESPONSE_MAX_BYTES, VOTE_AI_PROMPT_MAX_CHARS


def test_shared_provider_transport_rejects_response_over_two_mib():
    class _Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"x" * AI_PROVIDER_RESPONSE_MAX_BYTES
            yield b"x"

    class _Client:
        def stream(self, method, url, **kwargs):
            assert method == "POST"
            assert url == "https://provider.invalid/generate"
            assert kwargs == {"json": {"prompt": "bounded"}}
            return _Response()

    with pytest.raises(ValueError, match="exceeds server limit"):
        asyncio.run(ai_provider.post_json_bounded(
            _Client(),
            "https://provider.invalid/generate",
            json={"prompt": "bounded"},
        ))


def test_vote_ai_uses_shared_bounded_transport_and_actual_usage(monkeypatch):
    captured = {}

    async def generate(config, system, user, **kwargs):
        captured.update(
            config=config,
            system=system,
            user=user,
            kwargs=kwargs,
        )
        return "bounded response", {
            "input_tokens": 10,
            "output_tokens": 4,
            "audio_tokens": 0,
            "search_calls": 0,
            "cost_source": "gemini_usage_metadata",
        }

    monkeypatch.setattr(vote_ai, "generate_text", generate)

    text, usage = asyncio.run(vote_ai.generate_general_ai_reply(
        "system", "user", {"GEMINI_API_KEY": "secret"},
        model_label="Gemini 2.5 Flash",
    ))

    assert text == "bounded response"
    assert captured["config"]["provider"] == "gemini"
    assert captured["kwargs"] == {
        "api_key": "secret",
        "web_search": False,
        "max_prompt_chars": len("systemuser"),
        "temperature": 0.7,
    }
    assert usage == {
        "model_label": "Gemini 2.5 Flash",
        "provider": "gemini",
        "input_tokens": 10,
        "output_tokens": 4,
        "audio_tokens": 0,
        "search_calls": 0,
        "estimated_cost_usd": 0.000013,
        "estimated_cost_hkd": 0.0001,
        "cost_source": "gemini_usage_metadata",
    }


def test_vote_ai_preserves_its_user_prompt_bound_before_transport(monkeypatch):
    captured = {}

    async def generate(_config, system, user, **kwargs):
        captured.update(system=system, user=user, kwargs=kwargs)
        return "ok", {}

    monkeypatch.setattr(vote_ai, "generate_text", generate)
    asyncio.run(vote_ai.generate_general_ai_reply(
        "fixed system prompt",
        "x" * (VOTE_AI_PROMPT_MAX_CHARS + 100),
        {"GEMINI_API_KEY": "secret"},
        model_label="Gemini 2.5 Flash",
    ))

    assert captured["user"] == (
        "x" * VOTE_AI_PROMPT_MAX_CHARS
        + "\n[輸入已按伺服器資源上限截斷]"
    )
    assert captured["kwargs"]["max_prompt_chars"] == (
        len(captured["system"]) + len(captured["user"])
    )


@pytest.mark.parametrize(
    ("model_label", "key_name", "provider"),
    [
        ("Gemini 2.5 Flash", "GEMINI_API_KEY", "gemini"),
        ("GPT-5.4 Mini", "OPENROUTER_API_KEY", "openrouter"),
    ],
)
def test_vote_ai_selects_the_central_model_provider_key(
    monkeypatch, model_label, key_name, provider,
):
    captured = {}

    async def generate(config, _system, _user, **kwargs):
        captured.update(config=config, kwargs=kwargs)
        return "ok", {}

    monkeypatch.setattr(vote_ai, "generate_text", generate)
    asyncio.run(vote_ai.generate_general_ai_reply(
        "system", "user", {key_name: "selected-key"}, model_label=model_label,
    ))

    assert captured["config"]["provider"] == provider
    assert captured["kwargs"]["api_key"] == "selected-key"


def test_vote_ai_sanitizes_provider_failures(monkeypatch, caplog):
    async def generate(*_args, **_kwargs):
        raise RuntimeError("secret upstream detail")

    monkeypatch.setattr(vote_ai, "generate_text", generate)

    text, usage = asyncio.run(vote_ai.generate_general_ai_reply(
        "system", "user", {"OPENROUTER_API_KEY": "secret"},
        model_label="GPT-5.4 Mini",
    ))

    assert text == "❌ OpenRouter 回覆失敗，請稍後再試。"
    assert usage is None
    assert "secret upstream detail" not in text
    assert "Vote AI provider request failed" in caplog.text


def test_vote_api_keeps_sync_worker_boundary_for_blocking_db_dependencies():
    assert not inspect.iscoroutinefunction(vote_api.post_comment)
    assert not inspect.iscoroutinefunction(vote_api.ai_review)
    assert not inspect.iscoroutinefunction(vote_api.run_analysis)


def test_vote_api_sync_bridge_awaits_review_transport(monkeypatch):
    calls = []

    async def review(topic, category, difficulty, db, secrets):
        calls.append((topic, category, difficulty, db, secrets))
        return "reviewed", {"input_tokens": 1}

    fake_db = object()
    logs = []
    monkeypatch.setattr(vote_ai, "review_topic", review)
    monkeypatch.setattr(vote_api, "_vote_db", lambda: fake_db)
    monkeypatch.setattr(vote_api, "_ai_secrets", lambda: {"GEMINI_API_KEY": "key"})
    monkeypatch.setattr(vote_api, "_log_vote_ai", lambda *args: logs.append(args))

    # The route imports the core callable lazily, so patch that import target.
    response = vote_api.ai_review(
        vote_api.AiReviewBody(
            topic="應否推行政策", category=vote_ai.CATEGORIES[0], difficulty=2,
        ),
        user_id="alice",
    )

    assert response == {"status": "ok", "review": "reviewed"}
    assert calls == [(
        "應否推行政策", vote_ai.CATEGORIES[0], 2, fake_db,
        {"GEMINI_API_KEY": "key"},
    )]
    assert logs == [(
        "alice", "vote_review", "reviewed", {"input_tokens": 1}, fake_db,
    )]
