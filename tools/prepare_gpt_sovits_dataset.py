#!/usr/bin/env python3
"""Prepare a current R2 recordings manifest or legacy TTS ZIP for GPT-SoVITS.

The current app exports ``recordings.json`` with metadata and short-lived,
signed R2 download URLs.  Older Streamlit exports contained:
    metadata.csv
    audio/<speaker>_<id>.wav

GPT-SoVITS expects a text labelling file:
    /absolute/path/to/audio.wav|speaker|yue|text
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_model_config import LOCAL_TTS_TRAINING_ENGINE
from system_limits import (
    DATASET_ARCHIVE_MAX_BYTES,
    DATASET_ARCHIVE_MAX_ITEMS,
    DATASET_MANIFEST_MAX_BYTES,
    DATASET_MEDIA_PROCESS_TIMEOUT_SECONDS,
    MAX_AUDIO_BYTES,
)


DEFAULT_LANGUAGE = "yue"
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
DOWNLOAD_ATTEMPTS = 3
RECOMMENDATION_RULE_VERSION = "gpt-sovits-v2pro-20250606-r1"
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
UPSTREAM_PROFILE = {
    "name": "GPT-SoVITS 20250606v2pro",
    "model_family": "v2Pro",
    "git_ref": "20250606v2pro",
    "git_commit": "d7c2210da8c013e81a94bfc7b811a477c99fd506",
    # The helper records a reviewed release profile, but the operator must
    # still verify the checkout and every weight licence before training.
    "repository": "https://github.com/RVC-Boss/GPT-SoVITS",
    "pretrained_gpt": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
    "pretrained_sovits_g": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
    "pretrained_sovits_d": "GPT_SoVITS/pretrained_models/v2Pro/s2Dv2Pro.pth",
    "sovits_epochs": 8,
    "sovits_save_every": 4,
    "sovits_lr_weight": 0.4,
    "gpt_epochs": 15,
    "gpt_save_every": 5,
}


class _DownloadValidationError(ValueError):
    """Safe-to-display validation failure that never contains a signed URL."""


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write a private file atomically so partial output never looks complete."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(path.name + ".part")
    with partial.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(partial, mode)
    os.replace(partial, path)


def _atomic_write_text(path: Path, value: str, *, mode: int = 0o600) -> None:
    _atomic_write_bytes(path, value.encode("utf-8"), mode=mode)


def _write_json(path: Path, value: object) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def _public_error(value: object) -> str:
    message = URL_RE.sub("[URL hidden]", str(value or "Preparation failed"))
    return message[:800]


def _safe_r2_error_code(error: urllib.error.HTTPError) -> str:
    """Return only a bounded S3/R2 XML error code, never the response message."""
    try:
        root = ET.fromstring(error.read(16 * 1024))
    except Exception:
        return ""
    code = ""
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "Code":
            code = str(element.text or "").strip()
            break
    return code if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", code) else ""


def _download_denied_message(recording_id: str, error: urllib.error.HTTPError) -> str:
    code = _safe_r2_error_code(error)
    detail = f"HTTP {error.code}" + (f", R2 {code}" if code else ", no R2 error code")
    if code in {"ExpiredRequest", "ExpiredToken", "RequestExpired"}:
        action = "export a fresh manifest and use it within one hour"
    elif code in {
        "AuthorizationQueryParametersError",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
    }:
        action = "check the production R2 signing credentials and endpoint"
    elif code == "RequestTimeTooSkewed":
        action = "check the production signer clock"
    elif code == "AccessDenied":
        action = "check the production R2 token and object read permission"
    elif not code:
        action = "check whether a network proxy or security gateway blocked the R2 request"
    else:
        action = "check the production R2 configuration"
    return f"download denied for recording {recording_id} ({detail}; {action})"


class _ProgressReporter:
    """Best-effort machine-readable progress for the localhost wrapper."""

    def __init__(self, path: str | None):
        self.path = Path(path).expanduser().resolve() if path else None

    def emit(
        self,
        stage: str,
        message: str,
        *,
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        if not self.path:
            return
        payload = {
            "stage": stage,
            "message": message,
            "current": current,
            "total": total,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _write_json(self.path, payload)
        except OSError:
            # Progress must never turn a valid preparation run into a failure.
            pass


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    value = value.strip("._-")
    return value or "tts_voice"


def _clean_text(value: str) -> str:
    return " ".join(str(value or "").replace("|", "，").split())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _positive_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _download_https_audio(
    url: str,
    target: Path,
    *,
    recording_id: str,
    expected_sha256: str,
    expected_size: int = 0,
    max_bytes: int = MAX_AUDIO_BYTES,
) -> int:
    """Download one signed R2 object without ever logging its signed URL."""
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"recording {recording_id} has an invalid HTTPS download URL")
    digest_text = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest_text):
        raise ValueError(f"recording {recording_id} has no valid SHA-256 metadata")
    limit = max(1, min(int(max_bytes), MAX_AUDIO_BYTES))
    declared_size = _positive_int(expected_size)
    if declared_size > limit:
        raise ValueError(f"recording {recording_id} exceeds the per-file byte limit")

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    partial = target.with_name(target.name + ".part")
    partial.unlink(missing_ok=True)
    digest = hashlib.sha256()
    total = 0
    last_error_name = "network error"
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        partial.unlink(missing_ok=True)
        digest = hashlib.sha256()
        total = 0
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": "audio/*", "User-Agent": "skhlmc-dataset-preparer/1"},
            )
            with urllib.request.urlopen(
                request, timeout=DATASET_MEDIA_PROCESS_TIMEOUT_SECONDS
            ) as response:
                final_url = urlsplit(str(response.geturl() or ""))
                if (
                    final_url.scheme != "https"
                    or not final_url.hostname
                    or final_url.username
                    or final_url.password
                ):
                    raise _DownloadValidationError(
                        f"recording {recording_id} redirected outside HTTPS"
                    )
                content_length = _positive_int(response.headers.get("Content-Length"))
                if content_length > limit:
                    raise _DownloadValidationError(
                        f"recording {recording_id} exceeds the per-file byte limit"
                    )
                if declared_size and content_length and content_length != declared_size:
                    raise _DownloadValidationError(
                        f"recording {recording_id} size metadata does not match R2"
                    )
                with partial.open("wb") as output:
                    while block := response.read(DOWNLOAD_CHUNK_BYTES):
                        total += len(block)
                        if total > limit:
                            raise _DownloadValidationError(
                                f"recording {recording_id} exceeds the per-file byte limit"
                            )
                        digest.update(block)
                        output.write(block)
            break
        except _DownloadValidationError:
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:
            partial.unlink(missing_ok=True)
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
                raise RuntimeError(_download_denied_message(recording_id, exc)) from None
            else:
                last_error_name = type(exc).__name__
            if attempt < DOWNLOAD_ATTEMPTS:
                time.sleep(min(attempt, 2))
    else:
        # urllib exceptions can contain the complete query string. Keep the
        # signed token out of terminal logs and shell history diagnostics.
        raise RuntimeError(
            f"download failed for recording {recording_id} ({last_error_name})"
        ) from None
    if declared_size and total != declared_size:
        partial.unlink(missing_ok=True)
        raise ValueError(f"recording {recording_id} size metadata does not match download")
    if digest.hexdigest() != digest_text:
        partial.unlink(missing_ok=True)
        raise ValueError(f"recording {recording_id} SHA-256 verification failed")
    os.replace(partial, target)
    return total


def _manifest_audio_name(row: dict[str, object]) -> str:
    recording_id = _slug(str(row.get("id") or "recording"))
    extension = re.sub(
        r"[^a-z0-9]", "", str(row.get("file_ext") or "").lower()
    )
    if not extension:
        extension = {
            "audio/webm": "webm",
            "audio/mp4": "m4a",
            "audio/mpeg": "mp3",
            "audio/wav": "wav",
            "audio/ogg": "ogg",
        }.get(str(row.get("mime_type") or "").lower(), "webm")
    return f"{recording_id}.{extension[:10]}"


def _materialize_recordings_manifest(
    manifest_path: Path,
    output_dir: Path,
    *,
    speaker: str | None,
    overwrite: bool,
    progress: _ProgressReporter | None = None,
) -> Path:
    """Download current R2 manifest items into the legacy-neutral workspace."""
    manifest_size = manifest_path.stat().st_size
    if manifest_size > DATASET_MANIFEST_MAX_BYTES:
        raise ValueError(
            f"recordings manifest is {manifest_size} bytes; limit is {DATASET_MANIFEST_MAX_BYTES}"
        )
    try:
        payload = json.loads(manifest_path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("recordings manifest is not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("recordings manifest must contain an items array")
    items = payload["items"]
    if not items:
        raise ValueError("recordings manifest contains no items")
    if len(items) > DATASET_ARCHIVE_MAX_ITEMS:
        raise ValueError(
            f"recordings manifest contains {len(items)} items; limit is {DATASET_ARCHIVE_MAX_ITEMS}"
        )
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("recordings manifest contains an invalid item")
    selected_speaker = _choose_speaker(items, speaker)
    selected = [
        item
        for item in items
        if str(item.get("speaker_user_id") or "").strip() == selected_speaker
    ]
    if not selected:
        raise ValueError(f"no recordings found for speaker {selected_speaker!r}")
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for item in selected:
        recording_id = str(item.get("id") or "").strip()
        digest_text = str(item.get("audio_sha256") or "").strip().lower()
        if not recording_id:
            raise ValueError("recordings manifest item has no id")
        if recording_id in seen_ids:
            raise ValueError(f"recordings manifest has duplicate id {recording_id}")
        seen_ids.add(recording_id)
        if not re.fullmatch(r"[0-9a-f]{64}", digest_text):
            raise ValueError(f"recording {recording_id} has no valid SHA-256 metadata")
        if digest_text in seen_hashes:
            raise ValueError(
                f"recordings manifest has duplicate audio SHA-256 at recording {recording_id}"
            )
        seen_hashes.add(digest_text)
        if not _clean_text(item.get("prompt_text") or ""):
            raise ValueError(f"recording {recording_id} has empty prompt_text")
        if not str(item.get("manuscript_id") or item.get("script_id") or "").strip():
            raise ValueError(f"recording {recording_id} has no split group")
        parsed_url = urlsplit(str(item.get("download_url") or ""))
        if (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
        ):
            raise ValueError(f"recording {recording_id} has an invalid HTTPS download URL")
    declared_total = sum(_positive_int(item.get("size_bytes")) for item in selected)
    if declared_total > DATASET_ARCHIVE_MAX_BYTES:
        raise ValueError(
            f"selected recordings declare {declared_total} bytes; limit is {DATASET_ARCHIVE_MAX_BYTES}"
        )

    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    audio_dir = output_dir / "raw" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(audio_dir.parent, 0o700)
    os.chmod(audio_dir, 0o700)
    rows: list[dict[str, object]] = []
    names: set[str] = set()
    total_bytes = 0
    if progress:
        progress.emit(
            "download",
            "Downloading and verifying accepted recordings",
            current=0,
            total=len(selected),
        )
    for position, item in enumerate(selected, start=1):
        if total_bytes >= DATASET_ARCHIVE_MAX_BYTES:
            raise ValueError("selected recordings reached the total download byte limit")
        recording_id = str(item.get("id") or "").strip()
        if not recording_id:
            raise ValueError("recordings manifest item has no id")
        name = _manifest_audio_name(item)
        if name in names:
            raise ValueError(f"recordings manifest has duplicate id {recording_id}")
        names.add(name)
        target = audio_dir / name
        expected_sha = str(item.get("audio_sha256") or "").strip().lower()
        expected_size = _positive_int(item.get("size_bytes"))
        if expected_size > MAX_AUDIO_BYTES:
            raise ValueError(f"recording {recording_id} exceeds the per-file byte limit")
        if target.exists():
            existing_size = target.stat().st_size
            if existing_size > MAX_AUDIO_BYTES:
                raise ValueError(f"recording {recording_id} exceeds the per-file byte limit")
            if _sha256(target) == expected_sha and (
                not expected_size or existing_size == expected_size
            ):
                downloaded = existing_size
            elif not overwrite:
                raise ValueError(
                    f"existing audio for recording {recording_id} failed verification; use --overwrite"
                )
            else:
                downloaded = _download_https_audio(
                    str(item.get("download_url") or ""),
                    target,
                    recording_id=recording_id,
                    expected_sha256=expected_sha,
                    expected_size=expected_size,
                    max_bytes=min(MAX_AUDIO_BYTES, DATASET_ARCHIVE_MAX_BYTES - total_bytes),
                )
        else:
            downloaded = _download_https_audio(
                str(item.get("download_url") or ""),
                target,
                recording_id=recording_id,
                expected_sha256=expected_sha,
                expected_size=expected_size,
                max_bytes=min(MAX_AUDIO_BYTES, DATASET_ARCHIVE_MAX_BYTES - total_bytes),
            )
        total_bytes += downloaded
        if total_bytes > DATASET_ARCHIVE_MAX_BYTES:
            raise ValueError("selected recordings exceeded the total download byte limit")
        rows.append(
            {
                "id": recording_id,
                "speaker_user_id": selected_speaker,
                "script_id": item.get("script_id") or "",
                "manuscript_id": item.get("manuscript_id") or "",
                "manuscript_title": item.get("manuscript_title") or "",
                "category": item.get("category") or "",
                "prompt_text": item.get("prompt_text") or "",
                "ai_transcript": item.get("ai_transcript") or "",
                "audio_file": f"../raw/audio/{name}",
                "audio_sha256": expected_sha,
                "size_bytes": downloaded,
                "duration_seconds": item.get("duration_seconds") or "",
                "sample_rate_hz": item.get("sample_rate_hz") or "",
                "channel_count": item.get("channel_count") or "",
                "detected_format": item.get("detected_format") or "",
                "mime_type": item.get("mime_type") or "",
                "manifest_source": "recordings_json",
            }
        )
        if progress:
            progress.emit(
                "download",
                f"Downloaded and verified recording {position} of {len(selected)}",
                current=position,
                total=len(selected),
            )

    fieldnames = list(rows[0]) if rows else [
        "speaker_user_id", "prompt_text", "audio_file"
    ]
    metadata_path = output_dir / "metadata" / "source.csv"
    metadata_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata_partial = metadata_path.with_name(metadata_path.name + ".part")
    with metadata_partial.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.chmod(metadata_partial, 0o600)
    os.replace(metadata_partial, metadata_path)

    locked = {key: value for key, value in payload.items() if key != "items"}
    locked["items"] = [
        {key: value for key, value in item.items() if key != "download_url"}
        for item in selected
    ]
    locked["locked_at"] = datetime.now(timezone.utc).isoformat()
    lock_bytes = (
        json.dumps(locked, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    provenance_dir = output_dir / "provenance"
    lock_path = provenance_dir / "manifest.lock.json"
    _atomic_write_bytes(lock_path, lock_bytes)
    lock_sha = hashlib.sha256(lock_bytes).hexdigest()
    _atomic_write_text(
        provenance_dir / "manifest.lock.sha256",
        f"{lock_sha}  manifest.lock.json\n",
    )
    for raw_file in audio_dir.iterdir():
        if raw_file.is_file():
            os.chmod(raw_file, 0o400)
    return metadata_path


def _split_for(group_key: str) -> str:
    bucket = int(hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:8], 16) % 10
    return "test" if bucket == 0 else "validation" if bucket == 1 else "train"


def _probe_audio_file(path: Path) -> dict[str, object]:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required for audio metadata verification")
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration:stream=codec_type,codec_name,sample_rate,channels,sample_fmt",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=DATASET_MEDIA_PROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffprobe timed out for {path.name}") from exc
    except OSError as exc:
        raise RuntimeError(f"ffprobe could not inspect {path.name}") from exc
    if probe.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {probe.stderr.strip()[:300]}"
        )
    try:
        data = json.loads(probe.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path.name}") from exc
    streams = [
        stream
        for stream in (data.get("streams") or [])
        if stream.get("codec_type") in (None, "audio")
    ]
    stream = streams[0] if streams else {}
    return {
        "duration_seconds": round(
            float((data.get("format") or {}).get("duration") or 0), 3
        ),
        "sample_rate_hz": _positive_int(stream.get("sample_rate")),
        "channels": _positive_int(stream.get("channels")),
        "sample_format": stream.get("sample_fmt") or "",
        "codec": stream.get("codec_name") or "",
        "format": (data.get("format") or {}).get("format_name") or "",
    }


def _verify_manifest_audio_metadata(path: Path, row: dict[str, str]) -> None:
    """Verify downloaded bytes still match the server-authoritative manifest."""
    recording_id = str(row.get("id") or path.name)
    expected_size = _positive_int(row.get("size_bytes"))
    if expected_size and path.stat().st_size != expected_size:
        raise ValueError(f"recording {recording_id} size metadata verification failed")
    expected_sha = str(row.get("audio_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or _sha256(path) != expected_sha:
        raise ValueError(f"recording {recording_id} SHA-256 verification failed")

    actual = _probe_audio_file(path)
    try:
        expected_duration = float(row.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        expected_duration = 0
    if expected_duration and abs(float(actual["duration_seconds"]) - expected_duration) > max(
        2.0, expected_duration * 0.1
    ):
        raise ValueError(f"recording {recording_id} duration metadata verification failed")
    for field, actual_field in (
        ("sample_rate_hz", "sample_rate_hz"),
        ("channel_count", "channels"),
    ):
        expected = _positive_int(row.get(field))
        if expected and expected != int(actual[actual_field]):
            raise ValueError(f"recording {recording_id} {field} verification failed")
    expected_formats = {
        value.strip().lower()
        for value in str(row.get("detected_format") or "").split(",")
        if value.strip()
    }
    actual_formats = {
        value.strip().lower()
        for value in str(actual.get("format") or "").split(",")
        if value.strip()
    }
    if expected_formats and actual_formats and expected_formats.isdisjoint(actual_formats):
        raise ValueError(f"recording {recording_id} format metadata verification failed")


def _normalize_audio(source: Path, target: Path) -> dict[str, object]:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg and ffprobe are required for deterministic audio conversion")
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:a:0", "-ac", "1", "-ar", "32000",
        "-c:a", "pcm_s16le", str(target),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=DATASET_MEDIA_PROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg timed out for {source.name}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {source.name}: {result.stderr.strip()[:300]}")
    quality = _probe_audio_file(target)
    quality.pop("format", None)
    if (
        float(quality.get("duration_seconds") or 0) <= 0
        or quality.get("sample_rate_hz") != 32000
        or quality.get("channels") != 1
        or quality.get("codec") != "pcm_s16le"
        or quality.get("sample_format") != "s16"
    ):
        target.unlink(missing_ok=True)
        raise RuntimeError(f"normalized WAV failed PCM16/32 kHz/mono checks for {source.name}")
    quality["sha256"] = _sha256(target)
    os.chmod(target, 0o600)
    return quality


def _run_quiet(cmd: list[str], *, timeout: int = 5) -> str:
    try:
        result = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _memory_gb() -> float | None:
    if platform.system() == "Darwin":
        raw = _run_quiet(["sysctl", "-n", "hw.memsize"])
        if raw.isdigit():
            return int(raw) / (1024**3)
    if platform.system() == "Linux":
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1]) / (1024**2)
    if platform.system() == "Windows":
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.length = ctypes.sizeof(_MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.total_physical / (1024**3)
        except (AttributeError, OSError):
            pass
    try:
        return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024**3)
    except (AttributeError, OSError, ValueError):
        pass
    return None


def _cpu_model() -> str:
    if platform.system() == "Darwin":
        detected = _run_quiet(["sysctl", "-n", "machdep.cpu.brand_string"])
        if detected:
            return detected
        if platform.machine() == "arm64":
            return "Apple Silicon"
        return platform.processor()
    if platform.system() == "Linux":
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.exists():
            for line in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.lower().startswith("model name") and ":" in line:
                    return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


def _tool_version(executable: str) -> str | None:
    path = shutil.which(executable)
    if not path:
        return None
    version_flag = "--version" if executable == "nvidia-smi" else "-version"
    first_line = _run_quiet([path, version_flag]).splitlines()
    return first_line[0].strip() if first_line else path


def _number(value: object) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(match.group()) if match else None


def _nvidia_gpus() -> list[dict[str, object]]:
    if not shutil.which("nvidia-smi"):
        return []
    fields = "index,uuid,name,memory.total,memory.free,driver_version,compute_cap"
    raw = _run_quiet(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"]
    )
    if not raw:
        fields = "index,uuid,name,memory.total,memory.free,driver_version"
        raw = _run_quiet(
            ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"]
        )
    names = fields.split(",")
    gpus: list[dict[str, object]] = []
    for values in csv.reader(raw.splitlines(), skipinitialspace=True):
        if len(values) != len(names):
            continue
        row = dict(zip(names, (value.strip() for value in values)))
        total_mib = _number(row.get("memory.total"))
        free_mib = _number(row.get("memory.free"))
        index_value = _positive_int(row.get("index"))
        gpus.append(
            {
                "index": index_value,
                "uuid": row.get("uuid") or None,
                "name": row.get("name") or "NVIDIA GPU",
                "memory_total_gb": round(total_mib / 1024, 3) if total_mib else None,
                "memory_free_gb": round(free_mib / 1024, 3) if free_mib else None,
                "driver_version": row.get("driver_version") or None,
                "compute_capability": row.get("compute_cap") or None,
                "backend": "cuda",
                "probe": "nvidia-smi",
                "verified_by_pytorch": False,
            }
        )
    return gpus


def _pytorch_gpus() -> tuple[list[dict[str, object]], dict[str, object]]:
    """Probe the active environment without importing a large ML stack here."""
    code = """
