#!/usr/bin/env python3
"""Explicitly approve one benchmarked ASR profile and hash its local artifacts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from workstation.scripts.benchmark_asr import REQUIRED_CATEGORIES
from workstation.workloads.asr_integrity import file_artifact, model_tree


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def approve(
    *, model: Path, runtime_python: Path, runtime_provenance: Path,
    benchmark_report: Path, device: str, compute_type: str, output: Path,
) -> dict:
    model = model.resolve(strict=True)
    runtime_python = runtime_python.resolve(strict=True)
    runtime_provenance = runtime_provenance.resolve(strict=True)
    benchmark_report = benchmark_report.resolve(strict=True)
    output_resolved = output.resolve(strict=False)
    if model == output_resolved or model in output_resolved.parents:
        raise ValueError("approval receipt must stay outside the approved model tree")
    report = json.loads(benchmark_report.read_bytes())
    results = report.get("results") if isinstance(report, dict) else None
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 1
        or report.get("approval_written") is not False
        or set(report.get("required_categories") or ()) != set(REQUIRED_CATEGORIES)
        or not isinstance(results, list)
        or not any(
            isinstance(item, dict)
            and Path(str(item.get("model_path") or "")).resolve(strict=True) == model
            and item.get("device") == device
            and item.get("compute_type") == compute_type
            for item in results
        )
    ):
        raise ValueError("selected ASR profile is absent from the reviewed benchmark")
    provenance = json.loads(runtime_provenance.read_bytes())
    if (
        not isinstance(provenance, dict)
        or set(provenance) != {
            "schema_version", "python_version", "pip_freeze_sha256",
            "wheelhouse_manifest_sha256",
        }
        or provenance.get("schema_version") != 1
        or not str(provenance.get("python_version") or "").strip()
        or not _SHA256_RE.fullmatch(str(provenance.get("pip_freeze_sha256") or ""))
        or not _SHA256_RE.fullmatch(str(provenance.get("wheelhouse_manifest_sha256") or ""))
    ):
        raise ValueError("ASR runtime provenance is invalid")
    receipt = {
        "schema_version": 1,
        "device": device,
        "compute_type": compute_type,
        "model": model_tree(model, include_content_hash=True),
        "runtime_python": file_artifact(runtime_python),
        "runtime_provenance": file_artifact(runtime_provenance),
        "benchmark_report": file_artifact(benchmark_report),
    }
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o640)
    os.replace(temporary, output)
    return receipt


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Approve one benchmarked local ASR profile")
    value.add_argument("--model", type=Path, required=True)
    value.add_argument("--runtime-python", type=Path, required=True)
    value.add_argument("--runtime-provenance", type=Path, required=True)
    value.add_argument("--benchmark-report", type=Path, required=True)
    value.add_argument("--device", choices=("cuda", "cpu"), required=True)
    value.add_argument("--compute-type", choices=("float16", "int8_float16", "int8", "float32"), required=True)
    value.add_argument("--output", type=Path, default=Path("/srv/lmc-ai/models/asr/active-receipt.json"))
    return value


def main() -> int:
    args = parser().parse_args()
    approve(
        model=args.model,
        runtime_python=args.runtime_python,
        runtime_provenance=args.runtime_provenance,
        benchmark_report=args.benchmark_report,
        device=args.device,
        compute_type=args.compute_type,
        output=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
