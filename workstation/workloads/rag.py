"""Immutable local RAG indexing and retrieval using Ollama embeddings."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse

from system_limits import (
    WORKSTATION_RAG_DOCUMENT_MAX,
    WORKSTATION_RAG_DOCUMENT_MAX_CHARS,
    WORKSTATION_RAG_INDEX_LINE_MAX_BYTES,
    WORKSTATION_RAG_INDEX_MAX_BYTES,
    WORKSTATION_RAG_TOP_K_MAX,
)
from workstation.config import RagConfig
from workstation.workloads.errors import WorkloadError
from workstation.workloads.ollama import OllamaAdapter


INDEX_FILE = "index.jsonl"
INDEX_META_FILE = "index-meta.json"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _stream_sha256(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("RAG index is not a regular file")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > WORKSTATION_RAG_INDEX_MAX_BYTES:
                raise ValueError("RAG index exceeds its safe limit")
            digest.update(chunk)
    if total <= 0:
        raise ValueError("RAG index is empty")
    return digest.hexdigest(), total


def _small_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 64 * 1024:
        raise ValueError("RAG metadata file is invalid")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("RAG metadata is not an object")
    return value


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    numerator = sum(a * b for a, b in zip(left, right))
    denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return numerator / denominator if denominator else -1.0


class LocalRagIndex:
    def __init__(self, config: RagConfig, ollama: OllamaAdapter):
        self.config = config
        self.ollama = ollama

    def validate_version(self, target: Path) -> dict:
        try:
            target = target.resolve(strict=True)
            if not target.is_dir():
                raise ValueError("RAG version is not a directory")
            meta = _small_json(target / INDEX_META_FILE)
            component = _small_json(target / "component-receipt.json")
            index = target / INDEX_FILE
            digest, index_bytes = _stream_sha256(index)
            if (
                not isinstance(meta, dict)
                or set(meta) != {
                    "bundle_version", "embedding_model", "documents",
                    "dimensions", "index_sha256", "index_bytes",
                }
                or not isinstance(component, dict)
                or set(component) != {"id", "sha256", "index"}
                or component.get("index") != meta
                or component.get("id") != meta.get("bundle_version")
                or not _SHA256_RE.fullmatch(str(component.get("sha256") or ""))
                or digest != meta.get("index_sha256")
                or index_bytes != int(meta.get("index_bytes") or 0)
                or meta.get("embedding_model") != self.config.embedding_model
                or not 1 <= int(meta.get("documents") or 0) <= WORKSTATION_RAG_DOCUMENT_MAX
                or not 1 <= int(meta.get("dimensions") or 0) <= 16_384
            ):
                raise ValueError("index metadata mismatch")
            return {
                "ok": True,
                "bundle_version": str(meta["bundle_version"]),
                "documents": int(meta["documents"]),
                "dimensions": int(meta["dimensions"]),
                "component_sha256": str(component["sha256"]),
                "target": str(target),
            }
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            return {"ok": False, "code": "index_unavailable"}

    def health(self) -> dict:
        if not self.config.enabled:
            return {"ok": False, "code": "disabled"}
        link = self.config.active_link
        try:
            if not link.is_symlink():
                raise ValueError("active RAG pointer is not a symlink")
            target = link.resolve(strict=True)
            versions = (link.parent / "versions").resolve(strict=True)
            if target.parent != versions:
                raise ValueError("active RAG pointer escaped versions")
            status = self.validate_version(target)
            if not status.get("ok"):
                raise ValueError("active RAG version is invalid")
            active = _small_json(link.parent / "active-receipt.json")
            if (
                not isinstance(active, dict)
                or set(active) != {"id", "sha256", "activated_epoch"}
                or active.get("id") != status["bundle_version"]
                or active.get("sha256") != status["component_sha256"]
                or int(active.get("activated_epoch") or 0) <= 0
            ):
                raise ValueError("active RAG receipt does not match")
            return status
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            return {"ok": False, "code": "index_unavailable"}

    def build(
        self,
        bundle_dir: Path,
        output_dir: Path,
        *,
        bundle_version: str,
        cancel_event=None,
    ) -> dict:
        source = bundle_dir / "documents.jsonl"
        documents = []
        document_ids: set[str] = set()
        try:
            with source.open("rb") as stream:
                while line := stream.readline(WORKSTATION_RAG_INDEX_LINE_MAX_BYTES + 1):
                    if cancel_event is not None and cancel_event.is_set():
                        raise WorkloadError("cancelled", "RAG indexing was cancelled.")
                    if len(line) > WORKSTATION_RAG_INDEX_LINE_MAX_BYTES:
                        raise ValueError("RAG source line exceeds its safe limit")
                    if len(documents) >= WORKSTATION_RAG_DOCUMENT_MAX:
                        raise WorkloadError("rag_too_many_documents", "RAG bundle exceeds the document limit.")
                    item = json.loads(line)
                    if not isinstance(item, dict) or set(item) != {
                        "id", "title", "text", "source_url",
                    }:
                        raise ValueError("document is not an object")
                    document_id = str(item.get("id") or "")[:200]
                    title = str(item.get("title") or "")[:500]
                    text = str(item.get("text") or "")
                    source_url = str(item.get("source_url") or "")[:2_000]
                    parsed_source = urlparse(source_url) if source_url else None
                    if (
                        not document_id
                        or document_id in document_ids
                        or not title
                        or not text
                        or len(text) > WORKSTATION_RAG_DOCUMENT_MAX_CHARS
                        or (
                            parsed_source is not None
                            and (
                                parsed_source.scheme != "https"
                                or not parsed_source.hostname
                                or parsed_source.port not in {None, 443}
                                or parsed_source.username
                                or parsed_source.password
                                or parsed_source.fragment
                            )
                        )
                    ):
                        raise ValueError("invalid RAG document")
                    document_ids.add(document_id)
                    documents.append({"id": document_id, "title": title, "text": text, "source_url": source_url})
        except WorkloadError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise WorkloadError("invalid_rag_bundle", "RAG bundle documents are invalid.") from exc
        if not documents:
            raise WorkloadError("empty_rag_bundle", "RAG bundle contains no documents.")
        vectors = []
        for offset in range(0, len(documents), 16):
            if cancel_event is not None and cancel_event.is_set():
                raise WorkloadError("cancelled", "RAG indexing was cancelled.")
            vectors.extend(self.ollama.embed(self.config.embedding_model, [item["text"] for item in documents[offset:offset + 16]]))
        if (
            len(vectors) != len(documents)
            or not vectors
            or not vectors[0]
            or len(vectors[0]) > 16_384
            or any(
                len(vector) != len(vectors[0])
                or any(not math.isfinite(float(value)) for value in vector)
                for vector in vectors
            )
        ):
            raise WorkloadError("embedding_shape", "Local embedding returned an invalid shape.")
        output_dir.mkdir(parents=True, exist_ok=False, mode=0o750)
        index_path = output_dir / INDEX_FILE
        descriptor, temporary = tempfile.mkstemp(prefix="rag-index-", dir=output_dir)
        index_bytes = 0
        try:
            with os.fdopen(descriptor, "wb") as stream:
                for item, vector in zip(documents, vectors):
                    encoded = (
                        json.dumps(
                            {**item, "embedding": vector}, ensure_ascii=False,
                            separators=(",", ":"), allow_nan=False,
                        ) + "\n"
                    ).encode("utf-8")
                    if len(encoded) > WORKSTATION_RAG_INDEX_LINE_MAX_BYTES:
                        raise WorkloadError("rag_index_too_large", "A RAG index document exceeds its safe limit.")
                    index_bytes += len(encoded)
                    if index_bytes > WORKSTATION_RAG_INDEX_MAX_BYTES:
                        raise WorkloadError("rag_index_too_large", "RAG index exceeds its safe limit.")
                    stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, index_path)
            os.chmod(index_path, 0o640)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        digest, measured_bytes = _stream_sha256(index_path)
        meta = {"bundle_version": str(bundle_version)[:200], "embedding_model": self.config.embedding_model, "documents": len(documents), "dimensions": len(vectors[0]), "index_sha256": digest, "index_bytes": measured_bytes}
        meta_path = output_dir / INDEX_META_FILE
        meta_path.write_text(json.dumps(meta, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(meta_path, 0o640)
        return meta

    def retrieve(self, query: str, *, top_k: int = 6) -> dict:
        status = self.health()
        if not status.get("ok"):
            raise WorkloadError("rag_not_ready", "Local RAG is not ready.")
        target = self.config.active_link.resolve(strict=True)
        clean_query = str(query or "").strip()
        if not clean_query or len(clean_query) > WORKSTATION_RAG_DOCUMENT_MAX_CHARS:
            raise WorkloadError("invalid_rag_query", "Local RAG query is invalid.")
        query_vector = self.ollama.embed(self.config.embedding_model, [clean_query])[0]
        if (
            len(query_vector) != int(status["dimensions"])
            or any(not math.isfinite(float(value)) for value in query_vector)
        ):
            raise WorkloadError("embedding_shape", "Local embedding returned an invalid shape.")
        scored = []
        try:
            with (target / INDEX_FILE).open("rb") as stream:
                while line := stream.readline(WORKSTATION_RAG_INDEX_LINE_MAX_BYTES + 1):
                    if len(line) > WORKSTATION_RAG_INDEX_LINE_MAX_BYTES:
                        raise ValueError("RAG index line is too large")
                    item = json.loads(line)
                    if not isinstance(item, dict) or set(item) != {
                        "id", "title", "text", "source_url", "embedding",
                    }:
                        raise ValueError("RAG index document is invalid")
                    vector = [float(value) for value in item.pop("embedding")]
                    if (
                        len(vector) != int(status["dimensions"])
                        or any(not math.isfinite(value) for value in vector)
                    ):
                        raise ValueError("RAG index vector is invalid")
                    score = _cosine(query_vector, vector)
                    if not math.isfinite(score):
                        raise ValueError("RAG score is invalid")
                    scored.append((score, item))
            if len(scored) != int(status["documents"]):
                raise ValueError("RAG document count changed")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise WorkloadError("rag_read_failed", "Local RAG index could not be read.") from exc
        selected = sorted(scored, key=lambda pair: pair[0], reverse=True)[:max(1, min(int(top_k), WORKSTATION_RAG_TOP_K_MAX))]
        return {
            "results": [{**item, "score": round(score, 6), "citation": f"RAG:{item['id']}"} for score, item in selected],
            "bundle_version": status.get("bundle_version"),
        }
