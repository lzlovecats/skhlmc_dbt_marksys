from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from ai_model_config import LMC_AI_MODEL_PROFILE_VERSION
from workstation.manager.release_manifest import (
    canonical_json,
    validate_manifest,
    verify_compatibility,
    verify_envelope,
    verify_release_tree,
)
from workstation.manager.update import _safe_extract
from workstation.manager import update as update_module
from workstation.privileged_helper import server as privileged_server
from workstation.privileged_helper.server import PrivilegedHelper, _seal_release_tree
from workstation.manager.update import UpdateStager
from workstation.scripts import workstationctl
from workstation.scripts import verify_release_artifact
from workstation.version import (
    WORKSTATION_CONFIG_SCHEMA_VERSION,
    WORKSTATION_PROTOCOL_VERSION,
)
from workstation.workloads.errors import WorkloadError


NOW = 2_000_000_000


def _manifest():
    component = {
        "id": "bundle-v1",
        "r2_key": "private/workstation/bundle-v1.bin",
        "sha256": "a" * 64,
        "bytes": 123,
    }
    return validate_manifest({
        "schema_version": 1,
        "release_version": "1.2.3",
        "channel": "stable",
        "published_epoch": NOW,
        "expires_epoch": NOW + 86_400,
        "compatibility": {
            "protocol_version": WORKSTATION_PROTOCOL_VERSION,
            "config_schema_version": WORKSTATION_CONFIG_SCHEMA_VERSION,
            "website_min": "4.11.0",
            "website_max": "4.99.0",
            "model_profile_version": LMC_AI_MODEL_PROFILE_VERSION,
            "ubuntu_version": "24.04",
            "nvidia_driver_min": "550.1.0",
            "cuda_min": "12.1.0",
            "ollama_min": "0.5.0",
            "gpt_sovits_commit": "0123456789abcdef0123456789abcdef01234567",
            "database_migration_requirement": "20260722_0001",
        },
        "components": {
            "release_archive": {**component, "id": "release-v1"},
            "deb_package": {**component, "id": "deb-v1"},
            "model_bundle": {**component, "id": "model-v1"},
            "rag_bundle": {**component, "id": "rag-v1"},
        },
    }, now_epoch=NOW)


def _signed(tmp_path: Path):
    key = Ed25519PrivateKey.generate()
    public = tmp_path / "public.pem"
    public.write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    manifest = _manifest()
    envelope = {
        "manifest": manifest,
        "signature": base64.b64encode(key.sign(canonical_json(manifest))).decode(),
        "downloads": {"release_archive": "https://r2.example/release?signature=secret"},
    }
    return key, public, envelope


def test_ed25519_manifest_is_canonical_and_tampering_is_rejected(tmp_path):
    _key, public, envelope = _signed(tmp_path)
    manifest, url = verify_envelope(envelope, public, now_epoch=NOW)
    assert manifest["release_version"] == "1.2.3"
    assert url.startswith("https://r2.example/")
    envelope["manifest"]["components"]["release_archive"]["bytes"] += 1
    with pytest.raises(WorkloadError) as raised:
        verify_envelope(envelope, public, now_epoch=NOW)
    assert raised.value.code == "signature_invalid"


def test_manifest_expiry_and_runtime_compatibility_fail_closed(tmp_path):
    _key, public, envelope = _signed(tmp_path)
    with pytest.raises(WorkloadError) as raised:
        verify_envelope(envelope, public, now_epoch=NOW + 86_401)
    assert raised.value.code == "manifest_expired"
    manifest = _manifest()
    facts = {
        "website_version": "4.11.0",
        "ubuntu_version": "24.04",
        "nvidia_driver": "550.2.0",
        "cuda": "12.2.0",
        "ollama": "0.6.0",
        "gpt_sovits_commit": manifest["compatibility"]["gpt_sovits_commit"],
        "database_migration_requirement": "20260722_0001",
    }
    verify_compatibility(manifest, facts)
    with pytest.raises(WorkloadError) as raised:
        verify_compatibility(manifest, {**facts, "ollama": "0.4.0"})
    assert raised.value.code == "incompatible_release"


