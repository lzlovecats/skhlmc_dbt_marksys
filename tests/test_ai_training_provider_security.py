"""Gemini credential and error-sanitization regressions for AI Training."""

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from api import ai_training_api
import deploy.proxy as proxy


ROOT = Path(__file__).resolve().parents[1]


def test_ai_training_gemini_secret_never_reaches_url_browser_or_ledger(monkeypatch):
    secret = "training-gemini-super-secret"
    provider_call = {}
    ledger = []

    class _Frame:
        def to_dict(self, orient=None):
            assert orient == "records"
            return []

    class _Db:
        def query(self, *_args, **_kwargs):
            return _Frame()

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def failed_post(_client, url, **kwargs):
        provider_call.update(url=url, kwargs=kwargs)
        raise RuntimeError(f"authenticated request failed: {secret}")

    monkeypatch.setattr(ai_training_api, "_admin", lambda _request: ("admin", _Db()))
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: secret)
    monkeypatch.setattr(ai_training_api.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ai_training_api, "post_json_bounded", failed_post)
    monkeypatch.setattr(
        ai_training_api, "_log_ai",
        lambda *args, **kwargs: ledger.append((args, kwargs)),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_training_api.coverage_ai(None))

    assert raised.value.status_code == 502
    assert secret not in raised.value.detail
    assert secret not in repr(ledger)
    assert ledger[0][1]["error"] == ai_training_api.AI_TRAINING_PROVIDER_PUBLIC_ERROR
    assert secret not in provider_call["url"]
    assert "key=" not in provider_call["url"]
    assert provider_call["kwargs"]["headers"] == {"x-goog-api-key": secret}
    assert "params" not in provider_call["kwargs"]


def test_ai_training_source_has_no_gemini_query_key_or_raw_provider_error():
    source = (ROOT / "api" / "ai_training_api.py").read_text(encoding="utf-8")
    assert "?key=" not in source
    assert 'params={"key"' not in source
    assert "error=str(exc)" not in source
    assert "{str(exc)" not in source
