"""Pure contracts for resumable full-match transcript structure extraction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from core.ai_data_factory import (
    FactoryContractError,
    canonicalize_factory_output,
    estimate_text_tokens,
    parse_provider_output,
)
from prompts import (
    AI_TRANSCRIPT_STRUCTURE_PROMPT_VERSION,
    AI_TRANSCRIPT_STRUCTURE_SYSTEM_PROMPT,
    build_ai_transcript_structure_user_prompt,
)
from system_limits import (
    AI_FACTORY_INSTRUCTION_MAX_CHARS,
    AI_FACTORY_SOURCE_MAX_CHARS,
    AI_FACTORY_TRANSCRIPT_BOUNDARY_MAX,
    AI_FACTORY_TRANSCRIPT_CORE_CHARS,
    AI_FACTORY_TRANSCRIPT_MAX_CHARS,
    AI_FACTORY_TRANSCRIPT_OUTPUT_MAX_TOKENS,
    AI_FACTORY_TRANSCRIPT_OVERLAP_CHARS,
    AI_PROVIDER_PROMPT_MAX_CHARS,
    AI_TRAINING_JSON_MAX_BYTES,
)


TRANSCRIPT_STRUCTURE_RECIPE = "transcript_structure_v1"
LANGUAGE = "yue-Hant-HK"
TRANSCRIPT_SIDES = ("pro", "con", "neutral", "unknown")
TRANSCRIPT_STAGES = (
    "opening",
    "questioning",
    "free_debate",
    "summary",
    "adjudication",
    "general",
    "unknown",
)
_BOUNDARY_KEYS = frozenset((
    "start_offset", "speaker_label", "side", "stage", "confidence",
    "review_items",
))
_SEGMENT_KEYS = frozenset((
    "sequence_no", "start_offset", "end_offset", "quote", "speaker_label",
    "side", "stage", "full_text", "confidence", "review_items",
))


@dataclass(frozen=True)
class TranscriptWindow:
    ordinal: int
    context_start: int
    context_end: int
    core_start: int
    core_end: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptPrompt:
    recipe_id: str
    prompt_version: str
    prompt_template_sha256: str
    prompt_sha256: str
    system: str
    user: str
    temperature: float
    window: TranscriptWindow

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["window"] = self.window.to_dict()
        return value


@dataclass(frozen=True)
class TranscriptParseResult:
    boundaries: tuple[dict[str, Any], ...]
    provider_text_sha256: str
    content_sha256: str


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strict_string(value: Any, label: str, *, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise FactoryContractError(f"{label} length is invalid")
    return value


def _strict_integer(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FactoryContractError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise FactoryContractError(f"{label} is outside the allowed range")
    return value


def _strict_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise FactoryContractError(f"{label} fields do not match the contract")


def build_transcript_window_plan(
    text_length: int,
    *,
    core_chars: int = AI_FACTORY_TRANSCRIPT_CORE_CHARS,
    overlap_chars: int = AI_FACTORY_TRANSCRIPT_OVERLAP_CHARS,
) -> tuple[TranscriptWindow, ...]:
    """Return non-overlapping ownership cores with bounded context overlap."""
    length = _strict_integer(
        text_length, "text_length", minimum=1,
        maximum=AI_FACTORY_TRANSCRIPT_MAX_CHARS,
    )
    core = _strict_integer(
        core_chars, "core_chars", minimum=1,
        maximum=AI_FACTORY_TRANSCRIPT_MAX_CHARS,
    )
    overlap = _strict_integer(
        overlap_chars, "overlap_chars", minimum=0,
        maximum=AI_FACTORY_TRANSCRIPT_MAX_CHARS,
    )
    windows = []
    ordinal = 1
    for core_start in range(0, length, core):
        core_end = min(length, core_start + core)
        windows.append(TranscriptWindow(
            ordinal=ordinal,
            context_start=max(0, core_start - overlap),
            context_end=min(length, core_end + overlap),
            core_start=core_start,
            core_end=core_end,
        ))
        ordinal += 1
    if len(windows) > 40:
        raise FactoryContractError("Transcript requires too many processing windows")
    return tuple(windows)


def transcript_boundary_schema(window: TranscriptWindow) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:skhlmc:ai-data-factory:transcript-structure-v1",
        "type": "object",
        "additionalProperties": False,
        "required": ["recipe_id", "language", "window_ordinal", "boundaries"],
        "properties": {
            "recipe_id": {"const": TRANSCRIPT_STRUCTURE_RECIPE},
            "language": {"const": LANGUAGE},
            "window_ordinal": {"const": window.ordinal},
            "boundaries": {
                "type": "array",
                "minItems": 0,
                "maxItems": AI_FACTORY_TRANSCRIPT_BOUNDARY_MAX,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(_BOUNDARY_KEYS),
                    "properties": {
                        "start_offset": {
                            "type": "integer",
                            "minimum": window.core_start,
                            "maximum": window.core_end - 1,
                        },
                        "speaker_label": {
                            "type": "string", "minLength": 1, "maxLength": 100,
                        },
                        "side": {"type": "string", "enum": list(TRANSCRIPT_SIDES)},
                        "stage": {"type": "string", "enum": list(TRANSCRIPT_STAGES)},
                        "confidence": {
                            "type": "integer", "minimum": 0, "maximum": 100,
                        },
                        "review_items": {
                            "type": "array", "minItems": 0, "maxItems": 8,
                            "items": {
                                "type": "string", "minLength": 1, "maxLength": 300,
                            },
                        },
                    },
                },
            },
        },
    }


def _prompt_json(value: Any) -> str:
    return (
        canonicalize_factory_output(value)
        .replace("&", r"\u0026")
        .replace("<", r"\u003c")
        .replace(">", r"\u003e")
        .replace("\u2028", r"\u2028")
        .replace("\u2029", r"\u2029")
    )


def _template_hash() -> str:
    sentinel = build_ai_transcript_structure_user_prompt(
        window_json="{window_json}",
        manager_instruction_json="{manager_instruction_json}",
        output_schema_json=canonicalize_factory_output(
            transcript_boundary_schema(TranscriptWindow(1, 0, 1, 0, 1))
        ),
    )
    return _sha256_text("\0".join((
        AI_TRANSCRIPT_STRUCTURE_PROMPT_VERSION,
        AI_TRANSCRIPT_STRUCTURE_SYSTEM_PROMPT,
        sentinel,
    )))


def transcript_prompt_template_hash() -> str:
    return _template_hash()


def build_transcript_prompt(
    transcript_text: str,
    window: TranscriptWindow,
    *,
    manager_instruction: str = "",
) -> TranscriptPrompt:
    if not isinstance(transcript_text, str) or not 1 <= len(transcript_text) <= AI_FACTORY_TRANSCRIPT_MAX_CHARS:
        raise FactoryContractError("transcript_text length is invalid")
    instruction = _strict_string(
        manager_instruction, "manager_instruction", minimum=0,
        maximum=AI_FACTORY_INSTRUCTION_MAX_CHARS,
    )
    if not (
        0 <= window.context_start <= window.core_start
        < window.core_end <= window.context_end <= len(transcript_text)
    ):
        raise FactoryContractError("Transcript window bounds are invalid")
    context_text = transcript_text[window.context_start:window.context_end]
    window_payload = {
        **window.to_dict(),
        "transcript_length": len(transcript_text),
        "transcript_sha256": _sha256_text(transcript_text),
        "transcript_text": context_text,
    }
    user = build_ai_transcript_structure_user_prompt(
        window_json=_prompt_json(window_payload),
        manager_instruction_json=_prompt_json(instruction),
        output_schema_json=canonicalize_factory_output(
            transcript_boundary_schema(window)
        ),
    )
    system = AI_TRANSCRIPT_STRUCTURE_SYSTEM_PROMPT
    if len(system) + len(user) > AI_PROVIDER_PROMPT_MAX_CHARS:
        raise FactoryContractError("Transcript window prompt exceeds provider limit")
    prompt_hash = _sha256_text("\0".join((system, user)))
    return TranscriptPrompt(
        recipe_id=TRANSCRIPT_STRUCTURE_RECIPE,
        prompt_version=AI_TRANSCRIPT_STRUCTURE_PROMPT_VERSION,
        prompt_template_sha256=_template_hash(),
        prompt_sha256=prompt_hash,
        system=system,
        user=user,
        temperature=0.1,
        window=window,
    )


def transcript_provider_payload(model_label: str, config: Mapping[str, Any], prompt: TranscriptPrompt) -> dict[str, Any]:
    return {
        "model_label": str(model_label),
        "provider": str(config.get("provider") or ""),
        "provider_model": str(config.get("model") or ""),
        "system_prompt": prompt.system,
        "user_prompt": prompt.user,
        "temperature": prompt.temperature,
        "max_output_tokens": AI_FACTORY_TRANSCRIPT_OUTPUT_MAX_TOKENS,
        "web_search": False,
        "structured_json": True,
        "require_complete": True,
    }


def transcript_preview_hashes(
    transcript_id: str,
    transcript_sha256: str,
    prompt: TranscriptPrompt,
    provider_payload: Mapping[str, Any],
) -> tuple[str, str]:
    input_hash = _sha256_text(canonicalize_factory_output({
        "transcript_id": str(transcript_id),
        "transcript_sha256": str(transcript_sha256),
        "window": prompt.window.to_dict(),
        "prompt_sha256": prompt.prompt_sha256,
    }))
    return input_hash, _sha256_text(canonicalize_factory_output(provider_payload))


def transcript_manifest_hash(window_previews: Sequence[Mapping[str, Any]]) -> str:
    manifest = [{
        "ordinal": int(item["ordinal"]),
        "input_sha256": str(item["input_sha256"]),
        "prompt_sha256": str(item["prompt_sha256"]),
        "preview_sha256": str(item["preview_sha256"]),
    } for item in window_previews]
    return _sha256_text(canonicalize_factory_output(manifest))


def estimate_transcript_cost(
    config: Mapping[str, Any], prompts: Sequence[TranscriptPrompt],
) -> dict[str, Any]:
    input_tokens = sum(
        estimate_text_tokens(prompt.system + prompt.user)
        for prompt in prompts
    )
    output_tokens = len(prompts) * AI_FACTORY_TRANSCRIPT_OUTPUT_MAX_TOKENS
    usd = (
        input_tokens * float(config.get("input_price_per_million") or 0)
        + output_tokens * float(config.get("output_price_per_million") or 0)
    ) / 1_000_000
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "window_count": len(prompts),
        "estimated_cost_usd": round(usd, 8),
        "estimated_cost_hkd": round(usd * 7.8, 8),
    }


def _validate_review_items(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 8:
        raise FactoryContractError(f"{label} must be a bounded array")
    return [
        _strict_string(item, f"{label}[{index}]", minimum=1, maximum=300)
        for index, item in enumerate(value)
    ]


def parse_validate_transcript_boundaries(
    provider_text: str,
    *,
    window: TranscriptWindow,
) -> TranscriptParseResult:
    value, provider_sha = parse_provider_output(
        provider_text, max_bytes=AI_TRAINING_JSON_MAX_BYTES,
    )
    expected_top = {"recipe_id", "language", "window_ordinal", "boundaries"}
    if set(value) != expected_top:
        raise FactoryContractError("Transcript output fields do not match the contract")
    if value["recipe_id"] != TRANSCRIPT_STRUCTURE_RECIPE or value["language"] != LANGUAGE:
        raise FactoryContractError("Transcript output identity is invalid")
    if value["window_ordinal"] != window.ordinal:
        raise FactoryContractError("Transcript output window ordinal is invalid")
    raw_boundaries = value["boundaries"]
    if not isinstance(raw_boundaries, list) or len(raw_boundaries) > AI_FACTORY_TRANSCRIPT_BOUNDARY_MAX:
        raise FactoryContractError("Transcript boundaries are not a bounded array")
    boundaries = []
    previous = None
    for index, raw in enumerate(raw_boundaries):
        if not isinstance(raw, dict):
            raise FactoryContractError("Transcript boundary must be an object")
        _strict_keys(raw, _BOUNDARY_KEYS, f"boundaries[{index}]")
        start = _strict_integer(
            raw["start_offset"], f"boundaries[{index}].start_offset",
            minimum=window.core_start, maximum=window.core_end - 1,
        )
        if previous is not None and start <= previous:
            raise FactoryContractError("Transcript boundaries must be strictly increasing")
        previous = start
        side = raw["side"]
        stage = raw["stage"]
        if side not in TRANSCRIPT_SIDES or stage not in TRANSCRIPT_STAGES:
            raise FactoryContractError("Transcript boundary classification is invalid")
        boundaries.append({
            "start_offset": start,
            "speaker_label": _strict_string(
                raw["speaker_label"], f"boundaries[{index}].speaker_label",
                minimum=1, maximum=100,
            ),
            "side": side,
            "stage": stage,
            "confidence": _strict_integer(
                raw["confidence"], f"boundaries[{index}].confidence",
                minimum=0, maximum=100,
            ),
            "review_items": _validate_review_items(
                raw["review_items"], f"boundaries[{index}].review_items",
            ),
        })
    if window.core_start == 0 and (not boundaries or boundaries[0]["start_offset"] != 0):
        raise FactoryContractError("The first transcript window must begin at offset 0")
    encoded = canonicalize_factory_output(boundaries)
    return TranscriptParseResult(
        boundaries=tuple(boundaries),
        provider_text_sha256=provider_sha,
        content_sha256=_sha256_text(encoded),
    )


def build_segment_payloads(
    transcript_text: str,
    boundary_windows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(transcript_text, str) or not transcript_text:
        raise FactoryContractError("Transcript text is empty")
    owned = []
    seen = set()
    for window in boundary_windows:
        window_id = str(window.get("id") or "")
        for boundary in window.get("boundaries") or ():
            start = int(boundary["start_offset"])
            if start in seen:
                raise FactoryContractError("Transcript boundary is duplicated across windows")
            seen.add(start)
            owned.append((start, window_id, dict(boundary)))
    owned.sort(key=lambda item: item[0])
    if not owned or owned[0][0] != 0:
        raise FactoryContractError("Transcript boundaries do not start at zero")
    payloads = []
    for index, (start, window_id, boundary) in enumerate(owned):
        end = owned[index + 1][0] if index + 1 < len(owned) else len(transcript_text)
        if end <= start or end > len(transcript_text):
            raise FactoryContractError("Transcript boundary range is invalid")
        quote = transcript_text[start:end]
        review_items = list(boundary.get("review_items") or [])
        confidence = int(boundary["confidence"])
        if len(quote) > AI_FACTORY_SOURCE_MAX_CHARS:
            review_items.append("此段超過一般來源字數上限，批准前必須修正分段位置。")
            confidence = min(confidence, 50)
        payload = {
            "sequence_no": index + 1,
            "start_offset": start,
            "end_offset": end,
            "quote": quote,
            "speaker_label": str(boundary["speaker_label"]),
            "side": str(boundary["side"]),
            "stage": str(boundary["stage"]),
            "full_text": quote,
            "confidence": confidence,
            "review_items": review_items[:8],
        }
        payloads.append({
            "origin_window_id": window_id,
            "payload": payload,
        })
    return payloads


def validate_reviewed_segment(
    payload: Mapping[str, Any],
    *,
    transcript_text: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise FactoryContractError("Reviewed transcript segment must be an object")
    _strict_keys(payload, _SEGMENT_KEYS, "segment")
    start = _strict_integer(
        payload["start_offset"], "segment.start_offset", minimum=0,
        maximum=len(transcript_text) - 1,
    )
    end = _strict_integer(
        payload["end_offset"], "segment.end_offset", minimum=start + 1,
        maximum=len(transcript_text),
    )
    quote = _strict_string(
        payload["quote"], "segment.quote", minimum=1,
        maximum=AI_FACTORY_SOURCE_MAX_CHARS,
    )
    full_text = _strict_string(
        payload["full_text"], "segment.full_text", minimum=1,
        maximum=AI_FACTORY_SOURCE_MAX_CHARS,
    )
    if quote != transcript_text[start:end] or full_text != quote:
        raise FactoryContractError("Segment quote does not match the transcript offsets")
    side = payload["side"]
    stage = payload["stage"]
    if side not in TRANSCRIPT_SIDES or stage not in TRANSCRIPT_STAGES:
        raise FactoryContractError("Segment classification is invalid")
    return {
        "sequence_no": _strict_integer(
            payload["sequence_no"], "segment.sequence_no", minimum=1,
            maximum=100_000,
        ),
        "start_offset": start,
        "end_offset": end,
        "quote": quote,
        "speaker_label": _strict_string(
            payload["speaker_label"], "segment.speaker_label",
            minimum=1, maximum=100,
        ),
        "side": side,
        "stage": stage,
        "full_text": full_text,
        "confidence": _strict_integer(
            payload["confidence"], "segment.confidence", minimum=0, maximum=100,
        ),
        "review_items": _validate_review_items(
            payload["review_items"], "segment.review_items",
        ),
    }


def transcript_recipe_metadata() -> dict[str, Any]:
    return {
        "recipe_id": TRANSCRIPT_STRUCTURE_RECIPE,
        "artifact_kind": "transcript_structure",
        "dataset_kind": "source",
        "language": LANGUAGE,
        "prompt_version": AI_TRANSCRIPT_STRUCTURE_PROMPT_VERSION,
        "prompt_template_sha256": _template_hash(),
        "temperature": 0.1,
        "structured_json": True,
        "require_complete": True,
        "transcript_workflow": True,
    }


__all__ = [
    "LANGUAGE",
    "TRANSCRIPT_SIDES",
    "TRANSCRIPT_STAGES",
    "TRANSCRIPT_STRUCTURE_RECIPE",
    "TranscriptParseResult",
    "TranscriptPrompt",
    "TranscriptWindow",
    "build_segment_payloads",
    "build_transcript_prompt",
    "build_transcript_window_plan",
    "estimate_transcript_cost",
    "parse_validate_transcript_boundaries",
    "transcript_boundary_schema",
    "transcript_manifest_hash",
    "transcript_preview_hashes",
    "transcript_prompt_template_hash",
    "transcript_provider_payload",
    "transcript_recipe_metadata",
    "validate_reviewed_segment",
]
