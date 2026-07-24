#!/usr/bin/env python3
"""Bounded operator commands used by systemd timers and diagnostics."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import getpass
import json
import os
from pathlib import Path
import sys
import time

from workstation.config import (
    DEFAULT_CONFIG_PATH,
    RELEASE_STATE_RELATIVE_PATH,
    load_config,
)
from workstation.manager.ipc import DEFAULT_MANAGER_SOCKET, ManagerClient
from workstation.manager.power import decide_power_action, read_power_override
from workstation.privileged_helper.client import (
    PrivilegedActionError,
    request_privileged,
)
from workstation.manager.update import UpdateStager
from workstation.workloads.errors import WorkloadError
from system_limits import (
    WORKSTATION_UPDATE_DRAIN_TIMEOUT_SECONDS,
    WORKSTATION_UPDATE_HEALTH_TIMEOUT_SECONDS,
)


def _request(socket_path: Path, payload: dict) -> dict:
    return asyncio.run(ManagerClient(socket_path).request(payload))


def status(args) -> int:
    result = _request(args.manager_socket, {"action": "snapshot"})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def health(args) -> int:
    result = _request(args.manager_socket, {
        "action": "health",
        "force": True,
        "full": bool(getattr(args, "full", False)),
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("healthy") else 1


def manager_action(args) -> int:
    result = _request(args.manager_socket, {"action": args.command})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def power_check(args) -> int:
    config = load_config(args.config)
    result = _request(args.manager_socket, {"action": "snapshot"})
    decision = decide_power_action(
        config.power,
        result.get("manager") or {},
        override_until_epoch=read_power_override(config.paths.state),
    )
    print(json.dumps(decision.__dict__, ensure_ascii=False, separators=(",", ":")))
    if decision.action == "suspend":
        request_privileged({"action": "suspend", "wake_epoch": decision.wake_epoch}, args.privileged_socket)
    return 0


def reboot_check(args) -> int:
    marker = Path("/var/run/reboot-required")
    if not marker.exists():
        return 0
    result = _request(args.manager_socket, {"action": "snapshot"})
    manager = result.get("manager") or {}
    busy = bool(
        manager.get("active_operation")
        or manager.get("voice_session_active")
        or manager.get("voice_session_pending")
        or manager.get("sleep_inhibited")
        or manager.get("mode") not in {"idle", None}
    )
    if busy:
        print("Security update reboot delayed because managed work is active.")
        return 0
    request_privileged({"action": "reboot"}, args.privileged_socket)
    return 0


def _restart_release_services(args) -> None:
    for service in (
        "lmc-ai-manager.service", "lmc-ai-node.service",
    ):
        deadline = time.monotonic() + 20
        while True:
            try:
                request_privileged(
                    {"action": "restart_service", "service": service},
                    args.privileged_socket,
                )
                break
            except (OSError, PrivilegedActionError, WorkloadError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)


@contextmanager
def _release_operation_lock(args):
    path = getattr(
        args,
        "release_lock",
        args.manager_socket.parent / "release-operation.lock",
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkloadError(
                "release_operation_busy",
                "Another Workstation release operation is already active.",
            ) from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _wait_for_idle(args) -> None:
    deadline = time.monotonic() + WORKSTATION_UPDATE_DRAIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = _request(args.manager_socket, {"action": "snapshot"})
        manager = result.get("manager") or {}
        if (
            not manager.get("active_operation")
            and not manager.get("voice_session_active")
            and not manager.get("voice_session_pending")
            and not manager.get("sleep_inhibited")
            and manager.get("mode") in {"idle", None}
        ):
            return
        time.sleep(2)
    raise WorkloadError("update_drain_timeout", "Workstation did not become idle before the update deadline.")


def _wait_for_full_health(args) -> dict:
    deadline = time.monotonic() + WORKSTATION_UPDATE_HEALTH_TIMEOUT_SECONDS
    last = {}
    while time.monotonic() < deadline:
        try:
            last = _request(
                args.manager_socket,
                {"action": "health", "force": True, "full": True},
            )
            if last.get("healthy"):
                return last
        except (OSError, WorkloadError):
            pass
        time.sleep(5)
    raise WorkloadError("update_health_failed", "Updated release did not pass full health in time.")


def _update_check(args) -> int:
    config = load_config(args.config)
    if not config.update.enabled:
        print(json.dumps({"ok": True, "updated": False, "reason": "disabled"}))
        return 0
    previous = ""
    switched = False
    rolled_back = False
    rollback_healthy = False
    staged_version = ""
    stager = None
    try:
        _request(args.manager_socket, {"action": "drain"})
        _wait_for_idle(args)
        stager = UpdateStager(config)
        staged = stager.stage()
        if not staged.get("update_available"):
            _request(args.manager_socket, {"action": "resume"})
            print(json.dumps({"ok": True, "updated": False, "version": staged.get("version")}))
            return 0
        version = str(staged["version"])
        staged_version = version
        request_privileged(
            {"action": "install_release", "version": version},
            args.privileged_socket,
        )
        switched_result = request_privileged(
            {"action": "switch_release", "version": version},
            args.privileged_socket,
        )
        previous = str(switched_result.get("previous") or "")
        switched = True
        _restart_release_services(args)
        health_report = _wait_for_full_health(args)
        request_privileged(
            {"action": "confirm_release", "version": version},
            args.privileged_socket,
        )
        _request(args.manager_socket, {"action": "resume"})
        print(json.dumps({
            "ok": True, "updated": True, "version": version,
            "checked_epoch": health_report.get("checked_epoch"),
        }))
        return 0
    except Exception as exc:
        if switched and previous:
            try:
                request_privileged(
                    {"action": "rollback_release", "version": previous},
                    args.privileged_socket,
                )
                _restart_release_services(args)
                rolled_back = True
                _wait_for_full_health(args)
                request_privileged(
                    {"action": "confirm_release", "version": previous},
                    args.privileged_socket,
                )
                rollback_healthy = True
            except Exception:
                pass
        if not switched or rollback_healthy:
            try:
                _request(args.manager_socket, {"action": "resume"})
            except Exception:
                pass
        print(json.dumps({
            "ok": False,
            "updated": False,
            "rolled_back": rolled_back,
            "error": getattr(exc, "code", "update_failed"),
        }))
        return 1
    finally:
        if stager is not None and staged_version:
            try:
                stager.cleanup_staging(staged_version)
            except Exception:
                pass


def update_check(args) -> int:
    try:
        with _release_operation_lock(args):
            return _update_check(args)
    except WorkloadError as exc:
        print(json.dumps({
            "ok": False,
            "updated": False,
            "rolled_back": False,
            "error": exc.code,
        }))
        return 1


def _rollback_previous(args) -> int:
    config = load_config(args.config)
    switched = False
    try:
        _request(args.manager_socket, {"action": "drain"})
        _wait_for_idle(args)
        try:
            path = config.paths.state / RELEASE_STATE_RELATIVE_PATH
            if (
                path.is_symlink()
                or not path.is_file()
                or not 0 < path.stat().st_size <= 64 * 1024
            ):
                raise ValueError("release state is invalid")
            state = json.loads(path.read_bytes())
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise WorkloadError(
                "rollback_state_unavailable",
                "The previous release state is unavailable.",
            ) from exc
        previous = str(state.get("previous") or "") if isinstance(state, dict) else ""
        if not previous:
            raise WorkloadError(
                "rollback_unavailable", "No previous verified release is available."
            )
        request_privileged(
            {"action": "rollback_release", "version": previous},
            args.privileged_socket,
        )
        switched = True
        _restart_release_services(args)
        health_report = _wait_for_full_health(args)
        request_privileged(
            {"action": "confirm_release", "version": previous},
            args.privileged_socket,
        )
        _request(args.manager_socket, {"action": "resume"})
        print(json.dumps({
            "ok": True,
            "rolled_back": True,
            "version": previous,
            "checked_epoch": health_report.get("checked_epoch"),
        }))
        return 0
    except Exception as exc:
        if not switched:
            try:
                _request(args.manager_socket, {"action": "resume"})
            except Exception:
                pass
        print(json.dumps({
            "ok": False,
            "rolled_back": switched,
            "error": getattr(exc, "code", "rollback_failed"),
        }))
        return 1


def rollback_previous(args) -> int:
    try:
        with _release_operation_lock(args):
            return _rollback_previous(args)
    except WorkloadError as exc:
        print(json.dumps({
            "ok": False,
            "rolled_back": False,
            "error": exc.code,
        }))
        return 1


_ARTIFACT_ACTIONS = {
    "artifact-inspect": "artifacts.inspect",
    "model-approve": "model.approve",
    "rag-install": "rag.install",
    "rag-rollback": "rag.rollback",
}


def artifact_action(args) -> int:
    result = _request(
        args.manager_socket, {"action": _ARTIFACT_ACTIONS[args.command]},
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def dataset_prepare(args) -> int:
    result = _request(args.manager_socket, {
        "action": "dataset.prepare",
        "dataset_id": args.dataset_id,
        "speaker": args.speaker,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def training_start(args) -> int:
    result = _request(args.manager_socket, {
        "action": "training.start",
        "dataset_id": args.dataset_id,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cancel_operation(args) -> int:
    result = _request(args.manager_socket, {
        "action": "cancel",
        "operation_id": args.operation_id,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def pair(args) -> int:
    # The one-time token must never appear in argv or shell history.
    if sys.stdin.isatty():
        token = getpass.getpass("Node token (input hidden): ")
    else:
        token = sys.stdin.readline().strip()
    if not token:
        print(json.dumps({"ok": False, "error": "empty_token"}))
        return 1
    result = request_privileged({
        "action": "pair_node",
        "name": args.name,
        "server_url": args.server_url,
        "token": token,
    }, args.privileged_socket)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def r2_probe(args) -> int:
    """On-demand direct-R2 round-trip diagnostic (upload, download, delete)."""
    config = load_config(args.config)
    try:
        result = UpdateStager(config).r2_health_probe()
    except WorkloadError as exc:
        print(json.dumps({"ok": False, "error": exc.code}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def set_update_channel(args) -> int:
    result = request_privileged({
        "action": "set_update_channel",
        "channel": args.channel,
    }, args.privileged_socket)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def power_override(args) -> int:
    result = request_privileged({
        "action": "set_power_override",
        "until_epoch": args.until_epoch,
    }, args.privileged_socket)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="LMC AI Workstation control")
    value.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    value.add_argument("--manager-socket", type=Path, default=DEFAULT_MANAGER_SOCKET)
    value.add_argument("--privileged-socket", type=Path, default=Path("/run/lmc-ai-workstation/privileged.sock"))
    value.add_argument("--release-lock", type=Path, default=Path("/run/lmc-ai-workstation/release-operation.lock"))
    commands = value.add_subparsers(dest="command", required=True)
    commands.add_parser("status").set_defaults(handler=status)
    commands.add_parser("health").set_defaults(handler=health, full=False)
    commands.add_parser("full-health").set_defaults(handler=health, full=True)
    commands.add_parser("drain").set_defaults(handler=manager_action)
    commands.add_parser("resume").set_defaults(handler=manager_action)
    commands.add_parser("ack_reconcile").set_defaults(handler=manager_action)
    commands.add_parser("power-check").set_defaults(handler=power_check)
    commands.add_parser("reboot-check").set_defaults(handler=reboot_check)
    commands.add_parser("update-check").set_defaults(handler=update_check)
    commands.add_parser("rollback-previous").set_defaults(handler=rollback_previous)
    for name in _ARTIFACT_ACTIONS:
        commands.add_parser(name).set_defaults(handler=artifact_action)
    prepare = commands.add_parser("dataset-prepare")
    prepare.add_argument("--dataset-id", required=True)
    prepare.add_argument("--speaker", required=True)
    prepare.set_defaults(handler=dataset_prepare)
    training = commands.add_parser("training-start")
    training.add_argument("--dataset-id", required=True)
    training.set_defaults(handler=training_start)
    cancel = commands.add_parser("cancel-operation")
    cancel.add_argument("--operation-id", required=True)
    cancel.set_defaults(handler=cancel_operation)
    pairing = commands.add_parser("pair")
    pairing.add_argument("--name", required=True)
    pairing.add_argument("--server-url", required=True)
    pairing.set_defaults(handler=pair)
    commands.add_parser("r2-probe").set_defaults(handler=r2_probe)
    channel = commands.add_parser("set-update-channel")
    channel.add_argument("--channel", required=True, choices=["stable", "candidate"])
    channel.set_defaults(handler=set_update_channel)
    override = commands.add_parser("power-override")
    override.add_argument("--until-epoch", required=True, type=int)
    override.set_defaults(handler=power_override)
    return value


def main() -> int:
    args = parser().parse_args()
    return int(args.handler(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
