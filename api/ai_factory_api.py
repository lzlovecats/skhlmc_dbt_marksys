"""Admin-only workflow for producing reviewed debate RAG and SFT artifacts.

The HTTP layer owns authentication, exact-send confirmation and provider
orchestration.  Durable transitions live in :mod:`core.ai_factory_store`, and
all prompt/output contracts live in :mod:`core.ai_data_factory`.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ai_model_config import (
    AI_FACTORY_MODEL_OPTIONS,
    NON_MANUAL_DEFAULT_AI_MODEL,
    resolve_interactive_model_settings,
)
from api.access import require_page_user_or_developer
from api.pagination import PAGE_SIZE, bounds, json_safe, payload, scalar_count
from core.ai_data_factory import (
    FACT_STATUSES,
    LANGUAGE,
    RECIPE_IDS,
    SCHEMA_VERSION,
    SFT_ATTACK_DEFENCE_RECIPE,
    SIDES,
    SKILLS,
    STAGES,
    FactoryContractError,
    build_factory_prompt,
    canonicalize_factory_output,
    content_hash,
    estimate_factory_cost,
    list_recipe_metadata,
    parse_validate_canonicalize,
    validate_factory_output,
)
from core.ai_factory_store import (
    FACTORY_TABLES,
    SOURCE_LANGUAGES,
    FactoryStoreError,
    canonical_json,
    claim_attempt,
    complete_attempt,
    create_or_refresh_job_preview,
    create_pasted_source,
    create_release,
    fail_attempt,
    get_release_for_download,
    get_source,
    mark_provider_started,
    reap_stale_attempts,
    review_item,
    sha256_text,
    snapshot_submission_source,
    utc_now,
    withdraw_item,
    withdraw_source,
)
from core.roles import is_ai_manager
from core.schema_features import PARTIAL, READY, feature_bundle_state
from schema import (
    TABLE_AI_FACTORY_ATTEMPTS,
    TABLE_AI_FACTORY_ITEMS,
    TABLE_AI_FACTORY_JOBS,
    TABLE_AI_FACTORY_RELEASES,
    TABLE_AI_FACTORY_SOURCES,
    TABLE_AI_FACTORY_TOPIC_TAGS,
    TABLE_LLM_TRAINING_SUBMISSIONS,
)
from system_limits import (
    AI_FACTORY_ATTEMPT_MAX,
    AI_FACTORY_CANDIDATE_DEFAULT,
    AI_FACTORY_CANDIDATE_MAX,
    AI_FACTORY_INSTRUCTION_MAX_CHARS,
    AI_FACTORY_PREVIEW_TTL_SECONDS,
    AI_FACTORY_RELEASE_MAX_BYTES,
    AI_FACTORY_RELEASE_MAX_ITEMS,
    AI_FACTORY_SOURCE_MAX_CHARS,
    AI_FACTORY_SOURCE_NOTE_MAX_CHARS,
    AI_FACTORY_TOPIC_TAG_MAX,
    AI_FACTORY_TOPIC_TAG_MAX_CHARS,
    AI_PROVIDER_OUTPUT_MAX_TOKENS,
    AI_TRAINING_JSON_MAX_BYTES,
    AI_TRAINING_PROVIDER_TIMEOUT_SECONDS,
)


router = APIRouter(prefix="/api/ai-training/factory", tags=["ai-training-factory"])
CONFIRMATION_VERSION = "factory-send-v1"
THIRD_PARTY_WARNING = (
    "以上完整 system prompt、user prompt 及來源文字會傳送到所選第三方 AI "
    "provider；請先確認內容已匿名化並具有所需使用權。"
)
_RECIPE_LABELS = {
    "rag_knowledge_card_v1": "RAG 知識卡",
    "rag_argument_decomposition_v1": "RAG 論證拆解",
    "sft_speech_critique_v1": "演辭評改",
    "sft_attack_defence_v1": "三輪攻防",
}
_PII_PATTERNS = (
    ("可能包含香港身份證號碼", re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{1,2}\d{6}\([0-9A]\)(?![A-Z0-9])")),
    ("可能包含電郵地址", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")),
    ("可能包含香港電話號碼", re.compile(r"(?<!\d)(?:\+?852[\s-]?)?[2-9]\d{3}[\s-]?\d{4}(?!\d)")),
)


class FactorySourceBody(BaseModel):
    kind: str = Field(default="paste", max_length=30)
    title: str = Field(min_length=1, max_length=200)
    topic_text: str = Field(default="", max_length=500)
    side: str = Field(default="not_applicable", max_length=40)
    source_note: str = Field(min_length=1, max_length=AI_FACTORY_SOURCE_NOTE_MAX_CHARS)
    rights_basis: str = Field(max_length=40)
    content_text: str = Field(min_length=1, max_length=AI_FACTORY_SOURCE_MAX_CHARS)
    language: str = Field(default=LANGUAGE, max_length=35)


class FactoryPreviewBody(BaseModel):
    source_id: str = Field(default="", max_length=100)
    recipe_id: str = Field(default="", max_length=80)
    model_label: str = Field(default=NON_MANUAL_DEFAULT_AI_MODEL, max_length=200)
    item_count: int = Field(default=AI_FACTORY_CANDIDATE_DEFAULT, ge=1, le=AI_FACTORY_CANDIDATE_MAX)
    topic_tag_ids: list[str] = Field(default_factory=list, max_length=AI_FACTORY_TOPIC_TAG_MAX)
    output_language: str = Field(default=LANGUAGE, max_length=35)
    manager_instruction: str = Field(default="", max_length=AI_FACTORY_INSTRUCTION_MAX_CHARS)
    job_id: str = Field(default="", max_length=100)


class FactoryGenerateBody(BaseModel):
    preview_token: str = Field(min_length=1, max_length=30_000)
    rights_confirmed: bool = False
    anonymized_confirmed: bool = False
    third_party_confirmed: bool = False
    pii_override_reason: str = Field(default="", max_length=1000)


class FactoryReviewBody(BaseModel):
    reviewed_payload: dict = Field(default_factory=dict)
    status: str = Field(max_length=30)
    note: str = Field(default="", max_length=2000)
    expected_revision: int = Field(default=0, ge=0)


class FactoryWithdrawBody(BaseModel):
    reason: str = Field(default="管理員由資料工廠撤回", max_length=1000)


class FactoryReleaseBody(BaseModel):
    dataset_kind: str = Field(max_length=20)
    item_ids: list[str] = Field(min_length=1, max_length=AI_FACTORY_RELEASE_MAX_ITEMS)
    note: str = Field(default="", max_length=2000)


def _manager(request: Request):
    from deploy.proxy import get_vote_db

    user = require_page_user_or_developer(request, "ai_training")
    db = get_vote_db()
    if not is_ai_manager(user, db=db):
        raise HTTPException(403, "只有 AI 管理員可使用資料工廠")
    return user, db


def _factory_state(db) -> str:
    try:
        return feature_bundle_state(db, "data_factory", FACTORY_TABLES)
    except Exception as exc:
        raise HTTPException(503, "資料工廠 schema 狀態暫時無法驗證") from exc


def _require_ready(db) -> None:
    state = _factory_state(db)
    if state == PARTIAL:
        raise HTTPException(503, "資料工廠只完成部分 migration，暫停所有操作")
    if state != READY:
        raise HTTPException(503, "資料工廠尚未由正式 migration 啟用")


def _store_call(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except FactoryStoreError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


def _contract_call(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except FactoryContractError as exc:
        raise HTTPException(400, str(exc)) from exc


def _as_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise HTTPException(500, "已儲存資料格式不正確") from exc
        if isinstance(parsed, dict):
            return parsed
    raise HTTPException(500, "已儲存資料格式不正確")


def _runtime_models(db) -> tuple[list[dict], str]:
    try:
        from core.config_store import get_configs

        configured = get_configs(db, ("ai_enabled_providers", "ai_default_model"))
    except Exception as exc:
        raise HTTPException(503, "AI provider 設定暫時無法驗證") from exc
    enabled, _interactive_default = resolve_interactive_model_settings(
        configured.get("ai_enabled_providers"), configured.get("ai_default_model")
    )
    from deploy.proxy import _get_proxy_secret

    models = []
    for label, config in AI_FACTORY_MODEL_OPTIONS.items():
        if config.get("provider") not in enabled:
            continue
        key_name = str(config.get("api_key") or "")
        models.append(
            {
                "label": label,
                "provider": str(config.get("provider") or ""),
                "provider_model": str(config.get("model") or ""),
                "available": bool(key_name and _get_proxy_secret(key_name).strip()),
                "pricing_label": str(config.get("pricing_label") or ""),
                "pricing_note": str(config.get("pricing_note") or ""),
            }
        )
    available_labels = [item["label"] for item in models if item["available"]]
    if NON_MANUAL_DEFAULT_AI_MODEL in available_labels:
        default_model = NON_MANUAL_DEFAULT_AI_MODEL
    elif available_labels:
        default_model = available_labels[0]
    else:
        default_model = NON_MANUAL_DEFAULT_AI_MODEL
    return models, default_model


def _model_config(db, label: str) -> tuple[dict, str]:
    models, _default = _runtime_models(db)
    row = next((item for item in models if item["label"] == label), None)
    if row is None:
        raise HTTPException(400, "所選模型或 provider 未獲啟用")
    if not row["available"]:
        raise HTTPException(503, "所選模型尚未設定 provider key")
    config = dict(AI_FACTORY_MODEL_OPTIONS[label])
    if config.get("billing_mode") == "free_only":
        rate_keys = (
            "input_price_per_million",
            "audio_input_price_per_million",
            "output_price_per_million",
            "web_search_price_per_call",
        )
        try:
            zero_rated = all(float(config.get(key) or 0) == 0 for key in rate_keys)
        except (TypeError, ValueError, OverflowError):
            zero_rated = False
        if (
            config.get("provider") != "openrouter"
            or config.get("model") != "openrouter/free"
            or not zero_rated
        ):
            raise HTTPException(503, "Free Provider 中央設定不安全，暫時不可使用")
    from deploy.proxy import _get_proxy_secret

    key = _get_proxy_secret(str(config.get("api_key") or "")).strip()
    if not key:
        raise HTTPException(503, "所選模型尚未設定 provider key")
    return config, key


def _side(value) -> str:
    normalized = str(value or "").strip().lower()
    aliases = {
        "正方": "pro",
        "proposition": "pro",
        "反方": "con",
        "opposition": "con",
        "中立": "neutral",
        "不適用": "not_applicable",
        "n/a": "not_applicable",
    }
    resolved = aliases.get(normalized, normalized)
    return resolved if resolved in SIDES else "not_applicable"


def _validate_recipe_side(recipe_id: str, side: str) -> None:
    if recipe_id == SFT_ATTACK_DEFENCE_RECIPE and side not in ("pro", "con"):
        raise HTTPException(400, "攻防演練只可使用已標示正方或反方嘅來源")


def _source_metadata(source: dict) -> dict[str, str]:
    return {
        "source_kind": str(source.get("source_kind") or ""),
        "source_revision_id": str(source.get("id") or ""),
        "title": str(source.get("title") or ""),
        "topic": str(source.get("topic_text") or ""),
        "data_type": str(source.get("data_type") or ""),
        "source_note": str(source.get("source_note") or ""),
    }


def _pii_warnings(text_value: str) -> list[str]:
    return [label for label, pattern in _PII_PATTERNS if pattern.search(str(text_value or ""))]


def _outbound_pii_warnings(
    source: dict,
    manager_instruction: str,
    topic_tag_labels: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Scan every untrusted value embedded in the exact outbound prompt."""
    values = [
        source.get("data_type"),
        source.get("title"),
        source.get("topic_text"),
        source.get("side"),
        source.get("source_note"),
        source.get("content_text"),
        manager_instruction,
        *topic_tag_labels,
    ]
    return _pii_warnings("\n".join(str(value or "") for value in values))


