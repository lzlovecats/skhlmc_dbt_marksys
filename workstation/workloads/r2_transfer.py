"""Bounded HTTPS transfers using only server-issued short-lived R2 URLs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse

import httpx

from system_limits import WORKSTATION_TRANSFER_CHUNK_BYTES
from workstation.workloads.errors import WorkloadError


def _https_url(value: object) -> str:
    raw = str(value or "")
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise WorkloadError("invalid_transfer_url", "Server issued an invalid media transfer URL.")
    return raw


def download_to_path(
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    expected_bytes: int = 0,
    expected_sha256: str = "",
    timeout_seconds: int = 60,
) -> dict:
    safe_url = _https_url(url)
    limit = int(max_bytes)
    if limit <= 0:
        raise ValueError("max_bytes must be positive")
    digest = hashlib.sha256()
    total = 0
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=10), follow_redirects=False) as client:
            with client.stream("GET", safe_url, headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    raise WorkloadError("download_failed", "Unable to download the private media object.", retryable=True)
                declared = int(response.headers.get("content-length") or 0)
                if declared and declared > limit:
                    raise WorkloadError("media_too_large", "Private media exceeds the Workstation limit.")
                with destination.open("wb") as stream:
                    for chunk in response.iter_bytes(chunk_size=WORKSTATION_TRANSFER_CHUNK_BYTES):
                        total += len(chunk)
                        if total > limit:
                            raise WorkloadError("media_too_large", "Private media exceeds the Workstation limit.")
                        digest.update(chunk)
                        stream.write(chunk)
    except WorkloadError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, httpx.HTTPError) as exc:
        destination.unlink(missing_ok=True)
        raise WorkloadError("download_failed", "Unable to download the private media object.", retryable=True) from exc
    actual_sha = digest.hexdigest()
    if expected_bytes and total != int(expected_bytes):
        destination.unlink(missing_ok=True)
        raise WorkloadError("size_mismatch", "Private media size verification failed.")
    if expected_sha256 and actual_sha != str(expected_sha256).lower():
        destination.unlink(missing_ok=True)
        raise WorkloadError("hash_mismatch", "Private media hash verification failed.")
    return {"byte_size": total, "sha256": actual_sha}


def upload_path(
    url: str,
    source: Path,
    *,
    headers: dict,
    expected_sha256: str,
    max_bytes: int,
    timeout_seconds: int = 60,
) -> dict:
    safe_url = _https_url(url)
    size = source.stat().st_size
    if size <= 0 or size > int(max_bytes):
        raise WorkloadError("media_too_large", "Generated media is empty or exceeds the Workstation limit.")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != str(expected_sha256).lower():
        raise WorkloadError("hash_mismatch", "Generated media changed before upload.")
    allowed_headers = {}
    for key, value in (headers or {}).items():
        lower = str(key).lower()
        if lower in {"content-type", "content-length", "cache-control", "x-amz-meta-sha256"}:
            allowed_headers[str(key)] = str(value)
    try:
        with source.open("rb") as stream, httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=10), follow_redirects=False) as client:
            response = client.put(safe_url, headers=allowed_headers, content=stream)
        if response.status_code not in {200, 201, 204}:
            raise WorkloadError("upload_failed", "Unable to upload generated media.", retryable=True)
    except WorkloadError:
        raise
    except (OSError, httpx.HTTPError) as exc:
        raise WorkloadError("upload_failed", "Unable to upload generated media.", retryable=True) from exc
    return {"byte_size": size, "sha256": digest}
