#!/usr/bin/env python3
"""Isolated official Qwen3-ASR worker invoked by the Manager adapter."""

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


def _transcribe(model, audio_path: Path) -> dict:
    results = model.transcribe(audio=str(audio_path), language="Cantonese")
    result = results[0] if results else None
    transcript = str(getattr(result, "text", "") or "").strip()
    if not transcript:
        return {"ok": False, "code": "empty_transcript"}
    if len(transcript) > LOCAL_PRACTICE_CONTEXT_MAX_CHARS:
        raise ValueError("transcript too long")
    return {
        "ok": True,
        "text": transcript,
        "language": str(
            getattr(result, "language", "Cantonese") or "Cantonese"
        )[:40],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--device", choices=("cuda", "cpu"), required=True)
    parser.add_argument(
        "--compute-type",
        choices=("float16", "bfloat16", "float32"),
        required=True,
    )
    arguments = parser.parse_args()
    try:
        model_path = arguments.model.resolve(strict=True)
        if (
            arguments.model.is_symlink()
            or not model_path.is_dir()
            or not (model_path / "config.json").is_file()
        ):
            raise ValueError("local ASR input is invalid")

        import torch
        from qwen_asr import Qwen3ASRModel

        model = Qwen3ASRModel.from_pretrained(
            str(model_path),
            dtype=getattr(torch, arguments.compute_type),
            device_map="cuda:0" if arguments.device == "cuda" else "cpu",
            local_files_only=True,
            max_inference_batch_size=1,
            max_new_tokens=LOCAL_PRACTICE_CONTEXT_MAX_CHARS,
        )
        if arguments.serve:
            print("READY", flush=True)
            raw = sys.stdin.buffer.readline(4_097)
            if not raw.endswith(b"\n") or len(raw) > 4_096:
                raise ValueError("ASR worker command is invalid")
            command = json.loads(raw)
            if not isinstance(command, dict) or set(command) != {"audio", "output"}:
                raise ValueError("ASR worker command is invalid")
            audio_path = Path(str(command["audio"])).resolve(strict=True)
            output_path = Path(str(command["output"]))
        else:
            if arguments.audio is None or arguments.output is None:
                raise ValueError("ASR audio and output are required")
            audio_path = arguments.audio.resolve(strict=True)
            output_path = arguments.output
        if (
            not audio_path.is_file()
            or output_path.parent.resolve(strict=True) != audio_path.parent
        ):
            raise ValueError("local ASR input is invalid")
        result = _transcribe(model, audio_path)
        _write(output_path, result)
        return 0 if result.get("ok") else 2
    except Exception as exc:
        marker = str(exc).casefold()
        code = (
            "out_of_memory"
            if any(value in marker for value in ("out of memory", "cuda", "oom"))
            else "asr_failed"
        )
        output = locals().get("output_path") or arguments.output
        if output is not None:
            try:
                _write(output, {"ok": False, "code": code})
            except OSError:
                pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
