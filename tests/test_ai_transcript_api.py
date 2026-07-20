"""API contracts for the manager-only full-transcript structure product."""

import asyncio

import pandas as pd
import pytest
from fastapi import HTTPException

from api import ai_factory_api as api
from core import r2_storage


class _Db:
    def __init__(self, frames=()):
        self.frames = list(frames)
        self.queries = []

    def query(self, sql, params=None):
        self.queries.append((" ".join(str(sql).split()), params or {}))
        if not self.frames:
            raise AssertionError(f"unexpected query: {sql}")
        return self.frames.pop(0)


def _frame(*rows):
    return pd.DataFrame(list(rows))


def _config(model="gemini-test"):
    return {
        "provider": "gemini",
        "model": model,
        "input_price_per_million": 0,
        "output_price_per_million": 0,
    }


def test_bootstrap_has_five_written_language_product_descriptions(monkeypatch):
    db = _Db()
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_factory_state", lambda _db: "absent")
    monkeypatch.setattr(api, "_runtime_models", lambda _db: ([], ""))

    result = api.bootstrap(None)

    assert [item["recipe_id"] for item in result["recipes"]] == [
        "rag_knowledge_card_v1",
        "rag_argument_decomposition_v1",
        "sft_speech_critique_v1",
        "sft_attack_defence_v1",
        "transcript_structure_v1",
    ]
    assert all(item["description"].endswith("。") for item in result["recipes"])
    assert all("；" not in item["description"] for item in result["recipes"])
    transcript = result["recipes"][-1]
    assert transcript["label"] == "完整逐字稿結構拆分"
    assert transcript["transcript_workflow"] is True


def test_transcript_preview_persists_exact_window_hashes_without_provider_call(monkeypatch):
    db = object()
    captured = {}
    ids = iter(("transcript-1", "run-1"))
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "_model_config", lambda _db, _label: (_config(), "provider-key"))
    monkeypatch.setattr(api, "_preview_secret", lambda: "preview-secret")
    monkeypatch.setattr(api, "new_id", lambda _prefix: next(ids))

    def store_preview(_db, _actor, **kwargs):
        captured.update(kwargs)
        return {
            "transcript_id": kwargs["transcript_id"],
            "run_id": kwargs["run_id"],
            "windows": [
                {**item, "id": f"window-{item['ordinal']}"}
                for item in kwargs["window_previews"]
            ],
        }

    monkeypatch.setattr(api, "create_transcript_preview", store_preview)
    monkeypatch.setattr(
        r2_storage,
        "sign_upload_claim",
        lambda claim, _secret, *, expires: f"signed:{claim['manifest_sha256']}:{expires}",
    )
    body = api.TranscriptPreviewBody(
        title="完整比賽",
        topic_text="測試辯題",
        source_note="已取得主辦方許可",
        language="yue-Hant-HK",
        rights_basis="permission",
        rights_for_storage_confirmed=True,
        content_text="司儀開場。正方發言。反方發言。",
        model_label="Gemini Test",
    )

    result = api.preview_transcript(body, None)

    assert result["run_id"] == "run-1"
    assert result["window_count"] == 1
    assert len(result["manifest_sha256"]) == 64
    assert captured["content_text"] == body.content_text
    assert captured["window_previews"][0]["system_prompt"]
    assert body.content_text in captured["window_previews"][0]["user_prompt"]
    assert captured["window_previews"][0]["preview_sha256"] == result["windows"][0]["preview_sha256"]


def test_transcript_preview_requires_signing_secret_before_persisting_content(monkeypatch):
    db = object()
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "_model_config", lambda _db, _label: (_config(), "provider-key"))
    monkeypatch.setattr(
        api,
        "_preview_secret",
        lambda: (_ for _ in ()).throw(HTTPException(503, "missing signing secret")),
    )
    monkeypatch.setattr(
        api,
        "create_transcript_preview",
        lambda *_args, **_kwargs: pytest.fail(
            "transcript content must not be persisted without a signing secret"
        ),
    )

    with pytest.raises(HTTPException) as unavailable:
        api.preview_transcript(
            api.TranscriptPreviewBody(
                title="完整比賽",
                source_note="已取得主辦方許可",
                language="yue-Hant-HK",
                rights_basis="permission",
                rights_for_storage_confirmed=True,
                content_text="司儀開場。正方發言。",
                model_label="Gemini Test",
            ),
            None,
        )

    assert unavailable.value.status_code == 503


def test_transcript_preview_is_signed_before_persisting_content(monkeypatch):
    db = object()
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "_model_config", lambda _db, _label: (_config(), "provider-key"))
    monkeypatch.setattr(api, "_preview_secret", lambda: "preview-secret")
    monkeypatch.setattr(
        r2_storage,
        "sign_upload_claim",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("signing failed")),
    )
    monkeypatch.setattr(
        api,
        "create_transcript_preview",
        lambda *_args, **_kwargs: pytest.fail(
            "transcript content must not be persisted before its claim is signed"
        ),
    )

    with pytest.raises(RuntimeError, match="signing failed"):
        api.preview_transcript(
            api.TranscriptPreviewBody(
                title="完整比賽",
                source_note="已取得主辦方許可",
                language="yue-Hant-HK",
                rights_basis="permission",
                rights_for_storage_confirmed=True,
                content_text="司儀開場。正方發言。",
                model_label="Gemini Test",
            ),
            None,
        )


