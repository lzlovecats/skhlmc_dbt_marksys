#!/usr/bin/env python3
"""Offline-only Ed25519 signing of one canonical Workstation manifest."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from workstation.manager.release_manifest import canonical_json, validate_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign Workstation release manifest offline")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("refusing to overwrite signed manifest")
    try:
        manifest = validate_manifest(json.loads(args.manifest.read_bytes()))
        key = load_pem_private_key(args.private_key.read_bytes(), password=None)
    except Exception as exc:
        raise SystemExit("manifest or offline private key is invalid") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("offline signing key must be Ed25519")
    signature = base64.b64encode(key.sign(canonical_json(manifest))).decode("ascii")
    envelope = {"manifest": manifest, "signature": signature}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json(envelope) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