def test_release_tree_rejects_hash_mismatch_and_tar_symlink(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "app.py").write_text("safe\n")
    digest = hashlib.sha256((tree / "app.py").read_bytes()).hexdigest()
    (tree / "release-files.sha256").write_text(f"{digest}  app.py\n")
    assert verify_release_tree(tree)["files"] == 1
    nested = tree / "nested"
    nested.mkdir()
    (nested / "release-files.sha256").write_text("unsigned\n")
    with pytest.raises(WorkloadError) as raised:
        verify_release_tree(tree)
    assert raised.value.code == "release_tree_invalid"
    (nested / "release-files.sha256").unlink()
    nested.rmdir()
    tree.chmod(0o777)
    with pytest.raises(WorkloadError) as raised:
        verify_release_tree(tree)
    assert raised.value.code == "release_tree_invalid"
    tree.chmod(0o755)
    (tree / "app.py").chmod(0o666)
    with pytest.raises(WorkloadError) as raised:
        verify_release_tree(tree)
    assert raised.value.code == "release_tree_invalid"
    (tree / "app.py").chmod(0o644)
    (tree / "app.py").write_text("tampered\n")
    with pytest.raises(WorkloadError) as raised:
        verify_release_tree(tree)
    assert raised.value.code == "release_tree_invalid"

    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("escape")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        bundle.addfile(info)
    with pytest.raises(WorkloadError) as raised:
        _safe_extract(archive, tmp_path / "unpacked")
    assert raised.value.code == "release_archive_invalid"