def test_transcript_confirmation_is_bound_to_manager_run_and_manifest(monkeypatch):
    db = object()
    captured = {}
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "_preview_secret", lambda: "preview-secret")
    monkeypatch.setattr(
        r2_storage,
        "verify_upload_claim",
        lambda _token, _secret: {
            "kind": "ai_factory_transcript_preview",
            "confirmation_version": api.TRANSCRIPT_CONFIRMATION_VERSION,
            "user_id": "manager",
            "run_id": "run-1",
            "manifest_sha256": "a" * 64,
            "pii_warnings": ["可能包含電話號碼"],
        },
    )

    def confirm(_db, _actor, _run_id, **kwargs):
        captured.update(kwargs)
        return {"id": _run_id, "status": "processing"}

    monkeypatch.setattr(api, "confirm_transcript_run", confirm)
    body = api.TranscriptConfirmBody(
        preview_token="signed",
        rights_confirmed=True,
        anonymized_confirmed=True,
        third_party_confirmed=True,
        pii_override_reason="已人工核對並移除識別資料",
    )

    result = api.confirm_transcript("run-1", body, None)

    assert result["status"] == "processing"
    assert captured["manifest_sha256"] == "a" * 64
    assert captured["pii_warning_count"] == 1


def test_transcript_withdrawal_route_calls_atomic_store_contract(monkeypatch):
    db = object()
    captured = {}
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)

    def withdraw(_db, actor, transcript_id, reason):
        captured.update({
            "actor": actor,
            "transcript_id": transcript_id,
            "reason": reason,
        })
        return {"ok": True, "changed": True}

    monkeypatch.setattr(api, "withdraw_transcript", withdraw, raising=False)

    result = api.withdraw_factory_transcript(
        "transcript-1",
        None,
        api.TranscriptWithdrawBody(reason="來源授權已撤回"),
    )

    assert result == {"ok": True, "changed": True}
    assert captured == {
        "actor": "manager",
        "transcript_id": "transcript-1",
        "reason": "來源授權已撤回",
    }


def test_model_drift_fails_claimed_window_before_provider_call(monkeypatch):
    db = object()
    failed = []
    claimed = {
        "done": False,
        "attempt_id": "attempt-1",
        "attempt_no": 1,
        "run_id": "run-1",
        "window_id": "window-1",
        "window_ordinal": 1,
        "context_start": 0,
        "context_end": 5,
        "core_start": 0,
        "core_end": 5,
        "prompt_sha256": "p" * 64,
        "input_sha256": "i" * 64,
        "preview_sha256": "v" * 64,
        "model_label": "Pinned Model",
        "provider": "gemini",
        "provider_model": "pinned-model",
        "prompt_version": "version",
        "prompt_template_sha256": "t" * 64,
        "instruction_text": "",
        "transcript_id": "transcript-1",
        "content_text": "甲乙丙丁戊",
        "content_sha256": api.sha256_text("甲乙丙丁戊"),
        "window_count": 1,
    }
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)
    monkeypatch.setattr(api, "_require_nonessential_bandwidth", lambda: None)
    monkeypatch.setattr(api, "claim_transcript_window", lambda *_args: claimed)
    monkeypatch.setattr(
        api,
        "_model_config",
        lambda _db, _label: (_config(model="changed-model"), "provider-key"),
    )
    monkeypatch.setattr(
        api,
        "fail_transcript_attempt",
        lambda *_args, **kwargs: failed.append(kwargs),
    )

    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(api.generate_next_transcript_window("run-1", None))

    assert mismatch.value.status_code == 409
    assert failed and failed[0]["provider_called"] is False


def test_segment_context_is_bounded_around_selected_original(monkeypatch):
    text = "甲" * 10_000
    db = _Db([_frame({
        "id": "segment-1",
        "start_offset": 5000,
        "end_offset": 5100,
        "transcript_id": "transcript-1",
        "title": "完整比賽",
        "content_text": text,
        "content_sha256": api.sha256_text(text),
    })])
    monkeypatch.setattr(api, "_manager", lambda _request: ("manager", db))
    monkeypatch.setattr(api, "_require_ready", lambda _db: None)

    result = api.transcript_segment_context("segment-1", None)

    context_size = api.AI_FACTORY_TRANSCRIPT_REVIEW_CONTEXT_CHARS
    assert result["context_start"] == 5000 - context_size
    assert result["context_end"] == 5100 + context_size
    assert len(result["context_text"]) == 100 + 2 * context_size
    assert "content_text" not in result