def _topic_tags(db, tag_ids: list[str]) -> list[dict]:
    ids = list(dict.fromkeys(str(value or "").strip() for value in tag_ids if str(value or "").strip()))
    if len(ids) != len(tag_ids) or len(ids) > AI_FACTORY_TOPIC_TAG_MAX:
        raise HTTPException(400, "主題標籤選擇不正確")
    if not ids:
        return []
    rows = db.query(
        f"""SELECT id,label FROM {TABLE_AI_FACTORY_TOPIC_TAGS}
            WHERE id=ANY(:ids) AND retired_at IS NULL""",
        {"ids": ids},
    )
    values = json_safe([dict(row) for row in rows.to_dict("records")])
    by_id = {str(row["id"]): row for row in values}
    if set(by_id) != set(ids):
        raise HTTPException(409, "部分主題標籤已停用或不存在")
    return [by_id[value] for value in ids]


def _preview_payload(
    model_label: str, config: dict, prompt, *, max_output_tokens: int,
) -> dict:
    return {
        "model_label": model_label,
        "provider": str(config.get("provider") or ""),
        "provider_model": str(config.get("model") or ""),
        "system_prompt": prompt.system,
        "user_prompt": prompt.user,
        "temperature": prompt.temperature,
        "max_output_tokens": int(max_output_tokens),
        "web_search": False,
        "structured_json": True,
        "require_complete": True,
    }


