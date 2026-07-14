"""Shared, bounded ffprobe validation for browser-supplied audio."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile

from system_limits import MEDIA_PROBE_TIMEOUT_SECONDS


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


def probe_audio(
    audio: bytes,
    mime: str,
    claimed_duration: float,
    *,
    max_seconds: float,
) -> dict:
    """Verify container, audio stream and actual duration with bounded ffprobe.

    The caller remains responsible for applying its own byte limit before this
    function writes the already-bounded payload to a temporary file.
    """
    canonical_mime = canonical_audio_mime(mime)
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
                    "format=format_name,duration:stream=codec_type,sample_rate,channels",
                    "-of", "json", handle.name,
                ],
                capture_output=True,
                text=True,
                timeout=MEDIA_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaProbeError(
            "伺服器未能執行音訊格式驗證", service_unavailable=True,
        ) from exc

    if result.returncode != 0:
        raise MediaProbeError("錄音檔案損壞或實際格式不受支援")
    try:
        info = json.loads(result.stdout or "{}")
        fmt = info.get("format") or {}
        stream = next(
            item for item in (info.get("streams") or [])
            if item.get("codec_type") == "audio"
        )
        duration = float(fmt.get("duration") or 0)
        sample_rate = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
        format_names = {
            item.strip().lower()
            for item in str(fmt.get("format_name") or "").split(",")
            if item.strip()
        }
    except (ValueError, TypeError, StopIteration, OverflowError) as exc:
        raise MediaProbeError("錄音未包含可讀取的聲音軌") from exc

    if not 1 <= duration <= float(max_seconds):
        raise MediaProbeError(f"錄音實際長度必須為 1 至 {int(max_seconds)} 秒")
    tolerance = max(2.0, duration * 0.2)
    if abs(duration - claimed) > tolerance:
        raise MediaProbeError("錄音實際長度與瀏覽器回報不符，請重新錄製")
    if not format_names.intersection(_FORMAT_NAMES[canonical_mime]):
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
