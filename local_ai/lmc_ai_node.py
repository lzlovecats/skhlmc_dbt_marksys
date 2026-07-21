#!/usr/bin/env python3
"""Pop!_OS outbound node CLI for the committee local AI service."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import getpass
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

try:
    import httpx
    import websockets
except ImportError as exc:  # pragma: no cover - operator-facing dependency gate
    raise SystemExit(
        "缺少 node dependencies；請按 runbook 建立 venv 並安裝 requirements-node.txt。"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai_model_config import (  # noqa: E402
    LMC_AI_CONTEXT_LENGTH as CONTEXT_LENGTH,
    LMC_AI_FALLBACK_MODEL as FALLBACK_MODEL,
    LMC_AI_PRIMARY_MODEL as PRIMARY_MODEL,
)
from system_limits import (  # noqa: E402
    LMC_AI_HEARTBEAT_INTERVAL_SECONDS as HEARTBEAT_SECONDS,
    LMC_AI_NODE_NAME_MAX_CHARS as NODE_NAME_MAX_CHARS,
    LMC_AI_NODE_WS_FRAME_MAX_BYTES as FRAME_MAX_BYTES,
    LMC_AI_PREFLIGHT_TIMEOUT_SECONDS as PREFLIGHT_TIMEOUT_SECONDS,
    LMC_AI_REQUEST_TIMEOUT_SECONDS as REQUEST_TIMEOUT_SECONDS,
)

OLLAMA_URL = "http://127.0.0.1:11434"
SERVICE_NAME = "skhlmc-lmc-ai-node.service"
DEFAULT_CONFIG = Path.home() / ".config" / "skhlmc-lmc-ai" / "node.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit("尚未 configure；請先執行 configure。") from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit("Node config 無法讀取。") from exc


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix="node-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def _public_config(config: dict) -> dict:
    return {key: value for key, value in config.items() if key != "token"}


def _normalise_server(value: str) -> str:
    parsed = urlparse(value.strip().rstrip("/"))
    if parsed.scheme not in {"https", "wss"} or not parsed.netloc:
        raise ValueError("Server 必須係有效 https:// 或 wss:// 地址。")
    scheme = "wss" if parsed.scheme == "https" else parsed.scheme
    base_path = parsed.path.rstrip("/")
    return f"{scheme}://{parsed.netloc}{base_path}/api/lmc-ai/nodes/connect"


def configure(args) -> int:
    existing = _load(args.config) if args.config.exists() else {}
    server_default = existing.get("server_url", "")
    name_default = existing.get("name", socket.gethostname())
    server = input(f"System 地址 [{server_default or 'https://example.com'}]: ").strip() or server_default
    name = input(f"AI 電腦名稱 [{name_default}]: ").strip() or name_default
    token = getpass.getpass("Developer 一次性顯示嘅 node token（輸入時不顯示）: ").strip()
    if not token and existing.get("token"):
        keep = input("留空 token；保留現有 token？[y/N]: ").strip().lower()
        if keep == "y":
            token = existing["token"]
    if not token:
        raise SystemExit("Token 不可留空。")
    try:
        server_url = _normalise_server(server)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not name or len(name) > NODE_NAME_MAX_CHARS:
        raise SystemExit(f"AI 電腦名稱必須為 1–{NODE_NAME_MAX_CHARS} 字。")
    _save(
        args.config,
        {
            "server_url": server_url,
            "name": name,
            "token": token,
            "effective_model": existing.get("effective_model", ""),
            "preflight_ready": bool(
                existing.get("preflight_ready") and existing.get("effective_model")
            ),
            "preflight_at": existing.get("preflight_at", ""),
            "draining": bool(existing.get("draining", False)),
        },
    )
    print(f"已保存 config：{args.config}（mode 600）")
    return 0


def _run_checked(command: list[str], timeout: int = 15) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
        raise RuntimeError(detail[0] if detail else f"{command[0]} failed")
    return result.stdout


def _verify_local_binding() -> None:
    output = _run_checked(["ss", "-ltn"], timeout=10)
    listeners = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[3].endswith(":11434"):
            listeners.append(fields[3])
    if not listeners:
        raise RuntimeError("Ollama 未有監聽 127.0.0.1:11434。")
    if any(not value.startswith(("127.0.0.1:", "[::1]:")) for value in listeners):
        raise RuntimeError("Ollama 11434 似乎對外監聽；請改為只綁 localhost。")


def _installed_models() -> set[str]:
    with httpx.Client(timeout=10) as client:
        response = client.get(f"{OLLAMA_URL}/api/tags")
        response.raise_for_status()
        return {
            str(item.get("name") or "")
            for item in response.json().get("models", [])
            if isinstance(item, dict)
        }


def _gpu_offload_percent(model: str) -> int:
    output = _run_checked(["ollama", "ps"], timeout=10)
    matching = next((line for line in output.splitlines() if model in line), "")
    match = re.search(r"(\d+)\s*%\s*GPU", matching, re.IGNORECASE)
    if not match:
        raise RuntimeError("無法確認 Ollama GPU offload 比例。")
    return int(match.group(1))


def _model_probe(model: str) -> None:
    started = time.monotonic()
    with httpx.Client(timeout=PREFLIGHT_TIMEOUT_SECONDS) as client:
        response = client.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "用一句正體中文粵語介紹你自己。"}],
                "stream": False,
                "think": False,
                "keep_alive": "5m",
                "options": {"num_ctx": CONTEXT_LENGTH},
            },
        )
        response.raise_for_status()
        payload = response.json()
    if time.monotonic() - started > PREFLIGHT_TIMEOUT_SECONDS:
        raise RuntimeError("模型短測試超過 60 秒。")
    if not str((payload.get("message") or {}).get("content") or "").strip():
        raise RuntimeError("模型短測試冇產生正常文字。")
    gpu = _gpu_offload_percent(model)
    if gpu < 90:
        raise RuntimeError(f"GPU offload 只有 {gpu}%，CPU offload 超過 10%。")


def preflight(args) -> int:
    config = _load(args.config)
    checks = []
    try:
        _run_checked(["nvidia-smi"], timeout=15)
        checks.append("NVIDIA：OK")
        _run_checked(["ollama", "--version"], timeout=10)
        checks.append("Ollama：OK")
        _verify_local_binding()
        checks.append("Ollama localhost binding：OK")
        installed = _installed_models()
        missing = [model for model in (PRIMARY_MODEL, FALLBACK_MODEL) if model not in installed]
        if missing:
            raise RuntimeError("未下載 model：" + "、".join(missing))
        checks.append("兩個 models：OK")
    except Exception as exc:
        config.update(preflight_ready=False, preflight_at="", effective_model="")
        _save(args.config, config)
        print("\n".join(checks))
        raise SystemExit(f"Preflight 失敗：{exc}") from exc

    selected = ""
    errors = []
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            print(f"測試 {model}…")
            _model_probe(model)
            selected = model
            break
        except Exception as exc:
            errors.append(f"{model}: {exc}")
    if not selected:
        config.update(preflight_ready=False, preflight_at="", effective_model="")
        _save(args.config, config)
        raise SystemExit("兩個 models 都未能通過 preflight：\n- " + "\n- ".join(errors))
    config.update(
        preflight_ready=True,
        preflight_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        effective_model=selected,
    )
    _save(args.config, config)
    print("\n".join(checks))
    for error in errors:
        print("Fallback 原因：" + error)
    print(f"Preflight 完成；effective model：{selected}")
    return 0


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_unit(user: str, python: Path, script: Path, config: Path) -> str:
    return f"""[Unit]