def test_release_extract_counts_directories_and_files_together(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(update_module, "WORKSTATION_RELEASE_FILE_MAX", 2)
    archive = tmp_path / "too-many.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name in ("one", "two", "three"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            bundle.addfile(info)
    with pytest.raises(WorkloadError) as raised:
        _safe_extract(archive, tmp_path / "unpacked")
    assert raised.value.code == "release_archive_invalid"


def test_release_extract_accepts_standard_relative_root_directory(tmp_path):
    archive = tmp_path / "release.tar.gz"
    content = b"ready\n"
    with tarfile.open(archive, "w:gz") as bundle:
        # build_deb.sh archives ".", which emits this harmless root entry.
        root = tarfile.TarInfo("./")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        bundle.addfile(root)

        ready = tarfile.TarInfo("./release.ready")
        ready.size = len(content)
        ready.mode = 0o644
        bundle.addfile(ready, io.BytesIO(content))

    destination = tmp_path / "unpacked"
    _safe_extract(archive, destination)

    assert (destination / "release.ready").read_bytes() == content


def test_sealed_release_tree_is_root_owned_and_service_readable(
    tmp_path, monkeypatch,
):
    root = tmp_path / "release"
    nested = root / "workstation"
    nested.mkdir(parents=True, mode=0o750)
    regular = nested / "module.py"
    regular.write_text("value = 1\n", encoding="utf-8")
    regular.chmod(0o600)
    executable = nested / "tool"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    root.chmod(0o750)
    nested.chmod(0o750)
    ownership = []
    monkeypatch.setattr(
        "workstation.privileged_helper.server.os.chown",
        lambda path, uid, gid: ownership.append((Path(path), uid, gid)),
    )

    _seal_release_tree(root)

    assert root.stat().st_mode & 0o777 == 0o755
    assert nested.stat().st_mode & 0o777 == 0o755
    assert regular.stat().st_mode & 0o777 == 0o644
    assert executable.stat().st_mode & 0o777 == 0o755
    assert {item[0] for item in ownership} == {
        root, nested, regular, executable,
    }
    assert all(uid == 0 and gid == 0 for _path, uid, gid in ownership)


def test_pairing_waits_for_fresh_website_acceptance_receipt(
    tmp_path, monkeypatch,
):
    state = tmp_path / "state"
    state.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "schema_version": 1,
        "node": {
            "name": "Workstation",
            "server_url": "https://example.com",
            "token_file": str(tmp_path / "node-token"),
        },
        "paths": {
            "state": str(state),
            "cache": str(tmp_path / "cache"),
            "data": str(tmp_path / "data"),
            "releases": str(tmp_path / "releases"),
        },
        "power": {},
        "workloads": {},
        "gui": {},
    }), encoding="utf-8")
    helper = PrivilegedHelper(
        config_path=config_path,
        release_root=tmp_path / "releases",
        service_uid=__import__("os").getuid(),
        service_gid=__import__("os").getgid(),
    )
    receipt = state / "website.json"
    receipt.write_text(json.dumps({
        "website_version": "5.0.0",
        "database_migration_requirement": "20260722_0002",
        "checked_epoch": 200,
    }), encoding="utf-8")
    helper._wait_for_fresh_website_receipt(200)

    receipt.write_text(json.dumps({
        "website_version": "invalid",
        "database_migration_requirement": "20260722_0002",
        "checked_epoch": 200,
    }), encoding="utf-8")
    ticks = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(privileged_server, "WORKSTATION_PAIR_CONNECT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(privileged_server.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(privileged_server.time, "sleep", lambda _seconds: None)
    with pytest.raises(ValueError, match="not accepted"):
        helper._wait_for_fresh_website_receipt(200)


def test_root_helper_only_rolls_back_to_ledger_previous_and_consumes_it(
    tmp_path,
):
    release_root = tmp_path / "opt"
    releases = release_root / "releases"
    for version in ("0.8.0", "0.9.0", "1.0.0"):
        target = releases / version
        target.mkdir(parents=True)
        (target / "release.ready").touch()
        digest = hashlib.sha256((target / "release.ready").read_bytes()).hexdigest()
        (target / "release-files.sha256").write_text(
            f"{digest}  release.ready\n"
        )
    (release_root / "current").symlink_to(Path("releases") / "1.0.0")
    helper = PrivilegedHelper(
        config_path=tmp_path / "config.json",
        release_root=release_root,
        service_uid=501,
        service_gid=20,
    )
    state = {
        "current": "1.0.0",
        "previous": "0.9.0",
        "pending_health": False,
    }
    helper._read_release_state = lambda: dict(state)

    def save(value):
        state.clear()
        state.update(value)

    helper._release_state = save
    with pytest.raises(ValueError, match="previous"):
        helper._switch_release("0.8.0", rollback=True)
    result = helper._switch_release("0.9.0", rollback=True)
    assert result["_reload_helper"] is True
    assert state == {
        "current": "0.9.0",
        "previous": "",
        "rollback_from": "1.0.0",
        "pending_health": True,
        "last_action": "rollback",
    }
    with pytest.raises(ValueError, match="newer"):
        helper._switch_release("0.8.0")


def test_root_helper_rechecks_manager_idle_before_service_restart(
    tmp_path, monkeypatch,
):
    helper = PrivilegedHelper(
        config_path=tmp_path / "config.json",
        release_root=tmp_path / "opt",
        service_uid=501,
        service_gid=20,
    )
    calls = []

    def not_idle(**_kwargs):
        calls.append("idle_gate")
        raise ValueError("Workstation must be idle")

    monkeypatch.setattr(helper, "_require_manager_idle", not_idle)
    monkeypatch.setattr(
        "workstation.privileged_helper.server.subprocess.run",
        lambda *_args, **_kwargs: calls.append("restart"),
    )
    with pytest.raises(ValueError, match="idle"):
        helper.execute({
            "action": "restart_service",
            "service": "ollama.service",
        })
    assert calls == ["idle_gate"]


def test_r2_full_health_mismatch_still_calls_server_cleanup(tmp_path, monkeypatch):
    from workstation.manager import update as update_module

    config = SimpleNamespace(
        update=SimpleNamespace(
            auth_token_file=tmp_path / "token",
            manifest_url="https://workstation.example/api/lmc-ai/workstation/releases/stable",
        ),
        paths=SimpleNamespace(state=tmp_path / "state"),
    )
    finish_calls = []

    class Response:
        def __init__(self, *, payload=None, content=b""):
            self.content = (
                json.dumps(payload).encode("utf-8")
                if payload is not None else content
            )
            self.headers = {"content-length": str(len(self.content))}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            if self.content:
                yield self.content

    def stream(method, url, **kwargs):
        if url.endswith("/start"):
            return Response(payload={
                "claim": "claim-token",
                "upload": {
                    "url": "https://r2.example/upload?signature=secret",
                    "headers": {"Content-Type": "application/octet-stream"},
                },
                "download_url": "https://r2.example/download?signature=secret",
            })
        if url.endswith("/finish"):
            finish_calls.append(kwargs["json"]["claim"])
            return Response(payload={"deleted": True})
        if method == "GET":
            return Response(content=b"mismatch")
        return Response()

    monkeypatch.setattr(update_module, "read_secret", lambda _path: "node-token")
    monkeypatch.setattr(update_module.httpx, "stream", stream)
    with pytest.raises(WorkloadError) as raised:
        UpdateStager(config).r2_health_probe()
    assert raised.value.code == "r2_health_mismatch"
    assert finish_calls == ["claim-token"]


def test_failed_full_health_rolls_back_before_reporting_failure(
    tmp_path, monkeypatch, capsys,
):
    config = SimpleNamespace(update=SimpleNamespace(enabled=True))
    args = SimpleNamespace(
        config=tmp_path / "config.json",
        manager_socket=tmp_path / "manager.sock",
        privileged_socket=tmp_path / "privileged.sock",
    )
    privileged_actions = []
    manager_actions = []

    monkeypatch.setattr(workstationctl, "load_config", lambda _path: config)
    monkeypatch.setattr(
        workstationctl,
        "_request",
        lambda _socket, payload: manager_actions.append(payload["action"]) or {},
    )
    monkeypatch.setattr(workstationctl, "_wait_for_idle", lambda _args: None)
    monkeypatch.setattr(
        workstationctl.UpdateStager,
        "stage",
        lambda _self: {"update_available": True, "version": "9.9.9"},
    )

    def privileged(payload, _socket):
        privileged_actions.append(dict(payload))
        if payload["action"] == "switch_release":
            return {"previous": "1.2.3"}
        return {"ok": True}

    monkeypatch.setattr(workstationctl, "request_privileged", privileged)
    health_calls = []

    def health(_args):
        health_calls.append("check")
        if len(health_calls) == 1:
            raise WorkloadError("update_health_failed", "failed")
        return {"healthy": True}

    monkeypatch.setattr(workstationctl, "_wait_for_full_health", health)

    assert workstationctl.update_check(args) == 1
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "ok": False,
        "updated": False,
        "rolled_back": True,
        "error": "update_health_failed",
    }
    actions = [item["action"] for item in privileged_actions]
    assert actions[:2] == ["install_release", "switch_release"]
    assert {
        "action": "confirm_release", "version": "1.2.3",
    } in privileged_actions
    assert {
        "action": "confirm_release", "version": "9.9.9",
    } not in privileged_actions
    rollback_index = actions.index("rollback_release")
    assert all(
        item["version"] == "1.2.3"
        for item in privileged_actions
        if item["action"] == "rollback_release"
    )
    assert actions[rollback_index + 1:] == [
        "restart_service", "restart_service", "restart_service",
        "confirm_release",
    ]
    assert manager_actions == ["drain", "resume"]
    assert health_calls == ["check", "check"]


