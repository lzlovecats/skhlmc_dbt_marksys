#!/usr/bin/env python3
"""Generate one bounded GPT-SoVITS v2Pro training profile in its pinned runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import yaml


SUPPORTED_COMMIT = "d7c2210da8c013e81a94bfc7b811a477c99fd506"
SUPPORTED_FAMILY = "v2Pro"


def _atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _integer(value, *, minimum: int, maximum: int, name: str) -> int:
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} is outside the reviewed range")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--recommendation", type=Path, required=True)
    arguments = parser.parse_args()

    runtime = arguments.runtime_root.resolve(strict=True)
    experiment = arguments.experiment_root.resolve(strict=True)
    recommendation_path = arguments.recommendation.resolve(strict=True)
    commit = (runtime / "APPROVED_COMMIT").read_text(encoding="ascii").strip().lower()
    recommendation_raw = recommendation_path.read_bytes()
    recommendation = json.loads(recommendation_raw)
    upstream = recommendation.get("upstream_profile") if isinstance(recommendation, dict) else None
    if (
        commit != SUPPORTED_COMMIT
        or not isinstance(upstream, dict)
        or upstream.get("git_commit") != SUPPORTED_COMMIT
        or upstream.get("model_family") != SUPPORTED_FAMILY
        or recommendation.get("dataset_readiness") == "BLOCKED_SPLIT"
        or recommendation.get("gpu_info") != "0"
    ):
        raise ValueError("training recommendation does not match the pinned profile")

    pretrained_gpt = (runtime / str(upstream["pretrained_gpt"])).resolve(strict=True)
    pretrained_sovits_g = (runtime / str(upstream["pretrained_sovits_g"])).resolve(strict=True)
    pretrained_sovits_d = (runtime / str(upstream["pretrained_sovits_d"])).resolve(strict=True)
    for path in (pretrained_gpt, pretrained_sovits_g, pretrained_sovits_d):
        if runtime not in path.parents or not path.is_file():
            raise ValueError("reviewed pretrained weights are unavailable")

    s1_base = runtime / "GPT_SoVITS/configs/s1longer-v2.yaml"
    s2_base = runtime / "GPT_SoVITS/configs/s2v2Pro.json"
    s1 = yaml.safe_load(s1_base.read_text(encoding="utf-8"))
    s2 = json.loads(s2_base.read_text(encoding="utf-8"))
    if not isinstance(s1, dict) or not isinstance(s2, dict):
        raise ValueError("pinned upstream training configs are invalid")

    sovits_batch = _integer(recommendation["sovits_batch"], minimum=1, maximum=4, name="sovits_batch")
    sovits_epochs = _integer(recommendation["sovits_epochs"], minimum=1, maximum=100, name="sovits_epochs")
    sovits_save = _integer(recommendation["sovits_save_every"], minimum=1, maximum=100, name="sovits_save_every")
    gpt_batch = _integer(recommendation["gpt_batch"], minimum=1, maximum=4, name="gpt_batch")
    gpt_epochs = _integer(recommendation["gpt_epochs"], minimum=1, maximum=100, name="gpt_epochs")
    gpt_save = _integer(recommendation["gpt_save_every"], minimum=1, maximum=100, name="gpt_save_every")
    precision = str(recommendation.get("precision") or "")
    if precision not in {"16-mixed", "32"}:
        raise ValueError("training precision is outside the reviewed set")

    s2["train"].update({
        "batch_size": sovits_batch,
        "epochs": sovits_epochs,
        "text_low_lr_rate": float(recommendation["sovits_lr_weight"]),
        "pretrained_s2G": str(pretrained_sovits_g),
        "pretrained_s2D": str(pretrained_sovits_d),
        "if_save_latest": bool(recommendation["save_latest"]),
        "if_save_every_weights": bool(recommendation["save_every_weights"]),
        "save_every_epoch": sovits_save,
        "gpu_numbers": "0",
        "grad_ckpt": False,
        "lora_rank": "32",
        "fp16_run": precision == "16-mixed",
    })
    s2["model"]["version"] = SUPPORTED_FAMILY
    s2["data"]["exp_dir"] = str(experiment)
    s2["s2_ckpt_dir"] = str(experiment)
    s2["save_weight_dir"] = str(experiment / "published-sovits")
    s2["name"] = arguments.dataset_id
    s2["version"] = SUPPORTED_FAMILY

    s1["train"].update({
        "batch_size": gpt_batch,
        "epochs": gpt_epochs,
        "precision": precision,
        "save_every_n_epoch": gpt_save,
        "if_save_every_weights": bool(recommendation["save_every_weights"]),
        "if_save_latest": bool(recommendation["save_latest"]),
        "if_dpo": bool(recommendation["enable_dpo"]),
        "half_weights_save_dir": str(experiment / "published-gpt"),
        "exp_name": arguments.dataset_id,
    })
    s1["pretrained_s1"] = str(pretrained_gpt)
    s1["train_semantic_path"] = str(experiment / "6-name2semantic.tsv")
    s1["train_phoneme_path"] = str(experiment / "2-name2text.txt")
    s1["output_dir"] = str(experiment / "logs-s1-v2Pro")

    profile = experiment / "profiles"
    _atomic(profile / "sovits.json", json.dumps(s2, ensure_ascii=False, indent=2) + "\n")
    _atomic(profile / "gpt.yaml", yaml.safe_dump(s1, allow_unicode=True, sort_keys=True))
    receipt = {
        "schema_version": 1,
        "dataset_id": arguments.dataset_id,
        "upstream_commit": commit,
        "model_family": SUPPORTED_FAMILY,
        "recommendation_sha256": hashlib.sha256(recommendation_raw).hexdigest(),
        "gpt_config": str(profile / "gpt.yaml"),
        "sovits_config": str(profile / "sovits.json"),
    }
    _atomic(profile / "receipt.json", json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
