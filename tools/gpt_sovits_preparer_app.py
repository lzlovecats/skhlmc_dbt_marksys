#!/usr/bin/env python3
"""Local-only drag-and-drop wrapper for GPT-SoVITS dataset preparation.

This process is deliberately separate from the production FastAPI app.  It
accepts only loopback HTTP traffic, keeps the uploaded export in a private
temporary directory, and invokes ``prepare_gpt_sovits_dataset.py`` in a
background thread.  No route serves the downloaded recordings or generated
audio.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = Path(__file__).with_name("gpt_sovits_preparer_ui.html")
PREPARER_PATH = Path(__file__).with_name("prepare_gpt_sovits_dataset.py")
sys.path.insert(0, str(ROOT))

try:
    from system_limits import (
        DATASET_ARCHIVE_MAX_BYTES,
        DATASET_ARCHIVE_MAX_ITEMS,
        DATASET_MANIFEST_MAX_BYTES,
    )
except Exception:  # pragma: no cover - defensive fallback for a copied helper
    DATASET_ARCHIVE_MAX_BYTES = 10 * 1024**3
    DATASET_ARCHIVE_MAX_ITEMS = 5_000
    DATASET_MANIFEST_MAX_BYTES = 10 * 1024**2


LOOPBACK_HOST = "127.0.0.1"
UPLOAD_CHUNK_BYTES = 1024 * 1024
PROGRESS_MAX_BYTES = 1024 * 1024
RESULT_MAX_BYTES = 2 * 1024 * 1024
SETTINGS_MAX_BYTES = 16 * 1024
MIN_FREE_OUTPUT_BYTES = 10 * 1024**3
ACTIVE_STATUSES = frozenset({"queued", "running"})
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

Worker = Callable[[Path, Path, Path], int]
SystemProvider = Callable[[Path], dict[str, object]]


class JobConflict(RuntimeError):
    """Raised when a second preparation is submitted while one is active."""


class RequestRejected(ValueError):
    """A safe, user-facing request validation failure."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class UploadRejected(RequestRejected):
    """A safe, user-facing upload validation failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_private_dir(path: Path, *, chmod_existing: bool = True) -> Path:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if chmod_existing or not existed:
            path.chmod(0o700)
    except OSError:
        # Windows and some mounted filesystems do not expose POSIX modes.  The
        # application still never serves files from this directory.
        pass
    return path


def _validated_output_root(value: object) -> Path:
    if not isinstance(value, str):
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "輸出根目錄必須係文字路徑。")
    raw = value.strip()
    if not raw or "\x00" in raw or "\r" in raw or "\n" in raw:
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "請輸入有效嘅輸出根目錄。")
    try:
        expanded = Path(raw).expanduser()
    except (OSError, RuntimeError) as exc:
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "輸出根目錄格式不正確。") from exc
    if not expanded.is_absolute():
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "輸出根目錄必須使用絕對路徑或 ~/ 開頭。")
    try:
        root = expanded.resolve()
        if root.exists() and not root.is_dir():
            raise RequestRejected(HTTPStatus.BAD_REQUEST, "輸出路徑現時係檔案，唔係資料夾。")
        # Do not chmod an existing user-selected parent such as ~/Documents.
        # Every workspace and incoming directory created below it remains 0700.
        _ensure_private_dir(root, chmod_existing=False)
    except RequestRejected:
        raise
    except OSError as exc:
        raise RequestRejected(
            HTTPStatus.BAD_REQUEST,
            f"未能建立或使用輸出根目錄（{type(exc).__name__}）。",
        ) from exc
    if not os.access(root, os.W_OK | os.X_OK):
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "輸出根目錄不可寫入。")
    try:
        free_bytes = shutil.disk_usage(root).free
    except OSError as exc:
        raise RequestRejected(
            HTTPStatus.BAD_REQUEST,
            f"未能檢查輸出磁碟（{type(exc).__name__}）。",
        ) from exc
    if free_bytes < MIN_FREE_OUTPUT_BYTES:
        raise RequestRejected(
            HTTPStatus.CONFLICT,
            f"輸出磁碟只有 {free_bytes / 1024**3:.1f} GB 可用；最少需要 10 GB。",
        )
    return root


def _safe_payload(value):
    """Recursively remove URLs before data reaches the browser or a log."""
    if isinstance(value, dict):
        return {
            str(_safe_payload(str(key))): _safe_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_payload(item) for item in value]
    if isinstance(value, str):
        return URL_RE.sub("[已隱藏 URL]", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _load_small_json(path: Path, max_bytes: int) -> dict[str, object] | None:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _default_worker(input_path: Path, workspace: Path, progress_path: Path) -> int:
    command = [
        sys.executable,
        str(PREPARER_PATH),
        str(input_path),
        "--output-dir",
        str(workspace),
        "--progress-file",
        str(progress_path),
    ]
    # The preparer writes a structured progress/result contract.  Suppressing
    # child output prevents an unexpected dependency error from echoing a
    # signed R2 URL into terminal history.
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return 127
    return int(completed.returncode)


def _system_snapshot(output_root: Path) -> dict[str, object]:
    hardware: dict[str, object]
    recommendation: dict[str, object]
    try:
        from tools import prepare_gpt_sovits_dataset as engine

        hardware = dict(engine._hardware_info())
        recommendation = dict(engine._recommended_params(hardware, 0.0))
    except Exception:
        hardware = {
            "system": sys.platform,
            "machine": "unknown",
            "cpu_count": os.cpu_count() or 1,
            "memory_gb": None,
            "nvidia_vram_gb": [],
        }
        recommendation = {}

    dependencies = {
        name: {"available": bool(shutil.which(name)), "path": shutil.which(name) or ""}
        for name in ("ffmpeg", "ffprobe", "nvidia-smi")
    }
    disk = shutil.disk_usage(output_root)
    disk_info = {
        "total_gb": round(disk.total / 1024**3, 1),
        "free_gb": round(disk.free / 1024**3, 1),
    }

    checks: list[dict[str, str]] = []
    for executable in ("ffmpeg", "ffprobe"):
        if dependencies[executable]["available"]:
            checks.append({
                "status": "ok",
                "label": executable,
                "detail": "已安裝",
            })
        else:
            checks.append({
                "status": "error",
                "label": executable,
                "detail": "未安裝；音訊核對及轉檔不能開始",
            })

    free_gb = float(disk_info["free_gb"])
    if free_gb < 10:
        checks.append({
            "status": "error",
            "label": "可用磁碟空間",
            "detail": f"只有 {free_gb:.1f} GB；最少先騰出 10 GB",
        })
    elif free_gb < 200:
        checks.append({
            "status": "warning",
            "label": "可用磁碟空間",
            "detail": f"{free_gb:.1f} GB；完整實驗建議預留 200 GB",
        })
    else:
        checks.append({
            "status": "ok",
            "label": "可用磁碟空間",
            "detail": f"{free_gb:.1f} GB",
        })

    memory = hardware.get("memory_gb")
    if isinstance(memory, (int, float)) and float(memory) < 32:
        checks.append({
            "status": "warning",
            "label": "記憶體",
            "detail": f"{float(memory):.1f} GB；baseline 建議 32 GB",
        })

    has_nvidia = bool(
        hardware.get("nvidia_vram_gb")
        or hardware.get("nvidia_gpus")
        or hardware.get("nvidia_smi_gpus")
        or any(
            isinstance(gpu, dict) and gpu.get("backend") == "cuda"
            for gpu in (hardware.get("gpus") or [])
        )
    )
    checks.append({
        "status": "ok" if has_nvidia else "warning",
        "label": "NVIDIA GPU",
        "detail": "已偵測，可按 VRAM 建議參數" if has_nvidia else "未偵測；CPU 訓練會非常慢",
    })

    return {
        "hardware": _safe_payload(hardware),
        "dependencies": dependencies,
        "disk": disk_info,
        "preflight": checks,
        "ready": not any(item["status"] == "error" for item in checks),
        "initial_recommendation": _safe_payload(recommendation),
        "output_root": str(output_root),
        "limits": {
            "json_bytes": int(DATASET_MANIFEST_MAX_BYTES),
            "zip_bytes": int(DATASET_ARCHIVE_MAX_BYTES),
            "archive_items": int(DATASET_ARCHIVE_MAX_ITEMS),
        },
    }


class JobManager:
    """Serialize local preparation jobs and expose a URL-free snapshot."""

    def __init__(self, output_root: Path, worker: Worker | None = None):
        self.output_root = _ensure_private_dir(output_root)
        self.incoming_dir = _ensure_private_dir(self.output_root / ".incoming")
        self.worker = worker or _default_worker
        self._lock = threading.Lock()
        self._job: dict[str, object] = {
            "id": "",
            "status": "idle",
            "message": "等待拖入 recordings.json 或舊版 ZIP。",
            "created_at": None,
            "started_at": None,
            "finished_at": None,
            "workspace": "",
            "progress_path": None,
            "result": None,
            "error": "",
        }

    def is_active(self) -> bool:
        with self._lock:
            return str(self._job.get("status")) in ACTIVE_STATUSES

    def set_output_root(self, value: object) -> Path:
        with self._lock:
            if str(self._job.get("status")) in ACTIVE_STATUSES:
                raise JobConflict("資料準備進行期間唔可以更改輸出根目錄")
            output_root = _validated_output_root(value)
            incoming_dir = _ensure_private_dir(output_root / ".incoming")
            self.output_root = output_root
            self.incoming_dir = incoming_dir
            return output_root

    def start(self, input_path: Path) -> dict[str, object]:
        with self._lock:
            if str(self._job.get("status")) in ACTIVE_STATUSES:
                raise JobConflict("已有資料準備工作進行中")
            job_id = secrets.token_hex(8)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            workspace = self.output_root / f"tts-{stamp}-{job_id[:6]}"
            workspace.mkdir(mode=0o700)
            try:
                workspace.chmod(0o700)
            except OSError:
                pass
            progress_path = workspace / ".progress.json"
            self._job = {
                "id": job_id,
                "status": "queued",
                "message": "已接收檔案，準備開始。",
                "created_at": _utc_now(),
                "started_at": None,
                "finished_at": None,
                "workspace": str(workspace),
                "progress_path": progress_path,
                "result": None,
                "error": "",
            }
            thread = threading.Thread(
                target=self._run,
                args=(job_id, input_path, workspace, progress_path),
                name=f"gpt-sovits-preparer-{job_id[:6]}",
                daemon=True,
            )
            thread.start()
        return self.snapshot()

    def _run(
        self,
        job_id: str,
        input_path: Path,
        workspace: Path,
        progress_path: Path,
    ) -> None:
        with self._lock:
            if self._job.get("id") != job_id:
                input_path.unlink(missing_ok=True)
                return
            self._job["status"] = "running"
            self._job["started_at"] = _utc_now()
            self._job["message"] = "正在下載、核對及整理錄音。"
        try:
            return_code = int(self.worker(input_path, workspace, progress_path))
            result_path = workspace / "preparation_result.json"
            result = _load_small_json(result_path, RESULT_MAX_BYTES)
            progress = _load_small_json(progress_path, PROGRESS_MAX_BYTES) or {}
            if return_code != 0:
                progress_error = progress.get("error")
                if not progress_error and progress.get("stage") == "error":
                    progress_error = progress.get("message")
                error = (
                    str(_safe_payload(progress_error))
                    if progress_error
                    else "資料處理未能完成；請重新匯出未過期清單，再核對 ffmpeg 及硬件。"
                )
                with self._lock:
                    if self._job.get("id") == job_id:
                        self._job.update({
                            "status": "error",
                            "message": "處理失敗。",
                            "error": error,
                            "finished_at": _utc_now(),
                        })
                return
            if result is None:
                with self._lock:
                    if self._job.get("id") == job_id:
                        self._job.update({
                            "status": "error",
                            "message": "處理完成但缺少結果摘要。",
                            "error": "找不到 preparation_result.json；資料不會自動送去訓練。",
                            "finished_at": _utc_now(),
                        })
                return
            with self._lock:
                if self._job.get("id") == job_id:
                    self._job.update({
                        "status": "completed",
                        "message": "資料集已準備完成。",
                        "result": _safe_payload(result),
                        "finished_at": _utc_now(),
                    })
        except Exception:
            # Never return exception text: urllib/subprocess exceptions may
            # contain a complete signed URL supplied by the manifest.
            with self._lock:
                if self._job.get("id") == job_id:
                    self._job.update({
                        "status": "error",
                        "message": "處理失敗。",
                        "error": "本機工具遇到未預期錯誤；未完成資料不可用作訓練。",
                        "finished_at": _utc_now(),
                    })
        finally:
            input_path.unlink(missing_ok=True)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            raw = dict(self._job)
        progress_path = raw.pop("progress_path", None)
        progress = None
        if isinstance(progress_path, Path):
            progress = _load_small_json(progress_path, PROGRESS_MAX_BYTES)
        raw["progress"] = _safe_payload(progress or {})
        return _safe_payload(raw)


class PreparerHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying local-only state for request handlers."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        token: str,
        output_root: Path,
        worker: Worker | None = None,
        system_provider: SystemProvider | None = None,
        ui_path: Path = UI_PATH,
    ):
        if address[0] != LOOPBACK_HOST:
            raise ValueError("GPT-SoVITS preparer may only bind 127.0.0.1")
        self.token = token
        self.output_root = _ensure_private_dir(output_root)
        self.jobs = JobManager(self.output_root, worker=worker)
        self.system_provider = system_provider or _system_snapshot
        self.ui_path = ui_path
        self._system_lock = threading.Lock()
        self._system_cache: dict[str, object] | None = None
        super().__init__(address, PreparerRequestHandler)

    @property
    def expected_host(self) -> str:
        return f"{LOOPBACK_HOST}:{self.server_port}"

    @property
    def expected_origin(self) -> str:
        return f"http://{self.expected_host}"

    def system_snapshot(self) -> dict[str, object]:
        with self._system_lock:
            if self._system_cache is None:
                self._system_cache = _safe_payload(
                    self.system_provider(self.output_root)
                )
            return dict(self._system_cache)

    def update_output_root(self, value: object) -> dict[str, object]:
        root = self.jobs.set_output_root(value)
        self.output_root = root
        with self._system_lock:
            self._system_cache = None
        return self.system_snapshot()


class PreparerRequestHandler(BaseHTTPRequestHandler):
    """Small authenticated API; intentionally has no static-file fallback."""

    server: PreparerHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args) -> None:
        # Request targets, headers and worker output may contain credentials.
        return

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        csp: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if csp:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; base-uri 'none'; form-action 'none'",
            )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(
            _safe_payload(payload), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _reject(self, status: int, message: str) -> bool:
        self._send_json(status, {"ok": False, "error": message})
        return False

    def _authorised(self, *, query_token: bool = False, require_origin: bool = False) -> bool:
        if self.client_address[0] != LOOPBACK_HOST:
            return self._reject(HTTPStatus.FORBIDDEN, "只接受本機連線。")
        if self.headers.get("Host", "") != self.server.expected_host:
            return self._reject(HTTPStatus.FORBIDDEN, "Host 驗證失敗。")

        origin = self.headers.get("Origin", "")
        if require_origin and origin != self.server.expected_origin:
            return self._reject(HTTPStatus.FORBIDDEN, "Origin 驗證失敗。")
        if origin and origin != self.server.expected_origin:
            return self._reject(HTTPStatus.FORBIDDEN, "Origin 驗證失敗。")

        supplied = self.headers.get("X-Preparer-Token", "")
        if query_token:
            query = parse_qs(urlsplit(self.path).query)
            supplied = str((query.get("token") or [""])[0])
        if not supplied or not secrets.compare_digest(supplied, self.server.token):
            return self._reject(HTTPStatus.FORBIDDEN, "本機工作階段已失效，請重新啟動工具。")
        return True

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = urlsplit(self.path).path
        if path != "/":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "找不到頁面。"})
            return
        if not self._authorised(query_token=True):
            return
        self._send_bytes(HTTPStatus.OK, b"", "text/html; charset=utf-8", csp=True)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = urlsplit(self.path).path
        if path == "/":
            if not self._authorised(query_token=True):
                return
            try:
                body = self.server.ui_path.read_bytes()
            except OSError:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                    "ok": False,
                    "error": "找不到本機介面檔案。",
                })
                return
            self._send_bytes(
                HTTPStatus.OK, body, "text/html; charset=utf-8", csp=True
            )
            return
        if path == "/api/system":
            if not self._authorised():
                return
            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "system": self.server.system_snapshot(),
                "job": self.server.jobs.snapshot(),
            })
            return
        if path == "/api/job":
            if not self._authorised():
                return
            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "job": self.server.jobs.snapshot(),
            })
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "找不到頁面。"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = urlsplit(self.path).path
        if path not in {"/api/output-root", "/api/prepare"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "找不到頁面。"})
            return
        if not self._authorised(require_origin=True):
            return
        if path == "/api/output-root":
            self._update_output_root()
            return
        if not bool(self.server.system_snapshot().get("ready")):
            self._send_json(HTTPStatus.CONFLICT, {
                "ok": False,
                "error": "本機預檢未通過；請先安裝 ffmpeg／ffprobe 並騰出足夠磁碟空間。",
            })
            return
        if self.server.jobs.is_active():
            self.close_connection = True
            self._send_json(HTTPStatus.CONFLICT, {
                "ok": False,
                "error": "已有資料準備工作進行中，請等待完成。",
            })
            return

        temp_path: Path | None = None
        try:
            filename, suffix, size = self._validate_upload_headers()
            del filename  # The private random temp name is used on disk.
            temp_path = self._receive_upload(suffix, size)
            self._validate_uploaded_file(temp_path, suffix)
            job = self.server.jobs.start(temp_path)
            temp_path = None  # JobManager owns and deletes it from now on.
        except UploadRejected as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            self._send_json(exc.status, {"ok": False, "error": str(exc)})
            return
        except JobConflict as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
            return
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False,
                "error": "未能安全接收檔案；請重新啟動工具再試。",
            })
            return

        self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "job": job})

    def _update_output_root(self) -> None:
        try:
            payload = self._read_small_json()
            system = self.server.update_output_root(payload.get("output_root"))
        except RequestRejected as exc:
            self._send_json(exc.status, {"ok": False, "error": str(exc)})
            return
        except JobConflict as exc:
            self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
            return
        except Exception:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False,
                "error": "未能更新輸出根目錄；請核對路徑及資料夾權限。",
            })
            return
        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "system": system,
            "job": self.server.jobs.snapshot(),
        })

    def _read_small_json(self) -> dict[str, object]:
        if self.headers.get("Transfer-Encoding"):
            raise RequestRejected(HTTPStatus.LENGTH_REQUIRED, "請以一般 JSON request 提交設定。")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise RequestRejected(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "設定只接受 JSON。")
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise RequestRejected(HTTPStatus.LENGTH_REQUIRED, "設定內容大小不正確。") from exc
        if size <= 0:
            raise RequestRejected(HTTPStatus.BAD_REQUEST, "設定內容係空嘅。")
        if size > SETTINGS_MAX_BYTES:
            raise RequestRejected(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "設定內容過大。")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise RequestRejected(HTTPStatus.BAD_REQUEST, "設定內容傳送中途停止。")
        try:
            payload = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RequestRejected(HTTPStatus.BAD_REQUEST, "設定 JSON 格式不正確。") from exc
        if not isinstance(payload, dict):
            raise RequestRejected(HTTPStatus.BAD_REQUEST, "設定 JSON 必須係 object。")
        return payload

    def _validate_upload_headers(self) -> tuple[str, str, int]:
        if self.headers.get("Transfer-Encoding"):
            raise UploadRejected(HTTPStatus.LENGTH_REQUIRED, "請以一般檔案上載。")
        encoded_name = self.headers.get("X-File-Name", "")
        if not encoded_name or len(encoded_name) > 512:
            raise UploadRejected(HTTPStatus.BAD_REQUEST, "缺少檔案名稱。")
        try:
            filename = unquote(encoded_name, errors="strict")
        except UnicodeError as exc:
            raise UploadRejected(HTTPStatus.BAD_REQUEST, "檔案名稱編碼不正確。") from exc
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
        ):
            raise UploadRejected(HTTPStatus.BAD_REQUEST, "檔案名稱不安全。")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".json", ".zip"}:
            raise UploadRejected(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "只接受 AI Training 下載的 recordings.json 或舊版 dataset ZIP。",
            )
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise UploadRejected(HTTPStatus.LENGTH_REQUIRED, "檔案大小資料不正確。") from exc
        if size <= 0:
            raise UploadRejected(HTTPStatus.BAD_REQUEST, "檔案是空的。")
        limit = DATASET_MANIFEST_MAX_BYTES if suffix == ".json" else DATASET_ARCHIVE_MAX_BYTES
        if size > limit:
            limit_mb = limit // (1024 * 1024)
            raise UploadRejected(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"檔案超過 {limit_mb} MB 本機安全上限。",
            )
        return filename, suffix, size

    def _receive_upload(self, suffix: str, size: int) -> Path:
        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="upload-",
            suffix=suffix,
            dir=self.server.jobs.incoming_dir,
            delete=False,
        )
        path = Path(handle.name)
        try:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            remaining = size
            while remaining:
                block = self.rfile.read(min(remaining, UPLOAD_CHUNK_BYTES))
                if not block:
                    raise UploadRejected(HTTPStatus.BAD_REQUEST, "檔案傳送中途停止。")
                handle.write(block)
                remaining -= len(block)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            handle.close()
            path.unlink(missing_ok=True)
            raise
        handle.close()
        return path

    @staticmethod
    def _validate_uploaded_file(path: Path, suffix: str) -> None:
        if suffix == ".zip":
            if not zipfile.is_zipfile(path):
                raise UploadRejected(HTTPStatus.BAD_REQUEST, "ZIP 檔案已損壞或格式不正確。")
            return
        try:
            value = json.loads(path.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise UploadRejected(HTTPStatus.BAD_REQUEST, "recordings.json 格式不正確。") from exc
        items = value.get("items") if isinstance(value, dict) else None
        if not isinstance(items, list):
            raise UploadRejected(
                HTTPStatus.BAD_REQUEST,
                "recordings.json 必須包含 items 清單。",
            )
        if not items:
            raise UploadRejected(HTTPStatus.BAD_REQUEST, "recordings.json 沒有錄音。")
        if len(items) > DATASET_ARCHIVE_MAX_ITEMS:
            raise UploadRejected(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"錄音數量超過 {DATASET_ARCHIVE_MAX_ITEMS} 項安全上限。",
            )
        if any(not isinstance(item, dict) for item in items):
            raise UploadRejected(HTTPStatus.BAD_REQUEST, "recordings.json 內有無效項目。")
        speakers = {
            str(item.get("speaker_user_id") or "").strip()
            for item in items
            if str(item.get("speaker_user_id") or "").strip()
        }
        if len(speakers) > 1:
            raise UploadRejected(
                HTTPStatus.BAD_REQUEST,
                "清單包含多個錄音者；請返 AI Training 揀定一位錄音者再下載。",
            )


def create_server(
    *,
    port: int = 0,
    output_root: str | Path | None = None,
    token: str | None = None,
    worker: Worker | None = None,
    system_provider: SystemProvider | None = None,
    ui_path: Path = UI_PATH,
) -> PreparerHTTPServer:
    root = Path(output_root or "~/private-ai-training").expanduser().resolve()
    return PreparerHTTPServer(
        (LOOPBACK_HOST, int(port)),
        token=token or secrets.token_urlsafe(32),
        output_root=root,
        worker=worker,
        system_provider=system_provider,
        ui_path=ui_path,
    )


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 0 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在本機開啟 GPT-SoVITS 錄音資料拖放準備工具。"
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=0,
        help="localhost port；預設 0 代表每次自動選擇空閒 port。",
    )
    parser.add_argument(
        "--output-root",
        default="~/private-ai-training",
        help="私人資料集輸出根目錄。",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="只顯示本機 URL，不自動開瀏覽器。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    if not UI_PATH.is_file() or not PREPARER_PATH.is_file():
        print("錯誤：本機工具檔案不完整。", file=sys.stderr)
        return 1
    try:
        server = create_server(port=args.port, output_root=args.output_root)
    except OSError as exc:
        print(f"錯誤：未能啟動 localhost server（{type(exc).__name__}）。", file=sys.stderr)
        return 1

    url = f"{server.expected_origin}/?token={server.token}"
    print("GPT-SoVITS 本機資料準備工具已啟動。")
    print(f"輸出目錄：{server.output_root}")
    print(f"本機介面：{url}")
    print("只接受 127.0.0.1；按 Ctrl+C 關閉。")
    if not args.no_browser:
        webbrowser.open(url, new=1, autoraise=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
