"""Subprocess-isolated tests for unsafe Live environment overrides."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
_LIVE_ENV_DEFAULTS = {
    "DB_POOL_TIMEOUT": "10",
    "LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS": "60",
    "LIVE_TOKEN_MINT_TIMEOUT_SECONDS": "20",
    "LIVE_TOKEN_LEDGER_DB_TIMEOUT_SECONDS": "3",
    "LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS": "45",
    "LIVE_TOKEN_RESPONSE_CACHE_SAFETY_SECONDS": "5",
    "LIVE_PRACTICE_CLAIM_TTL_SECONDS": "7200",
}
_DUMP_LIMITS = """
import json
import system_limits as limits
print(json.dumps({
    "new_session": limits.LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS,
    "mint_timeout": limits.LIVE_TOKEN_MINT_TIMEOUT_SECONDS,
    "ledger_timeout": limits.LIVE_TOKEN_LEDGER_DB_TIMEOUT_SECONDS,
    "cache_safety": limits.LIVE_TOKEN_RESPONSE_CACHE_SAFETY_SECONDS,
    "db_pool_timeout": limits.DB_POOL_TIMEOUT,
    "cache_ttl": limits.LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS,
    "claim_ttl": limits.LIVE_PRACTICE_CLAIM_TTL_SECONDS,
}))
"""


def _run_limits(overrides):
    env = os.environ.copy()
    env.update(_LIVE_ENV_DEFAULTS)
    env.update(overrides)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-c", _DUMP_LIMITS],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _load_limits(overrides):
    result = _run_limits(overrides)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("unsafe_value", ["1", "999999"])
def test_new_session_window_is_fixed_at_sixty_seconds(unsafe_value):
    values = _load_limits({
        "LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS": unsafe_value,
    })

    assert values["new_session"] == 60


@pytest.mark.parametrize(
    ("cache_override", "claim_override", "expected_cache"),
    [
        ("-100", "1", 10),
        ("999999", "999999", 55),
    ],
)
def test_cache_retry_ttl_stays_inside_start_window_and_claim_is_two_hours(
    cache_override, claim_override, expected_cache,
):
    values = _load_limits({
        "LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS": cache_override,
        "LIVE_PRACTICE_CLAIM_TTL_SECONDS": claim_override,
    })

    assert values["cache_ttl"] == expected_cache
    assert 0 < values["cache_ttl"] < values["new_session"] == 60
    assert values["claim_ttl"] == 2 * 60 * 60


def test_solo_token_mint_and_post_mint_ledger_fit_start_window():
    values = _load_limits({
        "DB_POOL_TIMEOUT": "999",
        "LIVE_TOKEN_MINT_TIMEOUT_SECONDS": "999",
        "LIVE_TOKEN_LEDGER_DB_TIMEOUT_SECONDS": "999",
        "LIVE_TOKEN_RESPONSE_CACHE_SAFETY_SECONDS": "999",
    })

    assert values["db_pool_timeout"] == 10
    assert values["mint_timeout"] == 20
    assert values["ledger_timeout"] == 3
    assert values["cache_safety"] == 5
    assert (
        values["mint_timeout"]
        + values["ledger_timeout"]
        + values["cache_safety"]
        < values["new_session"]
    )


@pytest.mark.parametrize(
    "variable",
    [
        "LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS",
        "LIVE_TOKEN_MINT_TIMEOUT_SECONDS",
        "LIVE_TOKEN_LEDGER_DB_TIMEOUT_SECONDS",
        "LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS",
        "LIVE_TOKEN_RESPONSE_CACHE_SAFETY_SECONDS",
        "LIVE_PRACTICE_CLAIM_TTL_SECONDS",
    ],
)
def test_non_integer_live_limit_override_fails_startup_with_runtime_error(variable):
    result = _run_limits({variable: "not-an-integer"})

    assert result.returncode != 0
    assert f"RuntimeError: {variable} must be an integer" in result.stderr
