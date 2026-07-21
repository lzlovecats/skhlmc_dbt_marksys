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
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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
    LMC_AI_DEEP_MODEL as DEEP_MODEL,
    LMC_AI_DEFAULT_MODEL as DEFAULT_MODEL,
    LMC_AI_MODEL_PROFILE_VERSION as MODEL_PROFILE_VERSION,
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
AUTO_DRAIN_SERVICE = "skhlmc-lmc-ai-auto-drain.service"
AUTO_DRAIN_TIMER = "skhlmc-lmc-ai-auto-drain.timer"
AUTO_SUSPEND_SERVICE = "skhlmc-lmc-ai-auto-suspend.service"
AUTO_SUSPEND_TIMER = "skhlmc-lmc-ai-auto-suspend.timer"
AUTO_RESUME_SERVICE = "skhlmc-lmc-ai-auto-resume.service"
AUTO_RESUME_TIMER = "skhlmc-lmc-ai-auto-resume.timer"
DEFAULT_CONFIG = Path.home() / ".config" / "skhlmc-lmc-ai" / "node.json"
AUTO_POWER_TIMEZONE = "Asia/Hong_Kong"
AUTO_POWER_DRAIN_AT = "23:55"
AUTO_POWER_SUSPEND_AT = "00:00"
AUTO_POWER_WAKE_AT = "08:00"
NODE_CONNECT_PATH = "/api/lmc-ai/nodes/connect"


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


def _auto_power_config(config: dict) -> dict:
    value = config.get("auto_power")
    value = value if isinstance(value, dict) else {}
    return {
        "enabled": bool(value.get("enabled", False)),
        "mode": "suspend_rtc",
        "timezone": AUTO_POWER_TIMEZONE,
        "drain_at": AUTO_POWER_DRAIN_AT,
        "suspend_at": AUTO_POWER_SUSPEND_AT,
        "wake_at": AUTO_POWER_WAKE_AT,
    }


def _normalise_server(value: str) -> str:
    parsed = urlparse(value.strip().rstrip("/"))
    if parsed.scheme not in {"https", "wss"} or not parsed.netloc:
        raise ValueError("Server 必須係有效 https:// 或 wss:// 地址。")
    scheme = "wss" if parsed.scheme == "https" else parsed.scheme
    base_path = parsed.path.rstrip("/")
    while base_path.endswith(NODE_CONNECT_PATH):
        base_path = base_path[:-len(NODE_CONNECT_PATH)].rstrip("/")
    return f"{scheme}://{parsed.netloc}{base_path}{NODE_CONNECT_PATH}"


