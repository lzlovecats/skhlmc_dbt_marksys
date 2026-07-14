"""Bounded provider transport for AI features.

The return value is ``(markdown, usage)``.  ``usage`` mirrors the small subset
of provider billing metadata needed by the AI fund ledger.
"""

import json
import math

import httpx
from system_limits import (
    AI_PROVIDER_GEMINI_TIMEOUT_SECONDS, AI_PROVIDER_OPENROUTER_TIMEOUT_SECONDS,
    AI_PROVIDER_MAX_OUTPUT_TOKENS, AI_PROVIDER_PROMPT_MAX_CHARS,
    AI_PROVIDER_RESPONSE_MAX_BYTES, AI_PROVIDER_SOURCE_LIMIT,
    OPENROUTER_WEB_SEARCH_MAX_RESULTS,
    OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS,
)


_DEFAULT_TEMPERATURE = object()
_MAX_OUTPUT_TOKENS_PER_CALL = 65_536
_MAX_PROMPT_CHARS_PER_CALL = 250_000
_MAX_TIMEOUT_SECONDS_PER_CALL = 300


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


def _gemini_text(data, web_search=False, require_complete=False):
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("AI 未有回傳可讀結果")
    finish_reason = str(_read(
        candidates[0], "finishReason", "finish_reason", default="",
    ) or "").upper()
    if require_complete and finish_reason != "STOP":
        raise ValueError("AI provider response was incomplete")
    parts = _read(candidates[0].get("content") or {}, "parts", default=[]) or []
    text = "".join(str(part.get("text") or "") for part in parts).strip()
    if not text:
        raise ValueError("AI 未有回傳可讀結果")
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


def _openrouter_text(data, web_search=False):
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("AI 未有回傳可讀結果")
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        text = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        annotations = [annotation for part in content if isinstance(part, dict)
                       for annotation in (part.get("annotations") or [])]
    else:
        text = str(content)
        annotations = message.get("annotations") or []
    text = text.strip()
    if not text:
        raise ValueError("AI 未有回傳可讀結果")
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
        return {"input_tokens": max(0, prompt - audio), "output_tokens": output,
                "audio_tokens": audio, "search_calls": int(web_search),
                "cost_source": "gemini_usage_metadata"}
    server_tools = _read(raw, "server_tool_use", "serverToolUse", default={}) or {}
    try:
        search_calls = int(_read(
            server_tools, "web_search_requests", "webSearchRequests", default=0,
        ) or 0)
    except (TypeError, ValueError):
        search_calls = 0
    return {"input_tokens": int(_read(raw, "prompt_tokens", "promptTokens", default=0) or 0),
            "output_tokens": int(_read(raw, "completion_tokens", "completionTokens", default=0) or 0),
            "audio_tokens": 0, "search_calls": max(0, search_calls),
            "cost_source": "openrouter_response_usage"}


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
    web_search=False,
    max_output_tokens=None,
    max_prompt_chars=None,
    timeout_seconds=None,
    temperature=_DEFAULT_TEMPERATURE,
    require_complete=False,
):
    system, user = _bounded_prompt_pair(system, user, max_prompt_chars)
    output_limit = _bounded_integer(
        max_output_tokens,
        AI_PROVIDER_MAX_OUTPUT_TOKENS,
        _MAX_OUTPUT_TOKENS_PER_CALL,
    )
    selected_temperature = _bounded_temperature(temperature, web_search)
    if config["provider"] in ("openrouter", "custom"):
        payload = {"model": config["model"], "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user},
        ], "max_tokens": output_limit}
        if selected_temperature is not None:
            payload["temperature"] = selected_temperature
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
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await post_json_bounded(client, endpoint,
                headers={"Authorization": f"Bearer {api_key}"}, json=payload)
        return _openrouter_text(data, web_search), _usage(data, "openrouter", web_search)

    parts = [{"text": user}]
    if audio_base64:
        parts.append({"inline_data": {"mime_type": audio_mime or "audio/webm", "data": audio_base64}})
    payload = {"system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"maxOutputTokens": output_limit}}
    if selected_temperature is not None:
        payload["generationConfig"]["temperature"] = selected_temperature
    if web_search:
        payload["tools"] = [{"google_search": {}}]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config['model']}:generateContent"
    timeout = _bounded_integer(
        timeout_seconds,
        AI_PROVIDER_GEMINI_TIMEOUT_SECONDS,
        _MAX_TIMEOUT_SECONDS_PER_CALL,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        data = await post_json_bounded(
            client, url, headers={"x-goog-api-key": api_key}, json=payload,
        )
    return _gemini_text(
        data, web_search, require_complete=require_complete,
    ), _usage(data, "gemini", web_search)
