"""Strict non-secret Workstation configuration.

Secrets are references to root-managed files and are never copied into the
parsed public configuration or GUI responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workstation.version import WORKSTATION_CONFIG_SCHEMA_VERSION


DEFAULT_CONFIG_PATH = Path("/etc/lmc-ai-workstation/config.json")
DEFAULT_CREDENTIALS_DIR = Path("/etc/lmc-ai-workstation/credentials")
DEFAULT_STATE_DIR = Path("/var/lib/lmc-ai-workstation")
DEFAULT_CACHE_DIR = Path("/var/cache/lmc-ai-workstation")
DEFAULT_DATA_ROOT = Path("/srv/lmc-ai")
DEFAULT_RELEASE_ROOT = Path("/opt/lmc-ai-workstation")
RELEASE_STATE_RELATIVE_PATH = Path("release") / "release-state.json"
_NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,80}")
_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_CONFIG_MAX_BYTES = 256 * 1024


class ConfigError(ValueError):
    """A typed configuration field is missing or unsafe."""


def _object(value: object, field_name: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be an object")
    return value


def _string(value: object, field_name: str, *, maximum: int = 500) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > maximum or "\x00" in clean:
        raise ConfigError(f"{field_name} is invalid")
    return clean


def _absolute_path(value: object, field_name: str) -> Path:
    path = Path(_string(value, field_name, maximum=1_024))
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{field_name} must be an absolute normalized path")
    return path


def _local_url(value: object, field_name: str, *, schemes=("http",)) -> str:
    raw = _string(value, field_name)
    parsed = urlparse(raw)
    if (
        parsed.scheme not in schemes
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(f"{field_name} must use a localhost URL")
    return raw.rstrip("/")


def _server_url(value: object) -> str:
    raw = _string(value, "node.server_url")
    parsed = urlparse(raw)
    if (
        parsed.scheme not in {"https", "wss"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("node.server_url must be an https/wss URL")
    scheme = "wss" if parsed.scheme == "https" else parsed.scheme
    path = parsed.path.rstrip("/")
    suffix = "/api/lmc-ai/nodes/connect"
    if not path.endswith(suffix):
        path += suffix
    return f"{scheme}://{parsed.netloc}{path}"


@dataclass(frozen=True)
class PathsConfig:
    state: Path = DEFAULT_STATE_DIR
    cache: Path = DEFAULT_CACHE_DIR
    data: Path = DEFAULT_DATA_ROOT
    releases: Path = DEFAULT_RELEASE_ROOT


@dataclass(frozen=True)
class NodeConfig:
    name: str
    server_url: str
    token_file: Path


@dataclass(frozen=True)
class PowerConfig:
    enabled: bool = False
    timezone: str = "Asia/Hong_Kong"
    suspend_at: str = "00:00"
    wake_at: str = "08:00"


@dataclass(frozen=True)
class OllamaConfig:
    url: str = "http://127.0.0.1:11434"
    voice_keep_alive: str = "0"
    text_keep_alive: str = "5m"


@dataclass(frozen=True)
class AsrConfig:
    enabled: bool = False
    model: str = ""
    device: str = "cuda"
    compute_type: str = "float16"
    benchmark_approved: bool = False
    runtime_python: Path = (
        DEFAULT_DATA_ROOT / "vendor" / "asr-runtime" / "bin" / "python"
    )
    benchmark_report: Path = DEFAULT_STATE_DIR / "asr-benchmark-report.json"
    runtime_provenance: Path = (
        DEFAULT_DATA_ROOT / "vendor" / "asr-runtime" / "PROVENANCE.json"
    )
    approval_receipt: Path = (
        DEFAULT_DATA_ROOT / "models" / "asr" / "active-receipt.json"
    )


@dataclass(frozen=True)
class RagConfig:
    enabled: bool = False
    embedding_model: str = ""
    active_link: Path = DEFAULT_DATA_ROOT / "rag" / "current"


@dataclass(frozen=True)
class GptSoVitsConfig:
    enabled: bool = False
    url: str = "http://127.0.0.1:9880"
    runtime_root: Path = DEFAULT_DATA_ROOT / "vendor" / "GPT-SoVITS"
    model_version: str = ""
    reference_audio: Path = DEFAULT_DATA_ROOT / "models" / "gpt-sovits" / "reference.wav"
    reference_text_file: Path = DEFAULT_DATA_ROOT / "models" / "gpt-sovits" / "reference.txt"
    inference_config: Path = DEFAULT_DATA_ROOT / "models" / "gpt-sovits" / "tts_infer.json"
    approval_receipt: Path = DEFAULT_DATA_ROOT / "models" / "gpt-sovits" / "active-receipt.json"
    language: str = "yue"


@dataclass(frozen=True)
class WorkloadConfig:
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    rag: RagConfig = field(default_factory=RagConfig)
    gpt_sovits: GptSoVitsConfig = field(default_factory=GptSoVitsConfig)


@dataclass(frozen=True)
class GuiConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class UpdateConfig:
    enabled: bool = False
    channel: str = "stable"
    manifest_url: str = "https://example.invalid/api/lmc-ai/workstation/releases/stable"
    public_key_file: Path = Path(
        "/usr/share/lmc-ai-workstation/release-signing-public-key.pem"
    )
    auth_token_file: Path = DEFAULT_CREDENTIALS_DIR / "node-token"


@dataclass(frozen=True)
class WorkstationConfig:
    schema_version: int
    node: NodeConfig
    paths: PathsConfig = field(default_factory=PathsConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    workloads: WorkloadConfig = field(default_factory=WorkloadConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)

    def public_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "node": {"name": self.node.name, "server_url": self.node.server_url},
            "paths": {key: str(getattr(self.paths, key)) for key in ("state", "cache", "data", "releases")},
            "power": {
                "enabled": self.power.enabled,
                "timezone": self.power.timezone,
                "suspend_at": self.power.suspend_at,
                "wake_at": self.power.wake_at,
            },
            "workloads": {
                "asr": {"enabled": self.workloads.asr.enabled, "model": self.workloads.asr.model, "benchmark_approved": self.workloads.asr.benchmark_approved, "runtime_python": str(self.workloads.asr.runtime_python), "benchmark_report": str(self.workloads.asr.benchmark_report), "runtime_provenance": str(self.workloads.asr.runtime_provenance), "approval_receipt": str(self.workloads.asr.approval_receipt)},
                "rag": {"enabled": self.workloads.rag.enabled, "embedding_model": self.workloads.rag.embedding_model},
                "gpt_sovits": {"enabled": self.workloads.gpt_sovits.enabled, "model_version": self.workloads.gpt_sovits.model_version},
            },
            "gui": {"host": self.gui.host, "port": self.gui.port},
            "update": {
                "enabled": self.update.enabled,
                "channel": self.update.channel,
                "manifest_url": self.update.manifest_url,
                "public_key_file": str(self.update.public_key_file),
            },
        }


def parse_config(raw: object) -> WorkstationConfig:
    root = _object(raw, "config")
    schema_version = int(root.get("schema_version") or 0)
    if schema_version != WORKSTATION_CONFIG_SCHEMA_VERSION:
        raise ConfigError("unsupported config schema_version")

    node_raw = _object(root.get("node"), "node")
    name = _string(node_raw.get("name"), "node.name", maximum=80)
    if not _NAME_RE.fullmatch(name):
        raise ConfigError("node.name contains control characters")
    node = NodeConfig(
        name=name,
        server_url=_server_url(node_raw.get("server_url")),
        token_file=_absolute_path(node_raw.get("token_file"), "node.token_file"),
    )

    paths_raw = _object(root.get("paths", {}), "paths")
    paths = PathsConfig(**{
        key: _absolute_path(paths_raw.get(key, default), f"paths.{key}")
        for key, default in {
            "state": DEFAULT_STATE_DIR,
            "cache": DEFAULT_CACHE_DIR,
            "data": DEFAULT_DATA_ROOT,
            "releases": DEFAULT_RELEASE_ROOT,
        }.items()
    })

    power_raw = _object(root.get("power", {}), "power")
    timezone = str(power_raw.get("timezone") or "Asia/Hong_Kong")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError("power.timezone is invalid") from exc
    suspend_at = str(power_raw.get("suspend_at") or "00:00")
    wake_at = str(power_raw.get("wake_at") or "08:00")
    if not _TIME_RE.fullmatch(suspend_at) or not _TIME_RE.fullmatch(wake_at):
        raise ConfigError("power schedule must use HH:MM")
    if suspend_at == wake_at:
        raise ConfigError("power suspend_at and wake_at must differ")
    power = PowerConfig(bool(power_raw.get("enabled")), timezone, suspend_at, wake_at)

    workloads_raw = _object(root.get("workloads", {}), "workloads")
    ollama_raw = _object(workloads_raw.get("ollama", {}), "workloads.ollama")
    ollama = OllamaConfig(
        url=_local_url(ollama_raw.get("url", "http://127.0.0.1:11434"), "workloads.ollama.url"),
        voice_keep_alive=str(ollama_raw.get("voice_keep_alive", "0"))[:20],
        text_keep_alive=str(ollama_raw.get("text_keep_alive", "5m"))[:20],
    )
    asr_raw = _object(workloads_raw.get("asr", {}), "workloads.asr")
    asr = AsrConfig(
        enabled=bool(asr_raw.get("enabled")),
        model=str(asr_raw.get("model") or "")[:200],
        device=str(asr_raw.get("device") or "cuda")[:20],
        compute_type=str(asr_raw.get("compute_type") or "float16")[:40],
        benchmark_approved=bool(asr_raw.get("benchmark_approved")),
        runtime_python=_absolute_path(
            asr_raw.get(
                "runtime_python",
                paths.data / "vendor" / "asr-runtime" / "bin" / "python",
            ),
            "workloads.asr.runtime_python",
        ),
        benchmark_report=_absolute_path(
            asr_raw.get(
                "benchmark_report",
                paths.state / "asr-benchmark-report.json",
            ),
            "workloads.asr.benchmark_report",
        ),
        runtime_provenance=_absolute_path(
            asr_raw.get(
                "runtime_provenance",
                paths.data / "vendor" / "asr-runtime" / "PROVENANCE.json",
            ),
            "workloads.asr.runtime_provenance",
        ),
        approval_receipt=_absolute_path(
            asr_raw.get(
                "approval_receipt",
                paths.data / "models" / "asr" / "active-receipt.json",
            ),
            "workloads.asr.approval_receipt",
        ),
    )
    if asr.device not in {"cuda", "cpu"} or asr.compute_type not in {
        "float16", "int8_float16", "int8", "float32",
    }:
        raise ConfigError("ASR device or compute_type is invalid")
    if asr.enabled and (not asr.model or not asr.benchmark_approved):
        raise ConfigError("enabled ASR requires a benchmark-approved model")
    if asr.enabled and not Path(asr.model).is_absolute():
        raise ConfigError("enabled ASR model must be an absolute local path")
    rag_raw = _object(workloads_raw.get("rag", {}), "workloads.rag")
    rag = RagConfig(
        enabled=bool(rag_raw.get("enabled")),
        embedding_model=str(rag_raw.get("embedding_model") or "")[:200],
        active_link=_absolute_path(rag_raw.get("active_link", paths.data / "rag" / "current"), "workloads.rag.active_link"),
    )
    if rag.enabled and not rag.embedding_model:
        raise ConfigError("enabled RAG requires a local embedding model")
    tts_raw = _object(workloads_raw.get("gpt_sovits", {}), "workloads.gpt_sovits")
    gpt_sovits = GptSoVitsConfig(
        enabled=bool(tts_raw.get("enabled")),
        url=_local_url(tts_raw.get("url", "http://127.0.0.1:9880"), "workloads.gpt_sovits.url"),
        runtime_root=_absolute_path(tts_raw.get("runtime_root", paths.data / "vendor" / "GPT-SoVITS"), "workloads.gpt_sovits.runtime_root"),
        model_version=str(tts_raw.get("model_version") or "")[:200],
        reference_audio=_absolute_path(tts_raw.get("reference_audio", paths.data / "models" / "gpt-sovits" / "reference.wav"), "workloads.gpt_sovits.reference_audio"),
        reference_text_file=_absolute_path(tts_raw.get("reference_text_file", paths.data / "models" / "gpt-sovits" / "reference.txt"), "workloads.gpt_sovits.reference_text_file"),
        inference_config=_absolute_path(tts_raw.get("inference_config", paths.data / "models" / "gpt-sovits" / "tts_infer.json"), "workloads.gpt_sovits.inference_config"),
        approval_receipt=_absolute_path(tts_raw.get("approval_receipt", paths.data / "models" / "gpt-sovits" / "active-receipt.json"), "workloads.gpt_sovits.approval_receipt"),
        language=str(tts_raw.get("language") or "yue")[:20],
    )
    if gpt_sovits.enabled and not gpt_sovits.model_version:
        raise ConfigError("enabled GPT-SoVITS requires an approved model_version")
    gui_raw = _object(root.get("gui", {}), "gui")
    host = str(gui_raw.get("host") or "127.0.0.1")
    if host != "127.0.0.1":
        raise ConfigError("GUI must bind 127.0.0.1")
    port = int(gui_raw.get("port") or 8765)
    if port < 1_024 or port > 65_535:
        raise ConfigError("GUI port is out of range")
    update_raw = _object(root.get("update", {}), "update")
    channel = str(update_raw.get("channel") or "stable")
    if channel not in {"stable", "candidate"}:
        raise ConfigError("update.channel is invalid")
    manifest_url = _string(
        update_raw.get(
            "manifest_url",
            "https://example.invalid/api/lmc-ai/workstation/releases/stable",
        ),
        "update.manifest_url",
        maximum=2_048,
    )
    parsed_manifest_url = urlparse(manifest_url)
    if (
        parsed_manifest_url.scheme != "https"
        or not parsed_manifest_url.hostname
        or parsed_manifest_url.username
        or parsed_manifest_url.password
        or parsed_manifest_url.query
        or parsed_manifest_url.fragment
    ):
        raise ConfigError("update.manifest_url must use HTTPS")
    update = UpdateConfig(
        enabled=bool(update_raw.get("enabled")),
        channel=channel,
        manifest_url=manifest_url,
        public_key_file=_absolute_path(
            update_raw.get(
                "public_key_file",
                "/usr/share/lmc-ai-workstation/release-signing-public-key.pem",
            ),
            "update.public_key_file",
        ),
        auth_token_file=_absolute_path(
            update_raw.get("auth_token_file", DEFAULT_CREDENTIALS_DIR / "node-token"),
            "update.auth_token_file",
        ),
    )
    return WorkstationConfig(
        schema_version=schema_version,
        node=node,
        paths=paths,
        power=power,
        workloads=WorkloadConfig(ollama, asr, rag, gpt_sovits),
        gui=GuiConfig(host, port),
        update=update,
    )


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> WorkstationConfig:
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or not 0 < path.stat().st_size <= _CONFIG_MAX_BYTES
        ):
            raise ValueError("Workstation config file is invalid")
        raw = json.loads(path.read_bytes())
    except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read Workstation config: {path}") from exc
    return parse_config(raw)


def read_secret(path: Path, *, maximum_bytes: int = 4_096) -> str:
    try:
        stat = path.stat()
        if (
            path.is_symlink()
            or not path.is_file()
            or stat.st_size <= 0
            or stat.st_size > maximum_bytes
        ):
            raise ConfigError("credential file size is invalid")
        if stat.st_mode & 0o077:
            raise ConfigError("credential file must not be group/world accessible")
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise ConfigError("credential file is unavailable") from exc
    if not value or "\x00" in value:
        raise ConfigError("credential is empty or invalid")
    return value
