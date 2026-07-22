#!/usr/bin/env python3
"""Collect strict, privacy-safe automated evidence on the real Ubuntu host.

This report intentionally does not claim the manual RDP, suspend/wake, fault
injection or browser benchmark gates. Those remain separate signed-off
rehearsals in RUNBOOK.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import tempfile

from workstation.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_CREDENTIALS_DIR,
    DEFAULT_DATA_ROOT,
    DEFAULT_RELEASE_ROOT,
    DEFAULT_STATE_DIR,
    load_config,
)
from workstation.manager.release_manifest import verify_release_tree
from workstation.version import WORKSTATION_VERSION
from workstation.workloads.errors import WorkloadError


MAX_COMMAND_OUTPUT_CHARS = 1024 * 1024
MAX_AUTOSTART_SECONDS = 5 * 60
MIN_DATA_FILESYSTEM_BYTES = 450_000_000_000
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

REQUIRED_ACTIVE_SERVICES = (
    "lmc-ai-privileged.service",
    "lmc-ai-manager.service",
    "lmc-ai-node.service",
    "lmc-ai-gui.service",
    "ollama.service",
)
REQUIRED_ACTIVE_TIMERS = (
    "lmc-ai-health.timer",
    "lmc-ai-full-health.timer",
    "lmc-ai-power.timer",
    "lmc-ai-reboot.timer",
    "lmc-ai-update.timer",
)
MANUAL_GATES = (
    "mac_rdp_new_session_and_tailscale_ssh",
    "non_tailnet_port_rejection",
    "scheduled_suspend_active_job_delay_and_rtc_wake",
    "power_loss_and_process_restart_reconciliation",
    "fault_injection_and_update_rollback_rehearsal",
    "browser_voice_latency_and_gpu_vram_benchmark",
    "real_r2_retention_smoke",
)


def _command(command: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    )
    if (
        len(result.stdout) > MAX_COMMAND_OUTPUT_CHARS
        or len(result.stderr) > MAX_COMMAND_OUTPUT_CHARS
    ):
        raise ValueError("acceptance command output exceeded its safe limit")
    return result


def _key_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key.strip()] = raw.strip().strip('"')
    return values


def _result(ok: bool, code: str = "", **safe_values) -> dict:
    return {
        "ok": bool(ok),
        **({"code": str(code)[:80]} if code else {}),
        **safe_values,
    }


def _platform_check() -> dict:
    try:
        os_release = _key_values(Path("/etc/os-release"))
        lsb_release = _key_values(Path("/etc/lsb-release"))
        description = str(lsb_release.get("DISTRIB_DESCRIPTION") or "")
        ok = bool(
            os_release.get("ID") == "ubuntu"
            and os_release.get("VERSION_ID") == "24.04"
            and "24.04.4 LTS" in description
        )
        return _result(
            ok,
            "ubuntu_release_mismatch" if not ok else "",
            os_id=str(os_release.get("ID") or "")[:40],
            version_id=str(os_release.get("VERSION_ID") or "")[:40],
            point_release="24.04.4" if "24.04.4 LTS" in description else "other",
        )
    except (OSError, UnicodeError, ValueError):
        return _result(False, "ubuntu_release_unavailable")


def _package_check() -> dict:
    try:
        result = _command([
            "dpkg-query", "-W", "-f=${Status}\t${Version}",
            "lmc-ai-workstation",
        ])
        fields = result.stdout.strip().split("\t")
        status = fields[0] if fields else ""
        version = fields[1] if len(fields) == 2 else ""
        ok = bool(
            result.returncode == 0
            and status == "install ok installed"
            and version == WORKSTATION_VERSION
        )
        return _result(
            ok,
            "package_version_mismatch" if not ok else "",
            installed_version=version[:40],
            expected_version=WORKSTATION_VERSION,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return _result(False, "package_status_unavailable")


def _unit_state(unit: str) -> dict:
    try:
        active = _command(["systemctl", "is-active", unit], timeout=20)
        enabled = _command(["systemctl", "is-enabled", unit], timeout=20)
        return {
            "active": active.returncode == 0 and active.stdout.strip() == "active",
            "enabled": enabled.returncode == 0 and enabled.stdout.strip() == "enabled",
        }
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"active": False, "enabled": False}


def _services_check() -> dict:
    states = {
        unit: _unit_state(unit)
        for unit in (*REQUIRED_ACTIVE_SERVICES, *REQUIRED_ACTIVE_TIMERS)
    }
    legacy = _unit_state("skhlmc-lmc-ai-node.service")
    gpt = _unit_state("lmc-ai-gpt-sovits.service")
    ok = bool(
        all(item["active"] and item["enabled"] for item in states.values())
        and not legacy["active"]
        and not legacy["enabled"]
        and not gpt["active"]
    )
    return _result(
        ok,
        "service_state_invalid" if not ok else "",
        units=states,
        legacy_node_inactive=not legacy["active"],
        legacy_node_disabled=not legacy["enabled"],
        gpt_sovits_released=not gpt["active"],
    )


def _monotonic_start_seconds(unit: str) -> float | None:
    try:
        result = _command([
            "systemctl", "show", unit,
            "--property=ActiveEnterTimestampMonotonic", "--value",
        ], timeout=20)
        value = int(result.stdout.strip())
        return round(value / 1_000_000, 3) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _session_types() -> list[dict]:
    try:
        listing = _command(["loginctl", "list-sessions", "--no-legend"], timeout=20)
        sessions = []
        for line in listing.stdout.splitlines()[:50]:
            session_id = line.split()[0] if line.split() else ""
            if not session_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", session_id):
                continue
            details = _command([
                "loginctl", "show-session", session_id,
                "--property=Type", "--property=Class", "--property=Remote",
            ], timeout=20)
            values = {}
            for row in details.stdout.splitlines():
                if "=" in row:
                    key, value = row.split("=", 1)
                    values[key] = value
            sessions.append({
                "type": str(values.get("Type") or "")[:30],
                "class": str(values.get("Class") or "")[:30],
                "remote": str(values.get("Remote") or "").lower() == "yes",
            })
        return sessions
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _cold_boot_check() -> dict:
    starts = {
        unit: _monotonic_start_seconds(unit)
        for unit in ("lmc-ai-manager.service", "lmc-ai-node.service")
    }
    sessions = _session_types()
    graphical_user = any(
        item["type"] in {"x11", "wayland"} and item["class"] != "greeter"
        for item in sessions
    )
    ok = bool(
        all(
            value is not None and 0 <= value <= MAX_AUTOSTART_SECONDS
            for value in starts.values()
        )
        and sessions
        and not graphical_user
    )
    return _result(
        ok,
        "cold_boot_autostart_unproven" if not ok else "",
        active_enter_monotonic_seconds=starts,
        graphical_user_session=graphical_user,
        observed_sessions=sessions,
    )


def _gpu_and_disk_check() -> dict:
    try:
        gpu = _command([
            "nvidia-smi", "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ], timeout=20)
        lines = [line for line in gpu.stdout.splitlines() if line.strip()]
        fields = [item.strip() for item in lines[0].split(",")] if len(lines) == 1 else []
        name = fields[0] if len(fields) == 2 else ""
        vram = int(fields[1]) if len(fields) == 2 else 0
        disk_total = shutil.disk_usage(DEFAULT_DATA_ROOT).total
        ok = bool(
            gpu.returncode == 0
            and "RTX 3060" in name
            and vram >= 8 * 1024
            and disk_total >= MIN_DATA_FILESYSTEM_BYTES
        )
        return _result(
            ok,
            "hardware_profile_mismatch" if not ok else "",
            gpu_name=name[:100],
            gpu_vram_mib=vram,
            data_filesystem_bytes=disk_total,
        )
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return _result(False, "hardware_profile_unavailable")


def _identity_and_permissions_check() -> dict:
    try:
        account = pwd.getpwnam("lmc-ai")
        service_group = grp.getgrnam("lmc-ai")
        supplementary = {
            group.gr_name for group in grp.getgrall()
            if "lmc-ai" in group.gr_mem
        }
        config = DEFAULT_CONFIG_PATH.stat()
        credentials = DEFAULT_CREDENTIALS_DIR.stat()
        token_path = DEFAULT_CREDENTIALS_DIR / "node-token"
        token = token_path.stat()
        paths = [
            DEFAULT_DATA_ROOT,
            DEFAULT_DATA_ROOT / "models",
            DEFAULT_DATA_ROOT / "datasets",
            DEFAULT_DATA_ROOT / "checkpoints",
            DEFAULT_DATA_ROOT / "rag",
            DEFAULT_DATA_ROOT / "vendor",
        ]
        data_modes_ok = all(
            path.is_dir()
            and not path.is_symlink()
            and path.stat().st_uid == account.pw_uid
            and path.stat().st_gid == service_group.gr_gid
            and path.stat().st_mode & 0o007 == 0
            for path in paths
        )
        protected_paths = [
            DEFAULT_DATA_ROOT / "health",
            DEFAULT_STATE_DIR / "release",
        ]
        protected_modes_ok = all(
            path.is_dir()
            and not path.is_symlink()
            and path.stat().st_uid == 0
            and path.stat().st_gid == service_group.gr_gid
            and path.stat().st_mode & 0o777 == 0o750
            for path in protected_paths
        )
        hardware_groups_ok = {"video", "render"}.issubset(supplementary)
        ok = bool(
            account.pw_shell == "/usr/sbin/nologin"
            and account.pw_gid == service_group.gr_gid
            and "sudo" not in supplementary
            and hardware_groups_ok
            and not DEFAULT_CONFIG_PATH.is_symlink()
            and config.st_uid == 0
            and config.st_gid == service_group.gr_gid
            and config.st_mode & 0o777 == 0o640
            and not DEFAULT_CREDENTIALS_DIR.is_symlink()
            and credentials.st_uid == 0
            and credentials.st_gid == service_group.gr_gid
            and credentials.st_mode & 0o777 == 0o750
            and not token_path.is_symlink()
            and token.st_uid == account.pw_uid
            and token.st_gid == service_group.gr_gid
            and token.st_mode & 0o777 == 0o600
            and data_modes_ok
            and protected_modes_ok
        )
        return _result(
            ok,
            "identity_or_permission_invalid" if not ok else "",
            no_login_shell=account.pw_shell == "/usr/sbin/nologin",
            no_sudo_group="sudo" not in supplementary,
            hardware_groups_ok=hardware_groups_ok,
            config_mode=oct(config.st_mode & 0o777),
            credential_mode=oct(token.st_mode & 0o777),
            data_modes_ok=data_modes_ok,
            protected_modes_ok=protected_modes_ok,
        )
    except (KeyError, OSError, ValueError):
        return _result(False, "identity_or_permission_unavailable")


def _release_check() -> dict:
    current = DEFAULT_RELEASE_ROOT / "current"
    try:
        if current.is_symlink():
            target = current.resolve(strict=True)
        else:
            raise ValueError("current release is not a symlink")
        expected = (DEFAULT_RELEASE_ROOT / "releases" / WORKSTATION_VERSION).resolve(
            strict=True
        )
        inventory = verify_release_tree(target)
        current_owner_ok = current.lstat().st_uid == 0
        release_owner_ok = target.stat().st_uid == 0 and all(
            path.stat().st_uid == 0 for path in target.rglob("*")
        )
        ok = bool(
            target == expected
            and (target / "release.ready").is_file()
            and current_owner_ok
            and release_owner_ok
        )
        return _result(
            ok,
            "release_tree_invalid" if not ok else "",
            version=WORKSTATION_VERSION,
            verified_files=max(0, int(inventory.get("files") or 0)),
            verified_bytes=max(0, int(inventory.get("bytes") or 0)),
            root_owned=current_owner_ok and release_owner_ok,
        )
    except (OSError, RuntimeError, ValueError, TypeError, WorkloadError):
        return _result(False, "release_tree_invalid")


def _preflight_check() -> dict:
    script = Path(__file__).resolve().parent / "preflight_ubuntu.sh"
    try:
        result = _command([str(script)], timeout=120)
        return _result(
            result.returncode == 0,
            "ubuntu_preflight_failed" if result.returncode else "",
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return _result(False, "ubuntu_preflight_unavailable")


def _full_health_check() -> dict:
    try:
        result = _command([
            "/usr/bin/python3", "-m",
            "workstation.scripts.workstationctl", "full-health",
        ], timeout=20 * 60)
        value = json.loads(result.stdout)
        checks = value.get("checks") if isinstance(value, dict) else None
        safe_checks = {
            str(name)[:80]: {
                "ok": bool(details.get("ok")),
                **({"code": str(details.get("code") or "")[:80]}
                   if details.get("code") else {}),
            }
            for name, details in (checks or {}).items()
            if isinstance(details, dict)
        }
        ok = bool(result.returncode == 0 and value.get("healthy") and safe_checks)
        return _result(
            ok,
            "functional_health_failed" if not ok else "",
            checked_epoch=max(0, int(value.get("checked_epoch") or 0)),
            checks=safe_checks,
        )
    except (
        OSError, ValueError, TypeError, json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ):
        return _result(False, "functional_health_unavailable")


def collect() -> dict:
    checks = {
        "platform": _platform_check(),
        "package": _package_check(),
        "services": _services_check(),
        "cold_boot": _cold_boot_check(),
        "hardware": _gpu_and_disk_check(),
        "identity_permissions": _identity_and_permissions_check(),
        "release": _release_check(),
        "network_power_preflight": _preflight_check(),
        "functional_health": _full_health_check(),
    }
    automated_ok = all(item.get("ok") is True for item in checks.values())
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except (OSError, UnicodeError):
        boot_id = ""
    return {
        "schema_version": 1,
        "automated_ok": automated_ok,
        "manual_gates_complete": False,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "workstation_version": WORKSTATION_VERSION,
        "boot_id": boot_id if re.fullmatch(r"[0-9a-f-]{36}", boot_id) else "",
        "checks": checks,
        "manual_gates_required": list(MANUAL_GATES),
    }


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect automated evidence on the real Ubuntu Workstation"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/var/lib/lmc-ai-workstation/acceptance/ubuntu-evidence.json"),
    )
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        print(json.dumps({
            "schema_version": 1,
            "automated_ok": False,
            "error": "root_required",
        }, separators=(",", ":")))
        return 2
    try:
        # Parse typed configuration before executing any functional probe.
        load_config(DEFAULT_CONFIG_PATH)
        report = collect()
        _atomic_json(arguments.output, report)
    except Exception:
        print(json.dumps({
            "schema_version": 1,
            "automated_ok": False,
            "error": "acceptance_collection_failed",
        }, separators=(",", ":")))
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["automated_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