def configure(args) -> int:
    existing = _load(args.config) if args.config.exists() else {}
    server_default = existing.get("server_url", "")
    name_default = existing.get("name", socket.gethostname())
    server = input(f"System 地址 [{server_default or 'https://example.com'}]: ").strip() or server_default
    name = input(f"AI 電腦名稱 [{name_default}]: ").strip() or name_default
    existing_auto_power = _auto_power_config(existing)
    auto_default = "Y" if existing_auto_power["enabled"] else "N"
    auto_answer = input(
        "啟用每日 23:55 drain、00:00 休眠、08:00 RTC 喚醒？"
        f"[y/N；目前 {auto_default}]: "
    ).strip().lower()
    if auto_answer in {"y", "yes"}:
        auto_enabled = True
    elif auto_answer in {"n", "no"}:
        auto_enabled = False
    elif not auto_answer:
        auto_enabled = existing_auto_power["enabled"]
    else:
        raise SystemExit("自動開關機只接受 y 或 n。")
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
    profile_current = existing.get("model_profile_version") == MODEL_PROFILE_VERSION
    _save(
        args.config,
        {
            "server_url": server_url,
            "name": name,
            "token": token,
            "effective_model": (
                existing.get("effective_model", "") if profile_current else ""
            ),
            "available_models": (
                existing.get("available_models", []) if profile_current else []
            ),
            "model_digests": (
                existing.get("model_digests", {}) if profile_current else {}
            ),
            "model_profile_version": MODEL_PROFILE_VERSION,
            "preflight_ready": bool(
                profile_current
                and existing.get("preflight_ready")
                and existing.get("effective_model") == DEFAULT_MODEL
            ),
            "preflight_at": existing.get("preflight_at", "") if profile_current else "",
            "draining": bool(existing.get("draining", False)),
            "auto_power": {**existing_auto_power, "enabled": auto_enabled},
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


def _installed_models() -> dict[str, str]:
    with httpx.Client(timeout=10) as client:
        response = client.get(f"{OLLAMA_URL}/api/tags")
        response.raise_for_status()
        result = {}
        for item in response.json().get("models", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            digest = str(item.get("digest") or "").lower()
            if name and re.fullmatch(r"[0-9a-f]{64}", digest):
                result[name] = digest
        return result


def _model_digest_profile_valid(config: dict) -> bool:
    models = {
        str(value) for value in (config.get("available_models") or []) if str(value)
    }
    digests = config.get("model_digests")
    return bool(models) and isinstance(digests, dict) and set(digests) == models and all(
        re.fullmatch(r"[0-9a-f]{64}", str(digests.get(model) or ""))
        for model in models
    )


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
        installed_names = set(installed)
        if DEFAULT_MODEL not in installed_names:
            raise RuntimeError("未下載日常預設 model：" + DEFAULT_MODEL)
        checks.append("日常預設 model：已安裝")
    except Exception as exc:
        config.update(
            preflight_ready=False,
            preflight_at="",
            effective_model="",
            available_models=[],
            model_digests={},
            model_profile_version=MODEL_PROFILE_VERSION,
        )
        _save(args.config, config)
        print("\n".join(checks))
        raise SystemExit(f"Preflight 失敗：{exc}") from exc

    available_models = []
    errors = []
    try:
        print(f"測試日常預設 {DEFAULT_MODEL}…")
        _model_probe(DEFAULT_MODEL)
        available_models.append(DEFAULT_MODEL)
    except Exception as exc:
        config.update(
            preflight_ready=False,
            preflight_at="",
            effective_model="",
            available_models=[],
            model_digests={},
            model_profile_version=MODEL_PROFILE_VERSION,
        )
        _save(args.config, config)
        raise SystemExit(f"日常預設 model 未能通過 preflight：{exc}") from exc

    if DEEP_MODEL in installed_names:
        try:
            print(f"測試深入思考 {DEEP_MODEL}…")
            _model_probe(DEEP_MODEL)
            available_models.append(DEEP_MODEL)
        except Exception as exc:
            errors.append(f"{DEEP_MODEL}: {exc}")
    else:
        errors.append(f"{DEEP_MODEL}: 未安裝；深入思考模式暫停")
    config.update(
        preflight_ready=True,
        preflight_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        effective_model=DEFAULT_MODEL,
        available_models=available_models,
        model_digests={
            model: installed[model] for model in available_models
            if isinstance(installed, dict) and model in installed
        },
        model_profile_version=MODEL_PROFILE_VERSION,
    )
    _save(args.config, config)
    print("\n".join(checks))
    for error in errors:
        print("選用模式未啟用：" + error)
    print("Preflight 完成；日常預設：" + DEFAULT_MODEL)
    print("可用 models：" + "、".join(available_models))
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


def _scheduled_service_unit(
    description: str,
    python: Path,
    script: Path,
    config: Path,
    command: str,
    *,
    user: str = "",
) -> str:
    user_line = f"User={user}\n" if user else ""
    return f"""[Unit]
Description={description}
After=network-online.target

[Service]
Type=oneshot
{user_line}ExecStart={_systemd_quote(str(python))} {_systemd_quote(str(script))} --config {_systemd_quote(str(config))} {command}
NoNewPrivileges=true
PrivateTmp=true
"""


def _timer_unit(description: str, service: str, calendar: str, *, persistent: bool) -> str:
    return f"""[Unit]
Description={description}

[Timer]
OnCalendar={calendar} {AUTO_POWER_TIMEZONE}
Unit={service}
AccuracySec=1min
Persistent={'true' if persistent else 'false'}

[Install]
WantedBy=timers.target
"""


def _service_files(user: str, python: Path, script: Path, config: Path) -> dict[str, str]:
    return {
        SERVICE_NAME: _systemd_unit(user, python, script, config),
        AUTO_DRAIN_SERVICE: _scheduled_service_unit(
            "SKHLMC local AI pre-suspend drain", python, script, config,
            "scheduled-drain", user=user,
        ),
        AUTO_DRAIN_TIMER: _timer_unit(
            "Drain SKHLMC local AI before nightly suspend",
            AUTO_DRAIN_SERVICE, "*-*-* 23:55:00", persistent=False,
        ),
        AUTO_SUSPEND_SERVICE: _scheduled_service_unit(
            "SKHLMC local AI nightly RTC suspend", python, script, config,
            "scheduled-suspend",
        ),
        AUTO_SUSPEND_TIMER: _timer_unit(
            "Suspend SKHLMC local AI nightly",
            AUTO_SUSPEND_SERVICE, "*-*-* 00:00:00", persistent=False,
        ),
        AUTO_RESUME_SERVICE: _scheduled_service_unit(
            "SKHLMC local AI post-wake resume", python, script, config,
            "scheduled-resume", user=user,
        ),
        AUTO_RESUME_TIMER: _timer_unit(
            "Resume SKHLMC local AI after RTC wake",
            AUTO_RESUME_SERVICE, "*-*-* 08:00:00", persistent=True,
        ),
    }


def install_service(args) -> int:
    if os.geteuid() == 0:
        raise SystemExit("請以 AI OS account 執行，唔好直接用 root。")
    config = _load(args.config)
    if (
        not config.get("preflight_ready")
        or config.get("model_profile_version") != MODEL_PROFILE_VERSION
        or not _model_digest_profile_valid(config)
    ):
        raise SystemExit("請先完成 preflight。")
    if _auto_power_config(config)["enabled"] and not shutil.which("rtcwake"):
        raise SystemExit("已啟用自動休眠，但系統未安裝 rtcwake（util-linux）。")
    user = pwd.getpwuid(os.getuid()).pw_name
    script = Path(__file__).resolve()
    units = _service_files(user, Path(sys.executable), script, args.config)
    temporary_files = []
    try:
        for filename, content in units.items():
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False
            ) as stream:
                stream.write(content)
                temporary_files.append(stream.name)
            subprocess.run(
                [
                    "sudo", "install", "-o", "root", "-g", "root", "-m", "0644",
                    temporary_files[-1], f"/etc/systemd/system/{filename}",
                ],
                check=True,
            )
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
        subprocess.run(
            [
                "sudo", "systemctl", "enable", "--now", SERVICE_NAME,
                AUTO_DRAIN_TIMER, AUTO_SUSPEND_TIMER, AUTO_RESUME_TIMER,
            ],
            check=True,
        )
    finally:
        for temporary in temporary_files:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
    print(f"已安裝並啟動 {SERVICE_NAME} 同自動運作 timers")
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
                    "models": config.get("available_models") or [
                        config.get("effective_model")
                    ],
                    "model_digests": config.get("model_digests") or {},
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
        thinking_enabled = payload.get("think") is True
        if not operation_id or not isinstance(messages, list):
            return
        config = self.reload()
        if config.get("draining") or not config.get("preflight_ready"):
            await self.send({"type": "chat.error", "operation_id": operation_id, "code": "draining"})
            return
        requested_model = str(payload.get("model") or "").strip()
        available_models = tuple(
            str(item or "").strip()
            for item in (config.get("available_models") or [])
            if str(item or "").strip()
        )
        model = requested_model or str(config.get("effective_model") or "")
        if model not in available_models:
            await self.send({
                "type": "chat.error",
                "operation_id": operation_id,
                "code": "model_unavailable",
            })
            return
        self.operation_id = operation_id
        self.cancel_event = asyncio.Event()
        _write_runtime_state(self.config_path, connected=True, active=True, model=model)
        try:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=10)) as client:
                    async with client.stream(
                        "POST",
                        f"{OLLAMA_URL}/api/chat",
                        json={
                            "model": model,
                            "messages": messages,
                            "stream": True,
                            "think": thinking_enabled,
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
                                await self.send({"type": "chat.delta", "operation_id": operation_id, "text": content})
                            if item.get("done"):
                                usage = {
                                    "input_tokens": int(item.get("prompt_eval_count") or 0),
                                    "output_tokens": int(item.get("eval_count") or 0),
                                    "duration_ms": int(item.get("total_duration") or 0) // 1_000_000,
                                }
                        await self.send({"type": "chat.complete", "operation_id": operation_id, "model": model, "usage": usage})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = "out_of_memory" if self._load_failure(str(exc)) else "runtime_error"
                await self.send({"type": "chat.error", "operation_id": operation_id, "code": code})
        finally:
            _write_runtime_state(self.config_path, connected=True, active=False, model=model)
            self.active_task = None
            self.cancel_event = None
            self.operation_id = ""

    async def session(self) -> None:
        config = self.reload()
        if (
            not config.get("preflight_ready")
            or config.get("model_profile_version") != MODEL_PROFILE_VERSION
            or config.get("effective_model") != DEFAULT_MODEL
            or DEFAULT_MODEL not in (config.get("available_models") or [])
            or not _model_digest_profile_valid(config)
        ):
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
                    "model_profile_version": MODEL_PROFILE_VERSION,
                    "name": config["name"],
                    "runtime": "ollama",
                    "runtime_version": _run_checked(["ollama", "--version"], timeout=10).strip()[:80],
                    "model": config["effective_model"],
                    "models": config["available_models"],
                    "model_digests": config.get("model_digests") or {},
                    "ready": True,
                    "draining": bool(config.get("draining")),
                    "capabilities": {
                        "chat": True,
                        "rag": False,
                        "fine_tuned": False,
                        "thinking_control": True,
                    },
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
    if (
        not config.get("preflight_ready")
        or config.get("model_profile_version") != MODEL_PROFILE_VERSION
        or not _model_digest_profile_valid(config)
    ):
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


def _auto_power_enabled(config: dict) -> bool:
    return _auto_power_config(config)["enabled"]


def scheduled_drain(args) -> int:
    if not _auto_power_enabled(_load(args.config)):
        print("自動運作未啟用；略過 scheduled drain。")
        return 0
    return set_drain(args, True)


def scheduled_resume(args) -> int:
    config = _load(args.config)
    if not _auto_power_enabled(config):
        print("自動運作未啟用；略過 scheduled resume。")
        return 0
    config["draining"] = False
    _save(args.config, config)
    print("08:00 排程已恢復接收新工作。")
    return 0


def _next_wake_timestamp(now: datetime | None = None) -> int:
    zone = ZoneInfo(AUTO_POWER_TIMEZONE)
    current = now.astimezone(zone) if now is not None else datetime.now(zone)
    wake = current.replace(hour=8, minute=0, second=0, microsecond=0)
    if wake <= current:
        wake += timedelta(days=1)
    return int(wake.timestamp())


def scheduled_suspend(args) -> int:
    config = _load(args.config)
    if not _auto_power_enabled(config):
        print("自動運作未啟用；略過 scheduled suspend。")
        return 0
    if os.geteuid() != 0:
        raise SystemExit("scheduled-suspend 必須由 root systemd service 執行。")
    if not shutil.which("rtcwake"):
        raise SystemExit("系統未安裝 rtcwake（util-linux）。")
    wake_timestamp = _next_wake_timestamp()
    wake_display = datetime.fromtimestamp(
        wake_timestamp, ZoneInfo(AUTO_POWER_TIMEZONE)
    ).strftime("%Y-%m-%d %H:%M %Z")
    print(f"準備休眠；RTC 預定 {wake_display} 喚醒。", flush=True)
    subprocess.run(
        ["rtcwake", "--mode", "mem", "--time", str(wake_timestamp)],
        check=True,
    )
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
    sub.add_parser("scheduled-drain").set_defaults(handler=scheduled_drain)
    sub.add_parser("scheduled-suspend").set_defaults(handler=scheduled_suspend)
    sub.add_parser("scheduled-resume").set_defaults(handler=scheduled_resume)
    return value


def main() -> int:
    args = parser().parse_args()
    args.config = args.config.expanduser().resolve()
    return int(args.handler(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
