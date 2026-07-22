"""Unprivileged client for the root helper's fixed request schema."""

from __future__ import annotations

import json
from pathlib import Path
import socket

from workstation.privileged_helper.protocol import validate_request
from workstation.privileged_helper.server import DEFAULT_SOCKET, MAX_FRAME_BYTES


class PrivilegedActionError(RuntimeError):
    pass


def request_privileged(request: dict, socket_path: Path = DEFAULT_SOCKET) -> dict:
    clean = validate_request(request)
    raw = json.dumps(clean, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > MAX_FRAME_BYTES:
        raise PrivilegedActionError("privileged request exceeds limit")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(90)
    try:
        client.connect(str(socket_path))
        client.sendall(raw)
        response = b""
        while not response.endswith(b"\n") and len(response) <= MAX_FRAME_BYTES:
            chunk = client.recv(min(4_096, MAX_FRAME_BYTES + 1 - len(response)))
            if not chunk:
                break
            response += chunk
    finally:
        client.close()
    if not response.endswith(b"\n") or len(response) > MAX_FRAME_BYTES:
        raise PrivilegedActionError("privileged helper returned an invalid response")
    payload = json.loads(response)
    if payload.get("ok") is not True:
        raise PrivilegedActionError(str(payload.get("code") or "privileged action failed"))
    return payload