Description=SKHLMC outbound local AI node
After=network-online.target ollama.service
Wants=network-online.target
Requires=ollama.service

[Service]
Type=simple
User={user}
ExecStart={_systemd_quote(str(python))} {_systemd_quote(str(script))} --config {_systemd_quote(str(config))} run
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


def install_service(args) -> int:
    if os.geteuid() == 0:
        raise SystemExit("請以 AI OS account 執行，唔好直接用 root。")
    config = _load(args.config)
    if not config.get("preflight_ready"):
        raise SystemExit("請先完成 preflight。")
    user = pwd.getpwuid(os.getuid()).pw_name
    script = Path(__file__).resolve()
    unit = _systemd_unit(user, Path(sys.executable), script, args.config)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as stream:
        stream.write(unit)
        temporary = stream.name
    try:
        subprocess.run(
            ["sudo", "install", "-o", "root", "-g", "root", "-m", "0644", temporary, f"/etc/systemd/system/{SERVICE_NAME}"],
            check=True,
        )
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
        subprocess.run(["sudo", "systemctl", "enable", "--now", SERVICE_NAME], check=True)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
    print(f"已安裝並啟動 {SERVICE_NAME}")
    return 0


def _runtime_state_path(config_path: Path) -> Path:
    return config_path.with_name("runtime-state.json")


def _write_runtime_state(config_path: Path, *, connected: bool, active: bool, model: str) -> None:
    _save(
        _runtime_state_path(config_path),
        {"connected": connected, "active": active, "model": model, "updated_at": time.time()},
    )


