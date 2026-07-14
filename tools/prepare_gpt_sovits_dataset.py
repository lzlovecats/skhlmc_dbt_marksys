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
import urllib.error
import urllib.request
import zipfile
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


class _DownloadValidationError(ValueError):
    """Safe-to-display validation failure that never contains a signed URL."""


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    value = value.strip("._-")
    return value or "tts_voice"


def _clean_text(value: str) -> str:
    return " ".join((value or "").replace("|", " ").split())


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

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
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
    except _DownloadValidationError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:
        partial.unlink(missing_ok=True)
        # urllib exceptions can contain the complete query string.  Keep the
        # signed token out of terminal logs and shell history diagnostics.
        raise RuntimeError(
            f"download failed for recording {recording_id} ({type(exc).__name__})"
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
) -> None:
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
    declared_total = sum(_positive_int(item.get("size_bytes")) for item in selected)
    if declared_total > DATASET_ARCHIVE_MAX_BYTES:
        raise ValueError(
            f"selected recordings declare {declared_total} bytes; limit is {DATASET_ARCHIVE_MAX_BYTES}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = output_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    names: set[str] = set()
    total_bytes = 0
    for item in selected:
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
                "audio_file": f"audio/{name}",
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

    fieldnames = list(rows[0]) if rows else [
        "speaker_user_id", "prompt_text", "audio_file"
    ]
    metadata_path = output_dir / "metadata.csv"
    with metadata_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


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
        raise RuntimeError("ffmpeg and ffprobe are required for deterministic audio normalization")
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-vn", "-ac", "1", "-ar", "32000", "-c:a", "pcm_s16le",
        "-af", "loudnorm=I=-23:TP=-2:LRA=7", str(target),
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
    quality["sha256"] = _sha256(target)
    return quality


def _run_quiet(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
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
    return None


def _nvidia_vram_gb() -> list[float]:
    if not shutil.which("nvidia-smi"):
        return []
    raw = _run_quiet(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    values = []
    for line in raw.splitlines():
        line = line.strip()
        if line.isdigit():
            values.append(int(line) / 1024)
    return values


def _hardware_info() -> dict[str, object]:
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count() or 1,
        "memory_gb": _memory_gb(),
        "nvidia_vram_gb": _nvidia_vram_gb(),
    }


def _recommended_params(info: dict[str, object], total_minutes: float) -> dict[str, object]:
    vram_values = info.get("nvidia_vram_gb") or []
    has_nvidia = bool(vram_values)
    max_vram = max(vram_values) if has_nvidia else 0

    if has_nvidia:
        if max_vram >= 16:
            sovits_batch = 4
            gpt_batch = 4
        elif max_vram >= 10:
            sovits_batch = 2
            gpt_batch = 2
        else:
            sovits_batch = 1
            gpt_batch = 1
        gpu_info = "0"
        device_note = f"NVIDIA GPU detected ({max_vram:.1f} GB VRAM); use GPU 0."
    else:
        sovits_batch = 1
        gpt_batch = 1
        gpu_info = "0"
        if info.get("system") == "Darwin" and info.get("machine") == "arm64":
            device_note = (
                "Apple Silicon Mac detected; use CPU settings first. "
                "MPS may work but is less predictable for this workflow."
            )
        else:
            device_note = "No NVIDIA GPU detected; use CPU settings."

    if total_minutes < 15:
        sovits_epochs = 4
        gpt_epochs = 10
    elif total_minutes < 45:
        sovits_epochs = 8
        gpt_epochs = 15
    else:
        sovits_epochs = 8
        gpt_epochs = 20

    return {
        "gpu_info": gpu_info,
        "device_note": device_note,
        "sovits_batch": sovits_batch,
        "sovits_epochs": sovits_epochs,
        "sovits_save_every": max(1, sovits_epochs // 2),
        "sovits_lr_weight": 0.4,
        "gpt_batch": gpt_batch,
        "gpt_epochs": gpt_epochs,
        "gpt_save_every": 5 if gpt_epochs >= 10 else max(1, gpt_epochs // 2),
        "enable_dpo": "unchecked",
    }


def _default_output_dir(zip_path: Path) -> Path:
    name = zip_path.stem
    return zip_path.parent / name


def _extract_zip(zip_path: Path, output_dir: Path, overwrite: bool) -> None:
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
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
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, target.open("wb") as destination:
                while block := source.read(1024 * 1024):
                    extracted_bytes += len(block)
                    if extracted_bytes > DATASET_ARCHIVE_MAX_BYTES:
                        raise ValueError("Archive exceeded the uncompressed byte limit")
                    destination.write(block)


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
        return "speaker"
    raise ValueError(
        "Multiple speakers found. Re-run with --speaker one of: "
        + ", ".join(speakers)
    )


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
    memory_text = f"{memory:.1f} GB" if isinstance(memory, float) else "unknown"
    nvidia = hardware.get("nvidia_vram_gb") or []
    nvidia_text = ", ".join(f"{value:.1f} GB" for value in nvidia) if nvidia else "none detected"
    note_path.write_text(
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
                f"CPU cores: {hardware.get('cpu_count')}",
                f"Memory: {memory_text}",
                f"NVIDIA VRAM: {nvidia_text}",
                f"Recommendation: {params['device_note']}",
                "",
                "0 / Fine-Tuned Model Information",
                f"Experiment/model name: {experiment}",
                "GPU Information: 0 CPU Training on CPU (slower)",
                "Version of the trained model: v2Pro",
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
                f"SoVITS GPU number: {params['gpu_info']}",
                "SoVITS checkboxes: keep both checked",
                "",
                f"GPT batch size: {params['gpt_batch']}",
                f"GPT total epochs: {params['gpt_epochs']}",
                f"GPT save frequency: {params['gpt_save_every']}",
                f"GPT GPU number: {params['gpu_info']}",
                "GPT DPO training: unchecked",
                "GPT checkboxes: keep both checked",
                "",
                "Press Open SoVITS Training first. Wait until it finishes, then press Open GPT Training.",
                "",
                "1C / Inference",
                f"Reference audio: choose a clean wav under {dataset_dir / 'audio'}",
                f"Reference language: {language}",
                f"Target language: {language}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def prepare_dataset(args: argparse.Namespace) -> int:
    input_value = getattr(args, "input_file", None) or getattr(args, "zip_file", None)
    input_path = Path(input_value).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if input_path.suffix.lower() not in {".zip", ".json"}:
        raise ValueError("Input file must be recordings.json or a legacy dataset .zip")

    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else _default_output_dir(input_path)
    )
    if input_path.suffix.lower() == ".json":
        _materialize_recordings_manifest(
            input_path,
            output_dir,
            speaker=args.speaker,
            overwrite=args.overwrite,
        )
    else:
        _extract_zip(input_path, output_dir, args.overwrite)

    metadata_path = _find_metadata(output_dir)
    rows = _read_rows(metadata_path)
    speaker = _choose_speaker(rows, args.speaker)
    language = args.language
    experiment = _slug(args.experiment or f"{speaker}_{language}_v0")

    list_path = output_dir / args.list_name if args.list_name else output_dir / f"{experiment}.list"
    note_path = output_dir / f"{experiment}_webui_fields.txt"
    normalized_dir = output_dir / "normalized_audio"

    kept = 0
    missing_audio = 0
    empty_text = 0
    transcript_mismatch = 0
    total_seconds = 0.0

    manifest_items = []
    split_lines = {name: [] for name in ("train", "validation", "test")}
    with list_path.open("w", encoding="utf-8") as out:
        for row in rows:
            row_speaker = (row.get("speaker_user_id") or "").strip()
            if row_speaker != speaker:
                continue
            audio_rel = (row.get("audio_file") or "").strip()
            audio_path = (metadata_path.parent / audio_rel).resolve()
            try:
                audio_path.relative_to(output_dir)
            except ValueError as exc:
                raise ValueError(f"audio_file escapes the extracted dataset: {audio_rel!r}") from exc
            if not audio_path.exists():
                missing_audio += 1
                continue
            if row.get("manifest_source") == "recordings_json":
                _verify_manifest_audio_metadata(audio_path, row)

            text = _clean_text(row.get(args.text_column) or "")
            if not text:
                empty_text += 1
                continue

            transcript = _clean_text(row.get("ai_transcript") or "")
            if transcript and transcript != text:
                transcript_mismatch += 1

            normalized_path = normalized_dir / f"{int(row.get('id') or kept + 1):06d}.wav"
            quality = _normalize_audio(audio_path, normalized_path)
            total_seconds += float(quality["duration_seconds"])
            group_key = _clean_text(row.get("manuscript_id") or row.get("script_id") or row.get("id") or str(kept))
            split_name = _split_for(group_key)
            line = f"{normalized_path}|{speaker}|{language}|{text}\n"
            out.write(line)
            split_lines[split_name].append(line)
            manifest_items.append({
                "id": str(row.get("id") or ""), "speaker_user_id": speaker,
                "script_id": row.get("script_id") or "", "manuscript_id": row.get("manuscript_id") or "",
                "prompt_text": text, "raw_file": str(audio_path.relative_to(output_dir)),
                "raw_sha256": _sha256(audio_path),
                "normalized_file": str(normalized_path.relative_to(output_dir)),
                "split": split_name, "quality": quality,
                "transcript_matches_prompt": not transcript or transcript == text,
            })
            kept += 1

    if kept == 0:
        raise ValueError("No usable rows were written. Check speaker, metadata.csv, and audio paths.")

    total_minutes = total_seconds / 60
    hardware = _hardware_info()
    params = _recommended_params(hardware, total_minutes)
    _write_webui_note(note_path, experiment, list_path, output_dir, language, hardware, params)
    for split_name, lines in split_lines.items():
        (output_dir / f"{experiment}_{split_name}.list").write_text("".join(lines), encoding="utf-8")
    stable_manifest = {
        "format_version": 1, "experiment": experiment, "speaker_user_id": speaker,
        "language": language, "normalization": {
            "sample_rate_hz": 32000, "channels": 1, "codec": "pcm_s16le",
            "loudness": "EBU R128 I=-23 LUFS TP=-2 dB LRA=7",
        }, "items": manifest_items,
    }
    manifest_bytes = json.dumps(stable_manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    stable_manifest["manifest_sha256"] = manifest_sha
    (output_dir / "snapshot_manifest.json").write_text(
        json.dumps(stable_manifest, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    quality_report = {
        "rows": kept, "total_minutes": round(total_minutes, 3),
        "splits": {name: len(lines) for name, lines in split_lines.items()},
        "transcript_mismatches": transcript_mismatch,
        "missing_audio": missing_audio, "empty_text": empty_text,
        "format_checks": {"sample_rate_hz": 32000, "channels": 1, "codec": "pcm_s16le"},
    }
    (output_dir / "quality_report.json").write_text(
        json.dumps(quality_report, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")

    print(f"Prepared {LOCAL_TTS_TRAINING_ENGINE} dataset")
    print(f"  extracted dir: {output_dir}")
    print(f"  metadata:      {metadata_path}")
    print(f"  speaker:       {speaker}")
    print(f"  experiment:    {experiment}")
    print(f"  list file:     {list_path}")
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
    if missing_audio:
        print(f"  warning:       skipped {missing_audio} row(s) with missing audio")
    if empty_text:
        print(f"  warning:       skipped {empty_text} row(s) with empty text")
    print()
    print(f"Fill {LOCAL_TTS_TRAINING_ENGINE} WebUI with:")
    print(f"  Experiment/model name: {experiment}")
    print(f"  Text labelling file:   {list_path}")
    print("  Audio dataset folder:  leave blank")
    print("  Language:              yue")
    print()
    print("Recommended 1B fine-tuning parameters for this machine:")
    print(f"  hardware:              {hardware['system']} {hardware['machine']}, CPU cores={hardware['cpu_count']}")
    memory = hardware.get("memory_gb")
    if isinstance(memory, float):
        print(f"  memory:                {memory:.1f} GB")
    if hardware.get("nvidia_vram_gb"):
        print(f"  NVIDIA VRAM:           {hardware['nvidia_vram_gb']}")
    print(f"  note:                  {params['device_note']}")
    print(f"  SoVITS batch size:     {params['sovits_batch']}")
    print(f"  SoVITS epochs:         {params['sovits_epochs']}")
    print(f"  SoVITS save frequency: {params['sovits_save_every']}")
    print(f"  GPT batch size:        {params['gpt_batch']}")
    print(f"  GPT epochs:            {params['gpt_epochs']}")
    print(f"  GPT save frequency:    {params['gpt_save_every']}")
    print(f"  GPU number:            {params['gpu_info']}")
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
        help="Dataset workspace. Defaults to the input filename without its suffix",
    )
    parser.add_argument(
        "--speaker",
        help="Speaker user id to keep. Required if the input contains multiple speakers",
    )
    parser.add_argument(
        "--experiment",
        help=f"{LOCAL_TTS_TRAINING_ENGINE} experiment/model name. Defaults to <speaker>_yue_v0",
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
        "--overwrite",
        action="store_true",
        help="Replace an invalid existing download or re-extract a legacy ZIP",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return prepare_dataset(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