def _factory_output_token_limit(estimate: dict) -> int:
    """Reject a recipe that cannot fit the configured provider ceiling."""
    try:
        required = int(estimate.get("output_tokens") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(503, "模型成本估算缺少有效輸出上限") from exc
    if required < 1:
        raise HTTPException(503, "模型成本估算缺少有效輸出上限")
    if required > AI_PROVIDER_OUTPUT_MAX_TOKENS:
        raise HTTPException(
            503,
            "伺服器模型輸出上限不足以完成今次配方；請減少候選數量或調整系統上限",
        )
    return required


def _preview_hashes(source: dict, prompt, provider_payload: dict, context: dict) -> tuple[str, str]:
    input_hash = sha256_text(canonical_json({
        "source_id": str(source["id"]),
        "source_sha256": str(source["content_sha256"]),
        "prompt_sha256": prompt.prompt_sha256,
        **context,
    }))
    return input_hash, sha256_text(canonical_json(provider_payload))


def _preview_secret() -> str:
    from deploy.proxy import _get_relay_cookie_secret

    secret = _get_relay_cookie_secret()
    if not secret:
        raise HTTPException(503, "伺服器簽署設定未完成，暫時不可生成")
    return str(secret)


def _actual_usage(
    raw_usage: dict,
    config: dict,
    model_label: str,
    job_id: str,
    attempt_no: int,
    *,
    fallback_estimate: dict | None = None,
) -> dict:
    usage = dict(raw_usage or {})
    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    audio_tokens = max(0, int(usage.get("audio_tokens") or 0))
    has_provider_usage = any((input_tokens, output_tokens, audio_tokens))
    if has_provider_usage:
        usd = (
            input_tokens * float(config.get("input_price_per_million") or 0)
            + audio_tokens * float(
                config.get("audio_input_price_per_million")
                or config.get("input_price_per_million")
                or 0
            )
            + output_tokens * float(config.get("output_price_per_million") or 0)
        ) / 1_000_000
        hkd = usd * 7.8
        cost_source = str(
            usage.get("cost_source") or "provider_usage_metadata"
        )
    else:
        fallback = fallback_estimate or {}
        usd = max(0.0, float(fallback.get("estimated_cost_usd") or 0))
        hkd = max(0.0, float(fallback.get("estimated_cost_hkd") or 0))
        cost_source = "factory_preflight_estimate_no_provider_usage"
    usage.update(
        {
            "provider": str(config.get("provider") or "other"),
            "model_label": model_label,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "audio_tokens": audio_tokens,
            "estimated_cost_usd": round(usd, 8),
            "estimated_cost_hkd": round(hkd, 8),
            "operation_id": job_id,
            "operation_stage": f"attempt_{attempt_no}",
            "cost_source": cost_source,
        }
    )
    return usage


def _account_provider_bytes(user: str, job_id: str, byte_count: int) -> None:
    try:
        from deploy.proxy import record_bandwidth_usage

        record_bandwidth_usage(
            "ai_factory_provider",
            max(0, int(byte_count)),
            user,
            details=f"job:{job_id}"[:500],
        )
    except Exception:
        pass


def _account_download(user: str, release_id: str, byte_count: int) -> None:
    try:
        from deploy.proxy import record_bandwidth_usage

        record_bandwidth_usage(
            "ai_factory_release_download",
            max(0, int(byte_count)),
            user,
            details=f"release:{release_id}"[:500],
        )
    except Exception:
        pass


def _require_nonessential_bandwidth() -> None:
    from deploy.proxy import _bandwidth_essential_gate_error

    budget_error = _bandwidth_essential_gate_error()
    if budget_error:
        raise HTTPException(429, budget_error)


@router.get("/bootstrap")
def bootstrap(request: Request):
    _user, db = _manager(request)
    state = _factory_state(db)
    models, default_model = _runtime_models(db)
    tags = []
    if state == READY:
        frame = db.query(
            f"""SELECT id,label,approved_by,approved_at
                FROM {TABLE_AI_FACTORY_TOPIC_TAGS}
                WHERE retired_at IS NULL ORDER BY normalized_label
                LIMIT 500"""
        )
        tags = [dict(row) for row in frame.to_dict("records")]
    recipes = []
    for recipe in list_recipe_metadata():
        value = dict(recipe)
        value["label"] = _RECIPE_LABELS[value["recipe_id"]]
        recipes.append(value)
    readiness = {
        "ready": state == READY,
        "status": state,
        "message": (
            f"已就緒｜輸出語言固定為香港粵語繁體中文（{LANGUAGE}）"
            if state == READY
            else "資料工廠尚未由正式 migration 完整啟用"
        ),
    }
    return json_safe(
        {
            "models": models,
            "default_model": default_model,
            "recipes": recipes,
            "taxonomy": {
                "language": LANGUAGE,
                "sides": list(SIDES),
                "stages": list(STAGES),
                "skills": list(SKILLS),
                "fact_statuses": list(FACT_STATUSES),
            },
            "topic_tags": tags,
            "limits": {
                "source_max_chars": AI_FACTORY_SOURCE_MAX_CHARS,
                "source_note_max_chars": AI_FACTORY_SOURCE_NOTE_MAX_CHARS,
                "instruction_max_chars": AI_FACTORY_INSTRUCTION_MAX_CHARS,
                "default_items_per_job": AI_FACTORY_CANDIDATE_DEFAULT,
                "max_items_per_job": AI_FACTORY_CANDIDATE_MAX,
                "max_attempts_per_job": AI_FACTORY_ATTEMPT_MAX,
                "preview_ttl_seconds": AI_FACTORY_PREVIEW_TTL_SECONDS,
                "release_max_items": AI_FACTORY_RELEASE_MAX_ITEMS,
                "release_max_bytes": AI_FACTORY_RELEASE_MAX_BYTES,
                "topic_tag_max": AI_FACTORY_TOPIC_TAG_MAX,
                "topic_tag_max_chars": AI_FACTORY_TOPIC_TAG_MAX_CHARS,
            },
            "readiness": readiness,
            "third_party_warning": THIRD_PARTY_WARNING,
        }
    )


@router.get("/sources")
def sources(request: Request, page: int = 1, kind: str = "all", search: str = ""):
    _user, db = _manager(request)
    _require_ready(db)
    page, _, offset = bounds(page)
    if kind not in ("all", "submission", "paste"):
        raise HTTPException(400, "來源類型不正確")
    search = str(search or "").strip()[:200]
    params = {"search": f"%{search}%", "limit": PAGE_SIZE, "offset": offset}
    kind_clause = ""
    if kind == "submission":
        kind_clause = "AND source_kind='llm_submission'"
    elif kind == "paste":
        kind_clause = "AND source_kind='admin_paste'"
    cte = f"""WITH candidates AS (
        SELECT s.id,s.source_kind,s.origin_submission_id,s.data_type,s.title,
            s.topic_text,s.side,s.source_note,s.language_code,s.rights_basis,
            s.content_text,s.content_sha256,s.revision_no,s.source_group_id,
            s.created_by,s.created_at,TRUE AS snapshotted
        FROM {TABLE_AI_FACTORY_SOURCES} s
        WHERE s.withdrawn_at IS NULL
        UNION ALL
        SELECT 'submission:'||l.id::text AS id,'llm_submission' AS source_kind,
            l.id AS origin_submission_id,l.data_type,l.title,l.topic_text,l.side,
            l.source_note,'yue-Hant-HK' AS language_code,
            'submission_confirmed' AS rights_basis,l.content_text,
            NULL::text AS content_sha256,1 AS revision_no,
            'submission_'||l.id::text AS source_group_id,l.submitted_by AS created_by,
            l.created_at,FALSE AS snapshotted
        FROM {TABLE_LLM_TRAINING_SUBMISSIONS} l
        WHERE l.status='accepted' AND l.anonymized=TRUE
          AND l.permission_confirmed=TRUE
          AND NOT EXISTS (
            SELECT 1 FROM {TABLE_AI_FACTORY_SOURCES} s2
            WHERE s2.origin_submission_id=l.id
          )
    )"""
    where = f"""WHERE 1=1 {kind_clause}
        AND (:search='%%' OR COALESCE(title,'') ILIKE :search
             OR COALESCE(topic_text,'') ILIKE :search
             OR COALESCE(source_note,'') ILIKE :search)"""
    total = scalar_count(db, f"{cte} SELECT COUNT(*) AS total FROM candidates {where}", params)
    frame = db.query(
        f"""{cte} SELECT * FROM candidates {where}
            ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset""",
        params,
    )
    return payload([dict(row) for row in frame.to_dict("records")], page, total)


@router.post("/sources")
def create_source(body: FactorySourceBody, request: Request):
    user, db = _manager(request)
    _require_ready(db)
    if body.kind != "paste":
        raise HTTPException(400, "V0 只接受管理員貼上文字或已接受 LLM 投稿")
    if body.language not in SOURCE_LANGUAGES:
        raise HTTPException(400, "來源語言不正確")
    if body.side not in SIDES:
        raise HTTPException(400, "來源立場不正確")
    source = _store_call(
        create_pasted_source,
        db,
        user,
        title=body.title,
        topic_text=body.topic_text,
        side=body.side,
        source_note=body.source_note,
        rights_basis=body.rights_basis,
        content_text=body.content_text,
        language_code=body.language,
    )
    return {"source": json_safe(source)}


@router.post("/sources/{source_id}/withdraw")
def withdraw_factory_source(
    source_id: str,
    request: Request,
    body: FactoryWithdrawBody | None = None,
):
    user, db = _manager(request)
    _require_ready(db)
    if source_id.startswith("submission:"):
        raise HTTPException(409, "未建立快照嘅投稿請先由原投稿流程撤回")
    reason = (body.reason if body else "管理員由資料工廠撤回來源").strip()
    return _store_call(withdraw_source, db, user, source_id, reason)


@router.post("/jobs/preview")
def preview_job(body: FactoryPreviewBody, request: Request):
    user, db = _manager(request)
    _require_ready(db)
    if body.output_language != LANGUAGE:
        raise HTTPException(400, f"輸出語言固定為 {LANGUAGE}")
    source_id = body.source_id.strip()
    recipe_id = body.recipe_id.strip()
    item_count = body.item_count
    instruction = body.manager_instruction.strip()
    if recipe_id not in RECIPE_IDS:
        raise HTTPException(400, "資料配方不正確")
    config, _api_key = _model_config(db, body.model_label)
    tags = _topic_tags(db, body.topic_tag_ids)
    preview_secret = _preview_secret()
    if body.job_id:
        _store_call(reap_stale_attempts, db)
        frame = db.query(
            f"""SELECT source_id,recipe_key,requested_count,instruction_text,created_by,
                status,invalidated_at FROM {TABLE_AI_FACTORY_JOBS} WHERE id=:id""",
            {"id": body.job_id},
        )
        if frame.empty:
            raise HTTPException(404, "找不到要重試嘅生成工作")
        retry = json_safe(dict(frame.iloc[0]))
        if str(retry["created_by"]) != user:
            raise HTTPException(403, "只有建立工作嘅管理員可以重試")
        if retry["status"] != "failed" or retry.get("invalidated_at") is not None:
            raise HTTPException(409, "此工作目前不可重試")
        expected = (
            str(retry["source_id"]), str(retry["recipe_key"]),
            int(retry["requested_count"]), str(retry.get("instruction_text") or ""),
        )
        supplied = (source_id, recipe_id, item_count, instruction)
        if expected != supplied:
            raise HTTPException(409, "重試不可更改來源、配方、數量或補充指示")
    if source_id.startswith("submission:"):
        try:
            submission_id = int(source_id.split(":", 1)[1])
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "LLM 投稿來源識別碼不正確") from exc
        source = _store_call(snapshot_submission_source, db, user, submission_id)
        source_id = str(source["id"])
    else:
        source = _store_call(get_source, db, source_id)
    if source.get("withdrawn_at") is not None:
        raise HTTPException(410, "資料來源已撤回")
    tag_labels = [str(item["label"]) for item in tags]
    requested_side = _side(source.get("side"))
    _validate_recipe_side(recipe_id, requested_side)
    requested_stage = "general"
    prompt = _contract_call(
        build_factory_prompt,
        recipe_id,
        source_text=str(source["content_text"]),
        requested_count=item_count,
        side=requested_side,
        stage=requested_stage,
        allowed_topic_tags=tag_labels,
        manager_instruction=instruction,
        source_metadata=_source_metadata(source),
    )
    estimate = _contract_call(
        estimate_factory_cost,
        config,
        prompt,
        requested_count=item_count,
        model_label=body.model_label,
    )
    max_output_tokens = _factory_output_token_limit(estimate)
    provider_payload = _preview_payload(
        body.model_label,
        config,
        prompt,
        max_output_tokens=max_output_tokens,
    )
    context = {
        "recipe_id": recipe_id,
        "requested_count": item_count,
        "side": requested_side,
        "stage": requested_stage,
        "topic_tag_ids": [str(item["id"]) for item in tags],
        "topic_tag_labels": tag_labels,
        "manager_instruction": instruction,
    }
    input_sha, preview_sha = _preview_hashes(source, prompt, provider_payload, context)
    expires = utc_now() + timedelta(seconds=AI_FACTORY_PREVIEW_TTL_SECONDS)
    job = _store_call(
        create_or_refresh_job_preview,
        db,
        user,
        source_id=source_id,
        recipe_key=recipe_id,
        requested_count=item_count,
        instruction_text=instruction,
        preview_model_label=body.model_label,
        preview_provider=str(config["provider"]),
        preview_provider_model=str(config["model"]),
        preview_prompt_sha256=prompt.prompt_sha256,
        preview_input_sha256=input_sha,
        preview_sha256=preview_sha,
        preview_expires_at=expires,
        job_id=body.job_id,
    )
    from core import r2_storage

    claim = {
        "kind": "ai_factory_preview",
        "confirmation_version": CONFIRMATION_VERSION,
        "user_id": user,
        "job_id": str(job["id"]),
        "source_id": source_id,
        "source_sha256": str(source["content_sha256"]),
        "recipe_id": recipe_id,
        "prompt_version": prompt.prompt_version,
        "prompt_sha256": prompt.prompt_sha256,
        "input_sha256": input_sha,
        "preview_sha256": preview_sha,
        "model_label": body.model_label,
        "provider": str(config["provider"]),
        "provider_model": str(config["model"]),
        "requested_count": item_count,
        "side": requested_side,
        "stage": requested_stage,
        "topic_tag_ids": [str(item["id"]) for item in tags],
        "topic_tag_labels": tag_labels,
        "manager_instruction": instruction,
        "estimate": json_safe(estimate),
    }
    token = r2_storage.sign_upload_claim(
        claim, preview_secret, expires=AI_FACTORY_PREVIEW_TTL_SECONDS
    )
    return json_safe(
        {
            "job_id": str(job["id"]),
            "preview_token": token,
            "system_prompt": prompt.system,
            "user_prompt": prompt.user,
            "provider_payload": provider_payload,
            "model_label": body.model_label,
            "provider": config["provider"],
            "provider_model": config["model"],
            "estimated_cost_hkd": estimate["estimated_cost_hkd"],
            "estimated_cost_usd": estimate["estimated_cost_usd"],
            "source_sha256": source["content_sha256"],
            "preview_sha256": preview_sha,
            "expires_at": expires.isoformat(),
            "pii_warnings": _outbound_pii_warnings(source, instruction, tag_labels),
            "third_party_warning": THIRD_PARTY_WARNING,
        }
    )