def test_automatic_rollback_health_failure_stays_drained(
    tmp_path, monkeypatch, capsys,
):
    config = SimpleNamespace(update=SimpleNamespace(enabled=True))
    args = SimpleNamespace(
        config=tmp_path / "config.json",
        manager_socket=tmp_path / "manager.sock",
        privileged_socket=tmp_path / "privileged.sock",
    )
    manager_actions = []
    monkeypatch.setattr(workstationctl, "load_config", lambda _path: config)
    monkeypatch.setattr(
        workstationctl, "_request",
        lambda _socket, payload: manager_actions.append(payload["action"]) or {},
    )
    monkeypatch.setattr(workstationctl, "_wait_for_idle", lambda _args: None)
    monkeypatch.setattr(
        workstationctl.UpdateStager, "stage",
        lambda _self: {"update_available": True, "version": "9.9.9"},
    )
    monkeypatch.setattr(
        workstationctl,
        "request_privileged",
        lambda payload, _socket: (
            {"previous": "1.2.3"}
            if payload["action"] == "switch_release" else {"ok": True}
        ),
    )
    monkeypatch.setattr(
        workstationctl, "_wait_for_full_health",
        lambda _args: (_ for _ in ()).throw(
            WorkloadError("update_health_failed", "failed")
        ),
    )
    assert workstationctl.update_check(args) == 1
    assert json.loads(capsys.readouterr().out)["rolled_back"] is True
    assert manager_actions == ["drain"]


