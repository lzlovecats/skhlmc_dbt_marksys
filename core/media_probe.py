"""Shared, bounded ffprobe validation for browser-supplied audio."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile

from system_limits import (
    MEDIA_PROBE_TIMEOUT_SECONDS,
    MEDIA_TRANSCODE_TIMEOUT_SECONDS,
)


SUPPORTED_AUDIO_MIMES = frozenset({
    "audio/webm", "audio/mp4", "audio/mpeg", "audio/wav", "audio/ogg",
})

_FORMAT_NAMES = {
    "audio/webm": frozenset({"webm", "matroska"}),
    "audio/mp4": frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}),
    "audio/mpeg": frozenset({"mp3"}),
    "audio/wav": frozenset({"wav"}),
    "audio/ogg": frozenset({"ogg"}),
}

_EXTENSIONS = {
    "audio/webm": "webm", "audio/mp4": "m4a", "audio/mpeg": "mp3",
    "audio/wav": "wav", "audio/ogg": "ogg",
}

PROVIDER_AUDIO_MIME = "audio/mpeg"


class MediaProbeError(ValueError):
    """A safe validation error suitable for returning to an authenticated user."""

    def __init__(self, message: str, *, service_unavailable: bool = False):
        super().__init__(message)
        self.service_unavailable = bool(service_unavailable)


def canonical_audio_mime(value: str) -> str:
    """Return a supported MIME without optional browser codec parameters."""
    mime = str(value or "").split(";", 1)[0].strip().lower()
    if mime not in SUPPORTED_AUDIO_MIMES:
        raise MediaProbeError("錄音宣稱格式不受支援")
    return mime


def audio_extension(mime: str) -> str:
    return _EXTENSIONS[canonical_audio_mime(mime)]


def audio_container_matches_mime(mime: str, format_names: object) -> bool:
    """Validate measured ffprobe format names against a canonical audio MIME."""
    expected = _FORMAT_NAMES[canonical_audio_mime(mime)]
    if isinstance(format_names, str):
        actual = {
            item.strip().lower() for item in format_names.split(",") if item.strip()
        }
    else:
        actual = {
            str(item).strip().lower() for item in (format_names or ()) if str(item).strip()
        }
    return bool(actual.intersection(expected))


def transcode_audio_for_provider(
    audio: bytes,
    mime: str,
    *,
    max_output_bytes: int,
) -> tuple[bytes, str]:
    """Normalize bounded browser audio to Gemini's documented MP3 input.

    Callers must validate the source duration first.  ``-fs`` bounds temporary
    output even if a future caller omits that check, and the output is measured
    again before Python reads it into memory.
    """
    canonical_mime = canonical_audio_mime(mime)
    try:
        output_limit = int(max_output_bytes)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaProbeError("音訊轉換大小上限無效") from exc
    if not audio or output_limit < 1:
        raise MediaProbeError("音訊轉換資料無效")

    try:
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(
                directory, "source." + audio_extension(canonical_mime),
            )
            output_path = os.path.join(directory, "provider.mp3")
            with open(source_path, "wb") as source:
                source.write(audio)
            result = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-y", "-i", source_path, "-map", "0:a:0", "-vn", "-sn",
                    "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
                    "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame",
                    "-b:a", "16k", "-fs", str(output_limit + 1),
                    "-f", "mp3", output_path,
                ],
                capture_output=True,
                text=True,
                timeout=MEDIA_TRANSCODE_TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode != 0:
                raise MediaProbeError("錄音格式轉換失敗")
            try:
                output_size = os.path.getsize(output_path)
            except OSError as exc:
                raise MediaProbeError(
                    "伺服器未能讀取已轉換錄音",
                    service_unavailable=True,
                ) from exc
            if not 1 <= output_size <= output_limit:
                raise MediaProbeError("已轉換錄音超出大小上限")
            with open(output_path, "rb") as converted_file:
                converted = converted_file.read(output_limit + 1)
    except MediaProbeError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaProbeError(
            "伺服器未能執行音訊格式轉換",
            service_unavailable=True,
        ) from exc

    if not converted or len(converted) > output_limit:
        raise MediaProbeError("已轉換錄音超出大小上限")
    return converted, PROVIDER_AUDIO_MIME


def _decoded_duration_seconds(source_path: str, max_seconds: float) -> float:
    """Measure duration without buffering decoded PCM when metadata is absent."""
    decode_limit = float(max_seconds) + 1.0
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats",
            "-i", source_path, "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-t", f"{decode_limit:g}", "-f", "null", os.devnull,
        ],
        capture_output=True,
        text=True,
        timeout=MEDIA_TRANSCODE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise MediaProbeError("錄音檔案損壞或實際格式不受支援")
    duration = 0.0
    for line in str(result.stdout or "").splitlines():
        if not line.startswith("out_time_us="):
            continue
        try:
            duration = max(duration, int(line.split("=", 1)[1]) / 1_000_000)
        except (TypeError, ValueError, OverflowError):
            continue
    if duration <= 0:
        raise MediaProbeError("錄音未包含可量度長度的聲音軌")
    return duration


def probe_audio(
    audio: bytes,
    mime: str,
    claimed_duration: float | None,
    *,
    max_seconds: float,
) -> dict:
    """Verify container, audio stream and actual duration with bounded ffprobe.

    The caller remains responsible for applying its own byte limit before this
    function writes the already-bounded payload to a temporary file.
    """
    canonical_mime = canonical_audio_mime(mime)
    claimed = None
    if claimed_duration is not None:
        try:
            claimed = float(claimed_duration)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MediaProbeError("錄音長度資料無效，請重新錄製") from exc
        if not 1 <= claimed <= float(max_seconds):
            raise MediaProbeError(f"錄音長度必須為 1 至 {int(max_seconds)} 秒")

    try:
        with tempfile.NamedTemporaryFile(suffix="." + audio_extension(canonical_mime)) as handle:
            handle.write(audio)
            handle.flush()
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "format=format_name,duration:stream=codec_type,sample_rate,channels,duration",
                    "-of", "json", handle.name,
                ],
                capture_output=True,
                text=True,
                timeout=MEDIA_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode != 0:
                raise MediaProbeError("錄音檔案損壞或實際格式不受支援")
            try:
                info = json.loads(result.stdout or "{}")
                fmt = info.get("format") or {}
                stream = next(
                    item for item in (info.get("streams") or [])
                    if item.get("codec_type") == "audio"
                )
                duration = float(fmt.get("duration") or stream.get("duration") or 0)
                sample_rate = int(stream.get("sample_rate") or 0)
                channels = int(stream.get("channels") or 0)
                format_names = {
                    item.strip().lower()
                    for item in str(fmt.get("format_name") or "").split(",")
                    if item.strip()
                }
            except (ValueError, TypeError, StopIteration, OverflowError) as exc:
                raise MediaProbeError("錄音未包含可讀取的聲音軌") from exc
            if duration <= 0:
                duration = _decoded_duration_seconds(handle.name, max_seconds)
    except MediaProbeError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaProbeError(
            "伺服器未能執行音訊格式驗證", service_unavailable=True,
        ) from exc

    if not 1 <= duration <= float(max_seconds):
        raise MediaProbeError(f"錄音實際長度必須為 1 至 {int(max_seconds)} 秒")
    tolerance = max(2.0, duration * 0.2)
    if claimed is not None and abs(duration - claimed) > tolerance:
        raise MediaProbeError("錄音實際長度與瀏覽器回報不符，請重新錄製")
    if not audio_container_matches_mime(canonical_mime, format_names):
        raise MediaProbeError("錄音宣稱格式與實際檔案格式不符")
    if sample_rate <= 0 or channels <= 0:
        raise MediaProbeError("錄音未包含可讀取的聲音軌")

    return {
        "duration": round(duration, 3),
        "sample_rate": sample_rate,
        "channels": channels,
        "format": ",".join(sorted(format_names)),
        "mime": canonical_mime,
        "sha256": hashlib.sha256(audio).hexdigest(),
    }


def probe_audio_file(
    source_path: str,
    mime: str,
    claimed_duration: float | None,
    *,
    max_seconds: float,
) -> dict:
    """Probe an on-disk streamed upload without loading it into Python RAM."""
    canonical_mime = canonical_audio_mime(mime)
    try:
        claimed = float(claimed_duration) if claimed_duration is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaProbeError("錄音長度資料無效，請重新錄製") from exc
    if claimed is not None and not 1 <= claimed <= float(max_seconds):
        raise MediaProbeError(f"錄音長度必須為 1 至 {int(max_seconds)} 秒")
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=format_name,duration:stream=codec_type,sample_rate,channels,duration",
                "-of", "json", source_path,
            ],
            capture_output=True, text=True,
            timeout=MEDIA_PROBE_TIMEOUT_SECONDS, check=False,
        )
        if result.returncode != 0:
            raise MediaProbeError("錄音檔案損壞或實際格式不受支援")
        info = json.loads(result.stdout or "{}")
        fmt = info.get("format") or {}
        stream = next(
            item for item in (info.get("streams") or [])
            if item.get("codec_type") == "audio"
        )
        duration = float(fmt.get("duration") or stream.get("duration") or 0)
        sample_rate = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
        format_names = {
            item.strip().lower()
            for item in str(fmt.get("format_name") or "").split(",")
            if item.strip()
        }
        if duration <= 0:
            duration = _decoded_duration_seconds(source_path, max_seconds)
    except MediaProbeError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaProbeError(
            "伺服器未能執行音訊格式驗證", service_unavailable=True,
        ) from exc
    except (ValueError, TypeError, StopIteration, json.JSONDecodeError) as exc:
        raise MediaProbeError("錄音未包含可讀取的聲音軌") from exc
    if not 1 <= duration <= float(max_seconds):
        raise MediaProbeError(f"錄音實際長度必須為 1 至 {int(max_seconds)} 秒")
    if claimed is not None and abs(duration - claimed) > max(2.0, duration * 0.2):
        raise MediaProbeError("錄音實際長度與瀏覽器回報不符，請重新錄製")
    if not audio_container_matches_mime(canonical_mime, format_names):
        raise MediaProbeError("錄音宣稱格式與實際檔案格式不符")
    if sample_rate <= 0 or channels <= 0:
        raise MediaProbeError("錄音未包含可讀取的聲音軌")
    digest = hashlib.sha256()
    with open(source_path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return {
        "duration": round(duration, 3), "sample_rate": sample_rate,
        "channels": channels, "format": ",".join(sorted(format_names)),
        "mime": canonical_mime, "sha256": digest.hexdigest(),
    }
