"""Provider-neutral contracts for the debate LLM data factory.

This module is deliberately independent of HTTP, database and review-state
code.  It owns the four versioned provider-output contracts, prompt assembly,
strict parsing, source-reference validation and deterministic hashes which the
API can persist as provenance.

Source offsets are Python/Unicode code-point offsets: ``start`` is inclusive
and ``end`` is exclusive.  No normalization is applied before comparing
``quote`` with ``source_text[start:end]``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Mapping, Sequence

from core.funds_logic import HKD_PER_USD
from prompts import (
    AI_DATA_FACTORY_PROMPT_VERSION,
    AI_DATA_FACTORY_PROVIDER_SYSTEM_PROMPT,
    AI_DATA_FACTORY_RAG_ARGUMENT_INSTRUCTION,
    AI_DATA_FACTORY_RAG_KNOWLEDGE_INSTRUCTION,
    AI_DATA_FACTORY_SFT_ATTACK_DEFENCE_INSTRUCTION,
    AI_DATA_FACTORY_SFT_ATTACK_DEFENCE_SYSTEM_MESSAGE,
    AI_DATA_FACTORY_SFT_SPEECH_INSTRUCTION,
    AI_DATA_FACTORY_SFT_SPEECH_SYSTEM_MESSAGE,
    build_ai_data_factory_user_prompt,
)
from system_limits import (
    AI_FACTORY_CANDIDATE_DEFAULT,
    AI_FACTORY_CANDIDATE_MAX,
    AI_FACTORY_INSTRUCTION_MAX_CHARS,
    AI_FACTORY_RAG_CLAIM_MAX,
    AI_FACTORY_RAG_CONTENT_MAX_CHARS,
    AI_FACTORY_SFT_ASSISTANT_MAX_CHARS,
    AI_FACTORY_SFT_USER_MAX_CHARS,
    AI_FACTORY_SOURCE_MAX_CHARS,
    AI_FACTORY_SOURCE_NOTE_MAX_CHARS,
    AI_FACTORY_TOPIC_TAG_MAX,
    AI_FACTORY_TOPIC_TAG_MAX_CHARS,
    AI_PROVIDER_PROMPT_MAX_CHARS,
    AI_TRAINING_JSON_MAX_BYTES,
)


LANGUAGE = "yue-Hant-HK"
WEB_SEARCH_ENABLED = False
CRITIC_ENABLED = False
PROMPT_VERSION = AI_DATA_FACTORY_PROMPT_VERSION
SCHEMA_VERSION = "ai-data-factory-output-v1"

RAG_KNOWLEDGE_CARD_RECIPE = "rag_knowledge_card_v1"
RAG_ARGUMENT_DECOMPOSITION_RECIPE = "rag_argument_decomposition_v1"
SFT_SPEECH_CRITIQUE_RECIPE = "sft_speech_critique_v1"
SFT_ATTACK_DEFENCE_RECIPE = "sft_attack_defence_v1"
RECIPE_IDS = (
    RAG_KNOWLEDGE_CARD_RECIPE,
    RAG_ARGUMENT_DECOMPOSITION_RECIPE,
    SFT_SPEECH_CRITIQUE_RECIPE,
    SFT_ATTACK_DEFENCE_RECIPE,
)

SIDES = ("pro", "con", "neutral", "not_applicable")
STAGES = (
    "opening", "questioning", "free_debate", "summary", "adjudication",
    "general",
)
SKILLS = (
    "definition", "burden", "standard", "mechanism", "evidence",
    "causality", "comparison", "feasibility", "impact", "weighing",
    "rebuttal", "questioning",
)
FACT_STATUSES = ("source_backed", "derived", "synthetic")

_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_TITLE_MAX_CHARS = 160
_FACT_UNIT_MAX_CHARS = min(AI_FACTORY_RAG_CONTENT_MAX_CHARS, 2_000)
_SYNTHETIC_NOTE_MAX_CHARS = min(AI_FACTORY_INSTRUCTION_MAX_CHARS, 1_000)
_SOURCE_REFS_MAX = 5
_ARGUMENT_SECTION_MAX = 5
_RAG_LIMITATION_MAX = 5
_SKILLS_MAX = 6
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SOURCE_METADATA_KEYS = frozenset((
    "source_kind", "source_revision_id", "title", "topic", "data_type",
    "source_note",
))


class FactoryContractError(ValueError):
    """A local factory input or provider output violated the V0 contract."""


@dataclass(frozen=True)
class FactoryPrompt:
    recipe_id: str
    prompt_version: str
    prompt_template_sha256: str
    prompt_sha256: str
    system: str
    user: str
    temperature: float
    web_search: bool = False
    structured_json: bool = True
    require_complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FactoryParseResult:
    payload: dict[str, Any]
    canonical_json: str
    provider_text_sha256: str
    content_sha256: str
    candidate_hashes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["payload"] = deepcopy(self.payload)
        return value


_RECIPE_DEFINITIONS = {
    RAG_KNOWLEDGE_CARD_RECIPE: {
        "artifact_kind": "rag_knowledge_card",
        "temperature": 0.2,
        "instruction": AI_DATA_FACTORY_RAG_KNOWLEDGE_INSTRUCTION,
        "sft_message_count": None,
        "estimated_output_tokens_per_candidate": 1_000,
    },
    RAG_ARGUMENT_DECOMPOSITION_RECIPE: {
        "artifact_kind": "rag_argument_decomposition",
        "temperature": 0.2,
        "instruction": AI_DATA_FACTORY_RAG_ARGUMENT_INSTRUCTION,
        "sft_message_count": None,
        "estimated_output_tokens_per_candidate": 1_600,
    },
    SFT_SPEECH_CRITIQUE_RECIPE: {
        "artifact_kind": "sft_speech_critique",
        "temperature": 0.4,
        "instruction": AI_DATA_FACTORY_SFT_SPEECH_INSTRUCTION,
        "sft_message_count": 3,
        "estimated_output_tokens_per_candidate": 1_800,
    },
    SFT_ATTACK_DEFENCE_RECIPE: {
        "artifact_kind": "sft_attack_defence",
        "temperature": 0.4,
        "instruction": AI_DATA_FACTORY_SFT_ATTACK_DEFENCE_INSTRUCTION,
        "sft_message_count": 7,
        "estimated_output_tokens_per_candidate": 3_600,
    },
}


def _recipe(recipe_id: str) -> dict[str, Any]:
    value = _RECIPE_DEFINITIONS.get(str(recipe_id or ""))
    if value is None:
        raise FactoryContractError(f"Unsupported factory recipe: {recipe_id}")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FactoryContractError("Text contains an invalid Unicode surrogate") from exc
    return _sha256_bytes(encoded)


def canonicalize_factory_output(value: Any) -> str:
    """Return stable UTF-8 JSON without modifying any provider string."""
    try:
        rendered = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
        rendered.encode("utf-8")
        return rendered
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FactoryContractError("Factory value cannot be canonicalized") from exc


def content_hash(value: Any) -> str:
    """Hash a value using the same canonical JSON used by releases."""
    return _sha256_text(canonicalize_factory_output(value))


def _source_ref_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["start", "end", "quote"],
        "properties": {
            "start": {"type": "integer", "minimum": 0,
                      "maximum": AI_FACTORY_SOURCE_MAX_CHARS},
            "end": {"type": "integer", "minimum": 1,
                    "maximum": AI_FACTORY_SOURCE_MAX_CHARS},
            "quote": {"type": "string", "minLength": 1,
                      "maxLength": AI_FACTORY_SOURCE_MAX_CHARS},
        },
    }


def _fact_unit_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["text", "fact_status", "source_refs", "synthetic_note"],
        "properties": {
            "text": {"type": "string", "minLength": 1,
                     "maxLength": _FACT_UNIT_MAX_CHARS},
            "fact_status": {"type": "string", "enum": list(FACT_STATUSES)},
            "source_refs": {
                "type": "array", "minItems": 0, "maxItems": _SOURCE_REFS_MAX,
                "items": {"$ref": "#/$defs/source_ref"},
            },
            "synthetic_note": {
                "type": "string", "minLength": 0,
                "maxLength": _SYNTHETIC_NOTE_MAX_CHARS,
            },
        },
        "allOf": [
            {
                "if": {"properties": {"fact_status": {"const": "synthetic"}}},
                "then": {"properties": {"synthetic_note": {"minLength": 1}}},
                "else": {
                    "properties": {
                        "source_refs": {"minItems": 1},
                        "synthetic_note": {"maxLength": 0},
                    },
                },
            },
        ],
    }


def _topic_tags_schema(allowed_topic_tags: Sequence[str] | None) -> dict[str, Any]:
    # Existing approved tags are prompt suggestions, not an allowlist: the
    # model may propose a bounded new label for the human reviewer to approve.
    del allowed_topic_tags
    return {
        "type": "array", "minItems": 0, "maxItems": AI_FACTORY_TOPIC_TAG_MAX,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1,
                  "maxLength": AI_FACTORY_TOPIC_TAG_MAX_CHARS},
    }


def _common_candidate_properties(
    *, allowed_topic_tags: Sequence[str] | None, side: str | None,
    stage: str | None,
) -> dict[str, Any]:
    side_schema: dict[str, Any] = {"type": "string", "enum": list(SIDES)}
    stage_schema: dict[str, Any] = {"type": "string", "enum": list(STAGES)}
    if side is not None:
        side_schema = {"const": side}
    if stage is not None:
        stage_schema = {"const": stage}
    return {
        "title": {"type": "string", "minLength": 1,
                  "maxLength": _TITLE_MAX_CHARS},
        "side": side_schema,
        "stage": stage_schema,
        "skills": {
            "type": "array", "minItems": 1, "maxItems": _SKILLS_MAX,
            "uniqueItems": True,
            "items": {"type": "string", "enum": list(SKILLS)},
        },
        "topic_tags": _topic_tags_schema(allowed_topic_tags),
    }


def _rag_knowledge_candidate_schema(common: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "description": (
            "Across one candidate, title plus every fact-unit text and "
            f"synthetic_note must total at most {AI_FACTORY_RAG_CONTENT_MAX_CHARS} "
            "Unicode code points."
        ),
        "additionalProperties": False,
        "required": [
            "title", "side", "stage", "skills", "topic_tags", "summary",
            "claims", "limitations",
        ],
        "properties": {
            **common,
            "summary": {"$ref": "#/$defs/fact_unit"},
            "claims": {
                "type": "array", "minItems": 1,
                "maxItems": AI_FACTORY_RAG_CLAIM_MAX,
                "items": {"$ref": "#/$defs/fact_unit"},
            },
            "limitations": {
                "type": "array", "minItems": 0,
                "maxItems": _RAG_LIMITATION_MAX,
                "items": {"$ref": "#/$defs/fact_unit"},
            },
        },
    }


def _rag_argument_candidate_schema(common: dict[str, Any]) -> dict[str, Any]:
    section = {
        "type": "array", "minItems": 0, "maxItems": _ARGUMENT_SECTION_MAX,
        "items": {"$ref": "#/$defs/fact_unit"},
    }
    return {
        "type": "object",
        "description": (
            "Across one candidate, title plus every fact-unit text and "
            f"synthetic_note must total at most {AI_FACTORY_RAG_CONTENT_MAX_CHARS} "
            "Unicode code points."
        ),
        "additionalProperties": False,
        "required": [
            "title", "side", "stage", "skills", "topic_tags", "main_claim",
            "premises", "mechanisms", "impacts", "counterarguments",
            "rebuttals", "weighing",
        ],
        "properties": {
            **common,
            "main_claim": {"$ref": "#/$defs/fact_unit"},
            "premises": {
                "type": "array", "minItems": 1,
                "maxItems": _ARGUMENT_SECTION_MAX,
                "items": {"$ref": "#/$defs/fact_unit"},
            },
            "mechanisms": deepcopy(section),
            "impacts": deepcopy(section),
            "counterarguments": deepcopy(section),
            "rebuttals": deepcopy(section),
            "weighing": deepcopy(section),
        },
    }


def _message_schema(role: str, max_chars: int, content_const: str = "") -> dict[str, Any]:
    content: dict[str, Any]
    if content_const:
        content = {"const": content_const}
    else:
        content = {"type": "string", "minLength": 1, "maxLength": max_chars}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["role", "content"],
        "properties": {"role": {"const": role}, "content": content},
    }


def _provenance_schema(
    message_index: int, speaker_origin: str, *, fact_status: str | None = None,
    allowed_statuses: Sequence[str] = FACT_STATUSES,
) -> dict[str, Any]:
    status: dict[str, Any] = (
        {"const": fact_status}
        if fact_status else {"type": "string", "enum": list(allowed_statuses)}
    )
    schema = {
        "type": "object", "additionalProperties": False,
        "required": [
            "message_index", "speaker_origin", "fact_status", "source_refs",
            "synthetic_note",
        ],
        "properties": {
            "message_index": {"const": message_index},
            "speaker_origin": {"const": speaker_origin},
            "fact_status": status,
            "source_refs": {
                "type": "array", "minItems": 0, "maxItems": _SOURCE_REFS_MAX,
                "items": {"$ref": "#/$defs/source_ref"},
            },
            "synthetic_note": {
                "type": "string", "minLength": 0,
                "maxLength": _SYNTHETIC_NOTE_MAX_CHARS,
            },
        },
    }
    if fact_status == "synthetic":
        schema["properties"]["synthetic_note"]["minLength"] = 1
    elif fact_status in ("source_backed", "derived"):
        schema["properties"]["source_refs"]["minItems"] = 1
        schema["properties"]["synthetic_note"]["maxLength"] = 0
    else:
        schema["allOf"] = [{
            "if": {"properties": {"fact_status": {"const": "synthetic"}}},
            "then": {"properties": {"synthetic_note": {"minLength": 1}}},
            "else": {
                "properties": {
                    "source_refs": {"minItems": 1},
                    "synthetic_note": {"maxLength": 0},
                },
            },
        }]
    return schema


def _sft_speech_candidate_schema(common: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": [
            "title", "side", "stage", "skills", "topic_tags", "messages",
            "message_provenance",
        ],
        "properties": {
            **common,
            "messages": {
                "type": "array", "minItems": 3, "maxItems": 3,
                "prefixItems": [
                    _message_schema(
                        "system", len(AI_DATA_FACTORY_SFT_SPEECH_SYSTEM_MESSAGE),
                        AI_DATA_FACTORY_SFT_SPEECH_SYSTEM_MESSAGE,
                    ),
                    _message_schema("user", AI_FACTORY_SFT_USER_MAX_CHARS),
                    _message_schema("assistant", AI_FACTORY_SFT_ASSISTANT_MAX_CHARS),
                ],
                "items": False,
            },
            "message_provenance": {
                "type": "array", "minItems": 2, "maxItems": 2,
                "prefixItems": [
                    _provenance_schema(1, "source_speech", fact_status="source_backed"),
                    _provenance_schema(
                        2, "assistant_target",
                        allowed_statuses=("derived", "synthetic"),
                    ),
                ],
                "items": False,
            },
        },
    }


def _sft_attack_candidate_schema(common: dict[str, Any]) -> dict[str, Any]:
    attack_skills = deepcopy(common["skills"])
    attack_skills.update({
        "contains": {"enum": ["questioning", "rebuttal"]},
        "minContains": 1,
    })
    messages = [
        _message_schema(
            "system", len(AI_DATA_FACTORY_SFT_ATTACK_DEFENCE_SYSTEM_MESSAGE),
            AI_DATA_FACTORY_SFT_ATTACK_DEFENCE_SYSTEM_MESSAGE,
        ),
    ]
    provenance = []
    for index in range(1, 7):
        if index % 2:
            messages.append(_message_schema("user", AI_FACTORY_SFT_USER_MAX_CHARS))
            provenance.append(_provenance_schema(
                index, "synthetic_opponent", fact_status="synthetic",
            ))
        else:
            messages.append(_message_schema(
                "assistant", AI_FACTORY_SFT_ASSISTANT_MAX_CHARS,
            ))
            provenance.append(_provenance_schema(
                index, "assistant_target",
                allowed_statuses=("derived", "synthetic"),
            ))
    return {
        "type": "object", "additionalProperties": False,
        "required": [
            "title", "side", "stage", "skills", "topic_tags", "messages",
            "message_provenance",
        ],
        "properties": {
            **common,
            "skills": attack_skills,
            "messages": {
                "type": "array", "minItems": 7, "maxItems": 7,
                "prefixItems": messages, "items": False,
            },
            "message_provenance": {
                "type": "array", "minItems": 6, "maxItems": 6,
                "prefixItems": provenance, "items": False,
                "contains": {
                    "type": "object",
                    "required": ["source_refs"],
                    "properties": {
                        "source_refs": {"type": "array", "minItems": 1},
                    },
                },
                "minContains": 1,
            },
        },
    }


def get_output_schema(
    recipe_id: str, *, allowed_topic_tags: Sequence[str] | None = None,
    side: str | None = None, stage: str | None = None,
) -> dict[str, Any]:
    """Return a copy of the complete Draft 2020-12 provider schema."""
    _recipe(recipe_id)
    if allowed_topic_tags is not None:
        allowed_topic_tags = _validate_topic_tags(
            allowed_topic_tags, label="allowed_topic_tags",
        )
    if side is not None:
        _require_enum(side, SIDES, "side")
    if stage is not None:
        _require_enum(stage, STAGES, "stage")
    common = _common_candidate_properties(
        allowed_topic_tags=allowed_topic_tags, side=side, stage=stage,
    )
    if recipe_id == RAG_KNOWLEDGE_CARD_RECIPE:
        candidate = _rag_knowledge_candidate_schema(common)
    elif recipe_id == RAG_ARGUMENT_DECOMPOSITION_RECIPE:
        candidate = _rag_argument_candidate_schema(common)
    elif recipe_id == SFT_SPEECH_CRITIQUE_RECIPE:
        candidate = _sft_speech_candidate_schema(common)
    else:
        candidate = _sft_attack_candidate_schema(common)
    return {
        "$schema": _SCHEMA_DRAFT,
        "$id": f"urn:skhlmc:ai-data-factory:{recipe_id}",
        "title": f"{recipe_id} provider output",
        "type": "object",
        "additionalProperties": False,
        "required": ["recipe_id", "language", "candidates"],
        "properties": {
            "recipe_id": {"const": recipe_id},
            "language": {"const": LANGUAGE},
            "candidates": {
                "type": "array", "minItems": 1,
                "maxItems": AI_FACTORY_CANDIDATE_MAX,
                "items": candidate,
            },
        },
        "$defs": {
            "source_ref": _source_ref_schema(),
            "fact_unit": _fact_unit_schema(),
        },
    }


def _prompt_json(value: Any) -> str:
    # Escaping tag delimiters means untrusted text cannot manufacture the
    # closing XML-like boundary used by the prompt.
    value = canonicalize_factory_output(value)
    return (
        value.replace("&", r"\u0026")
        .replace("<", r"\u003c")
        .replace(">", r"\u003e")
        .replace("\u2028", r"\u2028")
        .replace("\u2029", r"\u2029")
    )


def _validate_source_metadata(value: Mapping[str, Any] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FactoryContractError("source_metadata must be an object")
    unknown = set(value) - _SOURCE_METADATA_KEYS
    if unknown:
        raise FactoryContractError(
            f"source_metadata has unsupported keys: {sorted(unknown)}"
        )
    result = {}
    for key, raw in value.items():
        maximum = (
            AI_FACTORY_SOURCE_NOTE_MAX_CHARS
            if key == "source_note"
            else AI_FACTORY_INSTRUCTION_MAX_CHARS
        )
        result[key] = _require_string(
            raw, f"source_metadata.{key}", minimum=0,
            maximum=maximum,
        )
    return result


def _prompt_template_sha256(recipe_id: str) -> str:
    schema = get_output_schema(recipe_id)
    sentinel = build_ai_data_factory_user_prompt(
        recipe_instruction=_recipe_prompt_instruction(recipe_id),
        requested_count=AI_FACTORY_CANDIDATE_DEFAULT,
        requested_side="{side}",
        requested_stage="{stage}",
        allowed_topic_tags_json="{allowed_topic_tags_json}",
        manager_instruction_json="{manager_instruction_json}",
        source_json="{source_json}",
        output_schema_json=canonicalize_factory_output(schema),
    )
    return _sha256_text("\0".join((
        PROMPT_VERSION,
        AI_DATA_FACTORY_PROVIDER_SYSTEM_PROMPT,
        sentinel,
    )))


def get_prompt_template_hash(recipe_id: str) -> str:
    return _prompt_template_sha256(recipe_id)


def get_recipe_metadata(recipe_id: str) -> dict[str, Any]:
    recipe = _recipe(recipe_id)
    return {
        "recipe_id": recipe_id,
        "artifact_kind": recipe["artifact_kind"],
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "prompt_template_sha256": _prompt_template_sha256(recipe_id),
        "language": LANGUAGE,
        "temperature": recipe["temperature"],
        "web_search": WEB_SEARCH_ENABLED,
        "critic": CRITIC_ENABLED,
        "candidate_default": AI_FACTORY_CANDIDATE_DEFAULT,
        "candidate_max": AI_FACTORY_CANDIDATE_MAX,
        "sft_message_count": recipe["sft_message_count"],
        "structured_json": True,
        "require_complete": True,
    }


def list_recipe_metadata() -> list[dict[str, Any]]:
    return [get_recipe_metadata(recipe_id) for recipe_id in RECIPE_IDS]


def _recipe_prompt_instruction(recipe_id: str) -> str:
    """Expose every semantic post-parse constraint before a provider call."""
    recipe = _recipe(recipe_id)
    constraints = []
    if recipe_id in (
        RAG_KNOWLEDGE_CARD_RECIPE,
        RAG_ARGUMENT_DECOMPOSITION_RECIPE,
    ):
        constraints.append(
            "每個 candidate 嘅 title 加所有 fact unit 嘅 text 同 synthetic_note，"
            f"合計最多 {AI_FACTORY_RAG_CONTENT_MAX_CHARS} 個 Unicode 字元。"
        )
    elif recipe_id == SFT_ATTACK_DEFENCE_RECIPE:
        constraints.extend((
            "每個 candidate 嘅 skills 必須至少包含 questioning 或 rebuttal "
            "其中一項。",
            "每個 candidate 全部 message_provenance 合計必須至少有一個準確 "
            "source_ref，令對話保持有來源錨點。",
        ))
    if not constraints:
        return str(recipe["instruction"])
    return "\n".join((str(recipe["instruction"]), *constraints))


def build_factory_prompt(
    recipe_id: str, *, source_text: str,
    requested_count: int = AI_FACTORY_CANDIDATE_DEFAULT,
    side: str = "not_applicable", stage: str = "general",
    allowed_topic_tags: Sequence[str] = (), manager_instruction: str = "",
    source_metadata: Mapping[str, Any] | None = None,
) -> FactoryPrompt:
    """Build the exact preview/call prompt; no value is silently truncated."""
    recipe = _recipe(recipe_id)
    source_text = _require_string(
        source_text, "source_text", minimum=1,
        maximum=AI_FACTORY_SOURCE_MAX_CHARS, allow_controls=True,
    )
    requested_count = _require_integer(
        requested_count, "requested_count", minimum=1,
        maximum=AI_FACTORY_CANDIDATE_MAX,
    )
    side = _require_enum(side, SIDES, "side")
    stage = _require_enum(stage, STAGES, "stage")
    tags = _validate_topic_tags(allowed_topic_tags, label="allowed_topic_tags")
    manager_instruction = _require_string(
        manager_instruction, "manager_instruction", minimum=0,
        maximum=AI_FACTORY_INSTRUCTION_MAX_CHARS,
    )
    metadata = _validate_source_metadata(source_metadata)
    source_payload = {
        "metadata": metadata,
        "text": source_text,
        "text_length": len(source_text),
        "text_sha256": _sha256_text(source_text),
    }
    schema = get_output_schema(
        recipe_id, allowed_topic_tags=tags, side=side, stage=stage,
    )
    user = build_ai_data_factory_user_prompt(
        recipe_instruction=_recipe_prompt_instruction(recipe_id),
        requested_count=requested_count,
        requested_side=side,
        requested_stage=stage,
        allowed_topic_tags_json=_prompt_json(tags),
        manager_instruction_json=_prompt_json(manager_instruction),
        source_json=_prompt_json(source_payload),
        output_schema_json=canonicalize_factory_output(schema),
    )
    system = AI_DATA_FACTORY_PROVIDER_SYSTEM_PROMPT
    if len(system) + len(user) > AI_PROVIDER_PROMPT_MAX_CHARS:
        raise FactoryContractError("Factory prompt exceeds the provider prompt limit")
    prompt_sha256 = _sha256_text("\0".join((system, user)))
    return FactoryPrompt(
        recipe_id=recipe_id,
        prompt_version=PROMPT_VERSION,
        prompt_template_sha256=_prompt_template_sha256(recipe_id),
        prompt_sha256=prompt_sha256,
        system=system,
        user=user,
        temperature=float(recipe["temperature"]),
    )


def _reject_constant(value: str):
    raise FactoryContractError(f"Invalid JSON numeric constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise FactoryContractError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_provider_output(
    provider_text: str, *, max_bytes: int = AI_TRAINING_JSON_MAX_BYTES,
) -> tuple[dict[str, Any], str]:
    """Bound and parse one untouched provider message, returning its hash."""
    if not isinstance(provider_text, str):
        raise FactoryContractError("Provider output must be text")
    try:
        encoded = provider_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FactoryContractError("Provider output contains invalid Unicode") from exc
    if not encoded:
        raise FactoryContractError("Provider output is empty")
    byte_limit = _require_integer(
        max_bytes, "max_bytes", minimum=1, maximum=AI_TRAINING_JSON_MAX_BYTES,
    )
    if len(encoded) > byte_limit:
        raise FactoryContractError("Provider output exceeds the factory JSON limit")
    provider_text_sha256 = _sha256_bytes(encoded)
    try:
        value = json.loads(
            provider_text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except FactoryContractError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise FactoryContractError("Provider output is not one strict JSON value") from exc
    if not isinstance(value, dict):
        raise FactoryContractError("Provider output must be one JSON object")
    return value, provider_text_sha256


def _require_exact_keys(
    value: Any, expected: Sequence[str] | set[str], label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FactoryContractError(f"{label} must be an object")
    expected_set = set(expected)
    actual = set(value)
    missing = expected_set - actual
    extra = actual - expected_set
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if extra:
            details.append(f"unexpected {sorted(extra)}")
        raise FactoryContractError(f"{label} has {'; '.join(details)}")
    return value


def _require_string(
    value: Any, label: str, *, minimum: int = 1, maximum: int,
    allow_controls: bool = False,
) -> str:
    if not isinstance(value, str):
        raise FactoryContractError(f"{label} must be a string")
    if len(value) < minimum or len(value) > maximum:
        raise FactoryContractError(
            f"{label} length must be between {minimum} and {maximum}"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FactoryContractError(f"{label} contains invalid Unicode") from exc
    if not allow_controls and _CONTROL_CHAR_RE.search(value):
        raise FactoryContractError(f"{label} contains a forbidden control character")
    return value


def _require_integer(
    value: Any, label: str, *, minimum: int, maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FactoryContractError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise FactoryContractError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return value


def _require_enum(value: Any, allowed: Sequence[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise FactoryContractError(f"{label} has an unsupported value")
    return value


def _require_list(
    value: Any, label: str, *, minimum: int, maximum: int,
) -> list[Any]:
    if not isinstance(value, list):
        raise FactoryContractError(f"{label} must be an array")
    if len(value) < minimum or len(value) > maximum:
        raise FactoryContractError(
            f"{label} item count must be between {minimum} and {maximum}"
        )
    return value


def _validate_topic_tags(
    value: Sequence[str], *, label: str,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise FactoryContractError(f"{label} must be an array")
    if len(value) > AI_FACTORY_TOPIC_TAG_MAX:
        raise FactoryContractError(
            f"{label} cannot contain more than {AI_FACTORY_TOPIC_TAG_MAX} tags"
        )
    result = []
    seen = set()
    for index, item in enumerate(value):
        tag = _require_string(
            item, f"{label}[{index}]", minimum=1,
            maximum=AI_FACTORY_TOPIC_TAG_MAX_CHARS,
        )
        if tag != tag.strip():
            raise FactoryContractError(f"{label}[{index}] has outer whitespace")
        collapsed = re.sub(r"\s+", " ", tag)
        if tag != collapsed:
            raise FactoryContractError(
                f"{label}[{index}] must use one space between words"
            )
        normalized = collapsed.casefold()
        if len(normalized) > AI_FACTORY_TOPIC_TAG_MAX_CHARS:
            raise FactoryContractError(
                f"{label}[{index}] is too long after normalization"
            )
        if normalized in seen:
            raise FactoryContractError(f"{label} contains a duplicate tag")
        seen.add(normalized)
        result.append(tag)
    return tuple(result)


def _validate_string_enum_list(
    value: Any, label: str, *, allowed: Sequence[str], minimum: int,
    maximum: int,
) -> tuple[str, ...]:
    items = _require_list(value, label, minimum=minimum, maximum=maximum)
    result = []
    for index, item in enumerate(items):
        result.append(_require_enum(item, allowed, f"{label}[{index}]"))
    if len(set(result)) != len(result):
        raise FactoryContractError(f"{label} contains a duplicate value")
    return tuple(result)


def _validate_source_refs(
    value: Any, source_text: str, label: str, *, minimum: int = 0,
) -> list[dict[str, Any]]:
    refs = _require_list(
        value, label, minimum=minimum, maximum=_SOURCE_REFS_MAX,
    )
    seen = set()
    for index, ref in enumerate(refs):
        ref_label = f"{label}[{index}]"
        _require_exact_keys(ref, {"start", "end", "quote"}, ref_label)
        start = _require_integer(
            ref["start"], f"{ref_label}.start", minimum=0,
            maximum=len(source_text),
        )
        end = _require_integer(
            ref["end"], f"{ref_label}.end", minimum=1,
            maximum=len(source_text),
        )
        quote = _require_string(
            ref["quote"], f"{ref_label}.quote", minimum=1,
            maximum=AI_FACTORY_SOURCE_MAX_CHARS, allow_controls=True,
        )
        if end <= start:
            raise FactoryContractError(f"{ref_label}.end must be greater than start")
        if source_text[start:end] != quote:
            raise FactoryContractError(f"{ref_label}.quote does not match the source")
        signature = (start, end, quote)
        if signature in seen:
            raise FactoryContractError(f"{label} contains a duplicate source_ref")
        seen.add(signature)
    return refs


def _validate_fact_unit(
    value: Any, source_text: str, label: str,
) -> int:
    _require_exact_keys(
        value, {"text", "fact_status", "source_refs", "synthetic_note"}, label,
    )
    text = _require_string(
        value["text"], f"{label}.text", minimum=1,
        maximum=_FACT_UNIT_MAX_CHARS,
    )
    status = _require_enum(
        value["fact_status"], FACT_STATUSES, f"{label}.fact_status",
    )
    refs = _validate_source_refs(
        value["source_refs"], source_text, f"{label}.source_refs",
        minimum=0 if status == "synthetic" else 1,
    )
    note = _require_string(
        value["synthetic_note"], f"{label}.synthetic_note", minimum=0,
        maximum=_SYNTHETIC_NOTE_MAX_CHARS,
    )
    if status == "synthetic" and not note.strip():
        raise FactoryContractError(f"{label}.synthetic_note is required")
    if status != "synthetic" and note:
        raise FactoryContractError(
            f"{label}.synthetic_note must be empty unless fact_status is synthetic"
        )
    # Referencing source context is allowed for a synthetic teaching example;
    # the non-empty note is what makes its external/common-knowledge status
    # explicit to reviewers and downstream RAG consumers.
    del refs
    return len(text) + len(note)


def _validate_common_candidate(
    candidate: dict[str, Any], label: str, *, expected_side: str | None,
    expected_stage: str | None, allowed_topic_tags: set[str],
) -> None:
    _require_string(
        candidate["title"], f"{label}.title", minimum=1,
        maximum=_TITLE_MAX_CHARS,
    )
    side = _require_enum(candidate["side"], SIDES, f"{label}.side")
    stage = _require_enum(candidate["stage"], STAGES, f"{label}.stage")
    if expected_side is not None and side != expected_side:
        raise FactoryContractError(f"{label}.side does not match the request")
    if expected_stage is not None and stage != expected_stage:
        raise FactoryContractError(f"{label}.stage does not match the request")
    _validate_string_enum_list(
        candidate["skills"], f"{label}.skills", allowed=SKILLS,
        minimum=1, maximum=_SKILLS_MAX,
    )
    _validate_topic_tags(candidate["topic_tags"], label=f"{label}.topic_tags")
    # ``allowed_topic_tags`` carries the approved suggestions shown to the
    # model.  New bounded labels remain candidates until this item is approved.
    del allowed_topic_tags


def _validate_fact_unit_list(
    value: Any, source_text: str, label: str, *, minimum: int, maximum: int,
) -> int:
    items = _require_list(value, label, minimum=minimum, maximum=maximum)
    return sum(
        _validate_fact_unit(item, source_text, f"{label}[{index}]")
        for index, item in enumerate(items)
    )


def _validate_rag_knowledge(
    candidate: dict[str, Any], source_text: str, label: str,
) -> None:
    expected = {
        "title", "side", "stage", "skills", "topic_tags", "summary",
        "claims", "limitations",
    }
    _require_exact_keys(candidate, expected, label)
    title = _require_string(
        candidate["title"], f"{label}.title", minimum=1,
        maximum=_TITLE_MAX_CHARS,
    )
    generated_chars = len(title)
    generated_chars += _validate_fact_unit(
        candidate["summary"], source_text, f"{label}.summary",
    )
    generated_chars += _validate_fact_unit_list(
        candidate["claims"], source_text, f"{label}.claims", minimum=1,
        maximum=AI_FACTORY_RAG_CLAIM_MAX,
    )
    generated_chars += _validate_fact_unit_list(
        candidate["limitations"], source_text, f"{label}.limitations", minimum=0,
        maximum=_RAG_LIMITATION_MAX,
    )
    if generated_chars > AI_FACTORY_RAG_CONTENT_MAX_CHARS:
        raise FactoryContractError(f"{label} exceeds the RAG content limit")


def _validate_rag_argument(
    candidate: dict[str, Any], source_text: str, label: str,
) -> None:
    expected = {
        "title", "side", "stage", "skills", "topic_tags", "main_claim",
        "premises", "mechanisms", "impacts", "counterarguments", "rebuttals",
        "weighing",
    }
    _require_exact_keys(candidate, expected, label)
    title = _require_string(
        candidate["title"], f"{label}.title", minimum=1,
        maximum=_TITLE_MAX_CHARS,
    )
    generated_chars = len(title)
    generated_chars += _validate_fact_unit(
        candidate["main_claim"], source_text, f"{label}.main_claim",
    )
    generated_chars += _validate_fact_unit_list(
        candidate["premises"], source_text, f"{label}.premises", minimum=1,
        maximum=_ARGUMENT_SECTION_MAX,
    )
    for field in (
        "mechanisms", "impacts", "counterarguments", "rebuttals", "weighing",
    ):
        generated_chars += _validate_fact_unit_list(
            candidate[field], source_text, f"{label}.{field}", minimum=0,
            maximum=_ARGUMENT_SECTION_MAX,
        )
    if generated_chars > AI_FACTORY_RAG_CONTENT_MAX_CHARS:
        raise FactoryContractError(f"{label} exceeds the RAG content limit")


def _validate_messages(
    value: Any, label: str, *, roles: Sequence[str], system_message: str,
) -> list[dict[str, str]]:
    messages = _require_list(
        value, label, minimum=len(roles), maximum=len(roles),
    )
    for index, (message, role) in enumerate(zip(messages, roles)):
        message_label = f"{label}[{index}]"
        _require_exact_keys(message, {"role", "content"}, message_label)
        if message["role"] != role:
            raise FactoryContractError(f"{message_label}.role must be {role}")
        maximum = (
            len(system_message) if role == "system"
            else AI_FACTORY_SFT_USER_MAX_CHARS if role == "user"
            else AI_FACTORY_SFT_ASSISTANT_MAX_CHARS
        )
        content = _require_string(
            message["content"], f"{message_label}.content", minimum=1,
            maximum=maximum,
        )
        if role == "system" and content != system_message:
            raise FactoryContractError(
                f"{message_label}.content must equal the locked system message"
            )
    return messages


def _validate_message_provenance(
    value: Any, source_text: str, label: str, *, message_index: int,
    speaker_origin: str, required_status: str | None = None,
    allowed_statuses: Sequence[str] = FACT_STATUSES,
) -> dict[str, Any]:
    _require_exact_keys(value, {
        "message_index", "speaker_origin", "fact_status", "source_refs",
        "synthetic_note",
    }, label)
    index = _require_integer(
        value["message_index"], f"{label}.message_index", minimum=0, maximum=6,
    )
    if index != message_index:
        raise FactoryContractError(f"{label}.message_index is out of sequence")
    if value["speaker_origin"] != speaker_origin:
        raise FactoryContractError(f"{label}.speaker_origin must be {speaker_origin}")
    status = _require_enum(
        value["fact_status"], allowed_statuses, f"{label}.fact_status",
    )
    if required_status is not None and status != required_status:
        raise FactoryContractError(f"{label}.fact_status must be {required_status}")
    refs = _validate_source_refs(
        value["source_refs"], source_text, f"{label}.source_refs",
        minimum=0 if status == "synthetic" else 1,
    )
    note = _require_string(
        value["synthetic_note"], f"{label}.synthetic_note", minimum=0,
        maximum=_SYNTHETIC_NOTE_MAX_CHARS,
    )
    if status == "synthetic" and not note.strip():
        raise FactoryContractError(f"{label}.synthetic_note is required")
    if status != "synthetic" and note:
        raise FactoryContractError(
            f"{label}.synthetic_note must be empty unless fact_status is synthetic"
        )
    return {"status": status, "refs": refs}


def _validate_sft_speech(
    candidate: dict[str, Any], source_text: str, label: str,
) -> None:
    expected = {
        "title", "side", "stage", "skills", "topic_tags", "messages",
        "message_provenance",
    }
    _require_exact_keys(candidate, expected, label)
    messages = _validate_messages(
        candidate["messages"], f"{label}.messages",
        roles=("system", "user", "assistant"),
        system_message=AI_DATA_FACTORY_SFT_SPEECH_SYSTEM_MESSAGE,
    )
    provenance = _require_list(
        candidate["message_provenance"], f"{label}.message_provenance",
        minimum=2, maximum=2,
    )
    user_provenance = _validate_message_provenance(
        provenance[0], source_text, f"{label}.message_provenance[0]",
        message_index=1, speaker_origin="source_speech",
        required_status="source_backed",
    )
    _validate_message_provenance(
        provenance[1], source_text, f"{label}.message_provenance[1]",
        message_index=2, speaker_origin="assistant_target",
        allowed_statuses=("derived", "synthetic"),
    )
    user_content = messages[1]["content"]
    if not all(ref["quote"] in user_content for ref in user_provenance["refs"]):
        raise FactoryContractError(
            f"{label}.messages[1] must contain every source-backed quote verbatim"
        )


def _validate_sft_attack(
    candidate: dict[str, Any], source_text: str, label: str,
) -> None:
    expected = {
        "title", "side", "stage", "skills", "topic_tags", "messages",
        "message_provenance",
    }
    _require_exact_keys(candidate, expected, label)
    side = _require_enum(candidate["side"], SIDES, f"{label}.side")
    if side not in ("pro", "con"):
        raise FactoryContractError(f"{label}.side must be pro or con")
    skills = _validate_string_enum_list(
        candidate["skills"], f"{label}.skills", allowed=SKILLS,
        minimum=1, maximum=_SKILLS_MAX,
    )
    if not set(skills).intersection(("questioning", "rebuttal")):
        raise FactoryContractError(
            f"{label}.skills must include questioning or rebuttal"
        )
    _validate_messages(
        candidate["messages"], f"{label}.messages",
        roles=("system", "user", "assistant", "user", "assistant", "user", "assistant"),
        system_message=AI_DATA_FACTORY_SFT_ATTACK_DEFENCE_SYSTEM_MESSAGE,
    )
    provenance = _require_list(
        candidate["message_provenance"], f"{label}.message_provenance",
        minimum=6, maximum=6,
    )
    total_refs = 0
    for offset, item in enumerate(provenance):
        message_index = offset + 1
        is_opponent = message_index % 2 == 1
        checked = _validate_message_provenance(
            item, source_text, f"{label}.message_provenance[{offset}]",
            message_index=message_index,
            speaker_origin=("synthetic_opponent" if is_opponent else "assistant_target"),
            required_status="synthetic" if is_opponent else None,
            allowed_statuses=("synthetic",) if is_opponent else ("derived", "synthetic"),
        )
        total_refs += len(checked["refs"])
    if total_refs < 1:
        raise FactoryContractError(f"{label} must remain anchored to the source")


def _normalise_hash_value(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if isinstance(value, list):
        return [_normalise_hash_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalise_hash_value(item) for key, item in value.items()}
    return value


def _fact_unit_substance(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"text": None, "fact_status": None}
    return {
        "text": value.get("text"),
        "fact_status": value.get("fact_status"),
    }


def _fact_unit_list_substance(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items = [_normalise_hash_value(_fact_unit_substance(item)) for item in value]
    return sorted(items, key=canonicalize_factory_output)


def candidate_content_hash(recipe_id: str, candidate: Mapping[str, Any]) -> str:
    """Hash generated substance, excluding labels which can hide duplicates."""
    _recipe(recipe_id)
    if recipe_id == RAG_KNOWLEDGE_CARD_RECIPE:
        substance = {
            "summary": _fact_unit_substance(candidate.get("summary")),
            "claims": _fact_unit_list_substance(candidate.get("claims")),
            "limitations": _fact_unit_list_substance(candidate.get("limitations")),
        }
    elif recipe_id == RAG_ARGUMENT_DECOMPOSITION_RECIPE:
        substance = {"main_claim": _fact_unit_substance(candidate.get("main_claim"))}
        for key in (
            "premises", "mechanisms", "impacts", "counterarguments",
            "rebuttals", "weighing",
        ):
            substance[key] = _fact_unit_list_substance(candidate.get(key))
    else:
        substance = {"messages": candidate.get("messages")}
    return content_hash(_normalise_hash_value(substance))


def validate_factory_output(
    payload: Any, *, recipe_id: str, source_text: str, requested_count: int,
    allowed_topic_tags: Sequence[str] = (), expected_side: str | None = None,
    expected_stage: str | None = None,
) -> tuple[str, ...]:
    """Validate the entire response atomically and return candidate hashes."""
    _recipe(recipe_id)
    source_text = _require_string(
        source_text, "source_text", minimum=1,
        maximum=AI_FACTORY_SOURCE_MAX_CHARS, allow_controls=True,
    )
    requested_count = _require_integer(
        requested_count, "requested_count", minimum=1,
        maximum=AI_FACTORY_CANDIDATE_MAX,
    )
    if expected_side is not None:
        expected_side = _require_enum(expected_side, SIDES, "expected_side")
    if expected_stage is not None:
        expected_stage = _require_enum(expected_stage, STAGES, "expected_stage")
    allowed_tags = set(_validate_topic_tags(
        allowed_topic_tags, label="allowed_topic_tags",
    ))
    envelope = _require_exact_keys(
        payload, {"recipe_id", "language", "candidates"}, "output",
    )
    if envelope["recipe_id"] != recipe_id:
        raise FactoryContractError("output.recipe_id does not match the request")
    if envelope["language"] != LANGUAGE:
        raise FactoryContractError(f"output.language must be {LANGUAGE}")
    candidates = _require_list(
        envelope["candidates"], "output.candidates", minimum=requested_count,
        maximum=requested_count,
    )
    hashes = []
    for index, candidate in enumerate(candidates):
        label = f"output.candidates[{index}]"
        if not isinstance(candidate, dict):
            raise FactoryContractError(f"{label} must be an object")
        # Recipe validators verify the exact key set before this common pass.
        if recipe_id == RAG_KNOWLEDGE_CARD_RECIPE:
            _validate_rag_knowledge(candidate, source_text, label)
        elif recipe_id == RAG_ARGUMENT_DECOMPOSITION_RECIPE:
            _validate_rag_argument(candidate, source_text, label)
        elif recipe_id == SFT_SPEECH_CRITIQUE_RECIPE:
            _validate_sft_speech(candidate, source_text, label)
        else:
            _validate_sft_attack(candidate, source_text, label)
        _validate_common_candidate(
            candidate, label, expected_side=expected_side,
            expected_stage=expected_stage, allowed_topic_tags=allowed_tags,
        )
        hashes.append(candidate_content_hash(recipe_id, candidate))
    if len(set(hashes)) != len(hashes):
        raise FactoryContractError("Provider batch contains duplicate candidates")
    return tuple(hashes)


def parse_validate_canonicalize(
    provider_text: str, *, recipe_id: str, source_text: str,
    requested_count: int = AI_FACTORY_CANDIDATE_DEFAULT,
    allowed_topic_tags: Sequence[str] = (), expected_side: str | None = None,
    expected_stage: str | None = None,
) -> FactoryParseResult:
    """Apply the full all-or-nothing provider-output contract."""
    payload, provider_text_sha256 = parse_provider_output(provider_text)
    candidate_hashes = validate_factory_output(
        payload, recipe_id=recipe_id, source_text=source_text,
        requested_count=requested_count,
        allowed_topic_tags=allowed_topic_tags,
        expected_side=expected_side, expected_stage=expected_stage,
    )
    canonical_json = canonicalize_factory_output(payload)
    if len(canonical_json.encode("utf-8")) > AI_TRAINING_JSON_MAX_BYTES:
        raise FactoryContractError("Canonical factory JSON exceeds the storage limit")
    return FactoryParseResult(
        payload=deepcopy(payload),
        canonical_json=canonical_json,
        provider_text_sha256=provider_text_sha256,
        content_sha256=_sha256_text(canonical_json),
        candidate_hashes=candidate_hashes,
    )


def estimate_text_tokens(value: str) -> int:
    # Conservative preview heuristic for mixed Cantonese/English.  Provider
    # usage metadata remains authoritative after a real attempt.
    ascii_chars = sum(ord(character) < 128 for character in value)
    non_ascii_chars = len(value) - ascii_chars
    return max(1, math.ceil(ascii_chars / 4 + non_ascii_chars * 1.2))


def estimate_factory_cost(
    model_config: Mapping[str, Any], prompt: FactoryPrompt, *,
    requested_count: int = AI_FACTORY_CANDIDATE_DEFAULT, model_label: str = "",
) -> dict[str, Any]:
    """Return a transparent pre-call estimate; actual provider usage wins."""
    if not isinstance(model_config, Mapping):
        raise FactoryContractError("model_config must be an object")
    recipe = _recipe(prompt.recipe_id)
    requested_count = _require_integer(
        requested_count, "requested_count", minimum=1,
        maximum=AI_FACTORY_CANDIDATE_MAX,
    )

    def rate(name: str) -> float:
        raw = model_config.get(name) or 0
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise FactoryContractError(f"Invalid model rate: {name}") from exc
        if not math.isfinite(value) or value < 0:
            raise FactoryContractError(f"Invalid model rate: {name}")
        return value

    input_tokens = estimate_text_tokens(prompt.system + prompt.user)
    output_tokens = int(
        recipe["estimated_output_tokens_per_candidate"] * requested_count
    )
    usd = (
        input_tokens * rate("input_price_per_million")
        + output_tokens * rate("output_price_per_million")
    ) / 1_000_000
    return {
        "recipe_id": prompt.recipe_id,
        "model_label": str(model_label or ""),
        "provider": str(model_config.get("provider") or ""),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "audio_tokens": 0,
        "search_calls": 0,
        "estimated_cost_usd": round(usd, 8),
        "estimated_cost_hkd": round(usd * HKD_PER_USD, 8),
        "cost_source": "factory_preflight_estimate",
    }


__all__ = [
    "CRITIC_ENABLED",
    "FACT_STATUSES",
    "FactoryContractError",
    "FactoryParseResult",
    "FactoryPrompt",
    "LANGUAGE",
    "RAG_ARGUMENT_DECOMPOSITION_RECIPE",
    "RAG_KNOWLEDGE_CARD_RECIPE",
    "RECIPE_IDS",
    "PROMPT_VERSION",
    "SCHEMA_VERSION",
    "SFT_ATTACK_DEFENCE_RECIPE",
    "SFT_SPEECH_CRITIQUE_RECIPE",
    "SIDES",
    "SKILLS",
    "STAGES",
    "WEB_SEARCH_ENABLED",
    "build_factory_prompt",
    "candidate_content_hash",
    "canonicalize_factory_output",
    "content_hash",
    "estimate_factory_cost",
    "estimate_text_tokens",
    "get_output_schema",
    "get_prompt_template_hash",
    "get_recipe_metadata",
    "list_recipe_metadata",
    "parse_provider_output",
    "parse_validate_canonicalize",
    "validate_factory_output",
]
