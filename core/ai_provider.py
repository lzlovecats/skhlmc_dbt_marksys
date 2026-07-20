"""Bounded provider transport for AI features.

The return value is ``(markdown, usage)``.  ``usage`` mirrors the small subset
of provider billing metadata needed by the AI fund ledger.
"""

import json
import math

import httpx
from system_limits import (
    AI_PROVIDER_GEMINI_TIMEOUT_SECONDS, AI_PROVIDER_OPENROUTER_TIMEOUT_SECONDS,
    AI_PROVIDER_OUTPUT_MAX_TOKENS, AI_PROVIDER_PROMPT_MAX_CHARS,
    AI_PROVIDER_RESPONSE_MAX_BYTES,
    AI_PROVIDER_SOURCE_LIMIT,
    OPENROUTER_WEB_SEARCH_MAX_RESULTS,
    OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS,
)


_DEFAULT_TEMPERATURE = object()
_MAX_PROMPT_CHARS_PER_CALL = 250_000
_MAX_TIMEOUT_SECONDS_PER_CALL = 300


class AIProviderResponseError(ValueError):
    """A provider returned a terminal response whose usage must be settled."""

    def __init__(self, message, usage):
        super().__init__(message)
        self.usage = dict(usage or {})


def _read(mapping, *names, default=None):
    for name in names:
        if isinstance(mapping, dict) and name in mapping:
            return mapping[name]
    return default


def _append_sources(text, sources):
    unique = []
    seen = set()
    for title, url in sources:
        if not url or url in seen:
            continue
        seen.add(url)
        safe_title = str(title or url).replace("[", "(").replace("]", ")")
        unique.append((safe_title, url))
        if len(unique) >= AI_PROVIDER_SOURCE_LIMIT:
            break
    if not unique:
        return text
    lines = [f"{index}. [{title}]({url})" for index, (title, url) in enumerate(unique, 1)]
    return text.rstrip() + "\n\n## 可核查來源\n" + "\n".join(lines)


def _gemini_text(
    data, web_search=False, require_complete=False, preserve_text=False,
):
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("AI 未有回傳可讀結果")
    finish_reason = str(_read(
        candidates[0], "finishReason", "finish_reason", default="",
    ) or "").upper()
    if require_complete and finish_reason != "STOP":
        raise ValueError("AI provider response was incomplete")
    parts = _read(candidates[0].get("content") or {}, "parts", default=[]) or []
    raw_text = "".join(str(part.get("text") or "") for part in parts)
    if not raw_text.strip():
        raise ValueError("AI 未有回傳可讀結果")
    text = raw_text if preserve_text else raw_text.strip()
    if not web_search:
        return text
    metadata = _read(candidates[0], "groundingMetadata", "grounding_metadata", default={}) or {}
    chunks = _read(metadata, "groundingChunks", "grounding_chunks", default=[]) or []
    sources = []
    for chunk in chunks:
        web = chunk.get("web") or {}
        if web.get("uri"):
            sources.append((web.get("title"), web.get("uri")))
    return _append_sources(text, sources)


def _openrouter_text(
    data, web_search=False, require_complete=False, preserve_text=False,
):
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("AI 未有回傳可讀結果")
    choice = choices[0]
    finish_reason = str(_read(
        choice, "finish_reason", "finishReason", default="",
    ) or "").upper()
    if require_complete and finish_reason != "STOP":
        raise ValueError("AI provider response was incomplete")
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        raw_text = "".join(
            str(part.get("text") or "")
            for part in content if isinstance(part, dict)
        )
        annotations = [annotation for part in content if isinstance(part, dict)
                       for annotation in (part.get("annotations") or [])]
    else:
        raw_text = str(content)
        annotations = message.get("annotations") or []
    if not raw_text.strip():
        raise ValueError("AI 未有回傳可讀結果")
    text = raw_text if preserve_text else raw_text.strip()
    if not web_search:
        return text
    sources = []
    for annotation in annotations:
        citation = annotation.get("url_citation") if isinstance(annotation, dict) else None
        citation = citation or annotation
        if isinstance(citation, dict) and citation.get("url"):
            sources.append((citation.get("title"), citation.get("url")))
    return _append_sources(text, sources)


