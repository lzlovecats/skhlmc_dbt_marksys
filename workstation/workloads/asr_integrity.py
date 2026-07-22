"""Deterministic integrity receipts for the manually approved ASR profile."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from system_limits import WORKSTATION_ASR_APPROVAL_RECEIPT_MAX_BYTES
from workstation.config import AsrConfig


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class AsrIntegrityError(ValueError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def file_artifact(path: Path, *, include_hash: bool = True) -> dict:
    if path.is_symlink():
        raise AsrIntegrityError("approved ASR artifact cannot be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise AsrIntegrityError("approved ASR artifact is not a file")
    before = resolved.stat()
    if before.st_size <= 0:
        raise AsrIntegrityError("approved ASR artifact is empty")
    value = {
        "path": str(resolved),
        "bytes": before.st_size,
        "mtime_ns": before.st_mtime_ns,
    }
    if include_hash:
        value["sha256"] = file_sha256(resolved)
        after = resolved.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise AsrIntegrityError("approved ASR artifact changed while hashing")
    return value


def model_tree(path: Path, *, include_content_hash: bool) -> dict:
    if path.is_symlink():
        raise AsrIntegrityError("approved ASR model cannot be a symlink")
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise AsrIntegrityError("approved ASR model is not a directory")
    metadata = hashlib.sha256()
    content = hashlib.sha256()
    file_count = total_bytes = 0
    for candidate in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if candidate.is_symlink():
            raise AsrIntegrityError("approved ASR model tree cannot contain symlinks")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise AsrIntegrityError("approved ASR model tree has an unsupported entry")
        relative = candidate.relative_to(root).as_posix().encode("utf-8")
        before = candidate.stat()
        record = relative + b"\0" + str(before.st_size).encode() + b"\0" + str(before.st_mtime_ns).encode() + b"\n"
        metadata.update(record)
        content.update(relative + b"\0" + str(before.st_size).encode() + b"\0")
        if include_content_hash:
            with candidate.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    content.update(chunk)
            after = candidate.stat()
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise AsrIntegrityError("approved ASR model changed while hashing")
        file_count += 1
        total_bytes += before.st_size
    if file_count <= 0 or total_bytes <= 0:
        raise AsrIntegrityError("approved ASR model tree is empty")
    value = {
        "path": str(root),
        "files": file_count,
        "bytes": total_bytes,
        "metadata_sha256": metadata.hexdigest(),
    }
    if include_content_hash:
        value["sha256"] = content.hexdigest()
    return value


def _same_artifact(expected: dict, actual: dict, *, full: bool) -> bool:
    keys = {"path", "bytes", "mtime_ns", "sha256"}
    return (
        isinstance(expected, dict)
        and set(expected) == keys
        and _SHA256_RE.fullmatch(str(expected.get("sha256") or "")) is not None
        and all(expected.get(key) == actual.get(key) for key in keys - {"sha256"})
        and (not full or expected["sha256"] == actual.get("sha256"))
    )


def verify_approval(config: AsrConfig, *, full: bool) -> dict:
    path = config.approval_receipt
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size <= 0
            or path.stat().st_size > WORKSTATION_ASR_APPROVAL_RECEIPT_MAX_BYTES
        ):
            raise AsrIntegrityError("ASR approval receipt size is invalid")
        receipt = json.loads(path.read_bytes())
        if not isinstance(receipt, dict) or set(receipt) != {
            "schema_version", "device", "compute_type", "model",
            "runtime_python", "runtime_provenance", "benchmark_report",
        } or receipt.get("schema_version") != 1:
            raise AsrIntegrityError("ASR approval receipt schema is invalid")
        if receipt.get("device") != config.device or receipt.get("compute_type") != config.compute_type:
            raise AsrIntegrityError("ASR approval profile changed")
        expected_model = receipt.get("model")
        actual_model = model_tree(Path(config.model), include_content_hash=full)
        if (
            not isinstance(expected_model, dict)
            or set(expected_model) != {"path", "files", "bytes", "metadata_sha256", "sha256"}
            or actual_model["path"] != expected_model.get("path")
            or actual_model["files"] != expected_model.get("files")
            or actual_model["bytes"] != expected_model.get("bytes")
            or actual_model["metadata_sha256"] != expected_model.get("metadata_sha256")
            or not _SHA256_RE.fullmatch(str(expected_model.get("sha256") or ""))
            or (full and actual_model.get("sha256") != expected_model.get("sha256"))
        ):
            raise AsrIntegrityError("ASR model digest changed")
        for name, configured in (
            ("runtime_python", config.runtime_python),
            ("runtime_provenance", config.runtime_provenance),
            ("benchmark_report", config.benchmark_report),
        ):
            actual = file_artifact(configured, include_hash=full)
            if not _same_artifact(receipt.get(name), actual, full=full):
                raise AsrIntegrityError(f"ASR {name} changed")
        return receipt
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, AsrIntegrityError):
            raise
        raise AsrIntegrityError("ASR approval receipt is invalid") from exc
