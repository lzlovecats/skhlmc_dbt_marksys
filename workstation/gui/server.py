#!/usr/bin/env python3
"""Small localhost GUI with Host, Origin and CSRF protections."""

from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import urlparse

from ai_model_config import lmc_ai_workstation_required_models
from workstation.config import (
    DEFAULT_CONFIG_PATH,
    RELEASE_STATE_RELATIVE_PATH,
    WorkstationConfig,
    load_config,
)
from workstation.manager.ipc import DEFAULT_MANAGER_SOCKET, ManagerClient
from workstation.manager.power import decide_power_action, read_power_override
from workstation.privileged_helper.client import PrivilegedActionError, request_privileged
from workstation.version import WORKSTATION_VERSION


STATIC_DIR = Path(__file__).resolve().parent / "static"
BODY_MAX_BYTES = 8_192


class GuiApplication:
    def __init__(self, config: WorkstationConfig, *, manager_socket: Path, privileged_socket: Path, config_path: Path | None = None):
        self.config = config
        self.config_path = config_path
        self.manager = ManagerClient(manager_socket)
        self.privileged_socket = privileged_socket
        self.csrf_token = secrets.token_urlsafe(32)

    def manager_request(self, payload: dict) -> dict:
        return asyncio.run(self.manager.request(payload))

    def _require_reconfiguration_idle(self) -> None:
        snapshot = self.manager_request({"action": "snapshot"})
        manager = snapshot.get("manager") if isinstance(snapshot, dict) else None
        if not isinstance(manager, dict) or any((
            str(manager.get("mode") or "") != "idle",
            bool(manager.get("active_operation")),
            bool(manager.get("voice_session_active")),
            bool(manager.get("voice_session_pending")),
            bool(manager.get("draining")),
        )):
            raise ValueError("Workstation must be idle before reconfiguration")

    def status(self) -> dict:
        if self.config_path is not None:
            self.config = load_config(self.config_path)
        snapshot = self.manager_request({"action": "snapshot"})
        manager = snapshot.get("manager") or {}
        override_until_epoch = read_power_override(self.config.paths.state)
        power_decision = decide_power_action(
            self.config.power,
            manager,
            override_until_epoch=override_until_epoch,
        )
        ollama = ((snapshot.get("health") or {}).get("checks") or {}).get("ollama") or {}
        try:
            release_path = self.config.paths.state / RELEASE_STATE_RELATIVE_PATH
            if (
                release_path.is_symlink()
                or not release_path.is_file()
                or not 0 < release_path.stat().st_size <= 64 * 1024
            ):
                raise ValueError("invalid release state")
            release_state = json.loads(release_path.read_bytes())
            if not isinstance(release_state, dict):
                raise ValueError("invalid release state")
        except (OSError, ValueError, json.JSONDecodeError):
            release_state = {}
        return {
            "config": self.config.public_dict(),
            "manager": manager,
            "health": snapshot.get("health") or {},
            "models": {
                "required": list(lmc_ai_workstation_required_models()),
                "installed": list(ollama.get("models") or []),
                "missing": list(ollama.get("missing_models") or []),
            },
            "inventory": (snapshot.get("health") or {}).get("inventory") or {},
            "release": {
                "current": str(release_state.get("current") or WORKSTATION_VERSION),
                "previous": str(release_state.get("previous") or ""),
                "pending_health": bool(release_state.get("pending_health")),
                "last_action": str(release_state.get("last_action") or "installed"),
                "channel": self.config.update.channel,
                "automatic": self.config.update.enabled,
            },
            "power_status": {
                **power_decision.__dict__,
                "override_until_epoch": override_until_epoch,
            },
        }

    def action(self, body: dict) -> dict:
        action = str(body.get("action") or "")
        if action in {"drain", "resume", "ack_reconcile"}:
            return self.manager_request({"action": action})
        if action == "preflight":
            return self.manager_request({"action": "health", "force": True, "full": True})
        if action == "dataset_prepare":
            return self.manager_request({
                "action": "dataset.prepare",
                "dataset_id": str(body.get("dataset_id") or ""),
                "speaker": str(body.get("speaker") or ""),
            })
        if action == "training_start":
            return self.manager_request({
                "action": "training.start",
                "dataset_id": str(body.get("dataset_id") or ""),
            })
        if action == "cancel_operation":
            return self.manager_request({
                "action": "cancel",
                "operation_id": str(body.get("operation_id") or ""),
            })
        if action == "update_check":
            return request_privileged(
                {"action": "trigger_update"}, self.privileged_socket,
            )
        if action == "set_update_channel":
            self._require_reconfiguration_idle()
            return request_privileged({
                "action": "set_update_channel",
                "channel": str(body.get("channel") or ""),
            }, self.privileged_socket)
        if action == "rollback_previous":
            return request_privileged(
                {"action": "trigger_rollback"}, self.privileged_socket,
            )
        if action == "artifact_inspect":
            return self.manager_request({"action": "artifacts.inspect"})
        if action == "model_approve":
            return self.manager_request({"action": "model.approve"})
        if action == "rag_install":
            return self.manager_request({"action": "rag.install"})
        if action == "rag_rollback":
            return self.manager_request({"action": "rag.rollback"})
        if action == "restart_service":
            self._require_reconfiguration_idle()
            service = str(body.get("service") or "")
            return request_privileged({"action": action, "service": service}, self.privileged_socket)
        if action == "set_power_schedule":
            return request_privileged({
                "action": action,
                "enabled": body.get("enabled"),
                "timezone": "Asia/Hong_Kong",
                "suspend_at": str(body.get("suspend_at") or ""),
                "wake_at": str(body.get("wake_at") or ""),
            }, self.privileged_socket)
        if action == "set_power_override":
            return request_privileged({
                "action": action,
                "until_epoch": int(body.get("until_epoch") or 0),
            }, self.privileged_socket)
        if action == "pair_node":
            self._require_reconfiguration_idle()
            return request_privileged({
                "action": action,
                "name": str(body.get("name") or ""),
                "server_url": str(body.get("server_url") or ""),
                "token": str(body.get("token") or ""),
            }, self.privileged_socket)
        raise ValueError("unsupported GUI action")


