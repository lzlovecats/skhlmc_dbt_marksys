#!/usr/bin/env python3
"""Compare local Faster-Whisper candidates on an approved Cantonese corpus.

This tool is intentionally read-only with respect to Workstation configuration.
It never downloads a model and never sets ``benchmark_approved``.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import threading
import time


REQUIRED_CATEGORIES = frozenset({
    "cantonese", "english", "numbers", "debate_terms",
    "background_noise", "short", "long",
})
PUNCTUATION = frozenset("，。！？；：,.!?;:")


def _normalise(value: str) -> str:
    return "".join(
        character.casefold() for character in str(value or "")
        if character.isalnum()
    )


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_value in enumerate(left, 1):
        current = [left_index]
        for right_index, right_value in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_value != right_value),
            ))
        previous = current
    return previous[-1]


def _punctuation_counts(reference: str, hypothesis: str) -> tuple[int, int, int]:
    expected = Counter(character for character in reference if character in PUNCTUATION)
    actual = Counter(character for character in hypothesis if character in PUNCTUATION)
    true_positive = sum((expected & actual).values())
    return true_positive, sum(actual.values()), sum(expected.values())


def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"ffprobe failed for {path}")
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"duration is unavailable for {path}") from exc
    if not 0 < duration <= 1800:
        raise ValueError(f"duration is outside the benchmark bound for {path}")
    return duration


class _VramMonitor:
    def __init__(self):
        self.peak_mib = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _sample(self) -> None:
        result = subprocess.run(
            [
                "nvidia-smi", "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            values = [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]
            if values:
                self.peak_mib = max(self.peak_mib, max(values))

    def _run(self) -> None:
        while not self._stop.wait(0.2):
            try:
                self._sample()
            except (OSError, subprocess.SubprocessError, ValueError):
                pass

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, _kind, _value, _traceback):
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._sample()
        except (OSError, subprocess.SubprocessError, ValueError):
            pass


def load_corpus(path: Path) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    value = json.loads(raw)
    samples = value.get("samples") if isinstance(value, dict) else None
    if not isinstance(value, dict) or set(value) != {"schema_version", "samples"}:
        raise ValueError("corpus must contain only schema_version and samples")
    if value["schema_version"] != 1 or not isinstance(samples, list) or not samples:
        raise ValueError("corpus schema_version or samples are invalid")
    base = path.resolve().parent
    ids: set[str] = set()
    categories: set[str] = set()
    clean_samples: list[dict] = []
    for item in samples:
        if not isinstance(item, dict) or set(item) != {"id", "audio", "reference", "categories", "keywords"}:
            raise ValueError("each sample must have the exact documented fields")
        sample_id = str(item["id"] or "").strip()
        reference = str(item["reference"] or "").strip()
        sample_categories = {str(entry) for entry in item["categories"]}
        keywords = [str(entry).strip() for entry in item["keywords"]]
        audio = (base / str(item["audio"])).resolve()
        if not sample_id or sample_id in ids or not reference or not sample_categories:
            raise ValueError("sample id, reference or categories are invalid")
        if base != audio and base not in audio.parents:
            raise ValueError("sample audio must stay below the corpus directory")
        if not audio.is_file() or not all(keywords):
            raise ValueError(f"sample audio or keywords are invalid: {sample_id}")
        ids.add(sample_id)
        categories.update(sample_categories)
        clean_samples.append({
            "id": sample_id,
            "audio": audio,
            "reference": reference,
            "categories": sorted(sample_categories),
            "keywords": keywords,
            "duration_seconds": _duration(audio),
        })
    missing = REQUIRED_CATEGORIES - categories
    if missing:
        raise ValueError("corpus is missing categories: " + ", ".join(sorted(missing)))
    return clean_samples, hashlib.sha256(raw).hexdigest()


def benchmark_candidate(
    label: str,
    model_path: Path,
    samples: list[dict],
    *,
    device: str,
    compute_type: str,
) -> dict:
    if not model_path.is_dir():
        raise ValueError(f"candidate must be an existing local model directory: {model_path}")
    from faster_whisper import WhisperModel

    with _VramMonitor() as vram:
        load_started = time.monotonic()
        model = WhisperModel(
            str(model_path),
            device=device,
            compute_type=compute_type,
            local_files_only=True,
        )
        load_seconds = time.monotonic() - load_started
        output_samples = []
        total_edits = total_reference_chars = 0
        punctuation_tp = punctuation_actual = punctuation_expected = 0
        keyword_hits = keyword_total = 0
        latencies: list[float] = []
        total_audio = 0.0
        for sample in samples:
            started = time.monotonic()
            segments, info = model.transcribe(
                str(sample["audio"]), language="zh", beam_size=5,
                vad_filter=True, condition_on_previous_text=True,
            )
            hypothesis = "".join(
                str(getattr(segment, "text", "") or "").strip()
                for segment in segments
            ).strip()
            latency = time.monotonic() - started
            normal_reference = _normalise(sample["reference"])
            normal_hypothesis = _normalise(hypothesis)
            edits = _edit_distance(normal_reference, normal_hypothesis)
            punct = _punctuation_counts(sample["reference"], hypothesis)
            hits = sum(_normalise(keyword) in normal_hypothesis for keyword in sample["keywords"])
            total_edits += edits
            total_reference_chars += len(normal_reference)
            punctuation_tp += punct[0]
            punctuation_actual += punct[1]
            punctuation_expected += punct[2]
            keyword_hits += hits
            keyword_total += len(sample["keywords"])
            latencies.append(latency)
            total_audio += sample["duration_seconds"]
            output_samples.append({
                "id": sample["id"],
                "categories": sample["categories"],
                "duration_seconds": round(sample["duration_seconds"], 3),
                "latency_seconds": round(latency, 3),
                "character_error_rate": _ratio(edits, len(normal_reference)),
                "keyword_hits": hits,
                "keyword_total": len(sample["keywords"]),
                "language": str(getattr(info, "language", "") or "")[:20],
                "reference": sample["reference"],
                "hypothesis": hypothesis,
            })
    precision = _ratio(punctuation_tp, punctuation_actual)
    recall = _ratio(punctuation_tp, punctuation_expected)
    return {
        "label": label,
        "model_path": str(model_path.resolve()),
        "device": device,
        "compute_type": compute_type,
        "model_load_seconds": round(load_seconds, 3),
        "peak_gpu_memory_mib": vram.peak_mib,
        "character_error_rate": _ratio(total_edits, total_reference_chars),
        "punctuation_precision": precision,
        "punctuation_recall": recall,
        "punctuation_f1": _ratio(2 * precision * recall, precision + recall),
        "keyword_accuracy": _ratio(keyword_hits, keyword_total),
        "latency_p50_seconds": round(statistics.median(latencies), 3),
        "latency_p95_seconds": _percentile(latencies, 0.95),
        "real_time_factor": _ratio(sum(latencies), total_audio),
        "samples": output_samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--candidate", action="append", required=True, metavar="LABEL=/LOCAL/MODEL/DIR")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="float16")
    arguments = parser.parse_args()
    samples, corpus_sha256 = load_corpus(arguments.corpus)
    candidates = []
    labels: set[str] = set()
    for value in arguments.candidate:
        label, separator, raw_path = value.partition("=")
        if not separator or not label.strip() or label.strip() in labels:
            parser.error("each --candidate must be a unique LABEL=/LOCAL/MODEL/DIR")
        labels.add(label.strip())
        candidates.append((label.strip(), Path(raw_path).resolve()))
    report = {
        "schema_version": 1,
        "generated_at_unix": int(time.time()),
        "corpus_sha256": corpus_sha256,
        "required_categories": sorted(REQUIRED_CATEGORIES),
        "sample_count": len(samples),
        "results": [
            benchmark_candidate(
                label, path, samples,
                device=arguments.device,
                compute_type=arguments.compute_type,
            )
            for label, path in candidates
        ],
        "approval_written": False,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = arguments.output.with_suffix(arguments.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o640)
    os.replace(temporary, arguments.output)
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
