#!/usr/bin/env python3
"""Offline verification for a signed Workstation release component."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from workstation.manager.release_manifest import (
    COMPONENT_KEYS,
    verify_signed_manifest,
)
from workstation.workloads.errors import WorkloadError


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify an offline Workstation artifact against Ed25519 manifest",
    )
    parser.add_argument("--envelope", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--component", choices=sorted(COMPONENT_KEYS), required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        envelope = json.loads(arguments.envelope.read_bytes())
        if not isinstance(envelope, dict) or set(envelope) != {"manifest", "signature"}:
            raise ValueError("signed envelope schema is invalid")
        manifest = verify_signed_manifest(
            envelope["manifest"], envelope["signature"], arguments.public_key,
        )
        component = manifest["components"][arguments.component]
        digest, size = _sha256(arguments.artifact)
        if digest != component["sha256"] or size != component["bytes"]:
            raise ValueError("artifact hash or size does not match")
    except (OSError, ValueError, TypeError, json.JSONDecodeError, WorkloadError) as exc:
        raise SystemExit("artifact verification failed") from exc
    print(json.dumps({
        "ok": True,
        "release_version": manifest["release_version"],
        "component": arguments.component,
        "id": component["id"],
        "sha256": digest,
        "bytes": size,
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
