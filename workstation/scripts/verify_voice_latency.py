#!/usr/bin/env python3
"""Verify privacy-safe browser Voice Coach latency samples against v1 targets."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile


MAX_INPUT_BYTES = 1024 * 1024
MAX_SAMPLES = 100
MIN_WARM_LOCAL_TURNS = 20
P50_FIRST_TEXT_MAX_MS = 8_000
P50_FIRST_AUDIO_MAX_MS = 15_000
P95_FIRST_AUDIO_MAX_MS = 25_000
_FAILURE_STAGE_RE = re.compile(r"[a-z][a-z0-9_]{0,79}")


def _milliseconds(value: object, *, nullable: bool) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("latency values must be integer milliseconds")
    if not 0 <= value <= 10 * 60 * 1000:
        raise ValueError("latency value is outside the bounded range")
    return value


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def verify_report(value: object, *, source_sha256: str = "") -> dict:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "generated_at", "samples",
    }:
        raise ValueError("acceptance report fields are invalid")
    if value.get("schema_version") != 1:
        raise ValueError("acceptance report schema is unsupported")
    generated_at = str(value.get("generated_at") or "")
    try:
        parsed = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("acceptance report timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("acceptance report timestamp must include a timezone")
    samples = value.get("samples")
    if not isinstance(samples, list) or not 1 <= len(samples) <= MAX_SAMPLES:
        raise ValueError("acceptance sample count is invalid")

    text_values: list[int] = []
    audio_values: list[int] = []
    non_local = 0
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != {
            "turn_index", "first_text_ms", "first_audio_ms", "tts_provider",
            "status", "failure_stage",
        }:
            raise ValueError("acceptance sample fields are invalid")
        turn_index = sample.get("turn_index")
        if (
            isinstance(turn_index, bool)
            or not isinstance(turn_index, int)
            or not 0 <= turn_index <= 100
        ):
            raise ValueError("acceptance turn index is invalid")
        provider = str(sample.get("tts_provider") or "")
        status = str(sample.get("status") or "")
        failure_stage = str(sample.get("failure_stage") or "")
        if provider not in {"local", "azure", "text", "none"}:
            raise ValueError("acceptance TTS provider is invalid")
        if status not in {"success", "fallback", "failed"}:
            raise ValueError("acceptance sample status is invalid")
        if failure_stage and not _FAILURE_STAGE_RE.fullmatch(failure_stage):
            raise ValueError("acceptance failure stage is invalid")
        text_ms = _milliseconds(sample.get("first_text_ms"), nullable=True)
        audio_ms = _milliseconds(sample.get("first_audio_ms"), nullable=True)
        if provider == "local" and status == "success" and not failure_stage:
            if text_ms is None or audio_ms is None:
                raise ValueError("successful local sample is missing latency")
            text_values.append(text_ms)
            audio_values.append(audio_ms)
        else:
            non_local += 1

    failures = []
    if non_local:
        failures.append("benchmark_contains_fallback_or_failed_turn")
    if len(text_values) < MIN_WARM_LOCAL_TURNS:
        failures.append("insufficient_warm_local_turns")
    observed = {
        "p50_first_text_ms": None,
        "p50_first_audio_ms": None,
        "p95_first_audio_ms": None,
    }
    if text_values:
        observed["p50_first_text_ms"] = _percentile(text_values, 0.5)
        observed["p50_first_audio_ms"] = _percentile(audio_values, 0.5)
        observed["p95_first_audio_ms"] = _percentile(audio_values, 0.95)
        if observed["p50_first_text_ms"] > P50_FIRST_TEXT_MAX_MS:
            failures.append("p50_first_text_target_missed")
        if observed["p50_first_audio_ms"] > P50_FIRST_AUDIO_MAX_MS:
            failures.append("p50_first_audio_target_missed")
        if observed["p95_first_audio_ms"] > P95_FIRST_AUDIO_MAX_MS:
            failures.append("p95_first_audio_target_missed")
    return {
        "schema_version": 1,
        "ok": not failures,
        "source_sha256": source_sha256,
        "generated_at": generated_at,
        "sample_count": len(samples),
        "warm_local_sample_count": len(text_values),
        "thresholds_ms": {
            "p50_first_text": P50_FIRST_TEXT_MAX_MS,
            "p50_first_audio": P50_FIRST_AUDIO_MAX_MS,
            "p95_first_audio": P95_FIRST_AUDIO_MAX_MS,
        },
        "observed_ms": observed,
        "failures": failures,
    }


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify browser Voice Coach warm-turn latency targets"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        if (
            arguments.input.is_symlink()
            or not arguments.input.is_file()
            or not 0 < arguments.input.stat().st_size <= MAX_INPUT_BYTES
        ):
            raise ValueError("acceptance report file is invalid")
        raw = arguments.input.read_bytes()
        value = json.loads(raw)
        report = verify_report(
            value, source_sha256=hashlib.sha256(raw).hexdigest()
        )
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        report = {
            "schema_version": 1,
            "ok": False,
            "error": "invalid_acceptance_report",
        }
        print(json.dumps(report, separators=(",", ":")))
        return 2
    if arguments.output:
        _atomic_json(arguments.output, report)
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