@router.post("/jobs/{job_id}/generate")
async def generate_job(job_id: str, body: FactoryGenerateBody, request: Request):
    user, db = _manager(request)
    _require_ready(db)
    _require_nonessential_bandwidth()
    from core import r2_storage

    signed = r2_storage.verify_upload_claim(body.preview_token, _preview_secret())
    if not isinstance(signed, dict) or signed.get("kind") != "ai_factory_preview":
        raise HTTPException(409, "精確預覽已過期或簽署不正確，請重新預覽")
    if str(signed.get("user_id") or "") != user or str(signed.get("job_id") or "") != job_id:
        raise HTTPException(403, "精確預覽不屬於目前管理員或工作")
    if signed.get("confirmation_version") != CONFIRMATION_VERSION:
        raise HTTPException(409, "確認文字版本已更新，請重新預覽")
    if not (body.rights_confirmed and body.anonymized_confirmed and body.third_party_confirmed):
        raise HTTPException(400, "請完成使用權、匿名化及第三方 AI 傳送確認")
    frame = db.query(
        f"""SELECT j.*,s.source_kind,s.source_group_id,s.revision_no,s.data_type,
            s.title,s.topic_text,s.side,s.source_note,s.content_text,s.content_sha256,
            s.withdrawn_at AS source_withdrawn_at
            FROM {TABLE_AI_FACTORY_JOBS} j
            JOIN {TABLE_AI_FACTORY_SOURCES} s ON s.id=j.source_id
            WHERE j.id=:id""",
        {"id": job_id},
    )
    if frame.empty:
        raise HTTPException(404, "找不到生成工作")
    job = json_safe(dict(frame.iloc[0]))
    if str(job["created_by"]) != user:
        raise HTTPException(403, "只有建立工作嘅管理員可以生成或重試")
    if job.get("invalidated_at") is not None or job.get("source_withdrawn_at") is not None:
        raise HTTPException(410, "來源或生成工作已撤回")
    expected_claim = {
        "source_id": str(job["source_id"]),
        "source_sha256": str(job["content_sha256"]),
        "recipe_id": str(job["recipe_key"]),
        "model_label": str(job["preview_model_label"]),
        "provider": str(job["preview_provider"]),
        "provider_model": str(job["preview_provider_model"]),
        "prompt_sha256": str(job["preview_prompt_sha256"]),
        "input_sha256": str(job["preview_input_sha256"]),
        "preview_sha256": str(job["preview_sha256"]),
        "requested_count": int(job["requested_count"]),
    }
    if any(signed.get(key) != value for key, value in expected_claim.items()):
        raise HTTPException(409, "預覽內容、來源或模型已改變，請重新預覽")
    tag_ids = signed.get("topic_tag_ids") or []
    tag_labels = signed.get("topic_tag_labels") or []
    current_tags = _topic_tags(db, list(tag_ids))
    if [str(item["label"]) for item in current_tags] != list(tag_labels):
        raise HTTPException(409, "主題標籤已改變，請重新預覽")
    config, api_key = _model_config(db, str(job["preview_model_label"]))
    _validate_recipe_side(
        str(job["recipe_key"]), str(signed.get("side") or "not_applicable")
    )
    prompt = _contract_call(
        build_factory_prompt,
        str(job["recipe_key"]),
        source_text=str(job["content_text"]),
        requested_count=int(job["requested_count"]),
        side=str(signed.get("side") or "not_applicable"),
        stage=str(signed.get("stage") or "general"),
        allowed_topic_tags=list(tag_labels),
        manager_instruction=str(job.get("instruction_text") or ""),
        source_metadata=_source_metadata({**job, "id": job["source_id"]}),
    )
    estimate = _contract_call(
        estimate_factory_cost,
        config,
        prompt,
        requested_count=int(job["requested_count"]),
        model_label=str(job["preview_model_label"]),
    )
    max_output_tokens = _factory_output_token_limit(estimate)
    provider_payload = _preview_payload(
        str(job["preview_model_label"]),
        config,
        prompt,
        max_output_tokens=max_output_tokens,
    )
    context = {
        "recipe_id": str(job["recipe_key"]),
        "requested_count": int(job["requested_count"]),
        "side": str(signed.get("side") or "not_applicable"),
        "stage": str(signed.get("stage") or "general"),
        "topic_tag_ids": list(tag_ids),
        "topic_tag_labels": list(tag_labels),
        "manager_instruction": str(job.get("instruction_text") or ""),
    }
    input_sha, preview_sha = _preview_hashes(
        {**job, "id": job["source_id"]}, prompt, provider_payload, context
    )
    if (
        input_sha != str(job["preview_input_sha256"])
        or preview_sha != str(job["preview_sha256"])
        or prompt.prompt_sha256 != str(job["preview_prompt_sha256"])
    ):
        raise HTTPException(409, "精確預覽無法重建，請重新預覽")
    warnings = _outbound_pii_warnings(
        job, str(job.get("instruction_text") or ""), list(tag_labels)
    )
    if warnings and not body.pii_override_reason.strip():
        raise HTTPException(400, "來源有個人資料警告，請填寫覆寫理由")
    if canonical_json(signed.get("estimate")) != canonical_json(json_safe(estimate)):
        raise HTTPException(409, "模型價格或成本估算已改變，請重新預覽")
    if signed.get("prompt_version") != prompt.prompt_version:
        raise HTTPException(409, "Prompt 版本已更新，請重新預覽")
    attempt = _store_call(
        claim_attempt,
        db,
        user,
        job_id=job_id,
        preview_sha256=preview_sha,
        model_label=str(job["preview_model_label"]),
        provider=str(config["provider"]),
        provider_model=str(config["model"]),
        recipe_version=prompt.prompt_version,
        source_sha256=str(job["content_sha256"]),
        prompt_sha256=prompt.prompt_sha256,
        input_sha256=input_sha,
        candidate_count=int(job["requested_count"]),
        confirmation_version=CONFIRMATION_VERSION,
        anonymization_confirmed=body.anonymized_confirmed,
        rights_confirmed=body.rights_confirmed,
        third_party_confirmed=body.third_party_confirmed,
        pii_warning_count=len(warnings),
        pii_override_reason=body.pii_override_reason,
        estimated_cost_hkd=estimate["estimated_cost_hkd"],
    )
    attempt_id = str(attempt["id"])
    attempt_no = int(attempt["attempt_no"])
    raw_text = ""
    raw_usage = {}
    provider_called = False

    def mark_attempt_started():
        nonlocal provider_called
        _store_call(mark_provider_started, db, attempt_id)
        provider_called = True

    try:
        from core.ai_provider import generate_text

        _account_provider_bytes(
            user,
            job_id,
            len(prompt.system.encode("utf-8")) + len(prompt.user.encode("utf-8")),
        )
        raw_text, raw_usage = await generate_text(
            config,
            prompt.system,
            prompt.user,
            api_key=api_key,
            web_search=False,
            max_prompt_chars=len(prompt.system) + len(prompt.user),
            timeout_seconds=AI_TRAINING_PROVIDER_TIMEOUT_SECONDS,
            temperature=prompt.temperature,
            require_complete=True,
            structured_json=True,
            preserve_text=True,
            max_response_bytes=AI_TRAINING_JSON_MAX_BYTES,
            max_output_tokens=max_output_tokens,
            on_provider_attempt=mark_attempt_started,
        )
        response_bytes = len(raw_text.encode("utf-8"))
        _account_provider_bytes(user, job_id, response_bytes)
        parsed = parse_validate_canonicalize(
            raw_text,
            recipe_id=str(job["recipe_key"]),
            source_text=str(job["content_text"]),
            requested_count=int(job["requested_count"]),
            allowed_topic_tags=list(tag_labels),
            expected_side=str(signed.get("side") or "not_applicable"),
            expected_stage=str(signed.get("stage") or "general"),
        )
        usage = _actual_usage(
            raw_usage,
            config,
            str(job["preview_model_label"]),
            job_id,
            attempt_no,
            fallback_estimate=estimate,
        )
        completed = _store_call(
            complete_attempt,
            db,
            user,
            attempt_id,
            payloads=list(parsed.payload["candidates"]),
            response_sha256=parsed.provider_text_sha256,
            response_bytes=response_bytes,
            usage=usage,
        )
        if completed.get("discarded"):
            raise HTTPException(410, "生成期間來源已撤回；回覆已捨棄，沒有建立候選資料")
        return {"ok": True, "job_id": job_id, "attempt_no": attempt_no, "item_ids": completed["items"]}
    except HTTPException as exc:
        try:
            fail_attempt(
                db,
                user,
                attempt_id,
                error_code="factory_state_error",
                response_sha256=sha256_text(raw_text) if raw_text else "",
                response_bytes=len(raw_text.encode("utf-8")) if raw_text else 0,
                provider_called=provider_called,
                usage=(
                    _actual_usage(
                        raw_usage,
                        config,
                        str(job["preview_model_label"]),
                        job_id,
                        attempt_no,
                        fallback_estimate=estimate,
                    )
                    if provider_called
                    else None
                ),
            )
        except Exception:
            pass
        raise exc
    except FactoryContractError as exc:
        response_bytes = len(raw_text.encode("utf-8")) if raw_text else 0
        response_sha = sha256_text(raw_text) if raw_text else ""
        _store_call(
            fail_attempt,
            db,
            user,
            attempt_id,
            error_code="invalid_provider_output",
            response_sha256=response_sha,
            response_bytes=response_bytes,
            provider_called=provider_called,
            usage=_actual_usage(
                raw_usage,
                config,
                str(job["preview_model_label"]),
                job_id,
                attempt_no,
                fallback_estimate=estimate,
            ),
        )
        raise HTTPException(502, "AI 回覆格式未通過整批驗證；沒有建立任何候選資料") from exc
    except Exception as exc:
        response_usage = getattr(exc, "usage", None)
        if isinstance(response_usage, dict):
            raw_usage = dict(response_usage)
        response_bytes = len(raw_text.encode("utf-8")) if raw_text else 0
        response_sha = sha256_text(raw_text) if raw_text else ""
        try:
            fail_attempt(
                db,
                user,
                attempt_id,
                error_code="provider_error",
                response_sha256=response_sha,
                response_bytes=response_bytes,
                provider_called=provider_called,
                usage=(
                    _actual_usage(
                        raw_usage,
                        config,
                        str(job["preview_model_label"]),
                        job_id,
                        attempt_no,
                        fallback_estimate=estimate,
                    )
                    if provider_called
                    else None
                ),
            )
        except Exception:
            pass
        raise HTTPException(502, "AI provider 暫時未能完成生成；系統不會自動重試") from exc


