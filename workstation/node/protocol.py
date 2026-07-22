"""Strict protocol-v2 frame validation and capability advertisement."""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from core.media_probe import MediaProbeError, audio_extension, canonical_audio_mime
from system_limits import (
    LIVE_FREE_SESSION_MAX_SECONDS,
    LOCAL_PRACTICE_AUDIO_MAX_BYTES,
    LMC_AI_REQUEST_MESSAGES_MAX,
    TTS_TEXT_MAX_CHARS,
    WORKSTATION_RAG_TOP_K_MAX,
    WORKSTATION_VOICE_PROMPT_MAX_CHARS,
)
from workstation.version import WORKSTATION_PROTOCOL_VERSION, WORKSTATION_VERSION


_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
WORKSTATION_CAPABILITY_KEYS = (
    "chat",
    "rag",
    "asr",
    "local_tts",
    "tts_training",
    "direct_r2",
    "fine_tuned",
    "thinking_control",
    "manager",
)


def advertised_capabilities(health: dict) -> dict:
    checks = health.get("checks") if isinstance(health, dict) else {}
    checks = checks if isinstance(checks, dict) else {}
    return {
        "chat": bool(checks.get("ollama", {}).get("ok")),
        "rag": bool(checks.get("rag", {}).get("ok")),
        "asr": bool(checks.get("asr", {}).get("ok")),
        "local_tts": bool(checks.get("gpt_sovits", {}).get("ok")),
        "tts_training": bool(checks.get("gpt_sovits_training", {}).get("ok")),
        "direct_r2": bool(checks.get("r2", {}).get("ok")),
        "fine_tuned": False,
        "thinking_control": True,
        "manager": True,
    }


def hello_frame(
    *,
    name: str,
    model_profile_version: int,
    model: str,
    models: list[str],
    model_digests: dict[str, str],
    health: dict,
    manager: dict,
) -> dict:
    return {
        "type": "hello",
        "protocol": WORKSTATION_PROTOCOL_VERSION,
        "workstation_version": WORKSTATION_VERSION,
        "model_profile_version": int(model_profile_version),
        "name": str(name)[:80],
        "runtime": "lmc-ai-workstation",
        "runtime_version": WORKSTATION_VERSION,
        "model": str(model)[:200],
        "models": [str(item)[:200] for item in models],
        "model_digests": dict(model_digests),
        "ready": bool(health.get("healthy")),
        "draining": bool(manager.get("draining")),
        "manager": dict(manager),
        "capabilities": advertised_capabilities(health),
    }


def validate_server_job(value: object) -> dict:
    if not isinstance(value, dict) or value.get("type") != "workstation.job.start":
        raise ValueError("unsupported workstation job frame")
    allowed = {
        "type", "operation_id", "job_kind", "session_id", "turn_id", "stage",
        "deadline_epoch", "payload",
    }
    if set(value) - allowed:
        raise ValueError("workstation job contains unknown fields")
    operation_id = str(value.get("operation_id") or "")
    job_kind = str(value.get("job_kind") or "")
    if not _ID_RE.fullmatch(operation_id) or job_kind not in {
        "voice.reserve", "voice.release", "asr", "rag", "voice_text", "tts",
    }:
        raise ValueError("invalid workstation job identity")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("workstation job payload must be an object")
    deadline = int(value.get("deadline_epoch") or 0)
    if deadline <= 0:
        raise ValueError("workstation job deadline is required")
    session_id = str(value.get("session_id") or "")
    turn_id = str(value.get("turn_id") or "")
    if not _ID_RE.fullmatch(session_id):
        raise ValueError("workstation session identity is invalid")
    if turn_id and not _ID_RE.fullmatch(turn_id):
        raise ValueError("workstation turn identity is invalid")
    if job_kind == "voice.reserve":
        if set(payload) != {"session_expires_epoch"}:
            raise ValueError("voice reservation payload is invalid")
        expires = int(payload.get("session_expires_epoch") or 0)
        now = int(time.time())
        if expires <= now or expires > now + LIVE_FREE_SESSION_MAX_SECONDS:
            raise ValueError("voice reservation expiry is invalid")
        clean_payload = {"session_expires_epoch": expires}
    elif job_kind == "voice.release":
        if payload:
            raise ValueError("voice release payload is invalid")
        clean_payload = {}
    elif job_kind == "asr":
        if set(payload) != {"download", "mime_type", "file_ext"}:
            raise ValueError("ASR payload is invalid")
        download = payload.get("download")
        if not isinstance(download, dict) or set(download) != {"url", "byte_size", "sha256"}:
            raise ValueError("ASR download claim is invalid")
        parsed = urlparse(str(download.get("url") or ""))
        size = int(download.get("byte_size") or 0)
        sha = str(download.get("sha256") or "").lower()
        try:
            mime = canonical_audio_mime(str(payload.get("mime_type") or ""))
        except MediaProbeError as exc:
            raise ValueError("ASR MIME is invalid") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not 1 <= size <= LOCAL_PRACTICE_AUDIO_MAX_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or str(payload.get("file_ext") or "") != audio_extension(mime)
        ):
            raise ValueError("ASR download claim is invalid")
        clean_payload = {
            "download": {"url": parsed.geturl(), "byte_size": size, "sha256": sha},
            "mime_type": mime,
            "file_ext": audio_extension(mime),
        }
    elif job_kind == "rag":
        if set(payload) != {"query", "top_k"}:
            raise ValueError("RAG payload is invalid")
        query = str(payload.get("query") or "").strip()
        top_k = int(payload.get("top_k") or 0)
        if (
            not query
            or len(query) > 500
            or not 1 <= top_k <= WORKSTATION_RAG_TOP_K_MAX
        ):
            raise ValueError("RAG payload is invalid")
        clean_payload = {"query": query, "top_k": top_k}
    elif job_kind == "voice_text":
        if set(payload) != {"messages"} or not isinstance(payload.get("messages"), list):
            raise ValueError("Voice Coach prompt is invalid")
        messages = payload["messages"]
        if not 1 <= len(messages) <= LMC_AI_REQUEST_MESSAGES_MAX:
            raise ValueError("Voice Coach prompt is invalid")
        clean_messages = []
        total_chars = 0
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise ValueError("Voice Coach prompt is invalid")
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            if role not in {"system", "user", "assistant"} or not content:
                raise ValueError("Voice Coach prompt is invalid")
            total_chars += len(content)
            clean_messages.append({"role": role, "content": content})
        if total_chars > WORKSTATION_VOICE_PROMPT_MAX_CHARS:
            raise ValueError("Voice Coach prompt is too large")
        clean_payload = {"messages": clean_messages}
    else:
        if set(payload) != {"text"}:
            raise ValueError("TTS payload is invalid")
        text = str(payload.get("text") or "").strip()
        if not text or len(text) > TTS_TEXT_MAX_CHARS:
            raise ValueError("TTS payload is invalid")
        clean_payload = {"text": text}
    return {
        "operation_id": operation_id,
        "job_kind": job_kind,
        "session_id": session_id,
        "turn_id": turn_id,
        "stage": str(value.get("stage") or "")[:80],
        "deadline_epoch": deadline,
        "payload": clean_payload,
    }
