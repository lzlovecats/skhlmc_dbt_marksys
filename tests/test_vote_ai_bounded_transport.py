"""Vote AI must use the shared timeout and response-bounded provider path."""

import asyncio
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_name import LMC_AI_MODEL_LABEL
from api import vote_api
from core import ai_provider
from core import vote_ai
from system_limits import AI_PROVIDER_RESPONSE_MAX_BYTES, VOTE_AI_PROMPT_MAX_CHARS

ROOT = Path(__file__).resolve().parents[1]


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


def test_vote_ai_local_choice_uses_shared_node_runtime_without_cloud_key(monkeypatch):
    from core import lmc_ai_client

    captured = {}

    async def local(db, **kwargs):
        captured.update(db=db, **kwargs)
        return "本地答案", {
            "input_tokens": 12,
            "output_tokens": 8,
            "cost_source": "local_zero_cost",
        }

    monkeypatch.setattr(lmc_ai_client, "generate_local_text", local)
    result, usage = asyncio.run(vote_ai.generate_general_ai_reply(
        "system", "user", {}, model_label=LMC_AI_MODEL_LABEL,
        feature="vote_review", db="vote-db",
    ))

    assert result == "本地答案"
    assert captured["db"] == "vote-db"
    assert vote_ai.VOTE_LOCAL_AI_MODE == "fast"
    assert captured["mode"] == "fast"
    assert usage["model_label"] == LMC_AI_MODEL_LABEL
    assert usage["estimated_cost_usd"] == 0


def test_vote_ai_selector_defaults_local_and_rejects_unknown_values():
    review = vote_api.AiReviewBody(topic="辯題", category="科技與未來", difficulty=2)
    assert review.ai_model == "local"
    assert vote_api._vote_ai_model("local") == LMC_AI_MODEL_LABEL
    assert vote_api._vote_ai_model("gemini") == "Gemini 3.5 Flash"
    with pytest.raises(ValidationError):
        vote_api.AiAnalysisBody(kind="bank", ai_model="other")


def test_vote_page_exposes_local_or_gemini_choice_on_every_ai_request():
    page = (ROOT / "frontend/vote/index.html").read_text("utf-8")
    proxy = (ROOT / "deploy/proxy.py").read_text("utf-8")
    assert 'id="voteAiModel"' in page
    assert '<option value="local">__LMC_AI_MODEL_LABEL__</option>' in page
    assert '<option value="gemini">__VOTE_GEMINI_MODEL_LABEL__</option>' in page
    assert page.count("ai_model: voteAiChoice()") == 3
    assert 'const LOCAL_AI_MENTION_TAG = __LMC_AI_MENTION_TAG_JSON__;' in page
    assert '"__LMC_AI_MODEL_LABEL__", xml_escape(LMC_AI_MODEL_LABEL)' in proxy
    assert '"__LMC_AI_MENTION_TAG_JSON__"' in proxy
    assert "json.dumps(LMC_AI_MENTION_TAG, ensure_ascii=False)" in proxy
    assert '"__VOTE_GEMINI_MODEL_LABEL__"' in proxy


def test_vote_status_gate_requires_selected_online_fast_mode(monkeypatch):
    from core import lmc_ai_client

    async def status(_db):
        return {
            "available": True,
            "selected": True,
            "state": "online",
            "message": "自家 AI 已選用並在線。",
                "modes": [{
                    "id": "fast",
                    "available": False,
                    "message": "目前選用的自家 AI 電腦未提供「快速回應」模式。",
                }],
        }

    monkeypatch.setattr(lmc_ai_client, "local_ai_availability", status)
    monkeypatch.setattr(vote_api, "_committee_user", lambda _request: "alice")
    monkeypatch.setattr(vote_api, "_vote_db", lambda: "vote-db")

    response = asyncio.run(vote_api.ai_status(object()))
    assert response.headers["cache-control"] == "no-store"
    assert b'"selected":true' in response.body

    with pytest.raises(vote_api.HTTPException) as exc_info:
        vote_api._require_vote_ai_available("local", "vote-db")
    assert exc_info.value.status_code == 503
    assert "快速回應" in str(exc_info.value.detail)
    vote_api._require_vote_ai_available("gemini", "vote-db")


def test_vote_page_polls_and_disables_local_ai_actions_when_unavailable():
    page = (ROOT / "frontend/vote/index.html").read_text("utf-8")
    assert 'id="voteLocalAiDetails"' in page
    assert 'id="voteAiStatus"' in page
    assert 'fetch("/api/vote/ai-status"' in page
    assert "localOption.disabled = !requiredMode?.available" in page
    assert 'item.id === LOCAL_AI_STATUS.required_mode' in page
    assert 'id="voteAiModeLabel"' in page
    assert '`投票頁使用「${requiredMode.label}」模式。`' in page
    assert "Vote Page 使用" not in page
    assert "selectedVoteAiAvailable" in page
    assert "requireSelectedVoteAi" in page
    assert 'localDetails.hidden = voteAiChoice() !== "local"' in page
    assert "setInterval(refreshLocalAiStatus, 10000)" in page


def test_vote_page_does_not_report_success_for_tag_only_ai_failure():
    page = (ROOT / "frontend/vote/index.html").read_text("utf-8")
    submit_comment = page.split("async function submitComment", 1)[1].split(
        "async function runAnalysis", 1,
    )[0]

    assert 'else if (data.status === "failed")' in submit_comment
    assert 'toast(data.message || "⚠️ AI 回應失敗，未有新增留言。");' in submit_comment


def test_vote_checks_local_node_before_saving_a_tagged_comment(monkeypatch):
    from core import vote_logic

    inserts = []
    monkeypatch.setattr(vote_api, "_vote_db", lambda: object())
    monkeypatch.setattr(vote_api, "_require_pending_motion", lambda *_args: None)
    monkeypatch.setattr(
        vote_logic, "insert_comment", lambda *args, **kwargs: inserts.append((args, kwargs)),
    )

    def unavailable(_choice, _db):
        raise vote_api.HTTPException(503, "目前選用的自家 AI 電腦離線。")

    monkeypatch.setattr(vote_api, "_require_vote_ai_available", unavailable)
    with pytest.raises(vote_api.HTTPException, match="離線"):
        vote_api.post_comment(
            vote_api.CommentBody(
                motion_type="topic_vote",
                motion_key="辯題",
                text="@AI 請分析",
                ai_model="local",
            ),
            user_id="alice",
        )
    assert inserts == []


def test_vote_api_keeps_sync_worker_boundary_for_blocking_db_dependencies():
    assert not inspect.iscoroutinefunction(vote_api.post_comment)
    assert not inspect.iscoroutinefunction(vote_api.ai_review)
    assert not inspect.iscoroutinefunction(vote_api.run_analysis)


def test_vote_api_sync_bridge_awaits_review_transport(monkeypatch):
    calls = []

    async def review(topic, category, difficulty, db, secrets, model_label):
        calls.append((topic, category, difficulty, db, secrets, model_label))
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
            ai_model="gemini",
        ),
        user_id="alice",
    )

    assert response == {
        "status": "ok",
        "review": "reviewed",
        "model_label": "Gemini 3.5 Flash",
    }
    assert calls == [(
        "應否推行政策", vote_ai.CATEGORIES[0], 2, fake_db,
        {"GEMINI_API_KEY": "key"}, "Gemini 3.5 Flash",
    )]
    assert logs == [(
        "alice", "vote_review", "reviewed", {"input_tokens": 1}, fake_db,
    )]
