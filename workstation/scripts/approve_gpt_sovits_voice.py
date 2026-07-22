#!/usr/bin/env python3
"""Create an explicit, local approval receipt for one evaluated voice pair."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from workstation.workloads.gpt_sovits import (
    SUPPORTED_GPT_SOVITS_COMMIT,
    SUPPORTED_GPT_SOVITS_FAMILY,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _small_required_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 64 * 1024:
        raise ValueError("reference text is invalid")
    return path.read_text(encoding="utf-8").strip()


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=Path("/srv/lmc-ai/vendor/GPT-SoVITS"))
    parser.add_argument("--checkpoint-root", type=Path, default=Path("/srv/lmc-ai/checkpoints"))
    parser.add_argument("--output-root", type=Path, default=Path("/srv/lmc-ai/models/gpt-sovits"))
    parser.add_argument("--gpt-weight", type=Path, required=True)
    parser.add_argument("--sovits-weight", type=Path, required=True)
    parser.add_argument("--reference-audio", type=Path, required=True)
    parser.add_argument("--reference-text", type=Path, required=True)
    parser.add_argument("--model-version", required=True)
    arguments = parser.parse_args()

    model_version = str(arguments.model_version or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", model_version):
        parser.error("model version is invalid")
    runtime = arguments.runtime_root.resolve(strict=True)
    checkpoint_root = arguments.checkpoint_root.resolve(strict=True)
    output_root = arguments.output_root.resolve(strict=False)
    gpt_weight = arguments.gpt_weight.resolve(strict=True)
    sovits_weight = arguments.sovits_weight.resolve(strict=True)
    reference_audio = arguments.reference_audio.resolve(strict=True)
    reference_text = arguments.reference_text.resolve(strict=True)
    if (
        checkpoint_root not in gpt_weight.parents
        or checkpoint_root not in sovits_weight.parents
        or gpt_weight.suffix != ".ckpt"
        or sovits_weight.suffix != ".pth"
        or not reference_audio.is_file()
        or not reference_text.is_file()
        or not _small_required_text(reference_text)
    ):
        parser.error("approved voice artifacts are invalid")
    commit = (runtime / "APPROVED_COMMIT").read_text(encoding="ascii").strip().lower()
    if commit != SUPPORTED_GPT_SOVITS_COMMIT:
        parser.error("GPT-SoVITS runtime commit is not supported by this release")
    for relative in (
        "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
        "GPT_SoVITS/pretrained_models/chinese-hubert-base",
    ):
        if not (runtime / relative).is_dir():
            parser.error("GPT-SoVITS pretrained runtime is incomplete")

    output_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    inference_path = output_root / "tts_infer.json"
    inference = {
        "custom": {
            "bert_base_path": str(runtime / "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large"),
            "cnhuhbert_base_path": str(runtime / "GPT_SoVITS/pretrained_models/chinese-hubert-base"),
            "device": "cuda",
            "is_half": True,
            "t2s_weights_path": str(gpt_weight),
            "version": SUPPORTED_GPT_SOVITS_FAMILY,
            "vits_weights_path": str(sovits_weight),
        }
    }
    _atomic(inference_path, inference)
    receipt = {
        "schema_version": 1,
        "model_version": model_version,
        "upstream_commit": commit,
        "model_family": SUPPORTED_GPT_SOVITS_FAMILY,
        "inference_config": _artifact(inference_path),
        "gpt_weight": _artifact(gpt_weight),
        "sovits_weight": _artifact(sovits_weight),
        "reference_audio": _artifact(reference_audio),
        "reference_text": _artifact(reference_text),
    }
    _atomic(output_root / "active-receipt.json", receipt)
    print(json.dumps({
        "ok": True,
        "model_version": model_version,
        "receipt": str(output_root / "active-receipt.json"),
        "service_restarted": False,
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