@router.get("/jobs")
def jobs(request: Request, page: int = 1):
    _user, db = _manager(request)
    _require_ready(db)
    page, _, offset = bounds(page)
    total = scalar_count(db, f"SELECT COUNT(*) AS total FROM {TABLE_AI_FACTORY_JOBS}")
    frame = db.query(
        f"""SELECT j.id,j.source_id,j.recipe_key,j.requested_count,
            j.instruction_text,
            CASE WHEN j.status='processing' AND j.updated_at<:stale_cutoff
                THEN 'failed' ELSE j.status END AS status,
            j.preview_model_label AS model_label,j.created_by,j.created_at,j.updated_at,
            s.source_kind,s.title AS source_title,
            COALESCE(a.attempt_count,0) AS attempt_count,a.error_code AS error_message
            FROM {TABLE_AI_FACTORY_JOBS} j
            JOIN {TABLE_AI_FACTORY_SOURCES} s ON s.id=j.source_id
            LEFT JOIN LATERAL (
                SELECT COUNT(*) OVER() AS attempt_count,error_code
                FROM {TABLE_AI_FACTORY_ATTEMPTS}
                WHERE job_id=j.id ORDER BY attempt_no DESC LIMIT 1
            ) a ON TRUE
            ORDER BY j.created_at DESC,j.id DESC LIMIT :limit OFFSET :offset""",
        {
            "limit": PAGE_SIZE,
            "offset": offset,
            "stale_cutoff": utc_now()
            - timedelta(seconds=AI_FACTORY_PREVIEW_TTL_SECONDS),
        },
    )
    return payload([dict(row) for row in frame.to_dict("records")], page, total)


