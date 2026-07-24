"""Localhost-only Ollama chat and embedding adapter."""

from __future__ import annotations

import json
import re
import time

import httpx

from system_limits import (
    LMC_AI_OUTPUT_MAX_BYTES,
    WORKSTATION_OPERATION_TIMING_MAX_MS,
    WORKSTATION_MODEL_PULL_MAX_SECONDS,
    WORKSTATION_MODEL_PULL_STATUS_MAX_BYTES,
)
from workstation.config import OllamaConfig
from workstation.workloads.errors import WorkloadError


class OllamaAdapter:
    def __init__(self, config: OllamaConfig):
        self.config = config

    @staticmethod
    def _nanoseconds_to_bounded_ms(value: object) -> int:
        measured = max(0, int(value or 0)) // 1_000_000
        return min(measured, WORKSTATION_OPERATION_TIMING_MAX_MS)

    def inventory(self) -> dict[str, str]:
        try:
            response = httpx.get(f"{self.config.url}/api/tags", timeout=10)
            response.raise_for_status()
            items = response.json().get("models") or []
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise WorkloadError("ollama_unavailable", "Ollama is unavailable.", retryable=True) from exc
        result = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("model") or "")
            digest = str(item.get("digest") or "").lower()
            if name and re.fullmatch(r"[0-9a-f]{64}", digest):
                result[name] = digest
        return result

    def health(self, required_models: tuple[str, ...] = ()) -> dict:
        try:
            inventory = self.inventory()
            missing = [item for item in required_models if item not in inventory]
            return {"ok": not missing, "models": sorted(inventory), "model_digests": inventory, "missing_models": missing}
        except WorkloadError as exc:
            return {"ok": False, "code": exc.code}

    def pull_approved(
        self,
        model: str,
        *,
        expected_digest: str,
        cancel_event=None,
    ) -> None:
        name = str(model or "")
        digest = str(expected_digest or "").lower()
        if (
            not name
            or len(name) > 200
            or any(ord(character) < 32 for character in name)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise WorkloadError(
                "model_approval_invalid", "Approved model metadata is invalid."
            )
        deadline = time.monotonic() + WORKSTATION_MODEL_PULL_MAX_SECONDS
        total_status_bytes = 0
        completed = False
        try:
            timeout = httpx.Timeout(connect=10, read=120, write=30, pool=10)
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "POST",
                    f"{self.config.url}/api/pull",
                    json={"model": name, "stream": True, "insecure": False},
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if cancel_event is not None and cancel_event.is_set():
                            raise WorkloadError(
                                "cancelled", "Approved model download was cancelled."
                            )
                        if time.monotonic() >= deadline:
                            raise WorkloadError(
                                "model_pull_timeout",
                                "Approved model download exceeded its safe deadline.",
                            )
                        if not line:
                            continue
                        total_status_bytes += len(line.encode("utf-8"))
                        if total_status_bytes > WORKSTATION_MODEL_PULL_STATUS_MAX_BYTES:
                            raise WorkloadError(
                                "model_pull_invalid",
                                "Ollama model download status exceeded its safe limit.",
                            )
                        item = json.loads(line)
                        if not isinstance(item, dict) or item.get("error"):
                            raise WorkloadError(
                                "model_pull_failed", "Approved model download failed."
                            )
                        completed = completed or item.get("status") == "success"
        except WorkloadError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise WorkloadError(
                "model_pull_failed", "Approved model download failed.",
                retryable=True,
            ) from exc
        if not completed or self.inventory().get(name) != digest:
            raise WorkloadError(
                "model_digest_mismatch",
                "Downloaded Ollama model does not match the signed inventory.",
            )

    def resident_models(self) -> tuple[str, ...]:
        try:
            response = httpx.get(f"{self.config.url}/api/ps", timeout=10)
            response.raise_for_status()
            items = response.json().get("models") or []
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise WorkloadError(
                "ollama_unavailable", "Ollama process inventory is unavailable.", retryable=True
            ) from exc
        return tuple(dict.fromkeys(
            str(item.get("name") or item.get("model") or "")
            for item in items if isinstance(item, dict) and (item.get("name") or item.get("model"))
        ))

    def unload(self, model: str) -> None:
        try:
            response = httpx.post(
                f"{self.config.url}/api/generate",
                json={"model": str(model), "keep_alive": 0},
                timeout=30,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise WorkloadError(
                "gpu_release_failed", "Ollama could not release a resident GPU model."
            ) from exc

    def unload_except(self, model: str) -> None:
        target = str(model)
        for resident in self.resident_models():
            if resident != target:
                self.unload(resident)

    def unload_all(self) -> None:
        for model in self.resident_models():
            self.unload(model)

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        think: bool,
        keep_alive: str,
        context_length: int,
        timeout_seconds: int,
        on_started=None,
        on_delta=None,
        cancel_event=None,
    ) -> tuple[str, dict]:
        started = time.monotonic()
        collected = []
        collected_bytes = 0
        usage = {}
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=10)) as client:
                with client.stream("POST", f"{self.config.url}/api/chat", json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "think": bool(think),
                    "keep_alive": keep_alive,
                    "options": {"num_ctx": int(context_length)},
                }) as response:
                    response.raise_for_status()
                    if on_started:
                        on_started()
                    for line in response.iter_lines():
                        if cancel_event is not None and cancel_event.is_set():
                            raise WorkloadError("cancelled", "Ollama request was cancelled.")
                        if not line:
                            continue
                        item = json.loads(line)
                        if item.get("error"):
                            raise WorkloadError("ollama_runtime", "Ollama could not complete the request.")
                        content = str((item.get("message") or {}).get("content") or "")
                        if content:
                            collected_bytes += len(content.encode("utf-8"))
                            if collected_bytes > LMC_AI_OUTPUT_MAX_BYTES:
                                raise WorkloadError(
                                    "output_too_large",
                                    "Ollama answer exceeded its safe output limit.",
                                )
                            collected.append(content)
                            if on_delta:
                                on_delta(content)
                        if item.get("done"):
                            usage = {
                                "input_tokens": max(0, int(item.get("prompt_eval_count") or 0)),
                                "output_tokens": max(0, int(item.get("eval_count") or 0)),
                                "duration_ms": self._nanoseconds_to_bounded_ms(
                                    item.get("total_duration")
                                ),
                                "load_duration_ms": self._nanoseconds_to_bounded_ms(
                                    item.get("load_duration")
                                ),
                                "prompt_eval_duration_ms": self._nanoseconds_to_bounded_ms(
                                    item.get("prompt_eval_duration")
                                ),
                                "generation_duration_ms": self._nanoseconds_to_bounded_ms(
                                    item.get("eval_duration")
                                ),
                            }
        except WorkloadError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
            marker = str(exc).casefold()
            code = "out_of_memory" if any(value in marker for value in ("out of memory", "cuda", "oom")) else "ollama_runtime"
            raise WorkloadError(code, "Ollama could not complete the request.", retryable=code != "out_of_memory") from exc
        text = "".join(collected).strip()
        if not text:
            raise WorkloadError("empty_response", "Ollama returned no answer.")
        return text, {
            **usage,
            "wall_duration_ms": min(
                int((time.monotonic() - started) * 1_000),
                WORKSTATION_OPERATION_TIMING_MAX_MS,
            ),
            "model": model,
        }

    def embed(
        self,
        model: str,
        texts: list[str],
        *,
        timeout_seconds: int = 60,
        keep_alive: str = "0",
    ) -> list[list[float]]:
        if not texts or len(texts) > 32:
            raise WorkloadError("invalid_embedding_batch", "Local embedding batch is invalid.")
        try:
            response = httpx.post(
                f"{self.config.url}/api/embed",
                json={
                    "model": model,
                    "input": texts,
                    "truncate": False,
                    "keep_alive": keep_alive,
                },
                timeout=httpx.Timeout(timeout_seconds, connect=10),
            )
            response.raise_for_status()
            values = response.json().get("embeddings") or []
            result = [[float(number) for number in vector] for vector in values]
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise WorkloadError("embedding_failed", "Local embedding failed.", retryable=True) from exc
        if len(result) != len(texts) or not result or not result[0] or any(len(vector) != len(result[0]) for vector in result):
            raise WorkloadError("embedding_shape", "Local embedding returned an invalid shape.")
        return result
