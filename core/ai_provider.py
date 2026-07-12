"""Streamlit-free provider transport for direct-HTML AI features.

The return value is ``(markdown, usage)``.  ``usage`` mirrors the small subset
of provider billing metadata needed by the AI fund ledger.
"""

import httpx


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
    if not unique:
        return text
    lines = [f"{index}. [{title}]({url})" for index, (title, url) in enumerate(unique, 1)]
    return text.rstrip() + "\n\n## 可核查來源\n" + "\n".join(lines)


def _gemini_text(data, web_search=False):
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("AI 未有回傳可讀結果")
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
    return {"input_tokens": int(_read(raw, "prompt_tokens", "promptTokens", default=0) or 0),
            "output_tokens": int(_read(raw, "completion_tokens", "completionTokens", default=0) or 0),
            "audio_tokens": 0, "search_calls": int(web_search),
            "cost_source": "openrouter_response_usage"}


async def generate_text(config, system, user, *, api_key, audio_base64="", audio_mime="audio/webm", web_search=False):
    if config["provider"] == "openrouter":
        payload = {"model": config["model"], "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user},
        ], "temperature": 0.3 if web_search else 0.7}
        if web_search:
            payload["tools"] = [{"type": "openrouter:web_search",
                                 "parameters": {"search_context_size": "medium"}}]
        async with httpx.AsyncClient(timeout=70) as client:
            response = await client.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"}, json=payload)
            response.raise_for_status()
        data = response.json()
        return _openrouter_text(data, web_search), _usage(data, "openrouter", web_search)

    parts = [{"text": user}]
    if audio_base64:
        parts.append({"inline_data": {"mime_type": audio_mime or "audio/webm", "data": audio_base64}})
    payload = {"system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.3 if web_search else 0.7}}
    if web_search:
        payload["tools"] = [{"google_search": {}}]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config['model']}:generateContent?key={api_key}"
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
    data = response.json()
    return _gemini_text(data, web_search), _usage(data, "gemini", web_search)