@router.get("/items")
def items(request: Request, status: str = "pending", page: int = 1):
    _user, db = _manager(request)
    _require_ready(db)
    if status not in ("pending", "approved", "rejected"):
        raise HTTPException(400, "審核狀態不正確")
    page, _, offset = bounds(page)
    params = {"status": status, "limit": PAGE_SIZE, "offset": offset}
    active = "i.invalidated_at IS NULL AND j.invalidated_at IS NULL AND s.withdrawn_at IS NULL"
    total = scalar_count(
        db,
        f"""SELECT COUNT(*) AS total FROM {TABLE_AI_FACTORY_ITEMS} i
            JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=i.job_id
            JOIN {TABLE_AI_FACTORY_SOURCES} s ON s.id=j.source_id
            WHERE i.review_status=:status AND {active}""",
        params,
    )
    frame = db.query(
        f"""SELECT i.id,i.job_id,i.ordinal,i.original_json,i.reviewed_json,
            i.review_status,i.review_note,i.reviewed_by,i.reviewed_at,i.created_at,
            j.recipe_key,j.created_by AS job_created_by,s.id AS source_id,
            s.source_kind,s.title AS source_title,s.topic_text,s.side AS source_side,
            s.source_note,s.content_text AS source_text,s.content_sha256 AS source_sha256
            FROM {TABLE_AI_FACTORY_ITEMS} i
            JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=i.job_id
            JOIN {TABLE_AI_FACTORY_SOURCES} s ON s.id=j.source_id
            WHERE i.review_status=:status AND {active}
            ORDER BY COALESCE(i.reviewed_at,i.created_at) DESC,i.id DESC
            LIMIT :limit OFFSET :offset""",
        params,
    )
    values = []
    for row in frame.to_dict("records"):
        value = json_safe(dict(row))
        value["payload"] = value.get("reviewed_json") or value.get("original_json")
        value["dataset_kind"] = "sft" if str(value.get("recipe_key") or "").startswith("sft_") else "rag"
        values.append(value)
    return payload(values, page, total)


