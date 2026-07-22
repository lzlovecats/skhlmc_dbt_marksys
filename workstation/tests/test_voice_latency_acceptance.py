from __future__ import annotations

import pytest

from workstation.scripts.verify_voice_latency import verify_report


def _sample(*, text=7_000, audio=14_000, provider="local", status="success"):
    return {
        "turn_index": 1,
        "first_text_ms": text,
        "first_audio_ms": audio,
        "tts_provider": provider,
        "status": status,
        "failure_stage": "" if status == "success" else "voice_pipeline",
    }


def _report(samples):
    return {
        "schema_version": 1,
        "generated_at": "2026-07-22T12:00:00+08:00",
        "samples": samples,
    }


def test_twenty_warm_local_turns_pass_exact_v1_targets():
    samples = [_sample() for _ in range(20)]
    samples[-1]["first_audio_ms"] = 25_000
    result = verify_report(_report(samples), source_sha256="a" * 64)
    assert result["ok"] is True
    assert result["warm_local_sample_count"] == 20
    assert result["observed_ms"] == {
        "p50_first_text_ms": 7_000,
        "p50_first_audio_ms": 14_000,
        "p95_first_audio_ms": 14_000,
    }
    assert result["source_sha256"] == "a" * 64


def test_fallback_insufficient_samples_and_latency_miss_fail_closed():
    samples = [_sample(text=8_001, audio=25_001) for _ in range(19)]
    samples.append(_sample(provider="azure", status="fallback"))
    result = verify_report(_report(samples))
    assert result["ok"] is False
    assert set(result["failures"]) == {
        "benchmark_contains_fallback_or_failed_turn",
        "insufficient_warm_local_turns",
        "p50_first_text_target_missed",
        "p50_first_audio_target_missed",
        "p95_first_audio_target_missed",
    }


def test_acceptance_report_rejects_private_or_unbounded_fields():
    private = _sample()
    private["transcript"] = "不應收集"
    with pytest.raises(ValueError, match="fields"):
        verify_report(_report([private]))

    unbounded = _sample(text=700_000)
    with pytest.raises(ValueError, match="bounded"):
        verify_report(_report([unbounded]))