def _usage(data, provider, web_search=False):
    def metadata(*names, maximum):
        value = str(_read(data, *names, default="") or "").strip()
        return value[:maximum]

    provider_request_id = metadata(
        "responseId", "response_id", "id", maximum=300,
    )
    resolved_provider_model = metadata(
        "modelVersion", "model_version", "model", maximum=200,
    )

    def with_provider_metadata(result):
        if provider_request_id:
            result["provider_request_id"] = provider_request_id
        if resolved_provider_model:
            result["resolved_provider_model"] = resolved_provider_model
        return result

    raw = data.get("usageMetadata") if provider == "gemini" else data.get("usage")
    raw = raw or {}
    if provider == "gemini":
        prompt = int(_read(raw, "promptTokenCount", "prompt_token_count", default=0) or 0)
        output = int(_read(raw, "candidatesTokenCount", "candidates_token_count", default=0) or 0)
        output += int(_read(raw, "thoughtsTokenCount", "thoughts_token_count", default=0) or 0)
        audio = 0
        for detail in _read(raw, "promptTokensDetails", "prompt_tokens_details", default=[]) or []:
            if "AUDIO" in str(_read(detail, "modality", default="")).upper():
                audio += int(_read(detail, "tokenCount", "token_count", default=0) or 0)
        return with_provider_metadata({
            "input_tokens": max(0, prompt - audio), "output_tokens": output,
            "audio_tokens": audio, "search_calls": int(web_search),
            "cost_source": "gemini_usage_metadata",
        })
    server_tools = _read(raw, "server_tool_use", "serverToolUse", default={}) or {}
    try:
        search_calls = int(_read(
            server_tools, "web_search_requests", "webSearchRequests", default=0,
        ) or 0)
    except (TypeError, ValueError):
        search_calls = 0
    return with_provider_metadata({
        "input_tokens": int(_read(
            raw, "prompt_tokens", "promptTokens", default=0,
        ) or 0),
        "output_tokens": int(_read(
            raw, "completion_tokens", "completionTokens", default=0,
        ) or 0),
        "audio_tokens": 0, "search_calls": max(0, search_calls),
        "cost_source": "openrouter_response_usage",
    })


async def post_json_bounded(client, url, *, max_bytes=AI_PROVIDER_RESPONSE_MAX_BYTES,
                            **kwargs) -> dict:
    """POST JSON and reject an oversized decoded response before buffering it.

    Provider output-token settings are not a byte guarantee, especially for a
    custom OpenAI-compatible endpoint. Streaming the response keeps a broken or
    compromised upstream from consuming the Render worker's remaining RAM.
    """
    limit = max(1, int(max_bytes))
    data = bytearray()
    async with client.stream("POST", url, **kwargs) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > limit:
                raise ValueError("AI provider response exceeds server limit")
            data.extend(chunk)
    if not data:
        raise ValueError("AI provider returned an empty response")
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise ValueError("AI provider returned an invalid JSON object")
    return parsed


def _bounded_integer(value, default, maximum) -> int:
    try:
        parsed = int(default if value is None else value)
    except (TypeError, ValueError, OverflowError):
        parsed = int(default)
    return max(1, min(int(maximum), parsed))


def _bounded_temperature(value, web_search: bool):
    if value is _DEFAULT_TEMPERATURE:
        return 0.3 if web_search else 0.7
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = 0.3 if web_search else 0.7
    if not math.isfinite(parsed):
        parsed = 0.3 if web_search else 0.7
    return max(0.0, min(2.0, parsed))


def _bounded_prompt_pair(system, user, max_chars=None) -> tuple[str, str]:
    """Apply one combined prompt budget instead of allowing twice the limit."""
    limit = _bounded_integer(
        max_chars, AI_PROVIDER_PROMPT_MAX_CHARS, _MAX_PROMPT_CHARS_PER_CALL,
    )
    system_text = str(system or "")[:limit]
    remaining = max(0, limit - len(system_text))
    return system_text, str(user or "")[:remaining]