@router.post("/items/{item_id}/review")
def review(item_id: str, body: FactoryReviewBody, request: Request):
    user, db = _manager(request)
    _require_ready(db)
    if body.status not in ("approved", "rejected"):
        raise HTTPException(400, "審核狀態不正確")
    frame = db.query(
        f"""SELECT i.review_status,j.recipe_key,s.content_text,s.withdrawn_at,
            i.invalidated_at,j.invalidated_at AS job_invalidated_at
            FROM {TABLE_AI_FACTORY_ITEMS} i
            JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=i.job_id
            JOIN {TABLE_AI_FACTORY_SOURCES} s ON s.id=j.source_id
            WHERE i.id=:id""",
        {"id": item_id},
    )
    if frame.empty:
        raise HTTPException(404, "找不到候選資料")
    row = json_safe(dict(frame.iloc[0]))
    if row["review_status"] != "pending":
        raise HTTPException(409, "此候選已完成審核")
    if row.get("withdrawn_at") is not None or row.get("invalidated_at") is not None or row.get("job_invalidated_at") is not None:
        raise HTTPException(410, "候選或來源已失效")
    reviewed = body.reviewed_payload
    tags = []
    reviewed_sha = ""
    if body.status == "approved":
        raw_tags = reviewed.get("topic_tags") if isinstance(reviewed, dict) else None
        if not isinstance(raw_tags, list):
            raise HTTPException(400, "已批准資料必須包含 topic_tags array")
        tags = [str(value) for value in raw_tags]
        envelope = {"recipe_id": row["recipe_key"], "language": LANGUAGE, "candidates": [reviewed]}
        _contract_call(
            validate_factory_output,
            envelope,
            recipe_id=str(row["recipe_key"]),
            source_text=str(row["content_text"]),
            requested_count=1,
            allowed_topic_tags=tags,
        )
        reviewed_sha = content_hash(reviewed)
    result = _store_call(
        review_item,
        db,
        user,
        item_id,
        decision=body.status,
        reviewed_payload=reviewed if body.status == "approved" else None,
        reviewed_sha256=reviewed_sha,
        note=body.note,
        topic_tags=tags,
    )
    return result


@router.post("/items/{item_id}/withdraw")
def withdraw(item_id: str, request: Request, body: FactoryWithdrawBody | None = None):
    user, db = _manager(request)
    _require_ready(db)
    reason = (body.reason if body else "管理員由資料工廠撤回").strip()
    return _store_call(withdraw_item, db, user, item_id, reason)


def _release_rows(db, item_ids: list[str]) -> list[dict]:
    frame = db.query(
        f"""SELECT i.id,i.attempt_id,i.original_sha256,i.reviewed_json,
            i.reviewed_sha256,i.review_status,i.reviewed_by,i.reviewed_at,
            i.invalidated_at,j.recipe_key,j.invalidated_at AS job_invalidated_at,
            j.preview_provider_model AS requested_provider_model,
            a.attempt_no,a.model_label,a.provider,a.provider_request_id,
            a.resolved_provider_model,a.recipe_version,a.prompt_sha256,
            a.response_sha256,a.confirmation_version,
            a.anonymization_confirmed,a.rights_confirmed,
            a.third_party_confirmed,a.pii_warning_count,
            (a.pii_override_reason IS NOT NULL) AS pii_override_used,
            a.confirmed_by,a.confirmed_at,
            s.id AS source_id,s.source_group_id,s.revision_no,
            s.content_sha256 AS source_sha256,s.rights_basis,
            s.rights_confirmed_by,s.rights_confirmed_at,
            s.withdrawn_at AS source_withdrawn_at
            FROM {TABLE_AI_FACTORY_ITEMS} i
            JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=i.job_id
            JOIN {TABLE_AI_FACTORY_ATTEMPTS} a
                ON a.id=i.attempt_id AND a.job_id=i.job_id
            JOIN {TABLE_AI_FACTORY_SOURCES} s ON s.id=j.source_id
            WHERE i.id=ANY(:ids)""",
        {"ids": item_ids},
    )
    by_id = {
        str(row["id"]): json_safe(dict(row))
        for row in frame.to_dict("records")
    }
    if set(by_id) != set(item_ids):
        raise HTTPException(404, "部分已批准資料不存在")
    values = [by_id[item_id] for item_id in item_ids]
    if any(
        row.get("review_status") != "approved"
        or row.get("invalidated_at") is not None
        or row.get("job_invalidated_at") is not None
        or row.get("source_withdrawn_at") is not None
        for row in values
    ):
        raise HTTPException(409, "發布只可包含仍然有效嘅已批准資料")
    return values


