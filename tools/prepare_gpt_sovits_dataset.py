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
import re
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
) -> None:
    note_path.write_text(
        "\n".join(
            [
                "GPT-SoVITS WebUI fields",
                "========================",
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
                "1B / Fine-Tuning",
                "SoVITS batch size: 1",
                "SoVITS total epochs: 4 for a quick smoke test, 8 for a longer first run",
                "SoVITS save frequency: 2",
                "GPT batch size: 1",
                "GPT total epochs: 10",
                "GPT save frequency: 5",
                "GPU number: 0",
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

    _write_webui_note(note_path, experiment, list_path, output_dir, language)

    print("Prepared GPT-SoVITS dataset")
    print(f"  extracted dir: {output_dir}")
    print(f"  metadata:      {metadata_path}")
    print(f"  speaker:       {speaker}")
    print(f"  experiment:    {experiment}")
    print(f"  list file:     {list_path}")
    print(f"  WebUI note:    {note_path}")
    print(f"  rows written:  {kept}")
    print(f"  total audio:   {total_seconds / 60:.1f} minutes")
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
