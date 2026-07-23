from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from ai_model_config import (
    LMC_AI_RAG_EMBEDDING_MODEL_TAG,
    lmc_ai_workstation_required_models,
)
from workstation.config import parse_config
from workstation.manager.artifacts import SignedArtifactManager
from workstation.workloads.errors import WorkloadError


def _config(tmp_path):
    return parse_config({
        "schema_version": 1,
        "node": {
            "name": "AI Workstation",
            "server_url": "https://example.com",
            "token_file": str(tmp_path / "token"),
        },
        "paths": {
            "state": str(tmp_path / "state"),
            "cache": str(tmp_path / "cache"),
            "data": str(tmp_path / "data"),
            "releases": str(tmp_path / "releases"),
        },
        "power": {},
        "workloads": {
            "rag": {
                "enabled": True,
                "embedding_model": LMC_AI_RAG_EMBEDDING_MODEL_TAG,
            },
        },
        "gui": {},
    })


class _Ollama:
    def __init__(self, inventory, *, apply_pull=True):
        self._inventory = inventory
        self.apply_pull = apply_pull
        self.pull_calls = []

    def inventory(self):
        return dict(self._inventory)

    def pull_approved(self, name, *, expected_digest, cancel_event):
        self.pull_calls.append((name, expected_digest, cancel_event.is_set()))
        if self.apply_pull:
            self._inventory[name] = expected_digest

    def embed(self, _model, texts):
        return [[1.0, float(index + 1)] for index, _text in enumerate(texts)]


def _rag_ready_ollama(tmp_path):
    required = list(lmc_ai_workstation_required_models())
    digests = {
        name: f"{index + 1:064x}"
        for index, name in enumerate(required)
    }
    receipt = tmp_path / "data/models/active-receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({
        "id": "models-v1",
        "sha256": "a" * 64,
        "approved_epoch": 123,
        "model_bytes": 123 * len(required),
        "models": {
            name: {"digest": digest, "bytes": 123}
            for name, digest in digests.items()
        },
    }))
    return _Ollama(digests)


def test_model_approval_skips_installed_exact_digests(tmp_path, monkeypatch):
    required = list(lmc_ai_workstation_required_models())
    digests = {name: f"{index + 1:064x}" for index, name in enumerate(required)}
    manager = SignedArtifactManager(_config(tmp_path), _Ollama(digests))
    component = {
        "id": "models-v1", "r2_key": "private/models.json",
        "sha256": "a" * 64, "bytes": 200,
    }
    monkeypatch.setattr(manager, "_catalog", lambda _name: ({}, component, "https://r2.example/models"))

    def download(_url, destination, **_kwargs):
        destination.write_text(json.dumps({
            "schema_version": 1,
            "models": [
                {"name": name, "digest": digest, "bytes": 123}
                for name, digest in digests.items()
            ],
        }))

    monkeypatch.setattr("workstation.manager.artifacts._download", download)
    receipt = manager.approve_models(cancel_event=threading.Event())
    assert receipt["id"] == "models-v1"
    saved = json.loads(
        (tmp_path / "data/models/active-receipt.json").read_text()
    )
    assert saved["sha256"] == "a" * 64
    assert saved["models"] == {
        name: {"digest": digest, "bytes": 123}
        for name, digest in digests.items()
    }
    assert manager.ollama.pull_calls == []


def test_explicit_model_approval_pulls_only_signed_required_models(
    tmp_path, monkeypatch,
):
    required = list(lmc_ai_workstation_required_models())
    digests = {name: f"{index + 1:064x}" for index, name in enumerate(required)}
    ollama = _Ollama({})
    manager = SignedArtifactManager(_config(tmp_path), ollama)
    component = {
        "id": "models-v1", "r2_key": "private/models.json",
        "sha256": "a" * 64, "bytes": 200,
    }
    monkeypatch.setattr(manager, "_catalog", lambda _name: ({}, component, "https://r2.example/models"))
    monkeypatch.setattr(
        "workstation.manager.artifacts.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=100_000_000_000),
    )

    def download(_url, destination, **_kwargs):
        destination.write_text(json.dumps({
            "schema_version": 1,
            "models": [
                {"name": name, "digest": digest, "bytes": 123}
                for name, digest in digests.items()
            ],
        }))

    monkeypatch.setattr("workstation.manager.artifacts._download", download)
    manager.approve_models(cancel_event=threading.Event())
    assert [(name, digest) for name, digest, _cancelled in ollama.pull_calls] == list(digests.items())

    manager.ollama = _Ollama({}, apply_pull=False)
    with pytest.raises(WorkloadError) as raised:
        manager.approve_models(cancel_event=threading.Event())
    assert raised.value.code == "model_digest_mismatch"