import json
try:
    import torch
    out = {"available": bool(torch.cuda.is_available()), "torch": torch.__version__,
           "cuda_runtime": torch.version.cuda, "hip_runtime": torch.version.hip, "gpus": []}
    if out["available"]:
        for i in range(torch.cuda.device_count()):
            p = torch.cuda.get_device_properties(i)
            try:
                free, total = torch.cuda.mem_get_info(i)
            except Exception:
                free, total = 0, p.total_memory
            out["gpus"].append({"index": i, "name": p.name,
                "memory_total_gb": round(total / 1024**3, 3),
                "memory_free_gb": round(free / 1024**3, 3),
                "compute_capability": f"{p.major}.{p.minor}",
                "backend": "rocm" if torch.version.hip else "cuda",
                "probe": "pytorch", "verified_by_pytorch": True})
    print(json.dumps(out))
except Exception as exc:
    print(json.dumps({"available": False, "error_type": type(exc).__name__, "gpus": []}))
"""
    raw = _run_quiet([sys.executable, "-c", code], timeout=15)
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {}
    gpus = payload.pop("gpus", [])
    return (gpus if isinstance(gpus, list) else []), payload


def _hardware_info(output_path: Path | None = None) -> dict[str, object]:
    torch_gpus, torch_info = _pytorch_gpus()
    fallback_gpus = _nvidia_gpus()
    gpus = torch_gpus or fallback_gpus
    disk_target = output_path or Path.home()
    try:
        usage = shutil.disk_usage(disk_target)
        disk_free_gb = usage.free / (1024**3)
    except OSError:
        disk_free_gb = None
    info = {
        "system": platform.system(),
        "system_release": platform.release(),
        "machine": platform.machine(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count() or 1,
        "memory_gb": _memory_gb(),
        "disk_free_gb": round(disk_free_gb, 3) if disk_free_gb is not None else None,
        "python_version": platform.python_version(),
        "ffmpeg": _tool_version("ffmpeg"),
        "ffprobe": _tool_version("ffprobe"),
        "nvidia_smi": _tool_version("nvidia-smi"),
        "gpus": gpus,
        "nvidia_smi_gpus": fallback_gpus,
        "pytorch": torch_info,
        "accelerator_probe": "pytorch" if torch_gpus else "nvidia-smi" if fallback_gpus else "none",
    }
    # Keep this compatibility field for callers of the original CLI API.
    info["nvidia_vram_gb"] = [
        gpu["memory_total_gb"]
        for gpu in gpus
        if gpu.get("backend") == "cuda" and gpu.get("memory_total_gb") is not None
    ]
    return info


def _recommended_params(info: dict[str, object], total_minutes: float) -> dict[str, object]:
    gpus = [gpu for gpu in (info.get("gpus") or []) if isinstance(gpu, dict)]
    cuda_gpus = [gpu for gpu in gpus if gpu.get("backend") == "cuda"]
    selected = max(
        cuda_gpus,
        key=lambda gpu: float(gpu.get("memory_total_gb") or 0),
        default=None,
    )
    memory_gb = info.get("memory_gb")
    warnings: list[str] = []
    fallback_batches: list[int] = []
    precision = "32"
    training_recommended = False
    hardware_suitable = False
    gpu_info = ""
    validated_platform = info.get("system") == "Linux" and info.get("machine") in (
        "x86_64",
        "AMD64",
    )
    if not validated_platform:
        warnings.append(
            "This OS/architecture is outside the project-validated Ubuntu x86-64 training baseline."
        )
    if selected:
        max_vram = float(selected.get("memory_total_gb") or 0)
        if max_vram >= 16:
            batch = 4
        elif max_vram >= 10:
            batch = 2
        else:
            batch = 1
        if isinstance(memory_gb, (int, float)):
            if memory_gb < 16:
                batch = 1
                warnings.append("System RAM is below 16 GB; training is high risk.")
            elif memory_gb < 32:
                batch = min(batch, 2)
                warnings.append("System RAM is below the 32 GB project baseline.")
        compute = _number(selected.get("compute_capability"))
        gpu_name = str(selected.get("name") or "")
        fp16_ok = bool(compute and compute > 6.1 and "GTX 16" not in gpu_name.upper())
        precision = "16-mixed" if fp16_ok else "32"
        if not selected.get("verified_by_pytorch"):
            warnings.append(
                "GPU was detected by nvidia-smi but is not verified by PyTorch in this environment."
            )
        if max_vram < 10:
            warnings.append("GPU VRAM is below the 12 GB project baseline; expect OOM risk.")
        free_vram = selected.get("memory_free_gb")
        if isinstance(free_vram, (int, float)) and free_vram < min(10, max_vram * 0.75):
            warnings.append(
                "Available GPU memory is substantially below total VRAM; close other GPU workloads "
                "and probe again before training."
            )
        hardware_suitable = max_vram >= 10
        training_recommended = hardware_suitable and bool(
            selected.get("verified_by_pytorch")
        ) and validated_platform
        gpu_info = str(selected.get("index", 0))
        device_note = (
            f"Use GPU {gpu_info}: {selected.get('name')} ({max_vram:.1f} GB VRAM), "
            f"starting at batch {batch}."
        )
        fallback_batches = sorted({batch, 2, 1}, reverse=True)
        fallback_batches = [value for value in fallback_batches if value <= batch]
    else:
        batch = 1
        if info.get("system") == "Darwin" and info.get("machine") == "arm64":
            device_note = (
                "Apple Silicon detected. Dataset preparation is supported, but this project "
                "does not recommend local Mac GPU training; use an NVIDIA workstation."
            )
        else:
            device_note = (
                "No CUDA GPU detected. Dataset preparation is supported; CPU training is "
                "extremely slow and only suitable for a smoke check."
            )
        warnings.append("No CUDA GPU is available for the validated training path.")
        if any(gpu.get("backend") == "rocm" for gpu in gpus):
            warnings.append("ROCm was detected but remains experimental for this project.")
        fallback_batches = [1]
    disk_free = info.get("disk_free_gb")
    if isinstance(disk_free, (int, float)) and disk_free < 200:
        warnings.append("Free disk space is below the 200 GB workstation baseline.")
    readiness = "SMOKE_TEST_ONLY" if total_minutes < 30 else "READY_FOR_BASELINE"

    return {
        "rule_version": RECOMMENDATION_RULE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile_kind": "project_safe_initial",
        "upstream_profile": dict(UPSTREAM_PROFILE),
        "validated_platform": validated_platform,
        "hardware_suitable": hardware_suitable,
        "training_recommended": training_recommended,
        "dataset_readiness": readiness,
        "gpu_info": gpu_info,
        "selected_gpu": selected,
        "precision": precision,
        "device_note": device_note,
        "warnings": warnings,
        "oom_fallback_batches": fallback_batches,
        "sovits_batch": batch,
        "sovits_epochs": UPSTREAM_PROFILE["sovits_epochs"],
        "sovits_save_every": UPSTREAM_PROFILE["sovits_save_every"],
        "sovits_lr_weight": UPSTREAM_PROFILE["sovits_lr_weight"],
        "gpt_batch": batch,
        "gpt_epochs": UPSTREAM_PROFILE["gpt_epochs"],
        "gpt_save_every": UPSTREAM_PROFILE["gpt_save_every"],
        "enable_dpo": False,
        "save_latest": True,
        "save_every_weights": True,
    }


def _default_output_dir(zip_path: Path) -> Path:
    root = Path.home() / "private-ai-training"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return root / f"tts-{stamp}-{_slug(zip_path.stem)}"


def _extract_zip(zip_path: Path, output_dir: Path, overwrite: bool) -> None:
    existing = (
        [path for path in output_dir.iterdir() if path.name != ".progress.json"]
        if output_dir.exists()
        else []
    )
    if existing and not overwrite:
        raise ValueError(f"output workspace is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        if len(members) > DATASET_ARCHIVE_MAX_ITEMS:
            raise ValueError(
                f"Archive contains {len(members)} items; limit is {DATASET_ARCHIVE_MAX_ITEMS}"
            )
        declared_bytes = sum(max(0, int(info.file_size)) for info in members)
        if declared_bytes > DATASET_ARCHIVE_MAX_BYTES:
            raise ValueError(
                f"Archive expands to {declared_bytes} bytes; limit is {DATASET_ARCHIVE_MAX_BYTES}"
            )

        root = output_dir.resolve()
        safe_members: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in members:
            relative = PurePosixPath(info.filename.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe archive path: {info.filename!r}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"Archive symlinks are not accepted: {info.filename!r}")
            clean_parts = tuple(part for part in relative.parts if part not in ("", "."))
            if not clean_parts:
                continue
            target = root.joinpath(*clean_parts).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"Unsafe archive path: {info.filename!r}") from exc
            safe_members.append((info, target))

        extracted_bytes = 0
        for info, target in safe_members:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(target, 0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with zf.open(info) as source, target.open("wb") as destination:
                while block := source.read(1024 * 1024):
                    extracted_bytes += len(block)
                    if extracted_bytes > DATASET_ARCHIVE_MAX_BYTES:
                        raise ValueError("Archive exceeded the uncompressed byte limit")
                    destination.write(block)
            os.chmod(target, 0o600)


def _find_metadata(output_dir: Path) -> Path:
    direct = output_dir / "metadata.csv"
    if direct.exists():
        return direct
    matches = list(output_dir.rglob("metadata.csv"))
    if not matches:
        raise FileNotFoundError(f"metadata.csv not found under {output_dir}")
    return matches[0]


def _read_rows(metadata_path: Path) -> list[dict[str, str]]:
    with metadata_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            rows.append(row)
            if len(rows) > DATASET_ARCHIVE_MAX_ITEMS:
                raise ValueError(
                    f"metadata.csv contains more than {DATASET_ARCHIVE_MAX_ITEMS} rows"
                )
    required = {"speaker_user_id", "prompt_text", "audio_file"}
    missing = required - set(rows[0].keys() if rows else [])
    if missing:
        raise ValueError(f"metadata.csv missing required columns: {', '.join(sorted(missing))}")
    return rows


def _choose_speaker(rows: list[dict[str, str]], speaker: str | None) -> str:
    speakers = sorted({(row.get("speaker_user_id") or "").strip() for row in rows if row.get("speaker_user_id")})
    if speaker:
        if speaker not in speakers:
            raise ValueError(f"speaker {speaker!r} not found. Available speakers: {', '.join(speakers)}")
        return speaker
    if len(speakers) == 1:
        return speakers[0]
    if not speakers:
        raise ValueError("dataset has no non-empty speaker_user_id")
    raise ValueError(
        "Multiple speakers found. Re-run with --speaker one of: "
        + ", ".join(speakers)
    )


def _default_experiment(rows: list[dict[str, str]], speaker: str, language: str) -> str:
    """Create a stable non-public model id without exposing the user id."""
    identity_parts = [speaker]
    identity_parts.extend(
        sorted(
            str(row.get("audio_sha256") or row.get("id") or row.get("audio_file") or "")
            for row in rows
            if (row.get("speaker_user_id") or "").strip() == speaker
        )
    )
    digest = hashlib.sha256("\n".join(identity_parts).encode("utf-8")).hexdigest()[:10]
    return _slug(f"voice_{digest}_{language}_v0")


def _ensure_private_workspace(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    for name in ("raw", "wav", "provenance", "metadata", "eval", "runs"):
        directory = output_dir / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)


def _readiness(total_minutes: float, split_lines: dict[str, list[str]]) -> dict[str, object]:
    warnings: list[str] = []
    if not split_lines["validation"] or not split_lines["test"]:
        status = "BLOCKED_SPLIT"
        warnings.append(
            "Validation or test is empty. Add recordings or re-split whole manuscript groups; "
            "never move test clips into train."
        )
    elif total_minutes < 30:
        status = "SMOKE_TEST_ONLY"
        warnings.append("Audio is below 30 minutes; do not treat this run as a baseline candidate.")
    elif total_minutes < 60:
        status = "READY_FOR_BASELINE"
    else:
        status = "READY_FOR_BASELINE_RESEARCH"
    return {
        "status": status,
        "warnings": warnings,
        "manual_gates_complete": False,
        "production_ready": False,
    }


def _write_webui_note(
    note_path: Path,
    experiment: str,
    list_path: Path,
    dataset_dir: Path,
    language: str,
    hardware: dict[str, object],
    params: dict[str, object],
) -> None:
    memory = hardware.get("memory_gb")
    memory_text = f"{memory:.1f} GB" if isinstance(memory, (int, float)) else "unknown"
    gpus = hardware.get("gpus") or []
    gpu_text = (
        ", ".join(
            f"{gpu.get('index')}: {gpu.get('name')} ({gpu.get('memory_total_gb')} GB)"
            for gpu in gpus
        )
        if gpus
        else "none detected"
    )
    gpu_field = params["gpu_info"] or "CPU (smoke check only; NVIDIA workstation recommended)"
    warning_lines = [f"- {warning}" for warning in params.get("warnings", [])]
    if params.get("dataset_readiness") == "BLOCKED_SPLIT":
        training_instruction = (
            "STOP: validation/test split is incomplete. Do not start training until whole "
            "manuscript groups have been added or manually re-split and recorded."
        )
    elif not params.get("hardware_suitable"):
        training_instruction = (
            "Prepare/review the dataset here, then move the private workspace to a validated "
            "NVIDIA workstation for training."
        )
    elif not params.get("training_recommended"):
        training_instruction = (
            "Activate the audited GPT-SoVITS PyTorch environment and verify CUDA before "
            "starting SoVITS followed by GPT training."
        )
    else:
        training_instruction = (
            "Press Open SoVITS Training first. Wait until it finishes, then press Open GPT Training."
        )
    _atomic_write_text(
        note_path,
        "\n".join(
            [
                f"{LOCAL_TTS_TRAINING_ENGINE} WebUI fields",
                "========================",
                "",
                "Open WebUI",
                "----------",
                f"cd ~/Documents/AI/{LOCAL_TTS_TRAINING_ENGINE}",
                "conda activate GPTSoVits",
                "python webui.py",
                "Browser URL: http://127.0.0.1:9874",
                "",
                "Detected hardware",
                "-----------------",
                f"System: {hardware.get('system')} {hardware.get('machine')}",
                f"CPU: {hardware.get('cpu_model')}",
                f"CPU cores: {hardware.get('cpu_count')}",
                f"Memory: {memory_text}",
                f"Free disk: {hardware.get('disk_free_gb')} GB",
                f"GPU: {gpu_text}",
                f"Accelerator probe: {hardware.get('accelerator_probe')}",
                f"Recommendation: {params['device_note']}",
                f"Training recommended on this machine: {params['training_recommended']}",
                f"Dataset readiness: {params['dataset_readiness']}",
                "Warnings:",
                *(warning_lines or ["- none"]),
                "",
                "0 / Fine-Tuned Model Information",
                f"Experiment/model name: {experiment}",
                f"GPU Information: {gpu_field}",
                f"Version of the trained model: {UPSTREAM_PROFILE['model_family']}",
                f"Audited upstream ref: {UPSTREAM_PROFILE['git_ref']}",
                f"Audited upstream commit: {UPSTREAM_PROFILE['git_commit']}",
                f"Precision: {params['precision']}",
                "",
                "1A / Dataset Formatting Tool",
                f"Text labelling file: {list_path}",
                "Audio dataset folder: leave blank",
                "",
                "Press these in order after filling the fields:",
                "1. Open Tokenization & BERT Feature Extraction",
                "2. Open Speech SSL Feature Extraction",
                "3. Open Semantics Token Extraction",
                "4. Open Training Set One-Click Formatting",
                "",
                "1B / Fine-Tuning",
                f"SoVITS batch size: {params['sovits_batch']}",
                f"SoVITS total epochs: {params['sovits_epochs']}",
                f"SoVITS text model learning rate weighting: {params['sovits_lr_weight']}",
                f"SoVITS save frequency: {params['sovits_save_every']}",
                f"SoVITS GPU number: {gpu_field}",
                "SoVITS checkboxes: keep both checked",
                "",
                f"GPT batch size: {params['gpt_batch']}",
                f"GPT total epochs: {params['gpt_epochs']}",
                f"GPT save frequency: {params['gpt_save_every']}",
                f"GPT GPU number: {gpu_field}",
                "GPT DPO training: unchecked",
                "GPT checkboxes: keep both checked",
                f"OOM fallback batch ladder: {params['oom_fallback_batches']}",
                "",
                training_instruction,
                "",
                "1C / Inference",
                f"Reference audio: choose a clean wav under {dataset_dir / 'wav'}",
                f"Reference language: {language}",
                f"Target language: {language}",
                "",
                "Manual gates still required",
                "---------------------------",
                "- Listen to the beginning/end, shortest/longest clips and important terms.",
                "- Verify the checked-out commit and all code/weight/vocoder licences.",
                "- Keep validation/test out of training and compare fixed evaluation sentences.",
                "- Complete withdrawal and artifact-deletion rehearsal before deployment.",
                "- This helper never marks a model production-ready.",
                "",
            ]
        ),
    )


def prepare_dataset(args: argparse.Namespace) -> int:
    progress = _ProgressReporter(getattr(args, "progress_file", None))
    progress.emit("preflight", "Checking input and local prerequisites")
    input_value = getattr(args, "input_file", None) or getattr(args, "zip_file", None)
    input_path = Path(input_value).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if input_path.suffix.lower() not in {".zip", ".json"}:
        raise ValueError("Input file must be recordings.json or a legacy dataset .zip")
    missing_tools = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing_tools:
        raise RuntimeError(
            "Missing required local tool(s): " + ", ".join(missing_tools)
        )

    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else _default_output_dir(input_path)
    )
    if input_path.suffix.lower() == ".json":
        metadata_path = _materialize_recordings_manifest(
            input_path,
            output_dir,
            speaker=args.speaker,
            overwrite=args.overwrite,
            progress=progress,
        )
    else:
        progress.emit("extract", "Safely extracting the legacy dataset archive")
        _extract_zip(input_path, output_dir, args.overwrite)
        metadata_path = _find_metadata(output_dir)

    _ensure_private_workspace(output_dir)
    rows = _read_rows(metadata_path)
    speaker = _choose_speaker(rows, args.speaker)
    language = args.language
    experiment = _slug(args.experiment) if args.experiment else _default_experiment(
        rows, speaker, language
    )

    metadata_dir = output_dir / "metadata"
    if args.list_name:
        list_path = (metadata_dir / args.list_name).resolve()
        try:
            list_path.relative_to(output_dir)
        except ValueError as exc:
            raise ValueError("--list-name must stay inside the dataset workspace") from exc
    else:
        list_path = metadata_dir / f"{experiment}_all.list"
    train_path = metadata_dir / f"{experiment}_train.list"
    validation_path = metadata_dir / f"{experiment}_validation.list"
    test_path = metadata_dir / f"{experiment}_test.list"
    note_path = output_dir / "GPT_SOVITS_NEXT_STEPS.txt"
    normalized_dir = output_dir / "wav"

    kept = 0
    transcript_mismatch = 0
    total_seconds = 0.0

    manifest_items = []
    split_lines = {name: [] for name in ("train", "validation", "test")}
    selected_rows = [
        row for row in rows if (row.get("speaker_user_id") or "").strip() == speaker
    ]
    seen_ids: set[str] = set()
    all_lines: list[str] = []
    progress.emit(
        "convert",
        "Verifying source metadata and converting audio to PCM16/32 kHz/mono",
        current=0,
        total=len(selected_rows),
    )
    for row in selected_rows:
        recording_id = str(row.get("id") or kept + 1).strip()
        if recording_id in seen_ids:
            raise ValueError(f"metadata contains duplicate recording id {recording_id}")
        seen_ids.add(recording_id)
        audio_rel = (row.get("audio_file") or "").strip()
        audio_path = (metadata_path.parent / audio_rel).resolve()
        try:
            audio_path.relative_to(output_dir)
        except ValueError as exc:
            raise ValueError(f"audio_file escapes the dataset workspace: {audio_rel!r}") from exc
        if not audio_path.is_file():
            raise ValueError(f"recording {recording_id} is missing its source audio")
        if row.get("manifest_source") == "recordings_json":
            _verify_manifest_audio_metadata(audio_path, row)

        text = _clean_text(row.get(args.text_column) or "")
        if not text:
            raise ValueError(f"recording {recording_id} has empty {args.text_column}")
        group_key = str(row.get("manuscript_id") or row.get("script_id") or "").strip()
        if not group_key:
            raise ValueError(f"recording {recording_id} has no manuscript_id or script_id")

        transcript = _clean_text(row.get("ai_transcript") or "")
        if transcript and transcript != text:
            transcript_mismatch += 1

        normalized_path = normalized_dir / f"{_slug(recording_id)}.wav"
        quality = _normalize_audio(audio_path, normalized_path)
        total_seconds += float(quality["duration_seconds"])
        split_name = _split_for(group_key)
        line = f"{normalized_path}|{experiment}|{language}|{text}\n"
        all_lines.append(line)
        split_lines[split_name].append(line)
        manifest_items.append(
            {
                "id": recording_id,
                "speaker_user_id": speaker,
                "script_id": row.get("script_id") or "",
                "manuscript_id": row.get("manuscript_id") or "",
                "prompt_text": text,
                "raw_file": str(audio_path.relative_to(output_dir)),
                "raw_sha256": _sha256(audio_path),
                "normalized_file": str(normalized_path.relative_to(output_dir)),
                "split": split_name,
                "quality": quality,
                "transcript_matches_prompt": not transcript or transcript == text,
            }
        )
        kept += 1
        progress.emit(
            "convert",
            f"Converted and verified recording {kept} of {len(selected_rows)}",
            current=kept,
            total=len(selected_rows),
        )

    if kept == 0:
        raise ValueError("No recordings were available for the selected speaker")
    if kept != len(selected_rows):
        raise RuntimeError("dataset item count changed during preparation")

    total_minutes = total_seconds / 60
    _atomic_write_text(list_path, "".join(all_lines))
    for split_name, path in (
        ("train", train_path),
        ("validation", validation_path),
        ("test", test_path),
    ):
        _atomic_write_text(path, "".join(split_lines[split_name]))

    readiness = _readiness(total_minutes, split_lines)
    progress.emit("hardware", "Detecting local hardware and calculating safe parameters")
    hardware = _hardware_info(output_dir)
    params = _recommended_params(hardware, total_minutes)
    params["dataset_readiness"] = readiness["status"]
    params["warnings"] = [*params.get("warnings", []), *readiness["warnings"]]
    _write_webui_note(
        note_path, experiment, train_path, output_dir, language, hardware, params
    )
    stable_manifest = {
        "format_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "experiment": experiment,
        "speaker_user_id": speaker,
        "language": language,
        "audio_derivation": {
            "sample_rate_hz": 32000,
            "channels": 1,
            "codec": "pcm_s16le",
            "operations": ["decode", "resample", "channel_conversion"],
            "filters": [],
        },
        "items": manifest_items,
    }
    manifest_bytes = (
        json.dumps(stable_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    snapshot_path = output_dir / "provenance" / "snapshot_manifest.json"
    _atomic_write_bytes(snapshot_path, manifest_bytes)
    _atomic_write_text(
        output_dir / "provenance" / "snapshot_manifest.sha256",
        f"{manifest_sha}  snapshot_manifest.json\n",
    )
    quality_report = {
        "rows": kept,
        "source_rows": len(selected_rows),
        "total_minutes": round(total_minutes, 3),
        "splits": {name: len(lines) for name, lines in split_lines.items()},
        "transcript_mismatches": transcript_mismatch,
        "missing_audio": 0,
        "empty_text": 0,
        "format_checks": {"sample_rate_hz": 32000, "channels": 1, "codec": "pcm_s16le"},
        "readiness": readiness,
    }
    _write_json(output_dir / "quality_report.json", quality_report)
    _write_json(output_dir / "hardware.json", hardware)
    _write_json(output_dir / "recommended_config.json", params)
    for item in manifest_items:
        raw_path = output_dir / str(item["raw_file"])
        if raw_path.is_file():
            os.chmod(raw_path, 0o400)

    result = {
        "status": "complete",
        "output_dir": str(output_dir),
        "experiment": experiment,
        "language": language,
        "training_list": str(train_path),
        "all_list": str(list_path),
        "validation_list": str(validation_path),
        "test_list": str(test_path),
        "next_steps": str(note_path),
        "snapshot_manifest": str(snapshot_path),
        "manifest_lock": (
            str(output_dir / "provenance" / "manifest.lock.json")
            if (output_dir / "provenance" / "manifest.lock.json").is_file()
            else None
        ),
        "hardware_report": str(output_dir / "hardware.json"),
        "recommended_config": str(output_dir / "recommended_config.json"),
        "manifest_sha256": manifest_sha,
        "quality": quality_report,
        "hardware": hardware,
        "recommendation": params,
        "readiness": readiness,
    }
    _write_json(output_dir / "preparation_result.json", result)
    progress.emit("complete", "Dataset preparation completed", current=kept, total=kept)

    print(f"Prepared {LOCAL_TTS_TRAINING_ENGINE} dataset")
    print(f"  extracted dir: {output_dir}")
    print(f"  metadata:      {metadata_path}")
    print(f"  speaker:       {speaker}")
    print(f"  experiment:    {experiment}")
    print(f"  training list: {train_path}")
    print(f"  WebUI note:    {note_path}")
    print(f"  rows written:  {kept}")
    print(f"  total audio:   {total_minutes:.1f} minutes")
    print(f"  manifest SHA:  {manifest_sha}")
    print(f"  splits:        {quality_report['splits']}")
    if transcript_mismatch:
        print(
            f"  note:          {transcript_mismatch} ASR transcript(s) differ "
            "from prompt_text; prompt_text was used"
        )
    print(f"  readiness:     {readiness['status']}")
    print()
    print(f"Fill {LOCAL_TTS_TRAINING_ENGINE} WebUI with:")
    print(f"  Experiment/model name: {experiment}")
    print(f"  Text labelling file:   {train_path}")
    print("  Audio dataset folder:  leave blank")
    print("  Language:              yue")
    print()
    print("Recommended 1B fine-tuning parameters for this machine:")
    print(f"  hardware:              {hardware['system']} {hardware['machine']}, CPU cores={hardware['cpu_count']}")
    memory = hardware.get("memory_gb")
    if isinstance(memory, float):
        print(f"  memory:                {memory:.1f} GB")
    if hardware.get("gpus"):
        print(f"  GPUs:                  {hardware['gpus']}")
    print(f"  note:                  {params['device_note']}")
    print(f"  SoVITS batch size:     {params['sovits_batch']}")
    print(f"  SoVITS epochs:         {params['sovits_epochs']}")
    print(f"  SoVITS save frequency: {params['sovits_save_every']}")
    print(f"  GPT batch size:        {params['gpt_batch']}")
    print(f"  GPT epochs:            {params['gpt_epochs']}")
    print(f"  GPT save frequency:    {params['gpt_save_every']}")
    print(f"  GPU number:            {params['gpu_info'] or 'none (CPU smoke only)'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download/verify an AI Training recordings.json manifest (or open a legacy ZIP) "
            f"and build a {LOCAL_TTS_TRAINING_ENGINE} dataset."
        )
    )
    parser.add_argument(
        "input_file",
        help="Path to recordings.json exported by AI Training, or a legacy dataset ZIP",
    )
    parser.add_argument(
        "--output-dir",
        help="Private dataset workspace. Defaults to ~/private-ai-training/tts-<timestamp>-<name>",
    )
    parser.add_argument(
        "--speaker",
        help="Speaker user id to keep. Required if the input contains multiple speakers",
    )
    parser.add_argument(
        "--experiment",
        help=(
            f"Non-public {LOCAL_TTS_TRAINING_ENGINE} experiment/model name. "
            "Defaults to a stable voice_<digest> id"
        ),
    )
    parser.add_argument(
        "--language",
        default=DEFAULT_LANGUAGE,
        help=f"{LOCAL_TTS_TRAINING_ENGINE} language code. Default: yue",
    )
    parser.add_argument(
        "--text-column",
        default="prompt_text",
        help="metadata.csv text column to use. Default: prompt_text",
    )
    parser.add_argument("--list-name", help="Output .list filename. Defaults to <experiment>.list")
    parser.add_argument(
        "--progress-file",
        help="Optional private JSON status file used by the localhost drag-and-drop app",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an invalid existing download or re-extract a legacy ZIP",
    )
    return parser


def main() -> int:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args()
    try:
        return prepare_dataset(args)
    except Exception as exc:
        public_message = _public_error(exc)
        _ProgressReporter(getattr(args, "progress_file", None)).emit(
            "error", public_message
        )
        print(f"error: {public_message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
