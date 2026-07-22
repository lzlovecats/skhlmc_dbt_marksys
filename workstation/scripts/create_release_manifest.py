#!/usr/bin/env python3
"""Create the canonical unsigned compatibility manifest from pinned artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

from ai_model_config import LMC_AI_MODEL_PROFILE_VERSION
from workstation.manager.release_manifest import canonical_json, validate_manifest
from workstation.version import (
    WORKSTATION_CONFIG_SCHEMA_VERSION,
    WORKSTATION_PROTOCOL_VERSION,
    WORKSTATION_VERSION,
)


def component(identifier: str, r2_key: str, path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return {
        "id": identifier,
        "r2_key": r2_key,
        "sha256": digest.hexdigest(),
        "bytes": size,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Create unsigned Workstation release manifest")
    value.add_argument("--channel", choices=("stable", "candidate"), required=True)
    value.add_argument("--website-min", required=True)
    value.add_argument("--website-max", required=True)
    value.add_argument("--nvidia-driver-min", required=True)
    value.add_argument("--cuda-min", required=True)
    value.add_argument("--ollama-min", required=True)
    value.add_argument("--gpt-sovits-commit", required=True)
    value.add_argument("--database-migration", required=True)
    for name in ("release-archive", "deb-package", "model-bundle", "rag-bundle"):
        value.add_argument(f"--{name}-id", required=True)
        value.add_argument(f"--{name}-r2-key", required=True)
        value.add_argument(f"--{name}-file", type=Path, required=True)
    value.add_argument("--published-epoch", type=int, default=0)
    value.add_argument("--expires-days", type=int, default=14, choices=range(1, 31))
    value.add_argument("--output", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    published = int(args.published_epoch or time.time())
    components = {}
    for cli, key in (
        ("release_archive", "release_archive"),
        ("deb_package", "deb_package"),
        ("model_bundle", "model_bundle"),
        ("rag_bundle", "rag_bundle"),
    ):
        components[key] = component(
            str(getattr(args, f"{cli}_id")),
            str(getattr(args, f"{cli}_r2_key")),
            getattr(args, f"{cli}_file"),
        )
    manifest = validate_manifest({
        "schema_version": 1,
        "release_version": WORKSTATION_VERSION,
        "channel": args.channel,
        "published_epoch": published,
        "expires_epoch": published + args.expires_days * 86_400,
        "compatibility": {
            "protocol_version": WORKSTATION_PROTOCOL_VERSION,
            "config_schema_version": WORKSTATION_CONFIG_SCHEMA_VERSION,
            "website_min": args.website_min,
            "website_max": args.website_max,
            "model_profile_version": LMC_AI_MODEL_PROFILE_VERSION,
            "ubuntu_version": "24.04",
            "nvidia_driver_min": args.nvidia_driver_min,
            "cuda_min": args.cuda_min,
            "ollama_min": args.ollama_min,
            "gpt_sovits_commit": args.gpt_sovits_commit,
            "database_migration_requirement": args.database_migration,
        },
        "components": components,
    }, now_epoch=published)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json(manifest) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
