"""Pure contracts for the full-transcript structure product."""

import json
import math

import pytest

from core.ai_data_factory import FactoryContractError
from core.ai_transcript_factory import (
    TRANSCRIPT_STRUCTURE_RECIPE,
    build_segment_payloads,
    build_transcript_prompt,
    build_transcript_window_plan,
    estimate_transcript_cost,
    parse_validate_transcript_boundaries,
    transcript_manifest_hash,
    transcript_preview_hashes,
    transcript_provider_payload,
    validate_reviewed_segment,
)


def _expected_mixed_text_tokens(value):
    ascii_chars = sum(ord(character) < 128 for character in value)
    non_ascii_chars = len(value) - ascii_chars
    return max(1, math.ceil(ascii_chars / 4 + non_ascii_chars * 1.2))


def test_window_plan_has_non_overlapping_cores_and_bounded_context():
    windows = build_transcript_window_plan(30_000, core_chars=11_000, overlap_chars=2_000)

    assert [(item.core_start, item.core_end) for item in windows] == [
        (0, 11_000),
        (11_000, 22_000),
        (22_000, 30_000),
    ]
    assert [(item.context_start, item.context_end) for item in windows] == [
        (0, 13_000),
        (9_000, 24_000),
        (20_000, 30_000),
    ]


def test_transcript_prompt_contains_only_the_bounded_context_and_exact_hashes():
    text = "甲" * 15_000 + "UNIQUE-TAIL" + "乙" * 15_000
    window = build_transcript_window_plan(len(text))[0]
    prompt = build_transcript_prompt(text, window, manager_instruction="留意司儀")
    config = {"provider": "gemini", "model": "gemini-test"}
    provider_payload = transcript_provider_payload("測試模型", config, prompt)
    input_sha, preview_sha = transcript_preview_hashes(
        "transcript-1", "a" * 64, prompt, provider_payload,
    )

    assert prompt.recipe_id == TRANSCRIPT_STRUCTURE_RECIPE
    assert "留意司儀" in prompt.user
    assert "UNIQUE-TAIL" not in prompt.user
    assert len(input_sha) == len(preview_sha) == 64
    manifest = transcript_manifest_hash([{
        "ordinal": 1,
        "input_sha256": input_sha,
        "prompt_sha256": prompt.prompt_sha256,
        "preview_sha256": preview_sha,
    }])
    assert len(manifest) == 64


def test_transcript_cost_uses_the_shared_conservative_mixed_language_estimate():
    text = "司儀開場。正方主辯開始發言。" * 20
    prompt = build_transcript_prompt(text, build_transcript_window_plan(len(text))[0])

    estimate = estimate_transcript_cost(
        {
            "input_price_per_million": 1,
            "output_price_per_million": 0,
        },
        [prompt],
    )

    assert estimate["input_tokens"] == _expected_mixed_text_tokens(
        prompt.system + prompt.user
    )


def test_transcript_window_uses_the_loose_factory_output_cap():
    text = "司儀開場。正方主辯開始發言。" * 20
    prompt = build_transcript_prompt(
        text, build_transcript_window_plan(len(text))[0]
    )
    config = {
        "provider": "gemini",
        "model": "gemini-test",
        "input_price_per_million": 1,
        "output_price_per_million": 9,
    }

    payload = transcript_provider_payload("測試模型", config, prompt)
    estimate = estimate_transcript_cost(config, [prompt])

    assert payload["max_output_tokens"] == 10_000
    assert estimate["output_tokens"] == 10_000


def test_boundary_output_is_strict_and_first_window_must_start_at_zero():
    window = build_transcript_window_plan(100)[0]
    valid = json.dumps({
        "recipe_id": TRANSCRIPT_STRUCTURE_RECIPE,
        "language": "yue-Hant-HK",
        "window_ordinal": 1,
        "boundaries": [
            {
                "start_offset": 0,
                "speaker_label": "司儀",
                "side": "neutral",
                "stage": "general",
                "confidence": 90,
                "review_items": [],
            },
            {
                "start_offset": 20,
                "speaker_label": "正方主辯",
                "side": "pro",
                "stage": "opening",
                "confidence": 80,
                "review_items": ["請核對辯位"],
            },
        ],
    }, ensure_ascii=False)

    parsed = parse_validate_transcript_boundaries(valid, window=window)

    assert [item["start_offset"] for item in parsed.boundaries] == [0, 20]
    missing_zero = json.loads(valid)
    missing_zero["boundaries"] = missing_zero["boundaries"][1:]
    with pytest.raises(FactoryContractError, match="begin at offset 0"):
        parse_validate_transcript_boundaries(
            json.dumps(missing_zero, ensure_ascii=False), window=window,
        )


def test_boundaries_become_complete_exact_non_overlapping_segments():
    text = "司儀發言。正方發言。反方發言。"
    payloads = build_segment_payloads(text, [{
        "id": "window-1",
        "boundaries": [
            {
                "start_offset": 0,
                "speaker_label": "司儀",
                "side": "neutral",
                "stage": "general",
                "confidence": 95,
                "review_items": [],
            },
            {
                "start_offset": 5,
                "speaker_label": "正方主辯",
                "side": "pro",
                "stage": "opening",
                "confidence": 90,
                "review_items": [],
            },
            {
                "start_offset": 10,
                "speaker_label": "反方主辯",
                "side": "con",
                "stage": "opening",
                "confidence": 85,
                "review_items": [],
            },
        ],
    }])

    segments = [item["payload"] for item in payloads]
    assert [item["sequence_no"] for item in segments] == [1, 2, 3]
    assert "".join(item["full_text"] for item in segments) == text
    assert [(item["start_offset"], item["end_offset"]) for item in segments] == [
        (0, 5), (5, 10), (10, len(text)),
    ]
    assert all(item["quote"] == item["full_text"] for item in segments)


def test_reviewed_segment_must_quote_the_exact_offsets():
    text = "甲方第一段乙方第二段"
    payload = {
        "sequence_no": 1,
        "start_offset": 0,
        "end_offset": 5,
        "quote": text[:5],
        "speaker_label": "正方主辯",
        "side": "pro",
        "stage": "opening",
        "full_text": text[:5],
        "confidence": 88,
        "review_items": [],
    }

    assert validate_reviewed_segment(payload, transcript_text=text) == payload
    payload["quote"] = "改寫內容"
    with pytest.raises(FactoryContractError, match="does not match"):
        validate_reviewed_segment(payload, transcript_text=text)
