"""Validation shared by the root helper and its unprivileged client."""

from __future__ import annotations

import re
import time

from workstation.remote_control import validate_remote_command


ALLOWED_SERVICES = frozenset({
    "lmc-ai-manager.service",
    "lmc-ai-node.service",
    "lmc-ai-gui.service",
    "ollama.service",
    "lmc-ai-gpt-sovits.service",
})
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?")
_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


class PrivilegedRequestError(ValueError):
    pass


def validate_request(value: object, *, now: int | None = None) -> dict:
    if not isinstance(value, dict):
        raise PrivilegedRequestError("request must be an object")
    action = str(value.get("action") or "")
    if action == "suspend":
        if set(value) != {"action", "wake_epoch"}:
            raise PrivilegedRequestError("suspend request fields are invalid")
        current = int(time.time() if now is None else now)
        wake = int(value.get("wake_epoch") or 0)
        if wake < current + 60 or wake > current + 48 * 60 * 60:
            raise PrivilegedRequestError("wake_epoch is outside the safe window")
        return {"action": action, "wake_epoch": wake}
    if action == "reboot":
        if set(value) != {"action"}:
            raise PrivilegedRequestError("reboot request fields are invalid")
        return {"action": action}
    if action in {"trigger_update", "trigger_rollback"}:
        if set(value) != {"action"}:
            raise PrivilegedRequestError("release trigger fields are invalid")
        return {"action": action}
    if action == "restart_service":
        if set(value) != {"action", "service"}:
            raise PrivilegedRequestError("restart request fields are invalid")
        service = str(value.get("service") or "")
        if service not in ALLOWED_SERVICES:
            raise PrivilegedRequestError("service is not allowlisted")
        return {"action": action, "service": service}
    if action == "set_service_state":
        if set(value) != {"action", "service", "state"}:
            raise PrivilegedRequestError("service state request fields are invalid")
        service = str(value.get("service") or "")
        state = str(value.get("state") or "")
        if service != "lmc-ai-gpt-sovits.service" or state not in {"start", "stop"}:
            raise PrivilegedRequestError("service state transition is not allowlisted")
        return {"action": action, "service": service, "state": state}
    if action in {
        "install_release", "switch_release", "confirm_release", "rollback_release",
    }:
        if set(value) != {"action", "version"}:
            raise PrivilegedRequestError("release request fields are invalid")
        version = str(value.get("version") or "")
        if not _VERSION_RE.fullmatch(version):
            raise PrivilegedRequestError("release version is invalid")
        return {"action": action, "version": version}
    if action == "set_power_schedule":
        if set(value) != {"action", "enabled", "timezone", "suspend_at", "wake_at"}:
            raise PrivilegedRequestError("power schedule request fields are invalid")
        timezone = str(value.get("timezone") or "")
        suspend_at = str(value.get("suspend_at") or "")
        wake_at = str(value.get("wake_at") or "")
        if timezone != "Asia/Hong_Kong" or not _TIME_RE.fullmatch(suspend_at) or not _TIME_RE.fullmatch(wake_at) or suspend_at == wake_at:
            raise PrivilegedRequestError("power schedule is invalid")
        if not isinstance(value.get("enabled"), bool):
            raise PrivilegedRequestError("power schedule enabled flag is invalid")
        return {"action": action, "enabled": value["enabled"], "timezone": timezone, "suspend_at": suspend_at, "wake_at": wake_at}
    if action == "set_power_override":
        if set(value) != {"action", "until_epoch"}:
            raise PrivilegedRequestError("power override request fields are invalid")
        current = int(time.time() if now is None else now)
        until_epoch = int(value.get("until_epoch") or 0)
        if until_epoch != 0 and not current + 60 <= until_epoch <= current + 7 * 24 * 60 * 60:
            raise PrivilegedRequestError("power override is outside the safe window")
        return {"action": action, "until_epoch": until_epoch}
    if action == "set_update_channel":
        if set(value) != {"action", "channel"}:
            raise PrivilegedRequestError("update channel fields are invalid")
        channel = str(value.get("channel") or "")
        if channel not in {"stable", "candidate"}:
            raise PrivilegedRequestError("update channel is invalid")
        return {"action": action, "channel": channel}
    if action == "set_workloads":
        try:
            clean = validate_remote_command({
                **value,
                "action": "workloads_apply",
            })
        except ValueError as exc:
            raise PrivilegedRequestError(str(exc)) from exc
        clean["action"] = "set_workloads"
        return clean
    if action == "rollback_workloads":
        if set(value) != {"action"}:
            raise PrivilegedRequestError("workload rollback fields are invalid")
        return {"action": action}
    if action == "pair_node":
        if set(value) != {"action", "name", "server_url", "token"}:
            raise PrivilegedRequestError("node pairing request fields are invalid")
        name = str(value.get("name") or "").strip()
        server_url = str(value.get("server_url") or "").strip()
        token = str(value.get("token") or "").strip()
        if not name or len(name) > 80 or any(ord(character) < 32 for character in name):
            raise PrivilegedRequestError("node name is invalid")
        from urllib.parse import urlparse
        parsed = urlparse(server_url)
        if parsed.scheme not in {"https", "wss"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PrivilegedRequestError("node server URL is invalid")
        if len(token) < 32 or len(token) > 512 or any(character.isspace() for character in token):
            raise PrivilegedRequestError("node token is invalid")
        return {"action": action, "name": name, "server_url": server_url, "token": token}
    raise PrivilegedRequestError("action is not allowlisted")
