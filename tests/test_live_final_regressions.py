"""Focused offline regressions from the final Solo Live review."""

import copy

import pytest

import deploy.proxy as proxy


@pytest.fixture(autouse=True)
def _empty_live_retry_cache():
    proxy._clear_solo_live_token_response_cache()
    yield
    proxy._clear_solo_live_token_response_cache()


def _claim():
    return {
        "user_id": "alice",
        "practice_id": "practice_mock_001",
        "mode": "mock",
        "session_seconds": [300, 600],
        "system_prompt": "server-locked prompt",
    }


def test_live_token_cache_key_binds_canonical_prompt_mode_and_sessions():
    claim = _claim()
    proxy._cache_solo_live_token(claim, 1, "bound-token")

    equivalent = copy.deepcopy(claim)
    equivalent["session_seconds"] = ["300", "600"]
    assert proxy._get_cached_solo_live_token(equivalent, 1) == "bound-token"

    variants = []
    for field, value in (
        ("system_prompt", "different prompt"),
        ("mode", "free"),
        ("session_seconds", [300, 601]),
    ):
        variant = copy.deepcopy(claim)
        variant[field] = value
        variants.append(variant)

    assert all(
        proxy._get_cached_solo_live_token(variant, 1) == ""
        for variant in variants
    )


def test_live_token_cache_key_contains_digest_not_raw_prompt():
    claim = _claim()
    key = proxy._solo_live_token_cache_key(claim, 0)

    assert key[:3] == ("alice", "practice_mock_001", 0)
    assert len(key[3]) == 64
    int(key[3], 16)
    assert claim["system_prompt"] not in repr(key)