async def generate_text(
    config,
    system,
    user,
    *,
    api_key,
    audio_base64="",
    audio_mime="audio/webm",
    audio_file_uri="",
    web_search=False,
    max_prompt_chars=None,
    timeout_seconds=None,
    temperature=_DEFAULT_TEMPERATURE,
    require_complete=False,
    structured_json=False,
    preserve_text=False,
    max_response_bytes=None,
    max_output_tokens=None,
    on_provider_attempt=None,
):
    system, user = _bounded_prompt_pair(system, user, max_prompt_chars)
    selected_temperature = _bounded_temperature(temperature, web_search)
    # A structured artifact must never be accepted from a token-truncated
    # response.  Preserve the provider's exact message text so its audit hash
    # is computed before any JSON parsing or canonicalization.
    require_complete = bool(require_complete or structured_json)
    preserve_text = bool(preserve_text or structured_json)
    output_token_limit = (
        _bounded_integer(
            max_output_tokens,
            AI_PROVIDER_OUTPUT_MAX_TOKENS,
            AI_PROVIDER_OUTPUT_MAX_TOKENS,
        )
        if max_output_tokens is not None
        else None
    )
    if config["provider"] in ("openrouter", "custom"):
        if audio_file_uri:
            raise ValueError("所選 provider 不支援 Google Files URI")
        payload = {"model": config["model"], "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user},
        ]}
        if selected_temperature is not None:
            payload["temperature"] = selected_temperature
        if output_token_limit is not None:
            payload["max_tokens"] = output_token_limit
        if structured_json:
            payload["response_format"] = {"type": "json_object"}
        if web_search and config["provider"] == "openrouter":
            payload["tools"] = [{"type": "openrouter:web_search",
                                 "parameters": {
                                     "max_results": OPENROUTER_WEB_SEARCH_MAX_RESULTS,
                                     "max_total_results": OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS,
                                     "search_context_size": "medium",
                                 }}]
        endpoint = (config.get("base_url") or "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"
        timeout = _bounded_integer(
            timeout_seconds,
            AI_PROVIDER_OPENROUTER_TIMEOUT_SECONDS,
            _MAX_TIMEOUT_SECONDS_PER_CALL,
        )
        response_kwargs = {}
        if max_response_bytes is not None:
            response_kwargs["max_bytes"] = _bounded_integer(
                max_response_bytes,
                AI_PROVIDER_RESPONSE_MAX_BYTES,
                AI_PROVIDER_RESPONSE_MAX_BYTES,
            )
        async with httpx.AsyncClient(timeout=timeout) as client:
            if on_provider_attempt is not None:
                on_provider_attempt()
            data = await post_json_bounded(client, endpoint,
                headers={"Authorization": f"Bearer {api_key}"}, json=payload,
                **response_kwargs)
        usage = _usage(data, "openrouter", web_search)
        try:
            output = _openrouter_text(
                data,
                web_search,
                require_complete=require_complete,
                preserve_text=preserve_text,
            )
        except ValueError as exc:
            raise AIProviderResponseError(str(exc), usage) from exc
        return output, usage

    parts = [{"text": user}]
    if audio_base64:
        parts.append({"inline_data": {"mime_type": audio_mime or "audio/webm", "data": audio_base64}})
    if audio_file_uri:
        parts.append({"file_data": {
            "mime_type": audio_mime or "audio/webm", "file_uri": audio_file_uri,
        }})
    payload = {"system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}]}
    generation_config = {}
    if selected_temperature is not None:
        generation_config["temperature"] = selected_temperature
    if output_token_limit is not None:
        generation_config["maxOutputTokens"] = output_token_limit
    if structured_json:
        generation_config["responseMimeType"] = "application/json"
    if generation_config:
        payload["generationConfig"] = generation_config
    if web_search:
        payload["tools"] = [{"google_search": {}}]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config['model']}:generateContent"
    timeout = _bounded_integer(
        timeout_seconds,
        AI_PROVIDER_GEMINI_TIMEOUT_SECONDS,
        _MAX_TIMEOUT_SECONDS_PER_CALL,
    )
    response_kwargs = {}
    if max_response_bytes is not None:
        response_kwargs["max_bytes"] = _bounded_integer(
            max_response_bytes,
            AI_PROVIDER_RESPONSE_MAX_BYTES,
            AI_PROVIDER_RESPONSE_MAX_BYTES,
        )
    async with httpx.AsyncClient(timeout=timeout) as client:
        if on_provider_attempt is not None:
            on_provider_attempt()
        data = await post_json_bounded(
            client, url, headers={"x-goog-api-key": api_key}, json=payload,
            **response_kwargs,
        )
    usage = _usage(data, "gemini", web_search)
    try:
        output = _gemini_text(
            data,
            web_search,
            require_complete=require_complete,
            preserve_text=preserve_text,
        )
    except ValueError as exc:
        raise AIProviderResponseError(str(exc), usage) from exc
    return output, usage
