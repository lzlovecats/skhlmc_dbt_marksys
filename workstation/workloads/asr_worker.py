#!/usr/bin/env python3
"""Pinned Faster-Whisper worker; invoked only by the Manager adapter."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from system_limits import LOCAL_PRACTICE_CONTEXT_MAX_CHARS


def _write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=("cuda", "cpu"), required=True)
    parser.add_argument("--compute-type", required=True)
    arguments = parser.parse_args()
    try:
        model_path = arguments.model.resolve(strict=True)
        audio_path = arguments.audio.resolve(strict=True)
        if not model_path.is_dir() or not audio_path.is_file():
            raise ValueError("local ASR input is invalid")
        from faster_whisper import WhisperModel

        model = WhisperModel(
            str(model_path),
            device=arguments.device,
            compute_type=arguments.compute_type,
            local_files_only=True,
        )
        segments, info = model.transcribe(
            str(audio_path),
            language="zh",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True,
        )
        parts = []
        for segment in segments:
            text = str(getattr(segment, "text", "") or "").strip()
            if text:
                parts.append(text)
            if len("".join(parts)) > LOCAL_PRACTICE_CONTEXT_MAX_CHARS:
                raise ValueError("transcript too long")
        transcript = "".join(parts).strip()
        if not transcript:
            _write(arguments.output, {"ok": False, "code": "empty_transcript"})
            return 2
        _write(arguments.output, {
            "ok": True,
            "text": transcript,
            "language": str(getattr(info, "language", "zh") or "zh")[:20],
            "language_probability": float(
                getattr(info, "language_probability", 0) or 0
            ),
        })
        return 0
    except Exception as exc:
        marker = str(exc).casefold()
        code = (
            "out_of_memory"
            if any(value in marker for value in ("out of memory", "cuda", "oom"))
            else "asr_failed"
        )
        try:
            _write(arguments.output, {"ok": False, "code": code})
        except OSError:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
