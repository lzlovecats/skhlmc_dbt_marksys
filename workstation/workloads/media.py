"""Actual media probing for downloaded input and generated output."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from core.media_probe import audio_container_matches_mime, canonical_audio_mime
from workstation.workloads.errors import WorkloadError


def probe_audio(
    path: Path,
    *,
    maximum_seconds: float,
    declared_mime: str,
    timeout_seconds: int = 20,
) -> dict:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration,format_name:stream=codec_type,codec_name,sample_rate,channels",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkloadError("media_probe_unavailable", "Audio validation is unavailable.", retryable=True) from exc
    if result.returncode:
        raise WorkloadError("invalid_audio", "Audio content could not be validated.")
    try:
        payload = json.loads(result.stdout)
        duration = float((payload.get("format") or {}).get("duration") or 0)
        streams = [item for item in payload.get("streams") or [] if item.get("codec_type") == "audio"]
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkloadError("invalid_audio", "Audio probe returned invalid metadata.") from exc
    format_names = str((payload.get("format") or {}).get("format_name") or "")
    try:
        mime = canonical_audio_mime(declared_mime)
    except ValueError as exc:
        raise WorkloadError("invalid_audio_mime", "Declared audio type is not supported.") from exc
    if not streams or duration <= 0 or duration > float(maximum_seconds):
        raise WorkloadError("invalid_audio_duration", "Audio duration is outside the allowed range.")
    if not audio_container_matches_mime(mime, format_names):
        raise WorkloadError("audio_mime_mismatch", "Audio content does not match its declared type.")
    stream = streams[0]
    return {
        "duration_seconds": round(duration, 3),
        "codec": str(stream.get("codec_name") or "")[:40],
        "sample_rate": max(0, int(stream.get("sample_rate") or 0)),
        "channels": max(0, int(stream.get("channels") or 0)),
        "container": format_names[:80],
        "mime_type": mime,
    }