def test_failed_rag_build_keeps_previous_atomic_link(tmp_path, monkeypatch):
    manager = SignedArtifactManager(_config(tmp_path), _rag_ready_ollama(tmp_path))
    component = {
        "id": "rag-v2", "r2_key": "private/rag.tar.gz",
        "sha256": "b" * 64, "bytes": 1_000,
    }
    monkeypatch.setattr(manager, "_catalog", lambda _name: ({}, component, "https://r2.example/rag"))
    monkeypatch.setattr(
        "workstation.manager.artifacts.UpdateStager.r2_health_probe",
        lambda _self: {"ok": True},
    )
    monkeypatch.setattr(
        "workstation.manager.artifacts.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=100_000_000_000),
    )
    monkeypatch.setattr(
        "workstation.manager.artifacts._download",
        lambda _url, destination, **_kwargs: destination.write_bytes(b"archive"),
    )
    monkeypatch.setattr(
        "workstation.manager.artifacts._safe_extract",
        lambda _archive, destination: destination.mkdir(parents=True),
    )
    rag_root = tmp_path / "data/rag"
    old = rag_root / "versions/rag-v1"
    old.mkdir(parents=True)
    (old / "component-receipt.json").write_text(json.dumps({"id": "rag-v1", "sha256": "c" * 64}))
    (rag_root / "current").symlink_to(Path("versions/rag-v1"))
    monkeypatch.setattr(
        manager.rag,
        "build",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            WorkloadError("embedding_failed", "failed")
        ),
    )
    with pytest.raises(WorkloadError):
        manager.install_rag(cancel_event=threading.Event())
    assert (rag_root / "current").readlink() == Path("versions/rag-v1")
    assert not (rag_root / "versions/rag-v2").exists()


def test_rag_install_requires_the_signed_embedding_model_first(
    tmp_path, monkeypatch,
):
    manager = SignedArtifactManager(_config(tmp_path), _Ollama({}))
    catalog_called = False

    def catalog(_name):
        nonlocal catalog_called
        catalog_called = True
        return {}, {}, ""

    monkeypatch.setattr(manager, "_catalog", catalog)
    with pytest.raises(WorkloadError) as raised:
        manager.install_rag(cancel_event=threading.Event())

    assert raised.value.code == "rag_embedding_model_unapproved"
    assert catalog_called is False


def test_rag_health_binds_active_receipts_and_streamed_index_digest(tmp_path):
    manager = SignedArtifactManager(_config(tmp_path), _Ollama({}))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "documents.jsonl").write_text(json.dumps({
        "id": "doc-1",
        "title": "辯論資料",
        "text": "香港辯論參考內容",
        "source_url": "https://example.com/source",
    }, ensure_ascii=False) + "\n")
    rag_root = tmp_path / "data/rag"
    version = rag_root / "versions/rag-v1"
    meta = manager.rag.build(bundle, version, bundle_version="rag-v1")
    component = {"id": "rag-v1", "sha256": "a" * 64, "index": meta}
    (version / "component-receipt.json").write_text(json.dumps(component))
    (rag_root / "current").symlink_to(Path("versions/rag-v1"))
    (rag_root / "active-receipt.json").write_text(json.dumps({
        "id": "rag-v1", "sha256": "a" * 64, "activated_epoch": 123,
    }))
    assert manager.rag.health()["ok"] is True
    result = manager.rag.retrieve("香港辯論", top_k=1)
    assert result["results"][0]["citation"] == "RAG:doc-1"

    with (version / "index.jsonl").open("ab") as stream:
        stream.write(b"tampered\n")
    assert manager.rag.health()["code"] == "index_unavailable"


def test_rag_bundle_rejects_duplicate_document_ids(tmp_path):
    manager = SignedArtifactManager(_config(tmp_path), _Ollama({}))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    item = {
        "id": "duplicate", "title": "A", "text": "內容",
        "source_url": "https://example.com/source",
    }
    (bundle / "documents.jsonl").write_text(
        json.dumps(item, ensure_ascii=False) + "\n"
        + json.dumps({**item, "title": "B"}, ensure_ascii=False) + "\n"
    )
    with pytest.raises(WorkloadError) as raised:
        manager.rag.build(bundle, tmp_path / "output", bundle_version="rag-v1")
    assert raised.value.code == "invalid_rag_bundle"
