"""Live page render contracts, each one a shipped bug:

1. The「自由辯論→Mock」UI rebrand once rewrote the injected system prompt,
   runtime prompts and segment labels (AI received corrupted instructions).
2. Only the first Mock session token was HMAC-signed, so relay sessions died
   at the first chapter hand-off.
"""

import json

import pytest

import deploy.proxy as proxy
from debate_timing import get_full_mock_sequence, split_mock_into_sessions
from prompts import LIVE_RUNTIME_PROMPTS, build_full_mock_live_prompt


def _mock_payload():
    prompt = build_full_mock_live_prompt("測試辯題", "正方", "聯中", free_debate_minutes=5)
    segments = get_full_mock_sequence("聯中", free_debate_minutes=5)
    sessions = split_mock_into_sessions(segments)
    flat = [
        {**segment, "session": index}
        for index, session in enumerate(sessions)
        for segment in session["segments"]
    ]
    return prompt, flat, [session["label"] for session in sessions]


def _render_mock(tokens):
    prompt, flat, labels = _mock_payload()
    html = proxy._render_live_debate_html(
        tokens[0], prompt, 60, [], False, segments=flat, tokens=tokens,
        session_labels=labels, session_label="Mock",
    )
    return prompt, flat, html


def test_mock_rebrand_never_rewrites_injected_payloads():
    prompt, flat, html = _render_mock(["tok-1", "tok-2"])
    assert json.dumps(prompt, ensure_ascii=False) in html
    assert json.dumps(flat, ensure_ascii=False) in html
    assert json.dumps(LIVE_RUNTIME_PROMPTS, ensure_ascii=False) in html
    assert "開始Mock" in html
    assert "開始自由辯論" not in html


def test_free_debate_render_keeps_original_copy_and_prompt():
    html = proxy._render_live_debate_html("tok-f", "prompt 自由辯論", 2.5, [], True)
    assert "開始自由辯論" in html
    assert json.dumps("prompt 自由辯論", ensure_ascii=False) in html


def test_every_mock_session_token_is_relay_signed(monkeypatch):
    monkeypatch.setattr(
        proxy, "_get_proxy_secret",
        lambda key, default="": "wss://relay.example" if key == "LIVE_RELAY_WS_BASE" else "",
    )
    monkeypatch.setattr(
        proxy, "_sign_relay_token",
        lambda token, *args, **kwargs: f"sig-{token}",
    )
    tokens = ["tok-1", "tok-2", "tok-3"]
    _, _, html = _render_mock(tokens)
    sigs_line = next(
        line.strip() for line in html.splitlines()
        if line.strip().startswith("const TOKEN_SIGS =")
    )
    sigs = json.loads(
        sigs_line[len("const TOKEN_SIGS ="):].strip().rstrip(";").removesuffix("|| {}").strip()
    )
    assert set(sigs) == set(tokens)
