import json
import os
from pathlib import Path

import pytest

from workstation.scripts.benchmark_asr import (
    _edit_distance,
    _normalise,
    _punctuation_counts,
    load_corpus,
)
from workstation.config import AsrConfig
from workstation.scripts.approve_asr_profile import approve
from workstation.workloads.asr import FasterWhisperAdapter
from workstation.workloads.errors import WorkloadError


def test_asr_metrics_handle_mixed_cantonese_english_and_punctuation():
    assert _normalise("我方 AI，2026！") == "我方ai2026"
    assert _edit_distance("正方", "反方") == 1
    assert _punctuation_counts("甲，乙。", "甲，乙！") == (1, 2, 2)


def test_corpus_rejects_missing_required_real_world_categories(tmp_path, monkeypatch):
    (tmp_path / "one.wav").write_bytes(b"RIFF")
    corpus = {
        "schema_version": 1,
        "samples": [{
            "id": "one", "audio": "one.wav", "reference": "廣東話",
            "categories": ["cantonese", "short"], "keywords": ["廣東話"],
        }],
    }
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(corpus))
    monkeypatch.setattr("workstation.scripts.benchmark_asr._duration", lambda _path: 1.0)
    with pytest.raises(ValueError, match="background_noise"):
        load_corpus(path)


def test_corpus_cannot_escape_its_directory(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside.wav"
    outside.write_bytes(b"RIFF")
    corpus = {
        "schema_version": 1,
        "samples": [{
            "id": "one", "audio": "../outside.wav", "reference": "廣東話",
            "categories": [
                "cantonese", "english", "numbers", "debate_terms",
                "background_noise", "short", "long",
            ],
            "keywords": ["廣東話"],
        }],
    }
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(corpus))
    monkeypatch.setattr("workstation.scripts.benchmark_asr._duration", lambda _path: 1.0)
    with pytest.raises(ValueError, match="stay below"):
        load_corpus(path)


def test_asr_health_requires_selected_profile_in_reviewed_report(
    tmp_path, monkeypatch,
):
    runtime = tmp_path / "runtime/bin/python"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"pinned python")
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.bin").write_bytes(b"approved model")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "schema_version": 1,
        "generated_at_unix": 2_000_000_000,
        "corpus_sha256": "a" * 64,
        "required_categories": [
            "background_noise", "cantonese", "debate_terms", "english",
            "long", "numbers", "short",
        ],
        "sample_count": 7,
        "results": [{
            "model_path": str(model.resolve()),
            "device": "cuda",
            "compute_type": "int8",
        }],
        "approval_written": False,
    }))
    adapter = FasterWhisperAdapter(AsrConfig(
        enabled=True,
        model=str(model),
        device="cuda",
        compute_type="float16",
        benchmark_approved=True,
        runtime_python=runtime,
        benchmark_report=report,
    ))
    assert adapter.health()["code"] == "benchmark_report_mismatch"
    value = json.loads(report.read_text())
    value["results"][0]["compute_type"] = "float16"
    report.write_text(json.dumps(value))
    provenance = tmp_path / "PROVENANCE.json"
    provenance.write_text(json.dumps({
        "schema_version": 1,
        "python_version": "3.12",
        "pip_freeze_sha256": "b" * 64,
        "wheelhouse_manifest_sha256": "c" * 64,
    }))
    receipt = tmp_path / "active-receipt.json"
    approve(
        model=model, runtime_python=runtime,
        runtime_provenance=provenance, benchmark_report=report,
        device="cuda", compute_type="float16", output=receipt,
    )
    adapter = FasterWhisperAdapter(AsrConfig(
        enabled=True,
        model=str(model),
        device="cuda",
        compute_type="float16",
        benchmark_approved=True,
        runtime_python=runtime,
        benchmark_report=report,
        runtime_provenance=provenance,
        approval_receipt=receipt,
    ))

    class Completed:
        returncode = 0

    monkeypatch.setattr(
        "workstation.workloads.asr.subprocess.run", lambda *_args, **_kwargs: Completed(),
    )
    assert adapter.health()["ok"] is True

    model_file = model / "model.bin"
    before = model_file.stat()
    model_file.write_bytes(b"tampered model")
    os.utime(model_file, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert adapter.health()["ok"] is True
    with pytest.raises(WorkloadError) as raised:
        adapter.verify_artifacts()
    assert raised.value.code == "asr_digest_mismatch"
