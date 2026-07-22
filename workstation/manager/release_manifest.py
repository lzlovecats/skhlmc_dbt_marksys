"""Canonical Ed25519 release manifest and staged-tree verification."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import time
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from ai_model_config import LMC_AI_MODEL_PROFILE_VERSION
from system_limits import (
    WORKSTATION_RELEASE_FILE_MAX,
    WORKSTATION_RELEASE_UNPACKED_MAX_BYTES,
)
from workstation.version import (
    WORKSTATION_CONFIG_SCHEMA_VERSION,
    WORKSTATION_PROTOCOL_VERSION,
)
from workstation.workloads.errors import WorkloadError


MANIFEST_SCHEMA_VERSION = 1
COMPONENT_KEYS = frozenset({
    "release_archive", "deb_package", "model_bundle", "rag_bundle",
})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?")
_MIGRATION_RE = re.compile(r"[0-9]{8}_[0-9]{4}")
_COMMIT_RE = re.compile(r"[0-9a-f]{7,64}")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if not match:
        raise WorkloadError("manifest_invalid", "Release version is invalid.")
    return tuple(int(item) for item in match.groups())


def _https_url(value: object) -> str:
    raw = str(value or "")
    parsed = urlparse(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or len(raw) > 4_096
    ):
        raise WorkloadError("manifest_invalid", "Release download URL is invalid.")
    return parsed.geturl()


def _component(value: object, name: str) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "id", "r2_key", "sha256", "bytes",
    }:
        raise WorkloadError("manifest_invalid", f"{name} component is invalid.")
    identifier = str(value.get("id") or "")
    key = str(value.get("r2_key") or "")
    digest = str(value.get("sha256") or "").lower()
    size = int(value.get("bytes") or 0)
    if (
        not identifier
        or len(identifier) > 200
        or not key
        or len(key) > 1_024
        or key.startswith("/")
        or ".." in PurePosixPath(key).parts
        or not _SHA256_RE.fullmatch(digest)
        or size <= 0
    ):
        raise WorkloadError("manifest_invalid", f"{name} component is invalid.")
    return {"id": identifier, "r2_key": key, "sha256": digest, "bytes": size}


def validate_manifest(value: object, *, now_epoch: int | None = None) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "release_version", "channel", "published_epoch",
        "expires_epoch", "compatibility", "components",
    }:
        raise WorkloadError("manifest_invalid", "Release manifest schema is invalid.")
    if int(value.get("schema_version") or 0) != MANIFEST_SCHEMA_VERSION:
        raise WorkloadError("manifest_invalid", "Release manifest schema is unsupported.")
    release_version = str(value.get("release_version") or "")
    if not _VERSION_RE.fullmatch(release_version):
        raise WorkloadError("manifest_invalid", "Release version is invalid.")
    channel = str(value.get("channel") or "")
    if channel not in {"stable", "candidate"}:
        raise WorkloadError("manifest_invalid", "Release channel is invalid.")
    published = int(value.get("published_epoch") or 0)
    expires = int(value.get("expires_epoch") or 0)
    current = int(time.time() if now_epoch is None else now_epoch)
    if published <= 0 or published > current + 300 or expires <= current or expires > published + 31 * 86_400:
        raise WorkloadError("manifest_expired", "Release manifest is expired or has an invalid lifetime.")
    compatibility = value.get("compatibility")
    required_compatibility = {
        "protocol_version", "config_schema_version", "website_min",
        "website_max", "model_profile_version", "ubuntu_version",
        "nvidia_driver_min", "cuda_min", "ollama_min",
        "gpt_sovits_commit", "database_migration_requirement",
    }
    if not isinstance(compatibility, dict) or set(compatibility) != required_compatibility:
        raise WorkloadError("manifest_invalid", "Release compatibility contract is invalid.")
    clean_compatibility = {
        "protocol_version": int(compatibility.get("protocol_version") or 0),
        "config_schema_version": int(compatibility.get("config_schema_version") or 0),
        "website_min": str(compatibility.get("website_min") or ""),
        "website_max": str(compatibility.get("website_max") or ""),
        "model_profile_version": int(compatibility.get("model_profile_version") or 0),
        "ubuntu_version": str(compatibility.get("ubuntu_version") or ""),
        "nvidia_driver_min": str(compatibility.get("nvidia_driver_min") or ""),
        "cuda_min": str(compatibility.get("cuda_min") or ""),
        "ollama_min": str(compatibility.get("ollama_min") or ""),
        "gpt_sovits_commit": str(compatibility.get("gpt_sovits_commit") or "").lower(),
        "database_migration_requirement": str(compatibility.get("database_migration_requirement") or ""),
    }
    for field in ("website_min", "website_max", "nvidia_driver_min", "cuda_min", "ollama_min"):
        _version_tuple(clean_compatibility[field])
    if (
        clean_compatibility["protocol_version"] != WORKSTATION_PROTOCOL_VERSION
        or clean_compatibility["config_schema_version"] != WORKSTATION_CONFIG_SCHEMA_VERSION
        or clean_compatibility["model_profile_version"] != LMC_AI_MODEL_PROFILE_VERSION
        or clean_compatibility["ubuntu_version"] != "24.04"
        or not _COMMIT_RE.fullmatch(clean_compatibility["gpt_sovits_commit"])
        or not _MIGRATION_RE.fullmatch(clean_compatibility["database_migration_requirement"])
        or _version_tuple(clean_compatibility["website_min"])
        > _version_tuple(clean_compatibility["website_max"])
    ):
        raise WorkloadError("manifest_invalid", "Release compatibility contract is invalid.")
    components = value.get("components")
    if not isinstance(components, dict) or set(components) != COMPONENT_KEYS:
        raise WorkloadError("manifest_invalid", "Release component set is invalid.")
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_version": release_version,
        "channel": channel,
        "published_epoch": published,
        "expires_epoch": expires,
        "compatibility": clean_compatibility,
        "components": {
            name: _component(components[name], name) for name in sorted(COMPONENT_KEYS)
        },
    }


def verify_envelope(
    value: object,
    public_key_file: Path,
    *,
    now_epoch: int | None = None,
) -> tuple[dict, str]:
    if not isinstance(value, dict) or set(value) != {"manifest", "signature", "downloads"}:
        raise WorkloadError("manifest_invalid", "Signed release envelope is invalid.")
    manifest = verify_signed_manifest(
        value.get("manifest"), value.get("signature"), public_key_file,
        now_epoch=now_epoch,
    )
    downloads = value.get("downloads")
    if not isinstance(downloads, dict) or set(downloads) != {"release_archive"}:
        raise WorkloadError("manifest_invalid", "Release downloads are invalid.")
    return manifest, _https_url(downloads["release_archive"])


def verify_signed_manifest(
    manifest_value: object,
    signature_value: object,
    public_key_file: Path,
    *,
    now_epoch: int | None = None,
) -> dict:
    manifest = validate_manifest(manifest_value, now_epoch=now_epoch)
    try:
        key = load_pem_public_key(public_key_file.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise TypeError("not Ed25519")
        signature = base64.b64decode(str(signature_value or ""), validate=True)
        if len(signature) != 64:
            raise ValueError("invalid Ed25519 signature length")
        key.verify(signature, canonical_json(manifest))
    except (OSError, TypeError, ValueError, InvalidSignature) as exc:
        raise WorkloadError("signature_invalid", "Release signature could not be verified.") from exc
    return manifest


def verify_compatibility(manifest: dict, facts: dict) -> None:
    contract = manifest["compatibility"]
    website = str(facts.get("website_version") or "")
    if not (
        _version_tuple(contract["website_min"])
        <= _version_tuple(website)
        <= _version_tuple(contract["website_max"])
    ):
        raise WorkloadError("incompatible_release", "Website version is outside the release compatibility window.")
    exact = {
        "ubuntu_version": contract["ubuntu_version"],
        "gpt_sovits_commit": contract["gpt_sovits_commit"],
        "database_migration_requirement": contract["database_migration_requirement"],
    }
    if any(str(facts.get(field) or "").lower() != str(expected).lower() for field, expected in exact.items()):
        raise WorkloadError("incompatible_release", "Release compatibility facts do not match.")
    minimums = {
        "nvidia_driver": contract["nvidia_driver_min"],
        "cuda": contract["cuda_min"],
        "ollama": contract["ollama_min"],
    }
    if any(_version_tuple(str(facts.get(field) or "")) < _version_tuple(minimum) for field, minimum in minimums.items()):
        raise WorkloadError("incompatible_release", "A required runtime is below the release minimum.")


def verify_release_tree(root: Path) -> dict:
    try:
        if root.is_symlink() or not root.is_dir():
            raise ValueError("release tree root is invalid")
        root_mode = root.stat().st_mode
        if root_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o022):
            raise ValueError("release tree root mode is invalid")
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkloadError(
            "release_tree_invalid", "Release tree root is invalid."
        ) from exc
    manifest_file = resolved / "release-files.sha256"
    try:
        if (
            manifest_file.is_symlink()
            or not manifest_file.is_file()
            or manifest_file.stat().st_mode
            & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o022)
            or not 0 < manifest_file.stat().st_size
            <= WORKSTATION_RELEASE_FILE_MAX * 1_200
        ):
            raise ValueError("release file manifest size is invalid")
        lines = manifest_file.read_bytes().decode("utf-8").splitlines()
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise WorkloadError("release_tree_invalid", "Release file manifest is missing.") from exc
    if not 1 <= len(lines) <= WORKSTATION_RELEASE_FILE_MAX:
        raise WorkloadError("release_tree_invalid", "Release file manifest has too many entries.")
    expected: dict[str, str] = {}
    for line in lines:
        if len(line) < 67 or line[64:66] != "  ":
            raise WorkloadError("release_tree_invalid", "Release file manifest is malformed.")
        digest, relative = line[:64], line[66:]
        pure = PurePosixPath(relative)
        if (
            not _SHA256_RE.fullmatch(digest)
            or pure.is_absolute()
            or not relative
            or ".." in pure.parts
            or relative in expected
        ):
            raise WorkloadError("release_tree_invalid", "Release file path is invalid.")
        expected[relative] = digest
    actual = set()
    entries = 0
    try:
        for path in resolved.rglob("*"):
            entries += 1
            if entries > WORKSTATION_RELEASE_FILE_MAX:
                raise WorkloadError(
                    "release_tree_invalid", "Release tree has too many entries."
                )
            if path.is_symlink():
                raise WorkloadError(
                    "release_tree_invalid", "Release tree contains a symlink."
                )
            mode = path.stat().st_mode
            if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o022):
                raise WorkloadError(
                    "release_tree_invalid", "Release tree has an unsafe file mode."
                )
            if path.is_file() and path != manifest_file:
                actual.add(path.relative_to(resolved).as_posix())
            elif not path.is_file() and not path.is_dir():
                raise WorkloadError(
                    "release_tree_invalid", "Release tree has an unsupported entry."
                )
    except RuntimeError as exc:
        raise WorkloadError(
            "release_tree_invalid", "Release tree inventory is invalid."
        ) from exc
    if actual != set(expected):
        raise WorkloadError("release_tree_invalid", "Release file inventory does not match.")
    total = 0
    for relative, digest in expected.items():
        path = resolved / relative
        if path.is_symlink() or not path.is_file():
            raise WorkloadError("release_tree_invalid", "Release contains an unsupported file.")
        data_hash = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > WORKSTATION_RELEASE_UNPACKED_MAX_BYTES:
                    raise WorkloadError(
                        "release_tree_invalid", "Release tree exceeds its safe size."
                    )
                data_hash.update(chunk)
        if data_hash.hexdigest() != digest:
            raise WorkloadError("release_tree_invalid", "Release file hash does not match.")
    return {"files": len(expected), "bytes": total}
