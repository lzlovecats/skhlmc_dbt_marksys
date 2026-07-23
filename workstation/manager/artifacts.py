"""Developer-approved signed model inventory and RAG bundle activation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time

from ai_model_config import lmc_ai_workstation_required_models
from system_limits import (
    WORKSTATION_MIN_FREE_DISK_BYTES,
    WORKSTATION_MODEL_BUNDLE_MANIFEST_MAX_BYTES,
    WORKSTATION_RAG_BUNDLE_MAX_BYTES,
)
from workstation.config import WorkstationConfig, read_secret
from workstation.manager.release_manifest import (
    verify_compatibility,
    verify_signed_manifest,
)
from workstation.manager.update import (
    UpdateStager,
    _bounded_get_json,
    _download,
    _https_endpoint,
    _safe_extract,
    collect_compatibility_facts,
)
from workstation.workloads.errors import WorkloadError
from workstation.workloads.ollama import OllamaAdapter
from workstation.workloads.rag import LocalRagIndex


_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)


class SignedArtifactManager:
    def __init__(self, config: WorkstationConfig, ollama: OllamaAdapter):
        self.config = config
        self.ollama = ollama
        self.rag = LocalRagIndex(config.workloads.rag, ollama)

    def _catalog(self, component_name: str) -> tuple[dict, dict, str]:
        token = read_secret(self.config.update.auth_token_file)
        url = _https_endpoint(
            self.config.update.manifest_url,
            f"/api/lmc-ai/workstation/artifacts/{self.config.update.channel}/{component_name}",
        )
        value = _bounded_get_json(url, token=token)
        if not isinstance(value, dict) or set(value) != {
            "manifest", "signature", "component", "download_url",
        } or value.get("component") != component_name:
            raise WorkloadError("artifact_manifest_invalid", "Signed artifact response is invalid.")
        manifest = verify_signed_manifest(
            value["manifest"], value["signature"], self.config.update.public_key_file,
        )
        if manifest["channel"] != self.config.update.channel:
            raise WorkloadError("channel_mismatch", "Signed artifact channel does not match.")
        verify_compatibility(manifest, collect_compatibility_facts(self.config))
        download_url = str(value.get("download_url") or "")
        _https_endpoint(download_url, "/")
        return manifest, manifest["components"][component_name], download_url

    def inspect(self) -> dict:
        result = {}
        for name in ("model_bundle", "rag_bundle"):
            _manifest, component, url = self._catalog(name)
            result[name] = {
                "id": component["id"],
                "bytes": component["bytes"],
                "sha256": component["sha256"],
            }
            if name == "model_bundle":
                approved, total_bytes = self._model_inventory(component, url)
                result[name]["model_bytes"] = total_bytes
                result[name]["models"] = [
                    {"name": model, "digest": details["digest"], "bytes": details["bytes"]}
                    for model, details in approved.items()
                ]
        _atomic_json(
            self.config.paths.state / "artifact-catalog.json",
            {"checked_epoch": int(time.time()), "components": result},
        )
        return result

    def _model_inventory(self, component: dict, url: str) -> tuple[dict, int]:
        if component["bytes"] > WORKSTATION_MODEL_BUNDLE_MANIFEST_MAX_BYTES:
            raise WorkloadError("model_bundle_too_large", "Model inventory bundle is too large.")
        root = self.config.paths.cache / "artifacts" / "model" / component["id"]
        if not _ID_RE.fullmatch(component["id"]):
            raise WorkloadError("artifact_id_invalid", "Model bundle identifier is invalid.")
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        path = root / "models.json"
        path.unlink(missing_ok=True)
        _download(
            url, path,
            expected_size=component["bytes"],
            expected_sha256=component["sha256"],
        )
        try:
            value = json.loads(path.read_bytes())
            models = value.get("models") if isinstance(value, dict) else None
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise WorkloadError("model_bundle_invalid", "Model inventory bundle is invalid.") from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "models"} or value.get("schema_version") != 1 or not isinstance(models, list):
            raise WorkloadError("model_bundle_invalid", "Model inventory bundle is invalid.")
        approved: dict[str, dict] = {}
        total_bytes = 0
        for item in models:
            if not isinstance(item, dict) or set(item) != {"name", "digest", "bytes"}:
                raise WorkloadError("model_bundle_invalid", "Model inventory entry is invalid.")
            name = str(item.get("name") or "")
            digest = str(item.get("digest") or "").lower()
            size = int(item.get("bytes") or 0)
            if name in approved or not re.fullmatch(r"[0-9a-f]{64}", digest) or size <= 0:
                raise WorkloadError("model_bundle_invalid", "Model inventory entry is invalid.")
            approved[name] = {"digest": digest, "bytes": size}
            total_bytes += size
        required = set(lmc_ai_workstation_required_models())
        if set(approved) != required:
            raise WorkloadError("model_bundle_invalid", "Model inventory does not exactly match the required profile.")
        return approved, total_bytes

    def approve_models(self, *, cancel_event: threading.Event) -> dict:
        _manifest, component, url = self._catalog("model_bundle")
        approved, total_bytes = self._model_inventory(component, url)
        installed = self.ollama.inventory()
        missing = [
            name for name, details in approved.items()
            if installed.get(name) != details["digest"]
        ]
        if missing:
            try:
                free = shutil.disk_usage(self.config.paths.data).free
            except OSError as exc:
                raise WorkloadError(
                    "disk_gate", "Model free disk space could not be verified."
                ) from exc
            required_bytes = sum(approved[name]["bytes"] for name in missing)
            if free - required_bytes < WORKSTATION_MIN_FREE_DISK_BYTES:
                raise WorkloadError(
                    "disk_gate",
                    "Approved models would breach the 20 GB free-space gate.",
                )
            for name in missing:
                if cancel_event.is_set():
                    raise WorkloadError("cancelled", "Approved model download was cancelled.")
                self.ollama.pull_approved(
                    name,
                    expected_digest=approved[name]["digest"],
                    cancel_event=cancel_event,
                )
        installed = self.ollama.inventory()
        if any(
            installed.get(name) != details["digest"]
            for name, details in approved.items()
        ):
            raise WorkloadError("model_digest_mismatch", "Installed Ollama models do not match the approved signed inventory.")
        receipt = {
            "id": component["id"],
            "sha256": component["sha256"],
            "approved_epoch": int(time.time()),
            "model_bytes": total_bytes,
            "models": approved,
        }
        _atomic_json(self.config.paths.data / "models" / "active-receipt.json", receipt)
        return receipt

    def install_rag(self, *, cancel_event: threading.Event) -> dict:
        self._require_approved_embedding_model()
        _manifest, component, url = self._catalog("rag_bundle")
        if component["bytes"] > WORKSTATION_RAG_BUNDLE_MAX_BYTES:
            raise WorkloadError("rag_bundle_too_large", "RAG bundle exceeds its safe limit.")
        identifier = component["id"]
        if not _ID_RE.fullmatch(identifier):
            raise WorkloadError("artifact_id_invalid", "RAG bundle identifier is invalid.")
        if cancel_event.is_set():
            raise WorkloadError("cancelled", "RAG installation was cancelled.")
        UpdateStager(self.config).r2_health_probe()
        try:
            if shutil.disk_usage(self.config.paths.data).free < WORKSTATION_MIN_FREE_DISK_BYTES:
                raise WorkloadError("disk_gate", "RAG installation requires at least 20 GB free disk space.")
        except OSError as exc:
            raise WorkloadError("disk_gate", "RAG free disk space could not be verified.") from exc
        staging = self.config.paths.cache / "artifacts" / "rag" / identifier
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, mode=0o750)
        archive = staging / "bundle.tar.gz"
        _download(
            url, archive,
            expected_size=component["bytes"],
            expected_sha256=component["sha256"],
        )
        source = staging / "source"
        _safe_extract(archive, source)
        if cancel_event.is_set():
            raise WorkloadError("cancelled", "RAG installation was cancelled.")
        rag_root = self.config.paths.data / "rag"
        versions = rag_root / "versions"
        versions.mkdir(parents=True, exist_ok=True, mode=0o750)
        destination = versions / identifier
        if destination.exists():
            receipt_file = destination / "component-receipt.json"
            try:
                existing = json.loads(receipt_file.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                existing = {}
            if existing.get("sha256") != component["sha256"]:
                raise WorkloadError("rag_version_conflict", "RAG version already exists with different content.")
            if not self.rag.validate_version(destination).get("ok"):
                raise WorkloadError("rag_version_conflict", "Existing RAG version failed its integrity check.")
        else:
            building = versions / f".{identifier}.building"
            if building.exists():
                shutil.rmtree(building)
            try:
                meta = self.rag.build(
                    source, building, bundle_version=identifier,
                    cancel_event=cancel_event,
                )
            except Exception:
                if building.exists():
                    shutil.rmtree(building)
                raise
            _atomic_json(building / "component-receipt.json", {
                "id": identifier, "sha256": component["sha256"], "index": meta,
            })
            if not self.rag.validate_version(building).get("ok"):
                shutil.rmtree(building)
                raise WorkloadError("rag_build_invalid", "Built RAG index failed its integrity check.")
            os.replace(building, destination)
        current = rag_root / "current"
        previous = ""
        try:
            old = current.readlink()
            if old.parent == Path("versions"):
                previous = old.name
        except OSError:
            pass
        temporary = rag_root / ".current.new"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(Path("versions") / identifier)
        os.replace(temporary, current)
        receipt = {
            "id": identifier,
            "sha256": component["sha256"],
            "activated_epoch": int(time.time()),
        }
        _atomic_json(rag_root / "active-receipt.json", receipt)
        _atomic_json(rag_root / "activation-state.json", {
            "current": identifier, "previous": previous,
        })
        try:
            shutil.rmtree(staging)
        except OSError:
            pass
        return receipt

    def _require_approved_embedding_model(self) -> None:
        model = self.config.workloads.rag.embedding_model
        receipt_path = self.config.paths.data / "models" / "active-receipt.json"
        try:
            if (
                receipt_path.is_symlink()
                or not receipt_path.is_file()
                or not 0 < receipt_path.stat().st_size <= 256 * 1024
            ):
                raise ValueError("model receipt is invalid")
            receipt = json.loads(receipt_path.read_bytes())
            approved = receipt.get("models") if isinstance(receipt, dict) else None
            if (
                not model
                or not isinstance(approved, dict)
                or set(approved) != set(lmc_ai_workstation_required_models())
            ):
                raise ValueError("embedding model is not approved")
            details = approved.get(model)
            if not isinstance(details, dict) or set(details) != {"digest", "bytes"}:
                raise ValueError("embedding model approval is invalid")
            digest = str(details.get("digest") or "").lower()
            if (
                not re.fullmatch(r"[0-9a-f]{64}", digest)
                or int(details.get("bytes") or 0) <= 0
                or self.ollama.inventory().get(model) != digest
            ):
                raise ValueError("embedding model does not match approval")
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkloadError(
                "rag_embedding_model_unapproved",
                "The approved RAG embedding model must be installed first.",
            ) from exc

    def rollback_rag(self) -> dict:
        rag_root = self.config.paths.data / "rag"
        try:
            state = json.loads((rag_root / "activation-state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise WorkloadError("rag_rollback_unavailable", "No RAG rollback state is available.") from exc
        previous = str(state.get("previous") or "")
        target = rag_root / "versions" / previous
        try:
            receipt = json.loads((target / "component-receipt.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise WorkloadError("rag_rollback_unavailable", "Previous RAG version is unavailable.") from exc
        if not _ID_RE.fullmatch(previous) or target.resolve(strict=True).parent != (rag_root / "versions").resolve(strict=True):
            raise WorkloadError("rag_rollback_unavailable", "Previous RAG version is invalid.")
        if not self.rag.validate_version(target).get("ok"):
            raise WorkloadError("rag_rollback_unavailable", "Previous RAG version failed its integrity check.")
        temporary = rag_root / ".current.new"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(Path("versions") / previous)
        os.replace(temporary, rag_root / "current")
        active = {
            "id": previous,
            "sha256": str(receipt.get("sha256") or ""),
            "activated_epoch": int(time.time()),
        }
        _atomic_json(rag_root / "active-receipt.json", active)
        _atomic_json(rag_root / "activation-state.json", {
            "current": previous, "previous": str(state.get("current") or ""),
        })
        return active