class GuiHandler(BaseHTTPRequestHandler):
    server_version = "LMC-AI-Workstation-GUI"

    @property
    def application(self) -> GuiApplication:
        return self.server.application

    def log_message(self, _format, *_args):
        # Do not log URLs or POST bodies; journald still records service errors.
        return

    def _allowed_host(self) -> bool:
        value = str(self.headers.get("Host") or "")
        return value in {
            f"127.0.0.1:{self.application.config.gui.port}",
            f"localhost:{self.application.config.gui.port}",
        }

    def _write(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict) -> None:
        self._write(status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def do_GET(self):
        if not self._allowed_host():
            self._json(400, {"ok": False, "error": "invalid host"})
            return
        path = urlparse(self.path).path
        if path == "/":
            body = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("__CSRF_TOKEN__", self.application.csrf_token).encode("utf-8")
            self._write(200, "text/html; charset=utf-8", body)
            return
        if path == "/app.js":
            self._write(200, "text/javascript; charset=utf-8", (STATIC_DIR / "app.js").read_bytes())
            return
        if path == "/api/status":
            try:
                self._json(200, {"ok": True, **self.application.status()})
            except Exception:
                self._json(503, {"ok": False, "error": "Manager 暫時未能回應。"})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._allowed_host():
            self._json(400, {"ok": False, "error": "invalid host"})
            return
        allowed_origins = {
            f"http://127.0.0.1:{self.application.config.gui.port}",
            f"http://localhost:{self.application.config.gui.port}",
        }
        if self.headers.get("Origin") not in allowed_origins or self.headers.get("X-LMC-CSRF") != self.application.csrf_token:
            self._json(403, {"ok": False, "error": "request verification failed"})
            return
        if urlparse(self.path).path != "/api/action":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "error": "invalid content length"})
            return
        if length <= 0 or length > BODY_MAX_BYTES:
            self._json(413, {"ok": False, "error": "request too large"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("body is not an object")
            result = self.application.action(body)
            self._json(200, {"ok": True, "result": result})
        except (ValueError, PrivilegedActionError):
            self._json(400, {"ok": False, "error": "操作內容無效或未獲允許。"})
        except Exception:
            self._json(503, {"ok": False, "error": "操作暫時未能完成。"})


def serve(config: WorkstationConfig, *, manager_socket: Path, privileged_socket: Path, config_path: Path | None = None) -> None:
    application = GuiApplication(config, manager_socket=manager_socket, privileged_socket=privileged_socket, config_path=config_path)
    server = ThreadingHTTPServer((config.gui.host, config.gui.port), GuiHandler)
    server.application = application
    server.serve_forever(poll_interval=0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description="LMC AI Workstation localhost GUI")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--manager-socket", type=Path, default=DEFAULT_MANAGER_SOCKET)
    parser.add_argument("--privileged-socket", type=Path, default=Path("/run/lmc-ai-workstation/privileged.sock"))
    args = parser.parse_args()
    serve(load_config(args.config), manager_socket=args.manager_socket, privileged_socket=args.privileged_socket, config_path=args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
