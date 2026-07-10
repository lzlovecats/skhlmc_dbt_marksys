#!/usr/bin/env python3
"""Prepare an exported TTS voice dataset zip for GPT-SoVITS.

The app exports:
    metadata.csv
    audio/<speaker>_<id>.wav

GPT-SoVITS expects a text labelling file:
    /absolute/path/to/audio.wav|speaker|yue|text
"""

from __future__ import annotations

import argparse
import csv
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


DEFAULT_LANGUAGE = "yue"


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    value = value.strip("._-")
    return value or "tts_voice"


def _clean_text(value: str) -> str:
    return " ".join((value or "").replace("|", " ").split())


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
            device_note = "Apple Silicon Mac detected; use CPU settings first. MPS may work but is less predictable for this workflow."
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
        zf.extractall(output_dir)


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
        rows = list(csv.DictReader(f))
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
                "GPT-SoVITS WebUI fields",
                "========================",
                "",
                "Open WebUI",
                "----------",
                "cd ~/Documents/AI/GPT-SoVITS",
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
    zip_path = Path(args.zip_file).expanduser().resolve()
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)
    if zip_path.suffix.lower() != ".zip":
        raise ValueError("Input file must be a .zip exported from ai_training.py")

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else _default_output_dir(zip_path)
    _extract_zip(zip_path, output_dir, args.overwrite)

    metadata_path = _find_metadata(output_dir)
    rows = _read_rows(metadata_path)
    speaker = _choose_speaker(rows, args.speaker)
    language = args.language
    experiment = _slug(args.experiment or f"{speaker}_{language}_v0")

    list_path = output_dir / args.list_name if args.list_name else output_dir / f"{experiment}.list"
    note_path = output_dir / f"{experiment}_webui_fields.txt"

    kept = 0
    missing_audio = 0
    empty_text = 0
    transcript_mismatch = 0
    total_seconds = 0.0

    with list_path.open("w", encoding="utf-8") as out:
        for row in rows:
            row_speaker = (row.get("speaker_user_id") or "").strip()
            if row_speaker != speaker:
                continue
            audio_rel = (row.get("audio_file") or "").strip()
            audio_path = (metadata_path.parent / audio_rel).resolve()
            if not audio_path.exists():
                missing_audio += 1
                continue

            text = _clean_text(row.get(args.text_column) or "")
            if not text:
                empty_text += 1
                continue

            transcript = _clean_text(row.get("ai_transcript") or "")
            if transcript and transcript != text:
                transcript_mismatch += 1

            try:
                total_seconds += float(row.get("duration_seconds") or 0)
            except ValueError:
                pass

            out.write(f"{audio_path}|{speaker}|{language}|{text}\n")
            kept += 1

    if kept == 0:
        raise ValueError("No usable rows were written. Check speaker, metadata.csv, and audio paths.")

    total_minutes = total_seconds / 60
    hardware = _hardware_info()
    params = _recommended_params(hardware, total_minutes)
    _write_webui_note(note_path, experiment, list_path, output_dir, language, hardware, params)

    print("Prepared GPT-SoVITS dataset")
    print(f"  extracted dir: {output_dir}")
    print(f"  metadata:      {metadata_path}")
    print(f"  speaker:       {speaker}")
    print(f"  experiment:    {experiment}")
    print(f"  list file:     {list_path}")
    print(f"  WebUI note:    {note_path}")
    print(f"  rows written:  {kept}")
    print(f"  total audio:   {total_minutes:.1f} minutes")
    if transcript_mismatch:
        print(f"  note:          {transcript_mismatch} ASR transcript(s) differ from prompt_text; prompt_text was used")
    if missing_audio:
        print(f"  warning:       skipped {missing_audio} row(s) with missing audio")
    if empty_text:
        print(f"  warning:       skipped {empty_text} row(s) with empty text")
    print()
    print("Fill GPT-SoVITS WebUI with:")
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
        description="Convert ai_training.py TTS dataset export zip into a GPT-SoVITS list file."
    )
    parser.add_argument("zip_file", help="Path to tts_voice_dataset*.zip exported from ai_training.py")
    parser.add_argument("--output-dir", help="Directory to extract into. Defaults to zip filename without .zip")
    parser.add_argument("--speaker", help="Speaker user id to keep. Required if the zip contains multiple speakers")
    parser.add_argument("--experiment", help="GPT-SoVITS experiment/model name. Defaults to <speaker>_yue_v0")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE, help="GPT-SoVITS language code. Default: yue")
    parser.add_argument("--text-column", default="prompt_text", help="metadata.csv text column to use. Default: prompt_text")
    parser.add_argument("--list-name", help="Output .list filename. Defaults to <experiment>.list")
    parser.add_argument("--overwrite", action="store_true", help="Re-extract zip over an existing output directory")
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
