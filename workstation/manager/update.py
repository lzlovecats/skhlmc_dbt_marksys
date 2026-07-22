"""Unprivileged signed-release staging and compatibility checks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlparse

import httpx

from system_limits import (
    WORKSTATION_RELEASE_ARCHIVE_MAX_BYTES,
    WORKSTATION_RELEASE_FILE_MAX,
    WORKSTATION_RELEASE_UNPACKED_MAX_BYTES,
    WORKSTATION_R2_HEALTH_API_MAX_BYTES,
    WORKSTATION_R2_HEALTH_PROBE_BYTES,
    WORKSTATION_UPDATE_MANIFEST_MAX_BYTES,
)
from workstation.config import WorkstationConfig, read_secret
from workstation.manager.release_manifest import (
    verify_compatibility,
    verify_envelope,
    verify_release_tree,
)
from workstation.version import WORKSTATION_VERSION
from workstation.workloads.errors import WorkloadError


def _bounded_get_json(url: str, *, token: str) -> dict:
    try:
        with httpx.stream(
            "GET",
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared > WORKSTATION_UPDATE_MANIFEST_MAX_BYTES:
                raise WorkloadError("manifest_too_large", "Release manifest exceeds its safe limit.")
            chunks = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > WORKSTATION_UPDATE_MANIFEST_MAX_BYTES:
                    raise WorkloadError("manifest_too_large", "Release manifest exceeds its safe limit.")
                chunks.append(chunk)
    except WorkloadError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise WorkloadError("manifest_unavailable", "Signed release manifest is unavailable.") from exc
    try:
        value = json.loads(b"".join(chunks))
    except (ValueError, json.JSONDecodeError) as exc:
        raise WorkloadError("manifest_invalid", "Release manifest is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise WorkloadError("manifest_invalid", "Release manifest is invalid.")
    return value


def _r2_health_json_request(method: str, url: str, **kwargs) -> dict:
    """Read a health-control response without allowing an unbounded body."""
    with httpx.stream(
        method,
        url,
        timeout=httpx.Timeout(30, connect=10),
        follow_redirects=False,
        **kwargs,
    ) as response:
        response.raise_for_status()
        declared = int(response.headers.get("content-length") or 0)
        if declared > WORKSTATION_R2_HEALTH_API_MAX_BYTES:
            raise WorkloadError(
                "r2_health_response_too_large",
                "R2 health-control response exceeded its safe limit.",
            )
        chunks = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > WORKSTATION_R2_HEALTH_API_MAX_BYTES:
                raise WorkloadError(
                    "r2_health_response_too_large",
                    "R2 health-control response exceeded its safe limit.",
                )
            chunks.append(chunk)
    value = json.loads(b"".join(chunks))
    if not isinstance(value, dict):
        raise ValueError("R2 health-control response must be an object")
    return value


def _download(url: str, destination: Path, *, expected_size: int, expected_sha256: str) -> None:
    if expected_size > WORKSTATION_RELEASE_ARCHIVE_MAX_BYTES:
        raise WorkloadError("release_too_large", "Release archive exceeds its safe limit.")
    digest = hashlib.sha256()
    total = 0
    try:
        with httpx.stream(
            "GET", url, timeout=httpx.Timeout(120, connect=10), follow_redirects=False,
        ) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared and declared != expected_size:
                raise WorkloadError("release_size_mismatch", "Release archive size does not match its signed manifest.")
            with destination.open("xb") as stream:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > expected_size or total > WORKSTATION_RELEASE_ARCHIVE_MAX_BYTES:
                        raise WorkloadError("release_too_large", "Release archive exceeds its signed size.")
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
    except WorkloadError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, httpx.HTTPError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        raise WorkloadError("release_download_failed", "Release archive download failed.") from exc
    if total != expected_size or digest.hexdigest() != expected_sha256:
        destination.unlink(missing_ok=True)
        raise WorkloadError("release_hash_mismatch", "Release archive hash does not match its signed manifest.")


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False, mode=0o750)
    members = 0
    total = 0
    try:
        with tarfile.open(archive, mode="r|gz") as bundle:
            for member in bundle:
                members += 1
                pure = PurePosixPath(member.name)
                if (
                    members > WORKSTATION_RELEASE_FILE_MAX
                    or pure.is_absolute()
                    or not member.name
                    or ".." in pure.parts
                    or not (member.isdir() or member.isfile())
                ):
                    raise WorkloadError("release_archive_invalid", "Release archive contains an unsafe entry.")
                if member.isfile():
                    total += max(0, int(member.size))
                    if total > WORKSTATION_RELEASE_UNPACKED_MAX_BYTES:
                        raise WorkloadError("release_archive_invalid", "Release archive exceeds unpack limits.")
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                resolved_parent = target.parent.resolve(strict=False)
                if destination.resolve() not in {resolved_parent, *resolved_parent.parents}:
                    raise WorkloadError("release_archive_invalid", "Release archive path escapes staging.")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o750)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                source = bundle.extractfile(member)
                if source is None:
                    raise WorkloadError("release_archive_invalid", "Release file could not be read.")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
    except WorkloadError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise WorkloadError("release_archive_invalid", "Release archive could not be unpacked.") from exc


def _command_version(command: list[str], pattern: str) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = re.search(pattern, (result.stdout or "") + "\n" + (result.stderr or ""), re.I)
    return match.group(1) if match else ""


def _application_version(value: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if not match:
        raise WorkloadError("release_version_invalid", "Workstation release version is invalid.")
    return tuple(int(item) for item in match.groups())


def collect_compatibility_facts(config: WorkstationConfig) -> dict:
    values = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"')
    except OSError:
        pass
    try:
        website_path = config.paths.state / "website.json"
        if (
            website_path.is_symlink()
            or not website_path.is_file()
            or not 0 < website_path.stat().st_size <= 64 * 1024
        ):
            raise ValueError("website receipt is invalid")
        website = json.loads(website_path.read_bytes())
        if not isinstance(website, dict):
            raise ValueError("website receipt is invalid")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        website = {}
    nvidia = _command_version(["nvidia-smi"], r"Driver Version:\s*([0-9]+(?:\.[0-9]+){1,2})")
    cuda = _command_version(["nvidia-smi"], r"CUDA Version:\s*([0-9]+(?:\.[0-9]+){1,2})")
    ollama = _command_version(["ollama", "--version"], r"([0-9]+\.[0-9]+\.[0-9]+)")
    try:
        commit_path = config.workloads.gpt_sovits.runtime_root / "APPROVED_COMMIT"
        if (
            commit_path.is_symlink()
            or not commit_path.is_file()
            or not 0 < commit_path.stat().st_size <= 4_096
        ):
            raise ValueError("GPT-SoVITS commit receipt is invalid")
        gpt_commit = commit_path.read_bytes().decode("ascii").strip().lower()
    except (OSError, RuntimeError, UnicodeError, ValueError):
        gpt_commit = ""
    return {
        "website_version": str(website.get("website_version") or ""),
        "database_migration_requirement": str(website.get("database_migration_requirement") or ""),
        "ubuntu_version": str(values.get("VERSION_ID") or ""),
        "nvidia_driver": nvidia,
        "cuda": cuda,
        "ollama": ollama,
        "gpt_sovits_commit": gpt_commit,
    }


def _verify_component_receipt(path: Path, component: dict) -> None:
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or not 0 < path.stat().st_size <= 64 * 1024
        ):
            raise ValueError("component receipt is invalid")
        receipt = json.loads(path.read_bytes())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkloadError("component_receipt_missing", "A pinned model or RAG receipt is unavailable.") from exc
    if (
        not isinstance(receipt, dict)
        or str(receipt.get("id") or "") != component["id"]
        or str(receipt.get("sha256") or "").lower() != component["sha256"]
    ):
        raise WorkloadError("component_receipt_mismatch", "A pinned model or RAG receipt does not match.")


def _https_endpoint(origin_url: str, path: str) -> str:
    parsed = urlparse(origin_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise WorkloadError("health_probe_invalid", "Workstation website URL is invalid.")
    return f"https://{parsed.netloc}{path}"


class UpdateStager:
    def __init__(self, config: WorkstationConfig):
        self.config = config

    @property
    def staging_root(self) -> Path:
        return self.config.paths.cache / "updates"

    def cleanup_staging(self, version: str) -> None:
        _application_version(version)
        root = self.staging_root.resolve(strict=False)
        target = (self.staging_root / version).resolve(strict=False)
        if target.parent != root:
            raise WorkloadError(
                "release_path_invalid", "Release staging path is invalid."
            )
        if target.exists():
            shutil.rmtree(target)

    def fetch_verified_manifest(self, *, now_epoch: int | None = None) -> tuple[dict, str, dict]:
        token = read_secret(self.config.update.auth_token_file)
        envelope = _bounded_get_json(self.config.update.manifest_url, token=token)
        manifest, download_url = verify_envelope(
            envelope, self.config.update.public_key_file, now_epoch=now_epoch,
        )
        if manifest["channel"] != self.config.update.channel:
            raise WorkloadError("channel_mismatch", "Release channel does not match Workstation configuration.")
        verify_compatibility(manifest, collect_compatibility_facts(self.config))
        _verify_component_receipt(
            self.config.paths.data / "models" / "active-receipt.json",
            manifest["components"]["model_bundle"],
        )
        _verify_component_receipt(
            self.config.paths.data / "rag" / "active-receipt.json",
            manifest["components"]["rag_bundle"],
        )
        return manifest, download_url, envelope

    def r2_health_probe(self) -> dict:
        token = read_secret(self.config.update.auth_token_file)
        data = os.urandom(WORKSTATION_R2_HEALTH_PROBE_BYTES)
        digest = hashlib.sha256(data).hexdigest()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        start_url = _https_endpoint(
            self.config.update.manifest_url,
            "/api/lmc-ai/workstation/health/r2/start",
        )
        finish_url = _https_endpoint(
            self.config.update.manifest_url,
            "/api/lmc-ai/workstation/health/r2/finish",
        )
        claim = ""
        finished = False

        def cleanup_probe() -> None:
            if not claim or finished:
                return
            try:
                _r2_health_json_request(
                    "POST",
                    finish_url,
                    headers=headers,
                    json={"claim": claim},
                )
            except Exception:
                pass

        try:
            payload = _r2_health_json_request(
                "POST",
                start_url,
                headers=headers,
                json={"sha256": digest, "byte_size": len(data)},
            )
            upload = payload.get("upload") if isinstance(payload, dict) else None
            claim = str(payload.get("claim") or "") if isinstance(payload, dict) else ""
            upload_url = str((upload or {}).get("url") or "")
            download_url = str(payload.get("download_url") or "") if isinstance(payload, dict) else ""
            _https_endpoint(upload_url, urlparse(upload_url).path)
            _https_endpoint(download_url, urlparse(download_url).path)
            with httpx.stream(
                "PUT",
                upload_url,
                headers=dict((upload or {}).get("headers") or {}),
                content=data,
                timeout=httpx.Timeout(30, connect=10),
                follow_redirects=False,
            ) as put:
                put.raise_for_status()
                response_bytes = 0
                for chunk in put.iter_bytes():
                    response_bytes += len(chunk)
                    if response_bytes > WORKSTATION_R2_HEALTH_API_MAX_BYTES:
                        raise WorkloadError(
                            "r2_health_response_too_large",
                            "R2 health upload response exceeded its safe limit.",
                        )
            downloaded = bytearray()
            with httpx.stream(
                "GET",
                download_url,
                headers={"Accept-Encoding": "identity"},
                timeout=httpx.Timeout(30, connect=10),
                follow_redirects=False,
            ) as get:
                get.raise_for_status()
                declared = int(get.headers.get("content-length") or 0)
                if declared and declared != len(data):
                    raise WorkloadError(
                        "r2_health_mismatch",
                        "R2 health download did not match its upload.",
                    )
                for chunk in get.iter_bytes():
                    downloaded.extend(chunk)
                    if len(downloaded) > len(data):
                        raise WorkloadError(
                            "r2_health_mismatch",
                            "R2 health download did not match its upload.",
                        )
            if bytes(downloaded) != data:
                raise WorkloadError("r2_health_mismatch", "R2 health download did not match its upload.")
            finish_payload = _r2_health_json_request(
                "POST",
                finish_url,
                headers=headers,
                json={"claim": claim},
            )
            if finish_payload.get("deleted") is not True:
                raise WorkloadError("r2_health_delete_failed", "R2 health object was not deleted.")
            finished = True
        except WorkloadError:
            cleanup_probe()
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            cleanup_probe()
            raise WorkloadError("r2_health_failed", "Direct-R2 health probe failed.") from exc
        destination = self.config.paths.state / "r2-health.json"
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"checked_epoch": int(time.time())}, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o640)
        os.replace(temporary, destination)
        return {"ok": True, "checked_epoch": int(time.time())}

    def stage(self, *, now_epoch: int | None = None) -> dict:
        manifest, download_url, envelope = self.fetch_verified_manifest(now_epoch=now_epoch)
        version = manifest["release_version"]
        if _application_version(version) <= _application_version(WORKSTATION_VERSION):
            return {"update_available": False, "version": version}
        self.r2_health_probe()
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        target = self.staging_root / version
        if target.parent.resolve() != self.staging_root.resolve():
            raise WorkloadError("release_path_invalid", "Release staging path is invalid.")
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(mode=0o750)
        try:
            archive = target / "release.tar.gz"
            component = manifest["components"]["release_archive"]
            _download(
                download_url,
                archive,
                expected_size=component["bytes"],
                expected_sha256=component["sha256"],
            )
            tree = target / "tree"
            _safe_extract(archive, tree)
            inventory = verify_release_tree(tree)
            envelope_file = target / "envelope.json"
            envelope_file.write_bytes(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n")
            os.chmod(envelope_file, 0o640)
        except Exception:
            self.cleanup_staging(version)
            raise
        return {
            "update_available": True,
            "version": version,
            "staging": str(target),
            "inventory": inventory,
        }