class NodeClient:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = _load(config_path)
        self.websocket = None
        self.active_task: asyncio.Task | None = None
        self.cancel_event: asyncio.Event | None = None
        self.operation_id = ""

    def reload(self) -> dict:
        self.config = _load(self.config_path)
        return self.config

    async def send(self, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > FRAME_MAX_BYTES:
            raise RuntimeError("outbound node frame too large")
        await self.websocket.send(raw)

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            config = self.reload()
            await self.send({"type": "heartbeat"})
            await self.send(
                {
                    "type": "status",
                    "ready": bool(config.get("preflight_ready")),
                    "draining": bool(config.get("draining")),
                    "model": config.get("effective_model"),
                }
            )

    async def cancel_active(self, operation_id: str) -> None:
        """Interrupt Ollama I/O and acknowledge only after the task has stopped."""
        task = self.active_task
        if task is None or operation_id != self.operation_id:
            return
        if self.cancel_event:
            self.cancel_event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await self.send(
            {
                "type": "chat.error",
                "operation_id": operation_id,
                "code": "cancelled",
            }
        )

    @staticmethod
    def _fallback_allowed(
        messages: list[dict], emitted_delta: bool, server_allows: bool
    ) -> bool:
        non_system = [item for item in messages if item.get("role") != "system"]
        return (
            server_allows
            and not emitted_delta
            and len(non_system) == 1
            and non_system[0].get("role") == "user"
        )

    @staticmethod
    def _load_failure(detail: str) -> bool:
        value = detail.casefold()
        return any(marker in value for marker in (
            "out of memory", "cuda", "model runner", "failed to load", "load model", "oom",
        ))

    async def generate(self, payload: dict) -> None:
        operation_id = str(payload.get("operation_id") or "")
        messages = payload.get("messages")
        if not operation_id or not isinstance(messages, list):
            return
        config = self.reload()
        if config.get("draining") or not config.get("preflight_ready"):
            await self.send({"type": "chat.error", "operation_id": operation_id, "code": "draining"})
            return
        model = str(config.get("effective_model") or "")
        self.operation_id = operation_id
        self.cancel_event = asyncio.Event()
        emitted = False
        started_at = time.monotonic()
        _write_runtime_state(self.config_path, connected=True, active=True, model=model)
        try:
            while True:
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=10)) as client:
                        async with client.stream(
                            "POST",
                            f"{OLLAMA_URL}/api/chat",
                            json={
                                "model": model,
                                "messages": messages,
                                "stream": True,
                                "think": False,
                                "options": {"num_ctx": CONTEXT_LENGTH},
                            },
                        ) as response:
                            # The HTTP request has now reached Ollama. Only from
                            # this point may the server record a real attempt.
                            await self.send({"type": "chat.started", "operation_id": operation_id, "model": model})
                            response.raise_for_status()
                            usage = {}
                            async for line in response.aiter_lines():
                                if self.cancel_event.is_set():
                                    raise asyncio.CancelledError
                                if not line:
                                    continue
                                item = json.loads(line)
                                if item.get("error"):
                                    raise RuntimeError(str(item["error"]))
                                # Deliberately ignore message.thinking and tool_calls.
                                content = str((item.get("message") or {}).get("content") or "")
                                if content:
                                    emitted = True
                                    await self.send({"type": "chat.delta", "operation_id": operation_id, "text": content})
                                if item.get("done"):
                                    usage = {
                                        "input_tokens": int(item.get("prompt_eval_count") or 0),
                                        "output_tokens": int(item.get("eval_count") or 0),
                                        "duration_ms": int(item.get("total_duration") or 0) // 1_000_000,
                                    }
                            await self.send({"type": "chat.complete", "operation_id": operation_id, "model": model, "usage": usage})
                            break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    detail = str(exc)
                    can_fallback = (
                        model == PRIMARY_MODEL
                        and self._load_failure(detail)
                        and self._fallback_allowed(
                            messages,
                            emitted,
                            bool(payload.get("allow_model_fallback")),
                        )
                    )
                    if can_fallback:
                        model = FALLBACK_MODEL
                        config = self.reload()
                        config["effective_model"] = model
                        config["preflight_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        _save(self.config_path, config)
                        await self.send({"type": "status", "ready": True, "draining": False, "model": model})
                        continue
                    code = "out_of_memory" if self._load_failure(detail) else "runtime_error"
                    await self.send({"type": "chat.error", "operation_id": operation_id, "code": code})
                    break
        finally:
            _write_runtime_state(self.config_path, connected=True, active=False, model=model)
            self.active_task = None
            self.cancel_event = None
            self.operation_id = ""

    async def session(self) -> None:
        config = self.reload()
        if not config.get("preflight_ready") or not config.get("effective_model"):
            raise RuntimeError("preflight not ready")
        headers = {"Authorization": f"Bearer {config['token']}"}
        async with websockets.connect(
            config["server_url"],
            additional_headers=headers,
            max_size=FRAME_MAX_BYTES,
            ping_interval=None,
            open_timeout=20,
        ) as websocket:
            self.websocket = websocket
            await self.send(
                {
                    "type": "hello",
                    "protocol": 1,
                    "name": config["name"],
                    "runtime": "ollama",
                    "runtime_version": _run_checked(["ollama", "--version"], timeout=10).strip()[:80],
                    "model": config["effective_model"],
                    "ready": True,
                    "draining": bool(config.get("draining")),
                    "capabilities": {"chat": True, "rag": False, "fine_tuned": False},
                }
            )
            accepted = json.loads(await asyncio.wait_for(websocket.recv(), timeout=20))
            if accepted.get("type") != "hello.accepted":
                raise RuntimeError("server rejected hello")
            _write_runtime_state(self.config_path, connected=True, active=False, model=config["effective_model"])
            heartbeat = asyncio.create_task(self.heartbeat())
            try:
                async for raw in websocket:
                    if not isinstance(raw, str) or len(raw.encode("utf-8")) > FRAME_MAX_BYTES:
                        await websocket.close(code=1009, reason="text frame required")
                        break
                    payload = json.loads(raw)
                    if payload.get("type") == "chat.start":
                        if self.active_task is not None:
                            await self.send({"type": "chat.error", "operation_id": payload.get("operation_id"), "code": "busy"})
                        else:
                            self.operation_id = str(payload.get("operation_id") or "")
                            self.active_task = asyncio.create_task(self.generate(payload))
                    elif payload.get("type") == "chat.cancel":
                        await self.cancel_active(str(payload.get("operation_id") or ""))
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
                if self.active_task:
                    self.active_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self.active_task
                _write_runtime_state(self.config_path, connected=False, active=False, model=config["effective_model"])


async def run_forever(config_path: Path) -> None:
    delay = 1
    while True:
        client = NodeClient(config_path)
        try:
            await client.session()
            delay = 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Node 連線中斷：{type(exc).__name__}；{delay}s 後重試。", file=sys.stderr, flush=True)
            _write_runtime_state(config_path, connected=False, active=False, model=client.config.get("effective_model", ""))
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


def run(args) -> int:
    config = _load(args.config)
    if not config.get("preflight_ready"):
        raise SystemExit("Preflight 未完成或已失效。")
    asyncio.run(run_forever(args.config))
    return 0


def set_drain(args, draining: bool) -> int:
    config = _load(args.config)
    config["draining"] = draining
    _save(args.config, config)
    print("已要求 drain；等待目前工作完成。" if draining else "已恢復接收新工作。")
    if draining:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                state = _load(_runtime_state_path(args.config))
            except SystemExit:
                return 0
            if not state.get("active"):
                print("Drain 完成。")
                return 0
            time.sleep(1)
        raise SystemExit("目前工作 180 秒內未完成；仍維持 draining，請用 status 檢查。")
    return 0


def status(args) -> int:
    config = _load(args.config)
    print(json.dumps(_public_config(config), ensure_ascii=False, indent=2))
    state_path = _runtime_state_path(args.config)
    if state_path.exists():
        print("Runtime state:")
        print(state_path.read_text(encoding="utf-8").strip())
    if shutil.which("systemctl"):
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True
        )
        print("systemd:", (result.stdout or "unknown").strip())
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="聖呂中辯自家 AI outbound node")
    value.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = value.add_subparsers(dest="command", required=True)
    sub.add_parser("configure").set_defaults(handler=configure)
    sub.add_parser("preflight").set_defaults(handler=preflight)
    sub.add_parser("install-service").set_defaults(handler=install_service)
    sub.add_parser("run").set_defaults(handler=run)
    sub.add_parser("status").set_defaults(handler=status)
    sub.add_parser("drain").set_defaults(handler=lambda args: set_drain(args, True))
    sub.add_parser("resume").set_defaults(handler=lambda args: set_drain(args, False))
    return value


def main() -> int:
    args = parser().parse_args()
    args.config = args.config.expanduser().resolve()
    return int(args.handler(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