@router.post("/releases")
def publish_release(body: FactoryReleaseBody, request: Request):
    user, db = _manager(request)
    _require_ready(db)
    kind = body.dataset_kind.strip().lower()
    if kind not in ("rag", "sft"):
        raise HTTPException(400, "發布類型只可為 RAG 或 SFT")
    item_ids = [str(value).strip() for value in body.item_ids]
    if any(not value for value in item_ids) or len(set(item_ids)) != len(item_ids):
        raise HTTPException(400, "發布項目不可為空或重複")
    rows = _release_rows(db, item_ids)
    lines = []
    lineage = []
    item_hashes = []
    line_hashes = []
    for row in rows:
        candidate = _as_object(row["reviewed_json"])
        recipe_id = str(row["recipe_key"])
        expected_kind = "sft" if recipe_id.startswith("sft_") else "rag"
        if expected_kind != kind:
            raise HTTPException(409, "RAG 與 SFT 資料不可混合發佈")
        leakage_group = f"source:{row['source_group_id']}"
        if kind == "sft":
            line = {"messages": candidate.get("messages"), "leakage_group": leakage_group}
        else:
            line = {
                "id": str(row["id"]),
                "recipe_id": recipe_id,
                "language": LANGUAGE,
                "content": candidate,
                "lineage": {
                    "source_id": str(row["source_id"]),
                    "source_revision": int(row["revision_no"]),
                    "source_sha256": str(row["source_sha256"]),
                },
            }
        line_text = canonicalize_factory_output(line)
        lines.append(line_text)
        item_hashes.append(str(row["reviewed_sha256"]))
        line_hashes.append(sha256_text(line_text))
        lineage.append(
            {
                "ordinal": len(lineage) + 1,
                "item_id": str(row["id"]),
                "item_sha256": str(row["reviewed_sha256"]),
                "jsonl_line_sha256": line_hashes[-1],
                "recipe_id": recipe_id,
                "source_id": str(row["source_id"]),
                "source_group_id": str(row["source_group_id"]),
                "source_revision": int(row["revision_no"]),
                "source_sha256": str(row["source_sha256"]),
                "leakage_group": leakage_group,
                "source_rights": {
                    "basis": str(row["rights_basis"]),
                    "confirmed_by": str(row["rights_confirmed_by"]),
                    "confirmed_at": row["rights_confirmed_at"],
                },
                "generation": {
                    "attempt_id": str(row["attempt_id"]),
                    "attempt_no": int(row["attempt_no"]),
                    "model_label": str(row["model_label"]),
                    "provider": str(row["provider"]),
                    "requested_provider_model": str(
                        row["requested_provider_model"]
                    ),
                    "resolved_provider_model": (
                        str(row["resolved_provider_model"])
                        if row.get("resolved_provider_model") else None
                    ),
                    "provider_request_id": (
                        str(row["provider_request_id"])
                        if row.get("provider_request_id") else None
                    ),
                    "recipe_version": str(row["recipe_version"]),
                    "prompt_sha256": str(row["prompt_sha256"]),
                    "response_sha256": str(row["response_sha256"]),
                    "confirmation": {
                        "version": str(row["confirmation_version"]),
                        "anonymization_confirmed": bool(
                            row["anonymization_confirmed"]
                        ),
                        "rights_confirmed": bool(row["rights_confirmed"]),
                        "third_party_confirmed": bool(
                            row["third_party_confirmed"]
                        ),
                        "pii_warning_count": int(row["pii_warning_count"]),
                        "pii_override_used": bool(row["pii_override_used"]),
                        "confirmed_by": str(row["confirmed_by"]),
                        "confirmed_at": row["confirmed_at"],
                    },
                },
                "review": {
                    "original_sha256": str(row["original_sha256"]),
                    "reviewed_by": str(row["reviewed_by"]),
                    "reviewed_at": row["reviewed_at"],
                },
            }
        )
    jsonl_text = "\n".join(lines) + "\n"
    manifest = {
        "format": "jsonl",
        "language": LANGUAGE,
        "source_schema_version": SCHEMA_VERSION,
        "created_by": user,
        "note": body.note,
        "items": lineage,
        "sft_contract": "messages_plus_leakage_group_only" if kind == "sft" else None,
    }
    return _store_call(
        create_release,
        db,
        user,
        release_kind=kind,
        item_ids=item_ids,
        schema_version=(
            "ai-factory-sft-messages-jsonl-v1"
            if kind == "sft"
            else "ai-factory-rag-jsonl-v1"
        ),
        jsonl_text=jsonl_text,
        manifest=manifest,
        jsonl_line_hashes=line_hashes,
        item_hashes=item_hashes,
    )


@router.get("/releases")
def releases(request: Request, page: int = 1):
    _user, db = _manager(request)
    _require_ready(db)
    page, _, offset = bounds(page)
    total = scalar_count(db, f"SELECT COUNT(*) AS total FROM {TABLE_AI_FACTORY_RELEASES}")
    frame = db.query(
        f"""SELECT id,release_kind AS dataset_kind,version_no AS version,
            schema_version,jsonl_sha256,jsonl_bytes,manifest_sha256,item_count,
            published_by,published_at AS created_at,
            CASE WHEN invalidated_at IS NULL THEN 'active' ELSE 'invalidated' END AS status,
            invalidated_at,invalidation_reason
            FROM {TABLE_AI_FACTORY_RELEASES}
            ORDER BY published_at DESC,id DESC LIMIT :limit OFFSET :offset""",
        {"limit": PAGE_SIZE, "offset": offset},
    )
    return payload([dict(row) for row in frame.to_dict("records")], page, total)


@router.get("/releases/{release_id}/export.jsonl")
def download_jsonl(release_id: str, request: Request):
    user, db = _manager(request)
    _require_ready(db)
    _require_nonessential_bandwidth()
    release = _store_call(get_release_for_download, db, release_id)
    encoded = str(release["jsonl_text"]).encode("utf-8")
    if len(encoded) > AI_FACTORY_RELEASE_MAX_BYTES:
        raise HTTPException(500, "已儲存發布檔案超過安全上限")
    _account_download(user, release_id, len(encoded))
    return Response(
        encoded,
        media_type="application/x-ndjson; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(release_id)}.jsonl",
            "Cache-Control": "private, no-store, max-age=0",
            "X-Content-SHA256": str(release["jsonl_sha256"]),
        },
    )


@router.get("/releases/{release_id}/manifest.json")
def download_manifest(release_id: str, request: Request):
    user, db = _manager(request)
    _require_ready(db)
    _require_nonessential_bandwidth()
    release = _store_call(get_release_for_download, db, release_id)
    manifest = _as_object(release["manifest_json"])
    encoded = canonical_json(manifest).encode("utf-8")
    if len(encoded) > AI_FACTORY_RELEASE_MAX_BYTES:
        raise HTTPException(500, "已儲存 manifest 超過安全上限")
    _account_download(user, release_id, len(encoded))
    return Response(
        encoded,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(release_id)}-manifest.json",
            "Cache-Control": "private, no-store, max-age=0",
            "X-Content-SHA256": str(release["manifest_sha256"]),
        },
    )
