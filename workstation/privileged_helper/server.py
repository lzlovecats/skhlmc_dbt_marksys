#!/usr/bin/env python3
"""Root-only allowlisted system action helper."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import grp
import json
import os
from pathlib import Path
import pwd
import re
import socket
import struct
import subprocess
import tempfile
import hashlib
import shutil
import time

from workstation.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_CREDENTIALS_DIR,
    DEFAULT_RELEASE_ROOT,
    RELEASE_STATE_RELATIVE_PATH,
    load_config,
    parse_config,
    read_secret,
)
from workstation.manager.release_manifest import verify_envelope, verify_release_tree
from workstation.manager.update import _application_version, _safe_extract
from workstation.privileged_helper.protocol import PrivilegedRequestError, validate_request
from workstation.workloads.errors import WorkloadError
from system_limits import (
    WORKSTATION_PAIR_CONNECT_TIMEOUT_SECONDS,
    WORKSTATION_RELEASE_ARCHIVE_MAX_BYTES,
    WORKSTATION_UPDATE_MANIFEST_MAX_BYTES,
)


DEFAULT_SOCKET = Path("/run/lmc-ai-workstation/privileged.sock")
SERVICE_USER = "lmc-ai"
SERVICE_GROUP = "lmc-ai"
LEGACY_NODE_SERVICE = "skhlmc-lmc-ai-node.service"
MAX_FRAME_BYTES = 8_192


def _atomic_json(path: Path, value: dict, *, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def _atomic_secret(path: Path, value: str, *, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def _seal_release_tree(root: Path) -> None:
    """Make verified application code immutable and readable by services."""
    entries = list(root.rglob("*"))
    for path in entries:
        if path.is_symlink():
            raise PrivilegedRequestError(
                "verified release unexpectedly contains a symlink"
            )
        if path.is_dir():
            mode = 0o755
        elif path.is_file():
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
        else:
            raise PrivilegedRequestError(
                "verified release contains an unsupported entry"
            )
        os.chown(path, 0, 0)
        os.chmod(path, mode)
    os.chown(root, 0, 0)
    os.chmod(root, 0o755)


class PrivilegedHelper:
    def __init__(self, *, config_path: Path, release_root: Path, service_uid: int, service_gid: int):
        self.config_path = config_path
        self.release_root = release_root
        self.service_uid = service_uid
        self.service_gid = service_gid

    def _config_json(self) -> dict:
        # Parse once through the typed contract, then preserve the raw object so
        # an allowlisted edit does not erase unrelated reviewed fields.
        load_config(self.config_path)
        try:
            if (
                self.config_path.is_symlink()
                or not self.config_path.is_file()
                or not 0 < self.config_path.stat().st_size <= 256 * 1024
            ):
                raise ValueError("Workstation config is invalid")
            value = json.loads(self.config_path.read_bytes())
        except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise PrivilegedRequestError("Workstation config is invalid") from exc
        if not isinstance(value, dict):
            raise PrivilegedRequestError("Workstation config is invalid")
        return value

    @staticmethod
    def _require_manager_idle(*, allow_draining: bool = False) -> None:
        try:
            from workstation.manager.ipc import ManagerClient

            snapshot = asyncio.run(ManagerClient().request({"action": "snapshot"}))
            manager = snapshot.get("manager") if isinstance(snapshot, dict) else None
        except Exception as exc:
            raise PrivilegedRequestError("Manager state is unavailable") from exc
        if not isinstance(manager, dict) or any((
            str(manager.get("mode") or "") != "idle",
            bool(manager.get("active_operation")),
            bool(manager.get("voice_session_active")),
            bool(manager.get("voice_session_pending")),
            bool(manager.get("draining")) and not allow_draining,
        )):
            raise PrivilegedRequestError(
                "Workstation must be idle before reconfiguration"
            )

    def _release_state(self, value: dict) -> None:
        config = load_config(self.config_path)
        _atomic_json(
            config.paths.state / RELEASE_STATE_RELATIVE_PATH,
            value,
            mode=0o640,
            uid=0,
            gid=self.service_gid,
        )

    def _read_release_state(self) -> dict:
        config = load_config(self.config_path)
        try:
            path = config.paths.state / RELEASE_STATE_RELATIVE_PATH
            stat = path.stat()
            if (
                path.is_symlink()
                or not path.is_file()
                or stat.st_uid != 0
                or not 0 < stat.st_size <= 64 * 1024
            ):
                raise ValueError("release state is invalid")
            value = json.loads(path.read_bytes())
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _current_version(self) -> str:
        current = self.release_root / "current"
        try:
            target = current.readlink()
        except OSError:
            return ""
        return target.name if target.parent == Path("releases") else ""

    def _wait_for_fresh_website_receipt(self, started_epoch: int) -> None:
        config = load_config(self.config_path)
        receipt = config.paths.state / "website.json"
        deadline = time.monotonic() + WORKSTATION_PAIR_CONNECT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                status = receipt.stat()
                if (
                    receipt.is_symlink()
                    or not receipt.is_file()
                    or status.st_uid != self.service_uid
                    or not 0 < status.st_size <= 64 * 1024
                ):
                    raise ValueError("website receipt file is invalid")
                value = json.loads(receipt.read_bytes())
                checked = int(value.get("checked_epoch") or 0)
                if (
                    isinstance(value, dict)
                    and checked >= started_epoch
                    and re.fullmatch(
                        r"[0-9]+\.[0-9]+\.[0-9]+",
                        str(value.get("website_version") or ""),
                    )
                    and re.fullmatch(
                        r"[0-9]{8}_[0-9]{4}",
                        str(value.get("database_migration_requirement") or ""),
                    )
                ):
                    return
            except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
                pass
            time.sleep(0.5)
        raise PrivilegedRequestError(
            "new Workstation node was not accepted by the website"
        )

    def _install_release(self, version: str) -> dict:
        config = load_config(self.config_path)
        staging = config.paths.cache / "updates" / version
        if staging.parent.resolve(strict=True) != (config.paths.cache / "updates").resolve(strict=True):
            raise PrivilegedRequestError("release staging path is invalid")
        envelope_path = staging / "envelope.json"
        archive = staging / "release.tar.gz"
        if (
            envelope_path.is_symlink()
            or not envelope_path.is_file()
            or not 0 < envelope_path.stat().st_size
            <= WORKSTATION_UPDATE_MANIFEST_MAX_BYTES
        ):
            raise PrivilegedRequestError("release envelope is invalid")
        envelope_bytes = envelope_path.read_bytes()
        envelope = json.loads(envelope_bytes)
        manifest, _download_url = verify_envelope(
            envelope, config.update.public_key_file,
        )
        component = manifest["components"]["release_archive"]
        if (
            manifest["release_version"] != version
            or component["bytes"] > WORKSTATION_RELEASE_ARCHIVE_MAX_BYTES
            or archive.is_symlink()
            or not archive.is_file()
        ):
            raise PrivilegedRequestError("release archive does not match signed manifest")
        releases = self.release_root / "releases"
        releases.mkdir(parents=True, exist_ok=True, mode=0o755)
        destination = releases / version
        if destination.parent.resolve(strict=True) != releases.resolve(strict=True):
            raise PrivilegedRequestError("release destination is invalid")
        if destination.exists():
            if (destination / "release.ready").is_file():
                verify_release_tree(destination)
                return {"ok": True, "version": version, "installed": False}
        temporary = releases / f".{version}.installing"
        archive_copy = releases / f".{version}.archive"
        if temporary.exists():
            shutil.rmtree(temporary)
        archive_copy.unlink(missing_ok=True)
        try:
            digest = hashlib.sha256()
            size = 0
            with archive.open("rb") as source, archive_copy.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if (
                        size > component["bytes"]
                        or size > WORKSTATION_RELEASE_ARCHIVE_MAX_BYTES
                    ):
                        raise PrivilegedRequestError(
                            "release archive exceeds its signed size"
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if (
                size != component["bytes"]
                or digest.hexdigest() != component["sha256"]
            ):
                raise PrivilegedRequestError(
                    "release archive does not match signed manifest"
                )
            os.chmod(archive_copy, 0o600)
            _safe_extract(archive_copy, temporary)
            verify_release_tree(temporary)
            if not (temporary / "release.ready").is_file():
                raise PrivilegedRequestError(
                    "verified release readiness marker is missing"
                )
            _seal_release_tree(temporary)
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(temporary, destination)
        finally:
            archive_copy.unlink(missing_ok=True)
            if temporary.exists():
                shutil.rmtree(temporary)
        return {"ok": True, "version": version, "installed": True}

    def _switch_release(self, version: str, *, rollback: bool = False) -> dict:
        release = self.release_root / "releases" / version
        resolved = release.resolve(strict=True)
        releases = (self.release_root / "releases").resolve(strict=True)
        if resolved.parent != releases or not (resolved / "release.ready").is_file():
            raise PrivilegedRequestError("release is not verified and ready")
        verify_release_tree(resolved)
        previous = self._current_version()
        state = self._read_release_state()
        if rollback:
            if (
                not previous
                or state.get("current") != previous
                or state.get("previous") != version
            ):
                raise PrivilegedRequestError("rollback target is not the previous release")
        elif previous and _application_version(version) <= _application_version(previous):
            raise PrivilegedRequestError("release switch must move to a newer version")
        current = self.release_root / "current"
        temporary = self.release_root / ".current.new"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(Path("releases") / version)
        os.replace(temporary, current)
        if rollback:
            next_state = {
                "current": version,
                "previous": "",
                "rollback_from": previous,
                "pending_health": True,
                "last_action": "rollback",
            }
        else:
            next_state = {
                "current": version,
                "previous": previous,
                "pending_health": True,
                "last_action": "switch",
            }
        self._release_state(next_state)
        return {
            "ok": True,
            "version": version,
            "previous": previous,
            "_reload_helper": True,
        }

    def execute(self, request: dict) -> dict:
        action = request["action"]
        if action == "suspend":
            subprocess.run(["rtcwake", "--mode", "no", "--time", str(request["wake_epoch"])], check=True, timeout=20)
            subprocess.run(["systemctl", "suspend"], check=True, timeout=30)
            return {"ok": True}
        if action == "reboot":
            subprocess.run(["systemctl", "reboot"], check=True, timeout=30)
            return {"ok": True}
        if action == "trigger_update":
            subprocess.run(
                ["systemctl", "start", "--no-block", "lmc-ai-update.service"],
                check=True,
                timeout=30,
            )
            return {"ok": True}
        if action == "trigger_rollback":
            subprocess.run(
                ["systemctl", "start", "--no-block", "lmc-ai-rollback.service"],
                check=True,
                timeout=30,
            )
            return {"ok": True}
        if action == "restart_service":
            self._require_manager_idle(allow_draining=True)
            subprocess.run(["systemctl", "restart", request["service"]], check=True, timeout=60)
            return {"ok": True}
        if action == "set_service_state":
            subprocess.run(
                ["systemctl", request["state"], request["service"]],
                check=True,
                timeout=60,
            )
            return {"ok": True}
        if action == "install_release":
            return self._install_release(request["version"])
        if action == "switch_release":
            return self._switch_release(request["version"])
        if action == "rollback_release":
            return self._switch_release(request["version"], rollback=True)
        if action == "confirm_release":
            state = self._read_release_state()
            if (
                self._current_version() != request["version"]
                or state.get("current") != request["version"]
                or state.get("pending_health") is not True
            ):
                raise PrivilegedRequestError("release confirmation does not match current")
            state["pending_health"] = False
            state["last_action"] = "healthy"
            state["confirmed_epoch"] = int(time.time())
            self._release_state(state)
            return {"ok": True, "version": request["version"]}
        config = self._config_json()
        if action == "set_power_schedule":
            config["power"] = {key: request[key] for key in ("enabled", "timezone", "suspend_at", "wake_at")}
            parse_config(config)
            _atomic_json(self.config_path, config, mode=0o640, uid=0, gid=self.service_gid)
            return {"ok": True}
        if action == "set_power_override":
            config_value = load_config(self.config_path)
            _atomic_json(
                config_value.paths.state / "power-override.json",
                {"schema_version": 1, "until_epoch": request["until_epoch"]},
                mode=0o640,
                uid=self.service_uid,
                gid=self.service_gid,
            )
            return {"ok": True, "until_epoch": request["until_epoch"]}
        if action == "set_update_channel":
            self._require_manager_idle()
            update = config.setdefault("update", {})
            old_url = str(update.get("manifest_url") or "")
            parsed = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(old_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise PrivilegedRequestError("configured update URL is invalid")
            update.update({
                "enabled": True,
                "channel": request["channel"],
                "manifest_url": (
                    f"https://{parsed.netloc}/api/lmc-ai/workstation/releases/"
                    f"{request['channel']}"
                ),
            })
            parse_config(config)
            _atomic_json(self.config_path, config, mode=0o640, uid=0, gid=self.service_gid)
            subprocess.run(
                ["systemctl", "restart", "lmc-ai-manager.service"],
                check=True,
                timeout=60,
            )
            return {"ok": True, "channel": request["channel"]}
        if action == "pair_node":
            from urllib.parse import urlparse

            self._require_manager_idle()
            token_path = DEFAULT_CREDENTIALS_DIR / "node-token"
            old_config = json.loads(json.dumps(config))
            try:
                old_token = read_secret(token_path)
            except (OSError, ValueError):
                old_token = ""
            node_active = subprocess.run(
                ["systemctl", "is-active", "--quiet", "lmc-ai-node.service"],
                check=False, timeout=20,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode == 0
            node_enabled = subprocess.run(
                ["systemctl", "is-enabled", "--quiet", "lmc-ai-node.service"],
                check=False, timeout=20,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode == 0
            legacy_active = subprocess.run(
                ["systemctl", "is-active", "--quiet", LEGACY_NODE_SERVICE],
                check=False,
                timeout=20,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0
            legacy_enabled = subprocess.run(
                ["systemctl", "is-enabled", "--quiet", LEGACY_NODE_SERVICE],
                check=False,
                timeout=20,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0
            config.setdefault("node", {}).update({"name": request["name"], "server_url": request["server_url"], "token_file": str(token_path)})
            parsed_server = urlparse(request["server_url"])
            config["update"] = {
                "enabled": True,
                "channel": "stable",
                "manifest_url": (
                    f"https://{parsed_server.netloc}"
                    "/api/lmc-ai/workstation/releases/stable"
                ),
                "public_key_file": (
                    "/usr/share/lmc-ai-workstation/"
                    "release-signing-public-key.pem"
                ),
                "auth_token_file": str(token_path),
            }
            parse_config(config)
            try:
                subprocess.run(
                    ["systemctl", "stop", "lmc-ai-node.service"],
                    check=True,
                    timeout=60,
                )
                _atomic_secret(
                    token_path, request["token"],
                    uid=self.service_uid, gid=self.service_gid,
                )
                _atomic_json(
                    self.config_path, config,
                    mode=0o640, uid=0, gid=self.service_gid,
                )
                subprocess.run(
                    ["systemctl", "disable", "--now", LEGACY_NODE_SERVICE],
                    check=False,
                    timeout=60,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.run(
                    ["systemctl", "enable", "lmc-ai-node.service"],
                    check=True,
                    timeout=60,
                )
                subprocess.run(
                    ["systemctl", "restart", "lmc-ai-manager.service"],
                    check=True,
                    timeout=60,
                )
                website_receipt = (
                    load_config(self.config_path).paths.state / "website.json"
                )
                website_receipt.unlink(missing_ok=True)
                started_epoch = int(time.time())
                subprocess.run(
                    ["systemctl", "start", "lmc-ai-node.service"],
                    check=True,
                    timeout=60,
                )
                self._wait_for_fresh_website_receipt(started_epoch)
            except (
                OSError, subprocess.SubprocessError, PrivilegedRequestError,
            ):
                _atomic_json(
                    self.config_path, old_config,
                    mode=0o640, uid=0, gid=self.service_gid,
                )
                if old_token:
                    _atomic_secret(
                        token_path, old_token,
                        uid=self.service_uid, gid=self.service_gid,
                    )
                else:
                    token_path.unlink(missing_ok=True)
                subprocess.run(
                    ["systemctl", "restart", "lmc-ai-manager.service"],
                    check=False, timeout=60,
                )
                if node_active:
                    subprocess.run(
                        ["systemctl", "restart", "lmc-ai-node.service"],
                        check=False, timeout=60,
                    )
                else:
                    subprocess.run(
                        ["systemctl", "stop", "lmc-ai-node.service"],
                        check=False, timeout=30,
                    )
                if not node_enabled:
                    subprocess.run(
                        ["systemctl", "disable", "lmc-ai-node.service"],
                        check=False, timeout=30,
                    )
                if legacy_enabled:
                    subprocess.run(
                        ["systemctl", "enable", LEGACY_NODE_SERVICE],
                        check=False,
                        timeout=30,
                    )
                if legacy_active:
                    subprocess.run(
                        ["systemctl", "start", LEGACY_NODE_SERVICE],
                        check=False,
                        timeout=30,
                    )
                raise
            return {
                "ok": True,
                "legacy_node_stopped": legacy_active,
                "website_accepted": True,
            }
        raise PrivilegedRequestError("action is not implemented")


def _peer_uid(connection: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        return -1
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)[1]


def serve(socket_path: Path, helper: PrivilegedHelper) -> bool:
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o660)
    os.chown(socket_path, 0, helper.service_gid)
    server.listen(8)
    try:
        while True:
            connection, _address = server.accept()
            with connection:
                if _peer_uid(connection) not in {0, helper.service_uid}:
                    connection.sendall(b'{"ok":false,"code":"forbidden"}\n')
                    continue
                raw = b""
                while not raw.endswith(b"\n") and len(raw) <= MAX_FRAME_BYTES:
                    chunk = connection.recv(min(4_096, MAX_FRAME_BYTES + 1 - len(raw)))
                    if not chunk:
                        break
                    raw += chunk
                try:
                    if not raw.endswith(b"\n") or len(raw) > MAX_FRAME_BYTES:
                        raise PrivilegedRequestError("request frame is invalid")
                    request = validate_request(json.loads(raw))
                    response = helper.execute(request)
                except (PrivilegedRequestError, WorkloadError, ValueError, TypeError, json.JSONDecodeError):
                    response = {"ok": False, "code": "invalid_request"}
                except (OSError, subprocess.SubprocessError):
                    response = {"ok": False, "code": "action_failed"}
                reload_helper = bool(response.pop("_reload_helper", False))
                connection.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
                if reload_helper:
                    return True
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="LMC AI Workstation privileged helper")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--release-root", type=Path, default=DEFAULT_RELEASE_ROOT)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("privileged helper must run as root")
    service_uid = pwd.getpwnam(SERVICE_USER).pw_uid
    service_gid = grp.getgrnam(SERVICE_GROUP).gr_gid
    reload_helper = serve(
        args.socket,
        PrivilegedHelper(
            config_path=args.config,
            release_root=args.release_root,
            service_uid=service_uid,
            service_gid=service_gid,
        ),
    )
    # A controlled non-zero exit makes systemd reload this root boundary from
    # the newly selected immutable release after the response has been sent.
    return 75 if reload_helper else 0


if __name__ == "__main__":
    raise SystemExit(main())