def test_manual_rollback_drains_restarts_health_checks_then_resumes(
    tmp_path, monkeypatch, capsys,
):
    state = tmp_path / "state"
    state.mkdir()
    (state / "release").mkdir()
    (state / "release/release-state.json").write_text(json.dumps({
        "current": "9.9.9", "previous": "1.2.3",
    }))
    config = SimpleNamespace(paths=SimpleNamespace(state=state))
    args = SimpleNamespace(
        config=tmp_path / "config.json",
        manager_socket=tmp_path / "manager.sock",
        privileged_socket=tmp_path / "privileged.sock",
    )
    manager_actions = []
    privileged_actions = []
    monkeypatch.setattr(workstationctl, "load_config", lambda _path: config)
    monkeypatch.setattr(
        workstationctl,
        "_request",
        lambda _socket, payload: manager_actions.append(payload["action"]) or {},
    )
    monkeypatch.setattr(workstationctl, "_wait_for_idle", lambda _args: None)
    monkeypatch.setattr(
        workstationctl,
        "_wait_for_full_health",
        lambda _args: {"healthy": True, "checked_epoch": 12345},
    )
    monkeypatch.setattr(
        workstationctl,
        "request_privileged",
        lambda payload, _socket: privileged_actions.append(dict(payload)) or {"ok": True},
    )

    assert workstationctl.rollback_previous(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "rolled_back": True,
        "version": "1.2.3",
        "checked_epoch": 12345,
    }
    assert manager_actions == ["drain", "resume"]
    assert privileged_actions[0] == {
        "action": "rollback_release", "version": "1.2.3",
    }
    assert [item["action"] for item in privileged_actions[1:]] == [
        "restart_service", "restart_service", "restart_service",
        "confirm_release",
    ]
    assert privileged_actions[-1]["version"] == "1.2.3"


def test_manual_rollback_health_failure_stays_drained(
    tmp_path, monkeypatch, capsys,
):
    state = tmp_path / "state"
    state.mkdir()
    (state / "release").mkdir()
    (state / "release/release-state.json").write_text(json.dumps({"previous": "1.2.3"}))
    config = SimpleNamespace(paths=SimpleNamespace(state=state))
    args = SimpleNamespace(
        config=tmp_path / "config.json",
        manager_socket=tmp_path / "manager.sock",
        privileged_socket=tmp_path / "privileged.sock",
    )
    manager_actions = []
    monkeypatch.setattr(workstationctl, "load_config", lambda _path: config)
    monkeypatch.setattr(
        workstationctl,
        "_request",
        lambda _socket, payload: manager_actions.append(payload["action"]) or {},
    )
    monkeypatch.setattr(workstationctl, "_wait_for_idle", lambda _args: None)
    monkeypatch.setattr(
        workstationctl,
        "_wait_for_full_health",
        lambda _args: (_ for _ in ()).throw(
            WorkloadError("update_health_failed", "failed")
        ),
    )
    monkeypatch.setattr(
        workstationctl, "request_privileged", lambda *_args, **_kwargs: {"ok": True},
    )

    assert workstationctl.rollback_previous(args) == 1
    assert json.loads(capsys.readouterr().out)["rolled_back"] is True
    assert manager_actions == ["drain"]


def test_offline_deb_verifier_accepts_only_signed_component_bytes(
    tmp_path, monkeypatch, capsys,
):
    key, public, envelope = _signed(tmp_path)
    artifact = tmp_path / "package.deb"
    artifact.write_bytes(b"signed deb bytes")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    envelope["manifest"]["components"]["deb_package"].update({
        "sha256": digest,
        "bytes": artifact.stat().st_size,
    })
    envelope["manifest"]["published_epoch"] = int(time.time())
    envelope["manifest"]["expires_epoch"] = int(time.time()) + 86_400
    envelope["signature"] = base64.b64encode(
        key.sign(canonical_json(envelope["manifest"]))
    ).decode()
    signed = tmp_path / "signed.json"
    signed.write_bytes(canonical_json({
        "manifest": envelope["manifest"],
        "signature": envelope["signature"],
    }))
    monkeypatch.setattr("sys.argv", [
        "verify_release_artifact.py",
        "--envelope", str(signed),
        "--public-key", str(public),
        "--component", "deb_package",
        "--artifact", str(artifact),
    ])
    assert verify_release_artifact.main() == 0
    assert json.loads(capsys.readouterr().out)["sha256"] == digest
    artifact.write_bytes(b"tampered")
    with pytest.raises(SystemExit, match="verification failed"):
        verify_release_artifact.main()
