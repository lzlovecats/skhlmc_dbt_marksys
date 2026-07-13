"""Direct-HTML data API for the AI training workspace."""
import asyncio
import base64
import hashlib
import json
import logging
import re
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from api.pagination import PAGE_SIZE, bounds, json_safe, payload, scalar_count
from api.resource_limits import EXPORT_MAX_BYTES, EXPORT_MAX_ROWS, jsonl_response, require_row_limit
from core.ai_provider import post_json_bounded
from core.schema_features import DISABLED, PARTIAL, READY, feature_bundle_state, table_bundle_state

from schema import (
    TABLE_AI_DATASET_SNAPSHOTS, TABLE_AI_DATASET_SNAPSHOT_ITEMS,
    TABLE_AI_EVAL_CASES, TABLE_AI_EVAL_RUNS, TABLE_AI_MODEL_VERSIONS,
    TABLE_AI_TRAINING_AUDIT, TABLE_RAG_CHUNKS, TABLE_RAG_DOCUMENTS,
    TABLE_LLM_TRAINING_SUBMISSIONS, TABLE_R2_UPLOAD_INTENTS, TABLE_TTS_LEXICON, TABLE_TTS_SCRIPTS,
    TABLE_TTS_VOICE_CONSENTS, TABLE_TTS_VOICE_RECORDINGS,
)
from prompts import (
    TTS_COVERAGE_SYSTEM_PROMPT, TTS_REGENERATE_SYSTEM_PROMPT,
    build_tts_coverage_prompt, build_tts_regenerate_prompt,
)
from system_limits import (
    AI_EVAL_CASE_LIMIT, AI_MODEL_VERSION_LIMIT, AI_SUGGESTION_BATCH_MAX,
    AI_TRAINING_ADMIN_PAGE_SIZE, AI_TRAINING_AUDIT_RETENTION_DAYS,
    AI_TRAINING_INVENTORY_LIMIT, AI_TRAINING_JSON_MAX_BYTES,
    AI_TRAINING_PROMPT_MAX_CHARS, AI_TRAINING_PROVIDER_TIMEOUT_SECONDS,
    AI_TRAINING_READINESS_GROUP_LIMIT,
    DATASET_SNAPSHOT_MAX_COUNT,
    DATASET_SNAPSHOT_MAX_ITEMS, LLM_CONTENT_MAX_CHARS,
    LLM_REVIEW_CONCURRENCY, LLM_SUBMISSION_MAX_TOTAL,
    LLM_SUBMISSION_RATE_WINDOW_HOURS, LLM_SUBMISSIONS_PER_USER_DAY,
    MAINTENANCE_PRUNE_INTERVAL_SECONDS, MAX_AUDIO_BYTES, MEDIA_PROBE_TIMEOUT_SECONDS,
    RAG_DOCUMENT_MAX_TOTAL, RAG_EMBED_CONCURRENCY,
    RAG_REINDEX_MAX_CHUNKS, RAG_REINDEX_MAX_DOCUMENTS,
    R2_BULK_LINK_TTL_SECONDS, R2_MEDIA_LINK_TTL_SECONDS,
    R2_OBJECT_CACHE_MAX_AGE_SECONDS, R2_UPLOAD_CLAIM_TTL_SECONDS,
    RECORDING_MANIFEST_MAX_ROWS, TTS_AI_ANALYSIS_SCRIPT_LIMIT,
    TTS_MAX_DURATION_SECONDS, TTS_REVIEW_CONCURRENCY,
    TTS_REVIEW_CLAIM_TTL_SECONDS,
    TTS_UPLOAD_INTENTS_GLOBAL_MONTH, TTS_UPLOAD_INTENTS_PER_USER_DAY,
)

router = APIRouter(prefix="/api/ai-training", tags=["ai-training"])
CONSENT_VERSION = "tts_voice_v3_2026_07"
CONSENT_TEXT = "我同意聖呂中辯收集本人錄音，用作內部廣東話 TTS、讀音檢查及建立可生成近似本人聲音的語音模型；資料可交由受控雲端 GPU／AI 服務處理，但不會公開原始錄音或 checkpoint。我可撤回未來使用；撤回後錄音不再納入新資料集，使用過該錄音的 checkpoint會被停止部署並安排排除資料重訓。未成年錄音者須另有家長／學校授權。"
ALLOWED_KEY, REVIEWERS_KEY = "tts_recording_allowed_users", "tts_recording_reviewers"
MANUSCRIPT_SEGMENT_MAX_LEN = 35
ADMIN_RECORDING_PAGE_SIZE = AI_TRAINING_ADMIN_PAGE_SIZE
SUPPORTED_AUDIO_MIMES = {"audio/webm", "audio/mp4", "audio/mpeg", "audio/wav", "audio/ogg"}
MAX_AUDIO_MB = max(1, MAX_AUDIO_BYTES // (1024 * 1024))
TTS_REVIEW_SEMAPHORE = asyncio.Semaphore(TTS_REVIEW_CONCURRENCY)
LLM_REVIEW_SEMAPHORE = asyncio.Semaphore(LLM_REVIEW_CONCURRENCY)
RAG_EMBED_SEMAPHORE = asyncio.Semaphore(RAG_EMBED_CONCURRENCY)
RAG_EMBEDDING_MODEL = "gemini-embedding-2"
RAG_EMBEDDING_VERSION = "gemini-embedding-2@2026-04"
_AI_TRAINING_AUDIT_LAST_PRUNE = None
_AI_TRAINING_AUDIT_LOCK = threading.Lock()
logger = logging.getLogger(__name__)

_OPTIONAL_SCHEMA_BUNDLES = {
    "dataset_model": (
        TABLE_AI_DATASET_SNAPSHOTS,
        TABLE_AI_DATASET_SNAPSHOT_ITEMS,
        TABLE_AI_MODEL_VERSIONS,
    ),
    "eval": (TABLE_AI_EVAL_CASES, TABLE_AI_EVAL_RUNS),
    "rag": (TABLE_RAG_DOCUMENTS, TABLE_RAG_CHUNKS),
}


class ConsentBody(BaseModel):
    agreed: bool
    voice_cloning_confirmed: bool
    cloud_processing_confirmed: bool
    is_minor: bool
    guardian_confirmed: bool


class RecordingBody(BaseModel):
    script_id: str = Field(max_length=100)
    mime_type: str = Field(default="audio/webm", max_length=80)
    duration_seconds: int = Field(default=0, ge=0, le=TTS_MAX_DURATION_SECONDS + 1)
    manual_review: bool = False
    r2_upload_token: str = Field(default="", max_length=10_000)
    review_token: str = Field(default="", max_length=30_000)


class RecordingUploadIntentBody(BaseModel):
    script_id: str = Field(max_length=100)
    mime_type: str = Field(default="audio/webm", max_length=80)
    byte_size: int = Field(gt=0, le=MAX_AUDIO_BYTES)
    sha256: str = Field(min_length=64, max_length=64)


class LlmBody(BaseModel):
    data_type: str = Field(max_length=80)
    side: str = Field(default="不適用", max_length=40)
    title: str = Field(default="", max_length=200)
    topic_text: str = Field(default="", max_length=500)
    content_text: str = Field(min_length=1, max_length=LLM_CONTENT_MAX_CHARS)
    source_note: str = Field(default="", max_length=1000)
    anonymized: bool = False
    permission_confirmed: bool = False
    manual_review: bool = False


class LexiconBody(BaseModel):
    lexicon_id: str = Field(default="", max_length=100)
    term: str = Field(max_length=100)
    reading: str = Field(max_length=200)
    jyutping: str = Field(default="", max_length=200)
    example: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=1000)
    category: str = Field(default="", max_length=80)


class ScriptBody(BaseModel):
    script_id: str = Field(default="", max_length=100)
    category: str = Field(max_length=80)
    text: str = Field(max_length=500)
    sort_order: int = 0


class ReviewBody(BaseModel):
    status: str = Field(max_length=30)
    note: str = Field(default="", max_length=2000)


class ActiveBody(BaseModel):
    active: bool


class SuggestionsBody(BaseModel):
    items: list[dict] = Field(max_length=AI_SUGGESTION_BATCH_MAX)
    deactivate_ids: list[str] = Field(default_factory=list, max_length=AI_SUGGESTION_BATCH_MAX)


class ManuscriptBody(BaseModel):
    title: str = Field(max_length=200)
    text: str = Field(max_length=20_000)
    category: str = Field(default="完整稿", max_length=80)
    active: bool = True


class SnapshotBody(BaseModel):
    dataset_kind: str = Field(max_length=20)
    speaker_user_id: str = Field(default="", max_length=80)


class ModelMetricsBody(BaseModel):
    metrics: dict = Field(default_factory=dict)
    status: str | None = Field(default=None, max_length=30)


class ModelRegisterBody(BaseModel):
    model_id: str = Field(max_length=200)
    model_type: str = Field(max_length=30)
    base_model: str = Field(max_length=200)
    dataset_snapshot_id: str | None = Field(default=None, max_length=200)
    artifact_uri: str = Field(default="", max_length=1000)
    config: dict = Field(default_factory=dict)


class RagReindexBody(BaseModel):
    embedding_model: str = Field(default=RAG_EMBEDDING_MODEL, max_length=80)
    embedding_version: str = Field(default=RAG_EMBEDDING_VERSION, max_length=120)


def _admin(request):
    user, db = _ctx(request)
    if not _is_admin(db, user):
        raise HTTPException(403, "只有管理員可執行此操作")
    return user, db


def _segments(text_value, max_len=MANUSCRIPT_SEGMENT_MAX_LEN):
    """Split a manuscript at sentence boundaries without losing any text."""
    text_value = str(text_value or "").strip()
    if not text_value:
        raise HTTPException(400, "請輸入完整稿內容")
    pieces = [x.strip() for x in re.split(r"(?<=[，,、；;：:。！？!?…])\s*|\n+", text_value) if x.strip()]
    out, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) > max_len:
            out.append(current); current = piece
        elif len(piece) > max_len:
            if current: out.append(current); current = ""
            out.extend(piece[i:i + max_len] for i in range(0, len(piece), max_len))
        else:
            current += piece
    if current: out.append(current)
    return out


def _ctx(request):
    from deploy.proxy import _require_committee_user, get_vote_db
    return _require_committee_user(request), get_vote_db()


def _feature_schema_state(db, feature: str) -> bool:
    """Require both the exact migration marker and a complete table bundle."""
    try:
        state = feature_bundle_state(db, feature, _OPTIONAL_SCHEMA_BUNDLES[feature])
    except Exception as exc:
        raise HTTPException(503, f"{feature} schema狀態暫時無法驗證") from exc
    if state == PARTIAL:
        raise HTTPException(503, f"{feature} schema只建立咗一部分，請先完成正式migration")
    if state == DISABLED:
        return False
    return state == READY


def _require_feature_schema(db, feature: str) -> None:
    if not _feature_schema_state(db, feature):
        raise HTTPException(503, f"{feature}功能尚未由正式migration啟用")


def _run_optional_cleanup(db, required_tables, sql, params, label: str) -> bool:
    """Best-effort cleanup of legacy/future derived data after base withdrawal."""
    try:
        if any(table_bundle_state(db, (table,)) != READY for table in required_tables):
            return False
        with db.transaction() as conn:
            conn.execute(text(sql), params)
        return True
    except Exception:
        # Base consent/submission withdrawal is already durable. A future
        # feature migration must add an outbox before derived data is enabled;
        # until then, never let optional schema drift block the privacy action.
        logger.exception("Optional AI training cleanup failed: %s", label)
        return False


def _users(db, key):
    from core.config_store import get_config

    values = get_config(db, key, [])
    return [str(value).strip() for value in values if str(value).strip()]


def _is_admin(db, user): return user in _users(db, REVIEWERS_KEY)


def _consent_lock_key(user) -> str:
    # All consent versions share one privacy lock so an old/new release cannot
    # re-grant while a withdrawal or recording finalization is in flight.
    return f"tts_consent:{user}"


def _has_active_voice_consent(db, user) -> bool:
    consent = db.query(
        f"SELECT 1 FROM {TABLE_TTS_VOICE_CONSENTS} "
        "WHERE user_id=:user AND consent_version=:version "
        "AND consent_text=:consent_text "
        "AND withdrawn_at IS NULL "
        "AND voice_cloning_confirmed=TRUE "
        "AND cloud_processing_confirmed=TRUE "
        "AND (is_minor=FALSE OR guardian_confirmed=TRUE) LIMIT 1",
        {"user": user, "version": CONSENT_VERSION, "consent_text": CONSENT_TEXT},
    )
    return not consent.empty


def _rows(frame):
    return [dict(row) for row in frame.to_dict(orient="records")]


def _probe_audio(audio: bytes, mime: str, claimed_duration: int) -> dict:
    suffix = "." + _audio_ext(mime)
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
            handle.write(audio); handle.flush()
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=format_name,duration:stream=codec_type,sample_rate,channels",
                 "-of", "json", handle.name],
                capture_output=True, text=True, timeout=MEDIA_PROBE_TIMEOUT_SECONDS, check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(503, "伺服器未能執行音訊格式驗證") from exc
    if result.returncode != 0:
        raise HTTPException(400, "錄音檔案損壞或實際格式不受支援")
    try:
        info = json.loads(result.stdout or "{}")
        fmt = info.get("format") or {}
        stream = next(x for x in (info.get("streams") or []) if x.get("codec_type") == "audio")
        duration = float(fmt.get("duration") or 0)
        sample_rate = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
        format_name = str(fmt.get("format_name") or "")
    except (ValueError, TypeError, StopIteration) as exc:
        raise HTTPException(400, "錄音未包含可讀取的聲音軌") from exc
    if not 1 <= duration <= TTS_MAX_DURATION_SECONDS + 0.5:
        raise HTTPException(400, f"錄音實際長度必須為 1 至 {TTS_MAX_DURATION_SECONDS} 秒")
    tolerance = max(2.0, duration * 0.2)
    if abs(duration - int(claimed_duration or 0)) > tolerance:
        raise HTTPException(400, "錄音實際長度與瀏覽器回報不符，請重新錄製")
    expected = {
        "audio/webm": ("webm", "matroska"), "audio/mp4": ("mov", "mp4"),
        "audio/mpeg": ("mp3",), "audio/wav": ("wav",), "audio/ogg": ("ogg",),
    }[mime]
    if not any(name in format_name for name in expected):
        raise HTTPException(400, "錄音宣稱格式與實際檔案格式不符")
    return {"duration": round(duration, 3), "sample_rate": sample_rate,
            "channels": channels, "format": format_name,
            "sha256": hashlib.sha256(audio).hexdigest()}


def _verified_r2_audio_claim(body, user):
    """Verify one short-lived upload claim and its R2 object without DB binary fallback."""
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret
    review_secret = _get_relay_cookie_secret()
    if not review_secret:
        raise HTTPException(503, "錄音驗證服務暫時不可用")

    if not r2_storage.configured():
        raise HTTPException(503, "Cloudflare R2 尚未完成設定，錄音功能已暫停")
    if not 1 <= int(body.duration_seconds or 0) <= TTS_MAX_DURATION_SECONDS:
        raise HTTPException(400, f"錄音長度必須為 1 至 {TTS_MAX_DURATION_SECONDS} 秒")
    claim = r2_storage.verify_upload_claim(
        body.r2_upload_token or "", _get_relay_cookie_secret() or ""
    )
    if (
        not claim or claim.get("kind") != "tts" or claim.get("user") != str(user)
        or claim.get("script_id") != body.script_id
    ):
        raise HTTPException(400, "錄音上載憑證無效或已過期")
    try:
        remote = r2_storage.head(claim.get("pending_r2_key") or claim["r2_key"])
    except Exception as exc:
        raise HTTPException(400, "R2 未能確認錄音已完成上載") from exc
    remote_sha = str((remote.get("Metadata") or {}).get("sha256") or "")
    remote_mime = str(remote.get("ContentType") or "").split(";", 1)[0].lower()
    if (
        not 1_000 <= int(remote.get("ContentLength") or 0) <= MAX_AUDIO_BYTES
        or int(remote.get("ContentLength") or 0) != int(claim["byte_size"])
        or remote_sha != claim["sha256"] or remote_mime != claim["mime_type"]
    ):
        try:
            r2_storage.delete(claim.get("pending_r2_key") or claim["r2_key"])
        except Exception:
            pass
        raise HTTPException(400, "R2 錄音格式、大小或雜湊驗證失敗")
    return claim


def _audit(db, actor, action, target_type, target_id="", details=None, *, conn=None):
    sql = (
        f"INSERT INTO {TABLE_AI_TRAINING_AUDIT}"
        "(actor_user_id,action,target_type,target_id,details_json) "
        "VALUES(:actor,:action,:target_type,:target_id,CAST(:details AS jsonb))"
    )
    params = {
        "actor": str(actor or "")[:200],
        "action": str(action)[:100],
        "target_type": str(target_type)[:100],
        "target_id": str(target_id or "")[:300],
        "details": _bounded_json_param(details or {}, "audit details"),
    }
    if conn is None:
        db.execute(sql, params)
        _prune_audit(db)
    else:
        conn.execute(text(sql), params)


def _prune_audit(db):
    """Bound operational audit rows while retaining consent evidence."""
    global _AI_TRAINING_AUDIT_LAST_PRUNE
    now = time.monotonic()
    if (
        _AI_TRAINING_AUDIT_LAST_PRUNE is not None
        and now - _AI_TRAINING_AUDIT_LAST_PRUNE < MAINTENANCE_PRUNE_INTERVAL_SECONDS
    ):
        return
    with _AI_TRAINING_AUDIT_LOCK:
        if (
            _AI_TRAINING_AUDIT_LAST_PRUNE is not None
            and now - _AI_TRAINING_AUDIT_LAST_PRUNE < MAINTENANCE_PRUNE_INTERVAL_SECONDS
        ):
            return
        try:
            db.execute(
                f"""DELETE FROM {TABLE_AI_TRAINING_AUDIT}
                    WHERE created_at < NOW() - make_interval(days => :days)
                      AND action NOT IN (
                        'consent_granted','consent_withdrawn','submission_withdrawn'
                      )""",
                {"days": AI_TRAINING_AUDIT_RETENTION_DAYS},
            )
        except Exception:
            logger.exception("AI training audit retention maintenance failed")
            _AI_TRAINING_AUDIT_LAST_PRUNE = now
            return
        _AI_TRAINING_AUDIT_LAST_PRUNE = now


def _json_param(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _bounded_json_param(value, label="JSON") -> str:
    encoded = _json_param(value)
    if len(encoded.encode("utf-8")) > AI_TRAINING_JSON_MAX_BYTES:
        raise HTTPException(413, f"{label}超過 {AI_TRAINING_JSON_MAX_BYTES // 1024}KB 儲存上限")
    return encoded


def _split_name(group_key: str) -> str:
    bucket = int(hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:8], 16) % 10
    return "test" if bucket == 0 else "validation" if bucket == 1 else "train"


def _rag_chunks(value: str, size: int = 700, overlap: int = 100) -> list[str]:
    value = re.sub(r"\r\n?", "\n", str(value or "")).strip()
    if not value:
        return []
    chunks, start = [], 0
    while start < len(value):
        end = min(len(value), start + size)
        if end < len(value):
            boundary = max(value.rfind(mark, start + size // 2, end) for mark in "。！？\n；")
            if boundary >= start + size // 2:
                end = boundary + 1
        chunks.append(value[start:end].strip())
        if end >= len(value):
            break
        start = max(start + 1, end - overlap)
    return [chunk for chunk in chunks if chunk]


def _require_rag_vector_schema(db):
    """Fail closed until pgvector is provisioned by a versioned migration."""
    from core.rag import rag_schema_ready

    if not rag_schema_ready(db, force=True):
        raise HTTPException(503, "RAG向量schema尚未由正式migration啟用")


def _audio_ext(mime):
    return {"audio/webm": "webm", "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/ogg": "ogg"}.get(mime, "webm")


@lru_cache(maxsize=1)
def _load_ai_roadmap() -> str:
    """Return only the TTS/LLM sections from the repo's single roadmap."""
    roadmap_path = Path(__file__).resolve().parents[1] / "docs" / "ROADMAP.md"
    try:
        roadmap = roadmap_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "統一研發路線圖暫時未能讀取。"
    start = roadmap.find("## P3.")
    end = roadmap.find("\n## P6.", start + 1) if start >= 0 else -1
    if start >= 0:
        return roadmap[start:end if end >= 0 else None].strip()
    return roadmap


def _gemini_usage(response_data, model_label="Gemini 2.5 Flash"):
    meta = response_data.get("usageMetadata") or {}
    prompt_tokens = int(meta.get("promptTokenCount") or 0)
    output_tokens = int(meta.get("candidatesTokenCount") or 0)
    audio_tokens = sum(
        int(item.get("tokenCount") or 0)
        for item in (meta.get("promptTokensDetails") or [])
        if "AUDIO" in str(item.get("modality") or "").upper()
    )
    text_tokens = max(0, prompt_tokens - audio_tokens)
    usd = (text_tokens * 0.30 + audio_tokens * 1.00 + output_tokens * 2.50) / 1_000_000
    return {
        "model_label": model_label, "provider": "gemini", "input_tokens": text_tokens,
        "output_tokens": output_tokens, "audio_tokens": audio_tokens, "search_calls": 0,
        "estimated_cost_usd": round(usd, 6), "estimated_cost_hkd": round(usd * 7.8, 4),
        "cost_source": "actual_tokens",
    }


def _log_ai(user, db, feature, success, response_data=None, error=""):
    try:
        from core.funds_logic import log_ai_usage
        usage = _gemini_usage(response_data or {}) if success else None
        log_ai_usage(user, feature, success, usage=usage, error_message=error, db=db)
    except Exception:
        pass


@router.get("/data")
def data(request: Request):
    user, db = _ctx(request)
    allowed, admin = user in _users(db, ALLOWED_KEY), _is_admin(db, user)
    consented = _has_active_voice_consent(db, user)
    scripts = _rows(db.query(f"SELECT script_id AS id,category,text,is_active,sort_order,COALESCE(script_type,'short') AS script_type,manuscript_id,manuscript_title FROM {TABLE_TTS_SCRIPTS} WHERE is_active=TRUE ORDER BY category,sort_order,script_id LIMIT :inventory_limit", {"inventory_limit": AI_TRAINING_INVENTORY_LIMIT}))
    lexicon = []
    # Recorder selection only needs one status per script; full history is paged below.
    mine = _rows(db.query(f"SELECT DISTINCT ON (script_id) id,script_id,status,created_at FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE speaker_user_id=:user ORDER BY script_id,created_at DESC LIMIT :inventory_limit", {"user":user,"inventory_limit":AI_TRAINING_INVENTORY_LIMIT}))
    llm = []
    rd_plan = _load_ai_roadmap()
    from core import r2_storage
    from deploy.proxy import bandwidth_budget_status
    result = {"user_id": user, "is_allowed": allowed, "is_admin": admin, "consented": consented, "consent_text": CONSENT_TEXT, "rd_plan":rd_plan, "scripts": scripts, "lexicon":lexicon, "my_recordings":mine, "my_llm":llm, "recording_storage":"r2", "recording_storage_ready":r2_storage.configured(), "bandwidth_budget":bandwidth_budget_status(notify=True), "storage_budget":r2_storage.storage_budget_status(db, refresh=True) if r2_storage.configured() else None}
    result["limits"] = {
        "max_audio_bytes": MAX_AUDIO_BYTES,
        "max_duration_seconds": TTS_MAX_DURATION_SECONDS,
        "upload_intents_per_user_day": TTS_UPLOAD_INTENTS_PER_USER_DAY,
        "upload_intents_global_month": TTS_UPLOAD_INTENTS_GLOBAL_MONTH,
        "r2_object_cache_max_age_seconds": R2_OBJECT_CACHE_MAX_AGE_SECONDS,
    }
    if admin:
        result["recordings"] = []; result["submissions"] = []
    return result


@router.get("/collection/{kind}")
def collection(kind: str, request: Request, page: int = 1):
    user, db = _ctx(request); admin = _is_admin(db, user); page, _, offset = bounds(page)
    specs = {
        "my-recordings": (TABLE_TTS_VOICE_RECORDINGS, "speaker_user_id=:user", {"user": user}, "id,script_id,prompt_text,status,ai_review_status,ai_transcript,created_at,review_note"),
        "my-llm": (TABLE_LLM_TRAINING_SUBMISSIONS, "submitted_by=:user", {"user": user}, "id,data_type,side,title,topic_text,content_text,source_note,status,ai_review_status,review_note,created_at"),
        "recordings": (TABLE_TTS_VOICE_RECORDINGS, "1=1", {}, "id,speaker_user_id,script_id,prompt_text,mime_type,status,ai_review_status,ai_transcript,review_note,created_at"),
        "submissions": (TABLE_LLM_TRAINING_SUBMISSIONS, "1=1", {}, "id,submitted_by,data_type,side,title,topic_text,content_text,source_note,status,ai_review_status,ai_review_json,review_note,created_at"),
        "lexicon": (TABLE_TTS_LEXICON, "1=1", {}, "lexicon_id AS id,term,reading,jyutping,example,note,category,is_active"),
    }
    if kind not in specs: raise HTTPException(404, "資料集不存在")
    if kind in {"recordings", "submissions"} and not admin: raise HTTPException(403, "只有管理員可查看審核資料")
    table, where, params, cols = specs[kind]; params = dict(params)
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {table} WHERE {where}", params)
    params.update(limit=PAGE_SIZE, offset=offset)
    order = "category,term" if kind == "lexicon" else "created_at DESC"
    rows = _rows(db.query(f"SELECT {cols} FROM {table} WHERE {where} ORDER BY {order} LIMIT :limit OFFSET :offset", params))
    return payload(rows, page, total)


@router.get("/admin/recordings")
def admin_recordings(request: Request, page: int = 1, status: str = "all", speaker: str = ""):
    _user, db = _admin(request)
    page = max(1, int(page or 1)); offset = (page - 1) * ADMIN_RECORDING_PAGE_SIZE
    clauses, params = ["1=1"], {}
    if status != "all": clauses.append("status=:status"); params["status"] = status
    if speaker.strip(): clauses.append("speaker_user_id=:speaker"); params["speaker"] = speaker.strip()
    where = " AND ".join(clauses); total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE {where}", params)
    params.update(limit=ADMIN_RECORDING_PAGE_SIZE, offset=offset)
    rows = _rows(db.query(f"SELECT id,speaker_user_id,script_id,prompt_text,mime_type,size_bytes,duration_seconds,status,ai_review_status,ai_review_json,ai_transcript,review_note,created_at FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE {where} ORDER BY created_at DESC LIMIT :limit OFFSET :offset", params))
    return {"items": json_safe(rows), "page": page, "page_size": ADMIN_RECORDING_PAGE_SIZE,
            "total": total, "total_pages": max(1, (total + ADMIN_RECORDING_PAGE_SIZE - 1) // ADMIN_RECORDING_PAGE_SIZE)}


@router.get("/admin/stats")
def admin_stats(request: Request):
    _user, db = _admin(request)
    recordings = _rows(db.query(f"SELECT status,COUNT(*) AS count FROM {TABLE_TTS_VOICE_RECORDINGS} GROUP BY status"))
    llm_rows = _rows(db.query(f"SELECT status,COUNT(*) AS count FROM {TABLE_LLM_TRAINING_SUBMISSIONS} GROUP BY status"))
    return {"recordings": recordings, "llm": llm_rows, "allowed_users": _users(db, ALLOWED_KEY)}


@router.get("/recordings/{record_id}/audio")
def recording_audio(record_id: int, request: Request):
    """Stream a recording to its submitter or an authenticated reviewer."""
    user, db = _ctx(request)
    row = db.query(
        f"SELECT speaker_user_id,r2_key,mime_type,file_ext FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE id=:id",
        {"id": record_id},
    )
    if row.empty:
        raise HTTPException(404, "找不到錄音")
    owner = str(row.iloc[0]["speaker_user_id"] or "").strip()
    if owner != str(user).strip() and not _is_admin(db, user):
        raise HTTPException(403, "你無權播放此錄音")
    from core import r2_storage
    r2_key = str(row.iloc[0].get("r2_key") or "").strip()
    if not r2_storage.configured():
        raise HTTPException(503, "Cloudflare R2 暫時不可用")
    if not r2_key:
        raise HTTPException(409, "錄音尚未完成R2遷移")
    return RedirectResponse(
        r2_storage.presign_get(
            r2_key,
            mime_type=row.iloc[0]["mime_type"] or "audio/webm",
            file_name=f"recording-{record_id}.{row.iloc[0].get('file_ext') or 'webm'}",
            expires=R2_MEDIA_LINK_TTL_SECONDS,
        ),
        status_code=307,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/recordings/upload-intent")
def recording_upload_intent(body: RecordingUploadIntentBody, request: Request):
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret

    user, db = _ctx(request)
    if user not in _users(db, ALLOWED_KEY):
        raise HTTPException(403, "你未獲加入 TTS 錄音收集名單")
    if not r2_storage.configured():
        raise HTTPException(503, "Cloudflare R2 尚未完成設定。")
    storage_budget = r2_storage.storage_budget_status(db, refresh=True)
    if storage_budget["blocked"]:
        stop_gb = storage_budget["stop_bytes"] / 1_000_000_000
        raise HTTPException(429, f"R2儲存量已達{stop_gb:g}GB保護上限，暫停新錄音上載。")
    if not _has_active_voice_consent(db, user):
        raise HTTPException(400, "請先確認錄音同意")
    mime = (body.mime_type or "").split(";", 1)[0].lower()
    if mime not in SUPPORTED_AUDIO_MIMES:
        raise HTTPException(400, "錄音格式不受支援")
    if not 1_000 <= body.byte_size <= MAX_AUDIO_BYTES:
        raise HTTPException(400, f"錄音大小必須介乎 1KB 至 {MAX_AUDIO_MB}MB")
    if not re.fullmatch(r"[0-9a-f]{64}", body.sha256.lower()):
        raise HTTPException(400, "錄音雜湊格式不正確")
    script = db.query(
        f"SELECT 1 FROM {TABLE_TTS_SCRIPTS} WHERE script_id=:id AND is_active=TRUE",
        {"id": body.script_id},
    )
    if script.empty:
        raise HTTPException(404, "錄音句子不存在或已停用")
    duplicate = db.query(
        f"SELECT 1 FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE speaker_user_id=:user AND script_id=:script "
        "AND status IN ('pending','accepted') LIMIT 1",
        {"user": user, "script": body.script_id},
    )
    if not duplicate.empty:
        raise HTTPException(409, "此句已有待審核或已接受錄音，請勿重複提交")
    safe_user = re.sub(r"[^A-Za-z0-9_-]", "_", str(user))[:48] or "member"
    ext = _audio_ext(mime)
    intent_id = uuid.uuid4().hex
    key = f"audio/tts/{safe_user}/{intent_id}.{ext}"
    pending_key = f"pending/{key}"
    secret = _get_relay_cookie_secret()
    if not secret:
        raise HTTPException(503, "系統簽署設定不可用。")
    token = r2_storage.sign_upload_claim({
        "kind": "tts", "intent_id": intent_id, "user": str(user), "script_id": body.script_id,
        "mime_type": mime, "byte_size": body.byte_size,
        "sha256": body.sha256.lower(), "r2_key": key, "pending_r2_key": pending_key,
    }, secret, expires=R2_UPLOAD_CLAIM_TTL_SECONDS)
    reserved, scope = r2_storage.reserve_upload_intent(
        db, intent_id=intent_id, user_id=str(user), media_kind="tts",
        object_keys=[pending_key, key], declared_bytes=body.byte_size,
        user_daily_limit=TTS_UPLOAD_INTENTS_PER_USER_DAY,
        global_monthly_limit=TTS_UPLOAD_INTENTS_GLOBAL_MONTH,
    )
    if not reserved:
        stop_gb = storage_budget["stop_bytes"] / 1_000_000_000
        message = f"R2儲存量已達{stop_gb:g}GB保護上限，暫停新錄音上載。" if scope == "storage_global" else (
            "你今日申請的錄音上載次數已達上限，請翌日再試。"
            if scope == "user_daily" else "本月全系統錄音上載申請已達上限。"
        )
        raise HTTPException(429, message)
    return {
        "upload_token": token,
        "url": r2_storage.presign_put(pending_key, mime, body.sha256, body.byte_size),
        "headers": {
            "Content-Type": mime,
            "Cache-Control": f"private, max-age={R2_OBJECT_CACHE_MAX_AGE_SECONDS}",
            "x-amz-meta-sha256": body.sha256.lower(),
        },
    }


@router.post("/consent")
def consent(body: ConsentBody, request: Request):
    user, db = _ctx(request)
    if not body.agreed: raise HTTPException(400, "必須同意錄音用途及授權安排")
    if not body.voice_cloning_confirmed or not body.cloud_processing_confirmed:
        raise HTTPException(400, "必須確認聲線模型及受控雲端處理用途")
    if body.is_minor and not body.guardian_confirmed:
        raise HTTPException(400, "未成年錄音者必須確認已取得家長／學校授權")
    changed = False
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {
            "key": _consent_lock_key(user),
        })
        existing = conn.execute(text(f"""SELECT consent_text
            FROM {TABLE_TTS_VOICE_CONSENTS}
            WHERE user_id=:user AND consent_version=:version FOR UPDATE"""),
            {"user": user, "version": CONSENT_VERSION}).mappings().first()
        if existing is not None and str(existing["consent_text"] or "") != CONSENT_TEXT:
            raise HTTPException(
                500,
                "同意書內容已改動但CONSENT_VERSION未更新，為保障授權證據已拒絕覆寫。",
            )
        result = conn.execute(text(f"""INSERT INTO {TABLE_TTS_VOICE_CONSENTS}
                       (user_id,consent_version,consent_text,consented_at,withdrawn_at,
                        voice_cloning_confirmed,cloud_processing_confirmed,is_minor,guardian_confirmed)
                       VALUES(:user,:version,:consent,:now,NULL,TRUE,TRUE,:minor,:guardian)
                       ON CONFLICT(user_id,consent_version) DO UPDATE SET
                        consent_text=EXCLUDED.consent_text,consented_at=EXCLUDED.consented_at,
                        withdrawn_at=NULL,voice_cloning_confirmed=TRUE,cloud_processing_confirmed=TRUE,
                        is_minor=EXCLUDED.is_minor,guardian_confirmed=EXCLUDED.guardian_confirmed
                       WHERE {TABLE_TTS_VOICE_CONSENTS}.withdrawn_at IS NOT NULL
                          OR {TABLE_TTS_VOICE_CONSENTS}.voice_cloning_confirmed IS DISTINCT FROM TRUE
                          OR {TABLE_TTS_VOICE_CONSENTS}.cloud_processing_confirmed IS DISTINCT FROM TRUE
                          OR {TABLE_TTS_VOICE_CONSENTS}.is_minor IS DISTINCT FROM EXCLUDED.is_minor
                          OR {TABLE_TTS_VOICE_CONSENTS}.guardian_confirmed IS DISTINCT FROM EXCLUDED.guardian_confirmed
                       RETURNING 1"""),
                   {"user":user,"version":CONSENT_VERSION,"consent":CONSENT_TEXT,"now":datetime.now(),
                    "minor":body.is_minor,"guardian":body.guardian_confirmed})
        stored = conn.execute(text(f"""SELECT consent_text
            FROM {TABLE_TTS_VOICE_CONSENTS}
            WHERE user_id=:user AND consent_version=:version"""),
            {"user": user, "version": CONSENT_VERSION}).mappings().first()
        if stored is None or str(stored["consent_text"] or "") != CONSENT_TEXT:
            raise HTTPException(
                500,
                "同意書版本核對失敗，為保障授權證據已取消本次操作。",
            )
        changed = result.fetchone() is not None
        if changed:
            _audit(db, user, "consent_granted", "tts_consent", CONSENT_VERSION,
                   {
                       "is_minor": body.is_minor,
                       "guardian_confirmed": body.guardian_confirmed,
                       "consent_text_sha256": hashlib.sha256(
                           CONSENT_TEXT.encode("utf-8")
                       ).hexdigest(),
                   },
                   conn=conn)
    if changed:
        _prune_audit(db)
    return {"ok": True, "changed": changed}


@router.delete("/consent")
def withdraw(request: Request):
    user, db = _ctx(request)
    now = datetime.now()
    changed = False
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {
            "key": _consent_lock_key(user),
        })
        consent_result = conn.execute(text(f"""UPDATE {TABLE_TTS_VOICE_CONSENTS}
            SET withdrawn_at=:now
            WHERE user_id=:user AND withdrawn_at IS NULL
            RETURNING 1"""), {"user":user,"now":now})
        recording_result = conn.execute(text(f"""UPDATE {TABLE_TTS_VOICE_RECORDINGS} SET status='withdrawn'
            WHERE speaker_user_id=:user AND status!='withdrawn'"""), {"user":user})
        consent_rows = max(0, int(consent_result.rowcount or 0))
        recording_rows = max(0, int(recording_result.rowcount or 0))
        changed = consent_rows > 0 or recording_rows > 0
        if changed:
            _audit(db, user, "consent_withdrawn", "tts_consent", CONSENT_VERSION,
                   {
                       "all_consent_versions": True,
                       "consent_rows": consent_rows,
                       "recording_rows": recording_rows,
                   }, conn=conn)
    if changed:
        _prune_audit(db)
    _run_optional_cleanup(
        db, (TABLE_AI_DATASET_SNAPSHOTS,),
        f"""UPDATE {TABLE_AI_DATASET_SNAPSHOTS} SET status='withdrawn'
            WHERE dataset_kind='tts' AND speaker_user_id=:user
              AND status IN ('draft','ready')""",
        {"user": user}, "withdraw TTS snapshots",
    )
    _run_optional_cleanup(
        db, (TABLE_AI_DATASET_SNAPSHOTS, TABLE_AI_MODEL_VERSIONS),
        f"""UPDATE {TABLE_AI_MODEL_VERSIONS}
            SET status='blocked',updated_at=:now
            WHERE dataset_snapshot_id IN (
                SELECT snapshot_id FROM {TABLE_AI_DATASET_SNAPSHOTS}
                WHERE dataset_kind='tts' AND speaker_user_id=:user
            ) AND status!='retired'""",
        {"user": user, "now": now}, "block withdrawn TTS models",
    )
    return {"ok":True, "changed": changed}


@router.post("/recordings")
def recording(body: RecordingBody, request: Request):
    user, db = _ctx(request)
    if user not in _users(db, ALLOWED_KEY): raise HTTPException(403, "你未獲加入 TTS 錄音收集名單")
    if not _has_active_voice_consent(db, user): raise HTTPException(400, "請先確認錄音同意")
    script = db.query(f"SELECT text FROM {TABLE_TTS_SCRIPTS} WHERE script_id=:id AND is_active=TRUE", {"id":body.script_id})
    if script.empty: raise HTTPException(404, "錄音句子不存在或已停用")
    duplicate = db.query(
        f"SELECT 1 FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE speaker_user_id=:user AND script_id=:script "
        "AND status IN ('pending','accepted') LIMIT 1",
        {"user": user, "script": body.script_id},
    )
    if not duplicate.empty:
        raise HTTPException(409, "此句已有待審核或已接受錄音，請勿重複提交")
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret
    review_secret = _get_relay_cookie_secret()
    if not review_secret:
        raise HTTPException(503, "錄音驗證服務暫時不可用")
    claim = _verified_r2_audio_claim(body, user)
    mime = claim["mime_type"]
    r2_key = claim["r2_key"]
    pending_r2_key = claim.get("pending_r2_key") or r2_key
    # Always derive technical metadata from the verified object. Never trust a
    # browser-supplied probe, duration, transcript or provider verdict.
    audio = r2_storage.download_bytes(pending_r2_key, MAX_AUDIO_BYTES)
    probe = _probe_audio(audio, mime, body.duration_seconds)
    size_bytes = int(claim["byte_size"])
    audio_sha = claim["sha256"]
    trusted_review = {"manual_review": True}
    if not body.manual_review:
        signed_review = r2_storage.verify_upload_claim(
            body.review_token or "", review_secret
        )
        if (
            not signed_review or signed_review.get("kind") != "tts_review"
            or signed_review.get("user") != str(user)
            or signed_review.get("script_id") != body.script_id
            or signed_review.get("r2_key") != r2_key
            or signed_review.get("sha256") != audio_sha
            or signed_review.get("passed") is not True
            or signed_review.get("matches_prompt") is not True
        ):
            raise HTTPException(400, "錄音未通過已簽署的AI音質及稿件一致性檢查")
        trusted_review = signed_review.get("review") if isinstance(signed_review.get("review"), dict) else {}
    review_status = "error" if body.manual_review else "passed"
    if pending_r2_key != r2_key:
        try:
            r2_storage.promote(pending_r2_key, r2_key)
        except Exception as exc:
            raise HTTPException(502, "R2錄音由暫存區轉入正式儲存失敗") from exc
    params = {"user":user,"script":body.script_id,"prompt":script.iloc[0]["text"],
              "r2_key":r2_key or None,"mime":mime,"ext":_audio_ext(mime),"size":size_bytes,"duration":int(body.duration_seconds),
              "sha":audio_sha,"measured":probe.get("duration"),"sample_rate":probe.get("sample_rate"),
              "channels":probe.get("channels"),"detected":probe.get("format"),"review_status":review_status,
              "review_json":_bounded_json_param(trusted_review, "錄音AI審核結果"),
              "transcript":str(trusted_review.get("transcript") or "")[:8000],"now":datetime.now()}
    try:
        with db.transaction() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {
                "key": _consent_lock_key(user),
            })
            active_consent = conn.execute(text(f"""SELECT 1
                FROM {TABLE_TTS_VOICE_CONSENTS}
                WHERE user_id=:user AND consent_version=:version
                  AND consent_text=:consent_text AND withdrawn_at IS NULL
                  AND voice_cloning_confirmed=TRUE
                  AND cloud_processing_confirmed=TRUE
                  AND (is_minor=FALSE OR guardian_confirmed=TRUE)
                FOR SHARE"""), {
                "user": user,
                "version": CONSENT_VERSION,
                "consent_text": CONSENT_TEXT,
            }).fetchone()
            if active_consent is None:
                raise HTTPException(409, "錄音同意已撤回或版本已更新，請勿提交呢段錄音。")
            claimed = conn.execute(text(f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
                SET status='completed',completed_at=:now
                WHERE intent_id=:intent_id AND status='issued'"""), {
                "intent_id": str(claim.get("intent_id") or ""), "now": datetime.now(),
            }).rowcount
            if claimed != 1:
                raise ValueError("R2 upload intent已使用或失效")
            conn.execute(text(f"""INSERT INTO {TABLE_TTS_VOICE_RECORDINGS}
                       (speaker_user_id,script_id,prompt_text,r2_key,mime_type,file_ext,size_bytes,
                        duration_seconds,audio_sha256,measured_duration_seconds,sample_rate_hz,
                        channel_count,detected_format,ai_review_status,ai_review_json,ai_transcript,status,created_at)
                       VALUES(:user,:script,:prompt,:r2_key,:mime,:ext,:size,:duration,:sha,:measured,
                        :sample_rate,:channels,:detected,:review_status,:review_json,:transcript,'pending',:now)"""), params)
    except Exception as exc:
        r2_storage.delete_intent_objects(
            db, str(claim.get("intent_id") or ""), (pending_r2_key, r2_key)
        )
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(502, "錄音metadata未能以交易方式寫入，已清理R2檔案。") from exc
    return {"ok":True, "message":"錄音已提交，等待人工審核。"}


@router.post("/recordings/quality-check")
async def recording_quality_check(body: RecordingBody, request: Request):
    """Run the deterministic gate before the provider-assisted/manual review.

    Manual review may bypass a provider outage, but never these deterministic
    format, duration and byte-size safeguards.
    """
    user, db = _ctx(request)
    from deploy.proxy import _bandwidth_essential_gate_error
    budget_error = _bandwidth_essential_gate_error()
    if budget_error: raise HTTPException(429, budget_error)
    if user not in _users(db, ALLOWED_KEY): raise HTTPException(403, "你未獲加入 TTS 錄音收集名單")
    if not _has_active_voice_consent(db, user): raise HTTPException(400, "請先確認錄音同意")
    script = db.query(f"SELECT text FROM {TABLE_TTS_SCRIPTS} WHERE script_id=:id AND is_active=TRUE", {"id": body.script_id})
    if script.empty: raise HTTPException(404, "錄音句子不存在或已停用")
    duplicate = db.query(
        f"SELECT 1 FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE speaker_user_id=:user AND script_id=:script "
        "AND status IN ('pending','accepted') LIMIT 1",
        {"user": user, "script": body.script_id},
    )
    if not duplicate.empty: raise HTTPException(409, "此句已有待審核或已接受錄音，毋須再作 AI 檢查")
    from core import r2_storage
    claim = _verified_r2_audio_claim(body, user)
    mime = claim["mime_type"]
    from deploy.proxy import _get_proxy_secret
    key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not key:
        _log_ai(user, db, "tts_review", False, error="GEMINI_API_KEY missing")
        raise HTTPException(503, "未設定 GEMINI_API_KEY，暫時未能進行 AI 音質檢查")
    prompt = (
        "以廣東話 TTS 資料審核員身份檢查錄音清晰度、雜音、截斷，以及是否逐字符合指定稿句。"
        f"\n指定稿句：{script.iloc[0]['text']}\n"
        "只回覆 JSON：{\"passed\":true,\"matches_prompt\":true,\"speech_clarity\":\"clear\","
        "\"volume\":\"ok\",\"noise_level\":\"low\",\"clipping\":false,\"transcript\":\"\",\"problems\":[],\"note\":\"\"}"
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    async with TTS_REVIEW_SEMAPHORE:
        try:
            audio = await asyncio.to_thread(
                r2_storage.download_bytes, claim.get("pending_r2_key") or claim["r2_key"],
                MAX_AUDIO_BYTES,
            )
        except Exception as exc:
            raise HTTPException(502, "未能從R2讀取錄音作音質檢查") from exc
        probe = _probe_audio(audio, mime, body.duration_seconds)
        payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode("ascii")}}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0, "maxOutputTokens": 2048}}
        try:
            from deploy.proxy import record_bandwidth_usage
            await asyncio.to_thread(
                record_bandwidth_usage, "tts_quality_provider",
                len(payload["contents"][0]["parts"][1]["inline_data"]["data"].encode("ascii"))
                + len(prompt.encode("utf-8")) + 512,
                str(user), aggregate_key=f"user={str(user)[:120]}",
            )
            async with httpx.AsyncClient(timeout=AI_TRAINING_PROVIDER_TIMEOUT_SECONDS) as client:
                response_data = await post_json_bounded(client, url, json=payload)
            raw = response_data["candidates"][0]["content"]["parts"][0]["text"]
            review = json.loads(raw)
        except Exception as exc:
            _log_ai(user, db, "tts_review", False, error=str(exc))
            raise HTTPException(502, f"AI 音質檢查失敗：{str(exc)[:160]}") from exc
    _log_ai(user, db, "tts_review", True, response_data=response_data)
    passed = (
        bool(review.get("passed")) and bool(review.get("matches_prompt"))
        and review.get("speech_clarity") == "clear" and review.get("volume") == "ok"
        and review.get("noise_level") in ("low", "medium") and not bool(review.get("clipping"))
    )
    review["passed"] = bool(passed)
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret
    review_secret = _get_relay_cookie_secret()
    if not review_secret:
        raise HTTPException(503, "錄音驗證服務暫時不可用")
    review_token = r2_storage.sign_upload_claim({
        "kind": "tts_review", "user": str(user), "script_id": body.script_id,
        "r2_key": claim["r2_key"], "sha256": claim["sha256"],
        "passed": bool(passed), "matches_prompt": bool(review.get("matches_prompt")),
        "review": review,
    }, review_secret, expires=TTS_REVIEW_CLAIM_TTL_SECONDS)
    return {"ok": passed, "status": "passed" if passed else "failed", "problems": review.get("problems") or [],
            "transcript": review.get("transcript") or "", "review": review, "probe": probe,
            "review_token": review_token,
            "message": review.get("note") or ("AI 音質檢查通過。" if passed else "AI 音質檢查未通過。")}


@router.post("/llm")
async def llm(body: LlmBody, request: Request):
    user, db = _ctx(request)
    if not body.content_text.strip(): raise HTTPException(400,"請填寫文字內容")
    if not body.anonymized or not body.permission_confirmed: raise HTTPException(400,"提交前必須確認已匿名化及有權提交")
    normalized = json.dumps({"data_type":body.data_type,"side":body.side,"title":body.title.strip(),"topic":body.topic_text.strip(),"content":body.content_text.strip(),"source":body.source_note.strip()},ensure_ascii=False,sort_keys=True)
    fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
    duplicate = db.query(f"SELECT id FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE submitted_by=:user AND md5(COALESCE(data_type,'')||'|'||COALESCE(side,'')||'|'||COALESCE(title,'')||'|'||COALESCE(topic_text,'')||'|'||COALESCE(content_text,'')||'|'||COALESCE(source_note,''))=md5(:raw) AND status!='withdrawn'", {"user":user,"raw":"|".join([body.data_type,body.side,body.title.strip(),body.topic_text.strip(),body.content_text.strip(),body.source_note.strip()])})
    if not duplicate.empty: raise HTTPException(409,"此資料已提交，請勿重複提交")
    rate_cutoff = datetime.now() - timedelta(hours=LLM_SUBMISSION_RATE_WINDOW_HOURS)
    recent = db.query(f"""SELECT COUNT(*) AS n FROM {TABLE_LLM_TRAINING_SUBMISSIONS}
        WHERE submitted_by=:user AND created_at >= :rate_cutoff""",
        {"user": user, "rate_cutoff": rate_cutoff})
    if int(recent.iloc[0]["n"] or 0) >= LLM_SUBMISSIONS_PER_USER_DAY:
        raise HTTPException(429, f"每位用戶24小時內最多提交 {LLM_SUBMISSIONS_PER_USER_DAY} 份文字訓練資料。")
    total = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_LLM_TRAINING_SUBMISSIONS}")
    if not total.empty and int(total.iloc[0]["n"] or 0) >= LLM_SUBMISSION_MAX_TOTAL:
        raise HTTPException(409, "LLM訓練資料已達保護上限，請先封存或清理舊提交。")
    review = {"fingerprint": fingerprint, "manual_confirmed": body.manual_review}
    review_status = "error" if body.manual_review else "passed"
    if not body.manual_review:
        from deploy.proxy import _get_proxy_secret
        key = _get_proxy_secret("GEMINI_API_KEY").strip()
        if not key:
            _log_ai(user, db, "llm_review", False, error="GEMINI_API_KEY missing")
            raise HTTPException(503,"AI 預檢暫時未能完成；可確認後選擇略過 AI 檢查")
        prompt = "審核以下香港粵語辯論訓練文字，只回覆 JSON，含 passed(boolean), reason, relevance, quality, anonymization, permission_risk。\n" + normalized
        try:
            url=f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
            async with LLM_REVIEW_SEMAPHORE:
                async with httpx.AsyncClient(timeout=AI_TRAINING_PROVIDER_TIMEOUT_SECONDS) as client:
                    response_data = await post_json_bounded(client, url, json={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"responseMimeType":"application/json","temperature":0,"maxOutputTokens":2048}})
            review=json.loads(response_data["candidates"][0]["content"]["parts"][0]["text"]); review["fingerprint"]=fingerprint
            _log_ai(user, db, "llm_review", True, response_data=response_data)
        except Exception as exc:
            _log_ai(user, db, "llm_review", False, error=str(exc))
            raise HTTPException(503,"AI 預檢暫時未能完成；可確認後選擇略過 AI 檢查") from exc
        if not bool(review.get("passed")): return {"ok":False,"status":"failed","message":review.get("reason") or "AI 預檢未通過", "review":review}
    params = {"user":user,"type":body.data_type,"title":body.title.strip() or None,
              "topic":body.topic_text.strip() or None,"side":body.side,
              "content":body.content_text.strip(),"source":body.source_note.strip() or None,
              "ai_status":review_status,"review":_bounded_json_param(review, "LLM AI預檢結果"),
              "raw":"|".join([body.data_type,body.side,body.title.strip(),body.topic_text.strip(),
                                body.content_text.strip(),body.source_note.strip()]),
              "now":datetime.now(), "rate_cutoff": rate_cutoff}
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                     {"key": f"llm_submission:{user}"})
        final_recent = int(conn.execute(text(f"""SELECT COUNT(*) FROM {TABLE_LLM_TRAINING_SUBMISSIONS}
            WHERE submitted_by=:user AND created_at>=:rate_cutoff"""),
            params).scalar() or 0)
        final_total = int(conn.execute(text(f"SELECT COUNT(*) FROM {TABLE_LLM_TRAINING_SUBMISSIONS}")).scalar() or 0)
        final_duplicate = conn.execute(text(f"""SELECT 1 FROM {TABLE_LLM_TRAINING_SUBMISSIONS}
            WHERE submitted_by=:user
              AND md5(COALESCE(data_type,'')||'|'||COALESCE(side,'')||'|'||COALESCE(title,'')||'|'||COALESCE(topic_text,'')||'|'||COALESCE(content_text,'')||'|'||COALESCE(source_note,''))=md5(:raw)
              AND status!='withdrawn' LIMIT 1"""), params).fetchone()
        if final_duplicate:
            raise HTTPException(409, "此資料已提交，請勿重複提交")
        if final_recent >= LLM_SUBMISSIONS_PER_USER_DAY:
            raise HTTPException(429, f"每位用戶24小時內最多提交 {LLM_SUBMISSIONS_PER_USER_DAY} 份文字訓練資料。")
        if final_total >= LLM_SUBMISSION_MAX_TOTAL:
            raise HTTPException(409, "LLM訓練資料已達保護上限，請先封存或清理舊提交。")
        conn.execute(text(f"""INSERT INTO {TABLE_LLM_TRAINING_SUBMISSIONS}(submitted_by,data_type,title,topic_text,side,content_text,source_note,anonymized,permission_confirmed,ai_review_status,ai_review_json,status,created_at)
            VALUES(:user,:type,:title,:topic,:side,:content,:source,TRUE,TRUE,:ai_status,:review,'pending',:now)"""), params)
    return {"ok":True,"message":"資料已提交，等待人工審核。"}


@router.delete("/llm/{submission_id}")
def withdraw_llm(submission_id: int, request: Request):
    user, db = _ctx(request)
    now = datetime.now()
    changed = False
    with db.transaction() as conn:
        current = conn.execute(text(f"""SELECT status
            FROM {TABLE_LLM_TRAINING_SUBMISSIONS}
            WHERE id=:id AND submitted_by=:user FOR UPDATE"""),
            {"id": submission_id, "user": user}).mappings().first()
        if current is None:
            raise HTTPException(404, "找不到提交")
        if current["status"] != "withdrawn":
            conn.execute(text(f"""UPDATE {TABLE_LLM_TRAINING_SUBMISSIONS}
                SET status='withdrawn' WHERE id=:id AND submitted_by=:user"""),
                {"id":submission_id,"user":user})
            changed = True
            _audit(db, user, "submission_withdrawn", "llm_submission", submission_id,
                   conn=conn)
    if changed:
        _prune_audit(db)
    cleanup_params = {
        "id": submission_id,
        "source_table": TABLE_LLM_TRAINING_SUBMISSIONS,
        "source_id": str(submission_id),
        "now": now,
    }
    _run_optional_cleanup(
        db, (TABLE_RAG_DOCUMENTS,),
        f"DELETE FROM {TABLE_RAG_DOCUMENTS} WHERE submission_id=:id",
        cleanup_params, "delete withdrawn RAG document",
    )
    _run_optional_cleanup(
        db, (TABLE_AI_DATASET_SNAPSHOTS, TABLE_AI_DATASET_SNAPSHOT_ITEMS),
        f"""UPDATE {TABLE_AI_DATASET_SNAPSHOTS} SET status='withdrawn'
            WHERE dataset_kind='llm' AND status IN ('draft','ready')
              AND snapshot_id IN (
                SELECT snapshot_id FROM {TABLE_AI_DATASET_SNAPSHOT_ITEMS}
                WHERE source_table=:source_table AND source_id=:source_id
              )""",
        cleanup_params, "withdraw LLM snapshots",
    )
    _run_optional_cleanup(
        db, (TABLE_AI_DATASET_SNAPSHOT_ITEMS, TABLE_AI_MODEL_VERSIONS),
        f"""UPDATE {TABLE_AI_MODEL_VERSIONS}
            SET status='blocked',updated_at=:now
            WHERE status!='retired' AND dataset_snapshot_id IN (
                SELECT snapshot_id FROM {TABLE_AI_DATASET_SNAPSHOT_ITEMS}
                WHERE source_table=:source_table AND source_id=:source_id
            )""",
        cleanup_params, "block withdrawn LLM models",
    )
    return {"ok":True, "changed": changed}


@router.post("/lexicon")
def lexicon(body: LexiconBody, request: Request):
    user, db = _ctx(request)
    if not _is_admin(db,user): raise HTTPException(403,"只有管理員可修改讀音字典")
    term, reading = body.term.strip(), body.reading.strip()
    if not term or not reading: raise HTTPException(400, "詞語與讀法都必須填寫")
    lid = body.lexicon_id.strip()
    if not lid:
        count = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_TTS_LEXICON}")
        if not count.empty and int(count.iloc[0]["n"] or 0) >= AI_TRAINING_INVENTORY_LIMIT:
            raise HTTPException(409, "讀音字典已達保護上限，請先停用或整理舊項目")
        existing=_rows(db.query(f"SELECT lexicon_id FROM {TABLE_TTS_LEXICON} WHERE lexicon_id LIKE 'lex_%' ORDER BY lexicon_id LIMIT :inventory_limit", {"inventory_limit": AI_TRAINING_INVENTORY_LIMIT})); nums=[int(m.group(1)) for x in existing if (m:=re.match(r"lex_(\\d+)$",str(x['lexicon_id'])))]
        lid=f"lex_{max(nums,default=0)+1:04d}"
    db.execute(f"""INSERT INTO {TABLE_TTS_LEXICON}(lexicon_id,term,reading,jyutping,example,note,category,is_active,created_by,updated_at)
                   VALUES(:id,:term,:reading,:jyutping,:example,:note,:category,TRUE,:user,:now)
                   ON CONFLICT(lexicon_id) DO UPDATE SET term=EXCLUDED.term,reading=EXCLUDED.reading,jyutping=EXCLUDED.jyutping,example=EXCLUDED.example,note=EXCLUDED.note,category=EXCLUDED.category,updated_at=EXCLUDED.updated_at""", {"id":lid,"term":term,"reading":reading,"jyutping":body.jyutping.strip(),"example":body.example.strip(),"note":body.note.strip(),"category":body.category.strip(),"user":user,"now":datetime.now()})
    return {"ok":True}


@router.post("/scripts")
def script(body: ScriptBody, request: Request):
    user, db = _ctx(request)
    if not _is_admin(db,user): raise HTTPException(403,"只有管理員可修改句庫")
    category, value = body.category.strip(), body.text.strip()
    if not category or not value: raise HTTPException(400, "類別與句子內容都必須填寫")
    sid=body.script_id.strip() or f"custom_{int(datetime.now().timestamp() * 1000)}"
    if not body.script_id.strip():
        count = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_TTS_SCRIPTS}")
        if not count.empty and int(count.iloc[0]["n"] or 0) >= AI_TRAINING_INVENTORY_LIMIT:
            raise HTTPException(409, "TTS句庫已達保護上限，請先停用或整理舊句子")
    db.execute(f"""INSERT INTO {TABLE_TTS_SCRIPTS}(script_id,category,text,is_active,sort_order,created_by,updated_at) VALUES(:id,:cat,:text,TRUE,:sort,:user,:now)
                   ON CONFLICT(script_id) DO UPDATE SET category=EXCLUDED.category,text=EXCLUDED.text,sort_order=EXCLUDED.sort_order,updated_at=EXCLUDED.updated_at""", {"id":sid,"cat":category,"text":value,"sort":body.sort_order,"user":user,"now":datetime.now()})
    return {"ok":True}


@router.patch("/scripts/{script_id}/active")
def set_script_active(script_id: str, body: ActiveBody, request: Request):
    _user, db = _admin(request)
    db.execute(f"UPDATE {TABLE_TTS_SCRIPTS} SET is_active=:active,updated_at=:now WHERE script_id=:id",
               {"active": body.active, "now": datetime.now(), "id": script_id})
    return {"ok": True, "active": body.active}


@router.patch("/lexicon/{lexicon_id}/active")
def set_lexicon_active(lexicon_id: str, body: ActiveBody, request: Request):
    _user, db = _admin(request)
    db.execute(f"UPDATE {TABLE_TTS_LEXICON} SET is_active=:active,updated_at=:now WHERE lexicon_id=:id",
               {"active": body.active, "now": datetime.now(), "id": lexicon_id})
    return {"ok": True, "active": body.active}


@router.post("/manuscripts")
def save_manuscript(body: ManuscriptBody, request: Request):
    user, db = _admin(request)
    title = body.title.strip()
    if not title: raise HTTPException(400, "請輸入完整稿標題")
    manuscript_id = f"ms_{int(datetime.now().timestamp() * 1000)}"
    segments = _segments(body.text)
    count = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_TTS_SCRIPTS}")
    if not count.empty and int(count.iloc[0]["n"] or 0) + len(segments) > AI_TRAINING_INVENTORY_LIMIT:
        raise HTTPException(409, "加入完整稿後會超出TTS句庫保護上限")
    now = datetime.now()
    with db.transaction() as conn:
      for index, value in enumerate(segments, 1):
        conn.execute(text(f"""INSERT INTO {TABLE_TTS_SCRIPTS}
            (script_id,category,text,is_active,sort_order,script_type,manuscript_id,manuscript_title,created_by,updated_at)
            VALUES(:id,:category,:text,:active,:sort,'full',:mid,:title,:user,:now)"""),
            {"id": f"{manuscript_id}_{index:03d}", "category": body.category.strip() or "完整稿",
             "text": value, "active": body.active, "sort": index, "mid": manuscript_id,
             "title": title, "user": user, "now": now})
    return {"ok": True, "manuscript_id": manuscript_id, "segments": len(segments)}


@router.patch("/manuscripts/{manuscript_id}/active")
def set_manuscript_active(manuscript_id: str, body: ActiveBody, request: Request):
    _user, db = _admin(request)
    db.execute(f"UPDATE {TABLE_TTS_SCRIPTS} SET is_active=:active,updated_at=:now WHERE manuscript_id=:id",
               {"active": body.active, "now": datetime.now(), "id": manuscript_id})
    return {"ok": True, "active": body.active}


@router.get("/coverage")
def coverage(request: Request):
    _user, db = _admin(request)
    rows = _rows(db.query(f"""SELECT s.category,COUNT(*) AS scripts,
        COUNT(DISTINCT r.script_id) FILTER (WHERE r.status='accepted') AS recorded
        FROM {TABLE_TTS_SCRIPTS} s LEFT JOIN {TABLE_TTS_VOICE_RECORDINGS} r ON r.script_id=s.script_id
        WHERE s.is_active=TRUE GROUP BY s.category ORDER BY s.category"""))
    for row in rows:
        row["missing"] = max(0, int(row["scripts"] or 0) - int(row["recorded"] or 0))
    return {"items": rows, "complete": bool(rows) and all(row["missing"] == 0 for row in rows)}


@router.post("/coverage/ai")
async def coverage_ai(request: Request):
    user, db = _admin(request)
    from deploy.proxy import _get_proxy_secret
    key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not key:
        _log_ai(user, db, "tts_script_analysis", False, error="GEMINI_API_KEY missing")
        raise HTTPException(503, "未設定 GEMINI_API_KEY，未能進行 AI 缺口分析")
    rows = _rows(db.query(f"""SELECT s.script_id,s.category,s.text,r.status,COUNT(r.id) AS n
        FROM {TABLE_TTS_SCRIPTS} s LEFT JOIN {TABLE_TTS_VOICE_RECORDINGS} r
          ON r.script_id=s.script_id AND r.status IN ('accepted','pending')
        WHERE s.is_active=TRUE GROUP BY s.script_id,s.category,s.text,r.status
        ORDER BY s.category,s.script_id LIMIT :analysis_limit""",
        {"analysis_limit": TTS_AI_ANALYSIS_SCRIPT_LIMIT}))
    grouped = {}
    for row in rows:
        item = grouped.setdefault(row["script_id"], {"category":row["category"], "text":row["text"], "accepted":0, "pending":0})
        if row.get("status") in ("accepted", "pending"): item[row["status"]] = int(row.get("n") or 0)
    summary = "\n".join(f"[{x['category']}] {sid}｜accepted={x['accepted']}｜pending={x['pending']}｜{x['text']}" for sid,x in grouped.items()) or "（句庫為空）"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    coverage_prompt = build_tts_coverage_prompt(summary)[:AI_TRAINING_PROMPT_MAX_CHARS]
    body = {"systemInstruction":{"parts":[{"text":TTS_COVERAGE_SYSTEM_PROMPT}]}, "contents":[{"parts":[{"text":coverage_prompt}]}], "generationConfig":{"responseMimeType":"application/json","temperature":.4,"maxOutputTokens":2048}}
    try:
        async with httpx.AsyncClient(timeout=AI_TRAINING_PROVIDER_TIMEOUT_SECONDS) as client:
            response_data = await post_json_bounded(client, url, json=body)
        analysis=json.loads(response_data["candidates"][0]["content"]["parts"][0]["text"])
        if not isinstance(analysis, dict): raise ValueError("AI 回覆格式不正確")
        _log_ai(user, db, "tts_script_analysis", True, response_data=response_data)
        return {"analysis": analysis}
    except Exception as exc:
        _log_ai(user, db, "tts_script_analysis", False, error=str(exc))
        raise HTTPException(502, f"AI 缺口分析失敗：{str(exc)[:160]}") from exc


@router.get("/inventory")
def inventory(request: Request):
    _user, db = _admin(request)
    scripts = _rows(db.query(f"SELECT script_id AS id,category,text,is_active,script_type,manuscript_id,manuscript_title,sort_order FROM {TABLE_TTS_SCRIPTS} ORDER BY category,sort_order,script_id LIMIT :inventory_limit", {"inventory_limit": AI_TRAINING_INVENTORY_LIMIT}))
    lexicon = _rows(db.query(f"SELECT lexicon_id AS id,term,reading,is_active,category FROM {TABLE_TTS_LEXICON} ORDER BY category,term LIMIT :inventory_limit", {"inventory_limit": AI_TRAINING_INVENTORY_LIMIT}))
    manuscripts = []
    seen = set()
    for row in scripts:
        mid = row.get("manuscript_id")
        if mid and mid not in seen:
            grouped = [x for x in scripts if x.get("manuscript_id") == mid]
            manuscripts.append({"id": mid, "title": row.get("manuscript_title") or mid,
                                "segments": len(grouped), "is_active": any(bool(x.get("is_active")) for x in grouped)})
            seen.add(mid)
    return json_safe({"scripts": scripts, "lexicon": lexicon, "manuscripts": manuscripts})


@router.post("/scripts/deactivate-complete")
def deactivate_complete(request: Request):
    _user, db = _admin(request); allowed = _users(db, ALLOWED_KEY)
    if not allowed: return {"ok":True,"deactivated":0}
    rows=db.query(f"""SELECT s.script_id FROM {TABLE_TTS_SCRIPTS}s LEFT JOIN {TABLE_TTS_VOICE_RECORDINGS}r
      ON r.script_id=s.script_id AND r.status='accepted' AND r.speaker_user_id=ANY(:users)
      WHERE s.is_active=TRUE GROUP BY s.script_id HAVING COUNT(DISTINCT r.speaker_user_id)>=:required""",{"users":allowed,"required":len(allowed)})
    complete_ids={str(x) for x in rows["script_id"].tolist()} if not rows.empty else set()
    active = _rows(db.query(f"SELECT script_id,script_type,manuscript_id FROM {TABLE_TTS_SCRIPTS} WHERE is_active=TRUE LIMIT :inventory_limit", {"inventory_limit": AI_TRAINING_INVENTORY_LIMIT}))
    ids = [str(x["script_id"]) for x in active if x.get("script_type") != "full" and str(x["script_id"]) in complete_ids]
    manuscripts = {}
    for item in active:
        if item.get("script_type") == "full" and item.get("manuscript_id"):
            manuscripts.setdefault(str(item["manuscript_id"]), []).append(str(item["script_id"]))
    for segment_ids in manuscripts.values():
        if segment_ids and all(sid in complete_ids for sid in segment_ids): ids.extend(segment_ids)
    if ids: db.execute(f"UPDATE {TABLE_TTS_SCRIPTS} SET is_active=FALSE,updated_at=:now WHERE script_id=ANY(:ids)",{"ids":ids,"now":datetime.now()})
    return {"ok":True,"deactivated":len(ids)}


@router.post("/suggestions/apply")
def apply_suggestions(body: SuggestionsBody, request: Request):
    user, db = _admin(request); added=0
    count = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_TTS_SCRIPTS}")
    current = int(count.iloc[0]["n"] or 0) if not count.empty else 0
    available = max(0, AI_TRAINING_INVENTORY_LIMIT - current)
    for item in body.items[:min(AI_SUGGESTION_BATCH_MAX, available)]:
        category=str(item.get("category") or "AI 建議").strip()[:80]
        value=str(item.get("text") or "").strip()[:500]
        if not value: continue
        sid=f"ai_{int(datetime.now().timestamp()*1000)}_{added:02d}"
        db.execute(f"INSERT INTO {TABLE_TTS_SCRIPTS}(script_id,category,text,is_active,sort_order,created_by,updated_at) VALUES(:id,:cat,:text,TRUE,0,:user,:now)",{"id":sid,"cat":category,"text":value,"user":user,"now":datetime.now()}); added+=1
    locked = _rows(db.query(f"SELECT DISTINCT script_id FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE status IN ('pending','accepted') LIMIT :inventory_limit", {"inventory_limit": AI_TRAINING_INVENTORY_LIMIT}))
    locked_ids = {str(x["script_id"]) for x in locked}
    deactivate = [str(x)[:100] for x in body.deactivate_ids[:AI_SUGGESTION_BATCH_MAX] if str(x)[:100] not in locked_ids]
    deactivated = 0
    if deactivate:
        deactivated = db.execute_count(f"UPDATE {TABLE_TTS_SCRIPTS} SET is_active=FALSE,updated_at=:now WHERE script_id=ANY(:ids) AND is_active=TRUE", {"ids":deactivate,"now":datetime.now()})
    return {"ok":True,"added":added,"deactivated":deactivated,
            "inventory_full": available == 0 and bool(body.items)}


@router.post("/regenerate-suggestions")
async def regenerate_suggestions(request: Request):
    user, db = _admin(request)
    from deploy.proxy import _get_proxy_secret
    key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not key:
        _log_ai(user, db, "tts_script_analysis", False, error="GEMINI_API_KEY missing")
        raise HTTPException(503, "未設定 GEMINI_API_KEY，暫時未能重出句庫")
    rows = _rows(db.query(f"SELECT script_id AS id,category,text FROM {TABLE_TTS_SCRIPTS} WHERE is_active=TRUE ORDER BY category,sort_order LIMIT :inventory_limit", {"inventory_limit": TTS_AI_ANALYSIS_SCRIPT_LIMIT}))
    locked_rows = _rows(db.query(f"SELECT DISTINCT script_id FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE status IN ('pending','accepted') LIMIT :inventory_limit", {"inventory_limit": AI_TRAINING_INVENTORY_LIMIT}))
    locked_ids = {str(x["script_id"]) for x in locked_rows}
    locked = "\n".join(f"[{x['category']}] {x['id']}｜{x['text']}" for x in rows if str(x["id"]) in locked_ids) or "（暫時冇已錄音句子）"
    unlocked = "\n".join(f"[{x['category']}] {x['id']}｜{x['text']}" for x in rows if str(x["id"]) not in locked_ids) or "（暫時冇未錄音句子）"
    prompt = build_tts_regenerate_prompt(locked, unlocked)[:AI_TRAINING_PROMPT_MAX_CHARS]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    payload = {"systemInstruction":{"parts":[{"text":TTS_REGENERATE_SYSTEM_PROMPT}]}, "contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": .5, "maxOutputTokens": 2048}}
    try:
        async with httpx.AsyncClient(timeout=AI_TRAINING_PROVIDER_TIMEOUT_SECONDS) as client:
            response_data = await post_json_bounded(client, url, json=payload)
        raw=response_data["candidates"][0]["content"]["parts"][0]["text"]
        plan=json.loads(raw)
        if not isinstance(plan, dict): raise ValueError("AI 回覆格式不正確")
        plan["deactivate_candidates"] = [x for x in (plan.get("deactivate_candidates") or []) if str(x.get("script_id")) not in locked_ids]
        _log_ai(user, db, "tts_script_analysis", True, response_data=response_data)
    except Exception as exc:
        _log_ai(user, db, "tts_script_analysis", False, error=str(exc))
        raise HTTPException(502, f"AI 重出句庫失敗：{str(exc)[:160]}") from exc
    return {"plan": plan}


@router.get("/export/recordings.json")
def export_recording_manifest(request: Request, speaker: str = ""):
    """Return metadata and direct R2 URLs; never proxy binary through Render."""
    _user, db = _admin(request)
    where, params = "status='accepted'", {}
    if speaker.strip(): where += " AND speaker_user_id=:speaker"; params["speaker"] = speaker.strip()
    rows = _rows(db.query(f"""SELECT r.id,r.speaker_user_id,r.script_id,r.prompt_text,r.r2_key,
        r.mime_type,r.file_ext,r.size_bytes,r.audio_sha256,
        COALESCE(r.measured_duration_seconds,r.duration_seconds) AS duration_seconds,
        r.sample_rate_hz,r.channel_count,r.detected_format,r.ai_transcript,
        s.manuscript_id,s.manuscript_title,s.category
        FROM {TABLE_TTS_VOICE_RECORDINGS} r LEFT JOIN {TABLE_TTS_SCRIPTS} s ON s.script_id=r.script_id
        WHERE {where.replace('status', 'r.status').replace('speaker_user_id', 'r.speaker_user_id')}
        ORDER BY r.id LIMIT :export_limit""", {**params, "export_limit": RECORDING_MANIFEST_MAX_ROWS + 1}))
    require_row_limit(rows, limit=RECORDING_MANIFEST_MAX_ROWS, label="錄音 manifest 匯出")
    from core import r2_storage
    if not r2_storage.configured():
        raise HTTPException(503, "Cloudflare R2 暫時不可用")
    manifest = []
    for row in rows:
        r2_key = str(row.get("r2_key") or "")
        if not r2_key:
            raise HTTPException(409, f"錄音 {row['id']} 尚未完成R2遷移")
        ext = re.sub(r"[^a-z0-9]", "", str(row.get("file_ext") or "webm").lower()) or "webm"
        row["file"] = f"audio/{row['id']}_{row['script_id']}.{ext}"
        row["download_url"] = r2_storage.presign_get(
            r2_key, mime_type=row.get("mime_type") or "audio/webm",
            file_name=row["file"].split("/", 1)[-1], download=True,
            expires=R2_BULK_LINK_TTL_SECONDS,
        )
        manifest.append(row)
    return {"storage": "r2", "expires_seconds": R2_BULK_LINK_TTL_SECONDS, "items": manifest}


@router.get("/export/llm.jsonl")
def export_llm(request: Request, after_id: int = 0, before_id: int = 0):
    _user, db = _admin(request)
    after_id, before_id = max(0, after_id), max(0, before_id)
    where = "status='accepted' AND id>:after_id"
    params = {"after_id": after_id}
    if before_id:
        where += " AND id<=:before_id"
        params["before_id"] = before_id
    # Check the database-side byte total first.  Loading 5,000 maximum-sized
    # submissions and only then rejecting JSONL could temporarily use >100MB.
    size = _rows(db.query(f"""SELECT COUNT(*) AS row_count,
        COALESCE(SUM(octet_length(COALESCE(content_text,''))
          +octet_length(COALESCE(title,''))+octet_length(COALESCE(topic_text,''))
          +octet_length(COALESCE(source_note,''))),0) AS payload_bytes
        FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE {where}""", params))
    row_count = int(size[0].get("row_count") or 0) if size else 0
    payload_bytes = int(size[0].get("payload_bytes") or 0) if size else 0
    if row_count > EXPORT_MAX_ROWS or payload_bytes > EXPORT_MAX_BYTES:
        raise HTTPException(413, "LLM訓練資料超過單次匯出保護上限，請用 after_id／before_id 分批下載。")
    rows = _rows(db.query(f"""SELECT id,submitted_by,data_type,title,topic_text,side,
        content_text,source_note,created_at FROM {TABLE_LLM_TRAINING_SUBMISSIONS}
        WHERE {where} ORDER BY id LIMIT :export_limit""",
        {**params, "export_limit": EXPORT_MAX_ROWS + 1}))
    require_row_limit(rows, label="LLM訓練資料匯出")
    return jsonl_response("llm-accepted.jsonl", rows)


@router.post("/recordings/{record_id}/review")
def review_recording(record_id:int, body:ReviewBody, request:Request):
    user,db=_ctx(request)
    if not _is_admin(db,user): raise HTTPException(403,"只有管理員可審核")
    if body.status not in ('accepted','rejected'): raise HTTPException(400,"狀態不正確")
    now = datetime.now()
    with db.transaction() as conn:
        current = conn.execute(text(f"""SELECT status,script_id
            FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE id=:id FOR UPDATE"""),
            {"id": record_id}).mappings().first()
        if current is None:
            raise HTTPException(404, "找不到錄音")
        if current["status"] != "pending":
            raise HTTPException(409, "只有待審核錄音可以更新")
        conn.execute(text(f"""UPDATE {TABLE_TTS_VOICE_RECORDINGS}
            SET status=:status,review_note=:note,reviewed_by=:user,reviewed_at=:now
            WHERE id=:id"""),
            {"status":body.status,"note":body.note,"user":user,"now":now,"id":record_id})
        if body.status == "rejected":
            conn.execute(text(f"""UPDATE {TABLE_TTS_SCRIPTS}
                SET is_active=TRUE,updated_at=:now WHERE script_id=:script"""),
                {"script": current["script_id"], "now": now})
        _audit(db, user, "recording_reviewed", "tts_recording", record_id,
               {"status": body.status}, conn=conn)
    _prune_audit(db)
    return {"ok":True}


@router.post("/llm/{submission_id}/review")
def review_llm(submission_id:int, body:ReviewBody, request:Request):
    user,db=_ctx(request)
    if not _is_admin(db,user): raise HTTPException(403,"只有管理員可審核")
    if body.status not in ('accepted','rejected'): raise HTTPException(400,"狀態不正確")
    with db.transaction() as conn:
        current = conn.execute(text(f"""SELECT status
            FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE id=:id FOR UPDATE"""),
            {"id": submission_id}).mappings().first()
        if current is None:
            raise HTTPException(404, "找不到提交")
        if current["status"] != "pending":
            raise HTTPException(409, "只有待審核資料可以更新")
        conn.execute(text(f"""UPDATE {TABLE_LLM_TRAINING_SUBMISSIONS}
            SET status=:status,review_note=:note,reviewed_by=:user,reviewed_at=:now
            WHERE id=:id"""),
            {"status":body.status,"note":body.note,"user":user,
             "now":datetime.now(),"id":submission_id})
        _audit(db, user, "submission_reviewed", "llm_submission", submission_id,
               {"status": body.status}, conn=conn)
    _prune_audit(db)
    return {"ok":True}


@router.get("/readiness")
def readiness(request: Request):
    _user, db = _admin(request)
    speakers = _rows(db.query(f"""SELECT r.speaker_user_id,
        COUNT(*) FILTER (WHERE r.status='accepted') AS accepted_clips,
        ROUND(COALESCE(SUM(COALESCE(r.measured_duration_seconds,r.duration_seconds))
              FILTER (WHERE r.status='accepted'),0)/60.0,1) AS accepted_minutes,
        COUNT(*) FILTER (WHERE r.status='pending') AS pending_clips,
        ROUND(COALESCE(SUM(COALESCE(r.measured_duration_seconds,r.duration_seconds))
              FILTER (WHERE r.status='pending'),0)/60.0,1) AS pending_minutes,
        COUNT(*) FILTER (WHERE r.status='accepted' AND c.consent_version=:version
              AND c.withdrawn_at IS NULL AND c.voice_cloning_confirmed=TRUE
              AND c.cloud_processing_confirmed=TRUE
              AND (c.is_minor=FALSE OR c.guardian_confirmed=TRUE)) AS eligible_clips
      FROM {TABLE_TTS_VOICE_RECORDINGS} r
      LEFT JOIN {TABLE_TTS_VOICE_CONSENTS} c
        ON c.user_id=r.speaker_user_id AND c.consent_version=:version
       AND c.consent_text=:consent_text
      GROUP BY r.speaker_user_id ORDER BY accepted_minutes DESC LIMIT :inventory_limit""",
      {"version": CONSENT_VERSION, "consent_text": CONSENT_TEXT,
       "inventory_limit": AI_TRAINING_INVENTORY_LIMIT}))
    lexicon = db.query(f"SELECT COUNT(*) n FROM {TABLE_TTS_LEXICON} WHERE is_active=TRUE")
    llm = _rows(db.query(f"""SELECT data_type,COUNT(*) docs,COALESCE(SUM(LENGTH(content_text)),0) chars
        FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE status='accepted'
        AND anonymized=TRUE AND permission_confirmed=TRUE GROUP BY data_type ORDER BY docs DESC
        LIMIT :group_limit""", {"group_limit": AI_TRAINING_READINESS_GROUP_LIMIT}))
    active_lexicon = int(lexicon.iloc[0]["n"] or 0) if not lexicon.empty else 0
    eval_provisioned = _feature_schema_state(db, "eval")
    eval_rows = (
        db.query(f"SELECT COUNT(*) n FROM {TABLE_AI_EVAL_CASES} WHERE is_active=TRUE")
        if eval_provisioned else None
    )
    eval_count = int(eval_rows.iloc[0]["n"] or 0) if eval_rows is not None and not eval_rows.empty else 0
    return json_safe({"consent_version": CONSENT_VERSION, "speakers": speakers,
        "active_lexicon": active_lexicon, "llm_by_type": llm,
        "eval_provisioned": eval_provisioned, "active_eval_cases": eval_count,
        "gates": {"tts_min_train_minutes": 30, "tts_target_collected_minutes": 40,
                  "tts_min_lexicon": 50, "llm_eval_cases": 30,
                  "llm_min_instruction_pairs_for_lora": 500}})


@router.get("/eval/cases")
def eval_cases(request: Request):
    _user, db = _admin(request)
    _require_feature_schema(db, "eval")
    return json_safe({"items": _rows(db.query(f"""SELECT case_id,task_type,title,input_json,
        rubric_json,reference_text FROM {TABLE_AI_EVAL_CASES} WHERE is_active=TRUE
        ORDER BY task_type,case_id LIMIT :eval_limit""", {"eval_limit": AI_EVAL_CASE_LIMIT}))})


@router.post("/eval/runs")
async def eval_baseline(request: Request):
    user, db = _admin(request)
    _require_feature_schema(db, "eval")
    payload_body = await request.json()
    model_label = str(payload_body.get("model_label") or "Gemini 2.5 Flash")[:200]
    cases = _rows(db.query(f"SELECT case_id,task_type,title,input_json,rubric_json FROM {TABLE_AI_EVAL_CASES} WHERE is_active=TRUE ORDER BY case_id LIMIT :eval_limit", {"eval_limit": AI_EVAL_CASE_LIMIT}))
    _audit(db, user, "eval_baseline_requested", "eval_run", model_label, {"cases": len(cases)})
    return {"ok":True,"model_label":model_label,"case_count":len(cases),
            "message":"評估題已鎖定；請由受控eval worker逐題執行並寫入ai_eval_runs，API不會在單一HTTP request內長時間批量呼叫模型。"}


@router.post("/datasets/snapshots")
def create_snapshot(body: SnapshotBody, request: Request):
    user, db = _admin(request)
    _require_feature_schema(db, "dataset_model")
    if body.dataset_kind not in ("tts", "llm"):
        raise HTTPException(400, "dataset_kind只可為tts或llm")
    items = []
    total_seconds = 0.0
    if body.dataset_kind == "tts":
        speaker = body.speaker_user_id.strip()
        if not speaker:
            raise HTTPException(400, "TTS snapshot必須指定單一speaker")
        rows = _rows(db.query(f"""SELECT r.id,r.script_id,r.prompt_text,r.r2_key,r.audio_sha256,
                COALESCE(r.measured_duration_seconds,r.duration_seconds,0) duration_seconds,
                s.manuscript_id,s.category,c.consent_version
            FROM {TABLE_TTS_VOICE_RECORDINGS} r
            JOIN {TABLE_TTS_SCRIPTS} s ON s.script_id=r.script_id
            JOIN {TABLE_TTS_VOICE_CONSENTS} c ON c.user_id=r.speaker_user_id
              AND c.consent_version=:version AND c.withdrawn_at IS NULL
              AND c.voice_cloning_confirmed=TRUE AND c.cloud_processing_confirmed=TRUE
              AND (c.is_minor=FALSE OR c.guardian_confirmed=TRUE)
            WHERE r.status='accepted' AND r.speaker_user_id=:speaker ORDER BY r.id
            LIMIT :item_limit""",
            {"speaker": speaker, "version": CONSENT_VERSION,
             "item_limit": DATASET_SNAPSHOT_MAX_ITEMS + 1}))
        require_row_limit(rows, limit=DATASET_SNAPSHOT_MAX_ITEMS, label="TTS dataset snapshot")
        for row in rows:
            sha = str(row.get("audio_sha256") or "")
            if not sha or not str(row.get("r2_key") or ""):
                raise HTTPException(409, f"錄音 {row['id']} 尚未完成R2遷移或缺少SHA256")
            group = str(row.get("manuscript_id") or row["script_id"])
            seconds = float(row.get("duration_seconds") or 0)
            total_seconds += seconds
            items.append({"source_table": TABLE_TTS_VOICE_RECORDINGS, "source_id": str(row["id"]),
                "source_sha256": sha, "consent_version": CONSENT_VERSION,
                "split_name": _split_name(group), "metadata": {"script_id": row["script_id"],
                "prompt_text": row["prompt_text"], "manuscript_id": row.get("manuscript_id"),
                "category": row.get("category"), "duration_seconds": seconds,
                "r2_key": row.get("r2_key")}})
    else:
        speaker = ""
        rows = _rows(db.query(f"""SELECT id,data_type,title,topic_text,side,content_text,source_note
            FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE status='accepted'
            AND anonymized=TRUE AND permission_confirmed=TRUE ORDER BY id
            LIMIT :item_limit""", {"item_limit": DATASET_SNAPSHOT_MAX_ITEMS + 1}))
        require_row_limit(rows, limit=DATASET_SNAPSHOT_MAX_ITEMS, label="LLM dataset snapshot")
        for row in rows:
            sha = hashlib.sha256(str(row["content_text"]).encode("utf-8")).hexdigest()
            group = str(row.get("topic_text") or row["id"])
            # The immutable source row already owns content_text.  Repeating it
            # in both manifest_json and snapshot_items doubled Supabase storage
            # on every snapshot without improving reproducibility.
            metadata = {key: row.get(key) for key in ("data_type", "side")}
            items.append({"source_table": TABLE_LLM_TRAINING_SUBMISSIONS, "source_id": str(row["id"]),
                "source_sha256": sha, "consent_version": None, "split_name": _split_name(group),
                "metadata": metadata})
    if not items:
        raise HTTPException(400, "沒有符合授權及審核條件的資料")
    # The manifest is the immutable identity list. Rich metadata lives once in
    # snapshot_items; duplicating it here multiplied storage on every version.
    manifest = {"dataset_kind": body.dataset_kind, "speaker_user_id": speaker,
                "consent_version": CONSENT_VERSION if body.dataset_kind == "tts" else None,
                "items": [{key: item[key] for key in
                           ("source_table", "source_id", "source_sha256",
                            "consent_version", "split_name")} for item in items]}
    manifest_json = _json_param(manifest)
    digest = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    duplicate = db.query(f"""SELECT snapshot_id FROM {TABLE_AI_DATASET_SNAPSHOTS}
        WHERE dataset_kind=:kind AND COALESCE(speaker_user_id,'')=:speaker
          AND manifest_sha256=:sha AND status IN ('draft','ready') LIMIT 1""",
        {"kind": body.dataset_kind, "speaker": speaker, "sha": digest})
    if not duplicate.empty:
        raise HTTPException(409, f"相同資料集已存在：{duplicate.iloc[0]['snapshot_id']}")
    snapshot_count = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_AI_DATASET_SNAPSHOTS}")
    if not snapshot_count.empty and int(snapshot_count.iloc[0]["n"] or 0) >= DATASET_SNAPSHOT_MAX_COUNT:
        raise HTTPException(409, "Dataset snapshot已達保護上限，請先封存舊版本。")
    snapshot_id = f"{body.dataset_kind}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{digest[:10]}"
    with db.transaction() as conn:
        conn.execute(text(f"""INSERT INTO {TABLE_AI_DATASET_SNAPSHOTS}
            (snapshot_id,dataset_kind,speaker_user_id,consent_version,item_count,total_seconds,
             manifest_sha256,manifest_json,status,created_by)
            VALUES(:id,:kind,:speaker,:consent,:count,:seconds,:sha,CAST(:manifest AS jsonb),'ready',:user)"""),
            {"id":snapshot_id,"kind":body.dataset_kind,"speaker":speaker or None,
             "consent":CONSENT_VERSION if body.dataset_kind == "tts" else None,"count":len(items),
             "seconds":total_seconds,"sha":digest,"manifest":manifest_json,"user":user})
        conn.execute(text(f"""INSERT INTO {TABLE_AI_DATASET_SNAPSHOT_ITEMS}
            (snapshot_id,source_table,source_id,source_sha256,consent_version,split_name,metadata_json)
            VALUES(:snapshot,:table,:source,:sha,:consent,:split,CAST(:metadata AS jsonb))"""), [
            {"snapshot":snapshot_id,"table":item["source_table"],"source":item["source_id"],
             "sha":item["source_sha256"],"consent":item["consent_version"],
             "split":item["split_name"],"metadata":_json_param(item["metadata"])}
            for item in items])
    _audit(db, user, "snapshot_created", "dataset_snapshot", snapshot_id,
           {"kind": body.dataset_kind, "items": len(items), "manifest_sha256": digest})
    return {"ok":True,"snapshot_id":snapshot_id,"manifest_sha256":digest,
            "item_count":len(items),"total_seconds":round(total_seconds,3)}


@router.get("/models")
def models(request: Request):
    _user, db = _admin(request)
    _require_feature_schema(db, "dataset_model")
    return json_safe({"items": _rows(db.query(f"""SELECT model_id,model_type,base_model,
        dataset_snapshot_id,artifact_uri,status,config_json,metrics_json,created_by,created_at,updated_at
        FROM {TABLE_AI_MODEL_VERSIONS} ORDER BY created_at DESC LIMIT :inventory_limit""",
        {"inventory_limit": AI_MODEL_VERSION_LIMIT}))})


@router.post("/models")
def register_model(body: ModelRegisterBody, request: Request):
    user, db = _admin(request)
    _require_feature_schema(db, "dataset_model")
    if body.model_type not in ("tts", "llm", "embedding") or not body.model_id.strip() or not body.base_model.strip():
        raise HTTPException(400, "模型資料不完整")
    model_count = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_AI_MODEL_VERSIONS}")
    if not model_count.empty and int(model_count.iloc[0]["n"] or 0) >= AI_MODEL_VERSION_LIMIT:
        raise HTTPException(409, "模型版本已達保護上限，請先退役或整理舊版本。")
    if body.dataset_snapshot_id:
        snap = db.query(f"SELECT status FROM {TABLE_AI_DATASET_SNAPSHOTS} WHERE snapshot_id=:id",
                        {"id": body.dataset_snapshot_id})
        if snap.empty or snap.iloc[0]["status"] != "ready":
            raise HTTPException(400, "模型必須連結ready dataset snapshot")
    db.execute(f"""INSERT INTO {TABLE_AI_MODEL_VERSIONS}
        (model_id,model_type,base_model,dataset_snapshot_id,artifact_uri,status,config_json,created_by)
        VALUES(:id,:type,:base,:snapshot,:uri,'research',CAST(:config AS jsonb),:user)""",
        {"id":body.model_id.strip(),"type":body.model_type,"base":body.base_model.strip(),
         "snapshot":body.dataset_snapshot_id,"uri":body.artifact_uri.strip() or None,
         "config":_bounded_json_param(body.config, "模型設定"),"user":user})
    _audit(db, user, "model_registered", "model", body.model_id, {"type": body.model_type})
    return {"ok": True, "model_id": body.model_id}


@router.post("/models/{model_id}/metrics")
def model_metrics(model_id: str, body: ModelMetricsBody, request: Request):
    user, db = _admin(request)
    _require_feature_schema(db, "dataset_model")
    row = db.query(f"SELECT model_type,dataset_snapshot_id,status FROM {TABLE_AI_MODEL_VERSIONS} WHERE model_id=:id",
                   {"id": model_id})
    if row.empty: raise HTTPException(404, "找不到模型")
    status = body.status or str(row.iloc[0]["status"])
    if status not in ("research","candidate","deployable","retired","blocked"):
        raise HTTPException(400, "模型狀態不正確")
    if status == "deployable":
        required = {"cer","mos","pronunciation_accuracy","first_audio_ms"} if row.iloc[0]["model_type"] == "tts" else {"eval_score"}
        missing = required - set(body.metrics)
        if missing: raise HTTPException(400, "deployable模型缺少評估指標：" + ", ".join(sorted(missing)))
    db.execute(f"UPDATE {TABLE_AI_MODEL_VERSIONS} SET metrics_json=CAST(:metrics AS jsonb),status=:status,updated_at=:now WHERE model_id=:id",
               {"metrics":_bounded_json_param(body.metrics, "模型指標"),"status":status,"now":datetime.now(),"id":model_id})
    _audit(db, user, "model_metrics_updated", "model", model_id, {"status": status})
    return {"ok":True,"status":status}


@router.post("/rag/reindex")
async def rag_reindex(body: RagReindexBody, request: Request):
    user, db = _admin(request)
    if body.embedding_model != RAG_EMBEDDING_MODEL or body.embedding_version != RAG_EMBEDDING_VERSION:
        raise HTTPException(400, "embedding model/version 必須使用目前鎖定版本，避免重複建立索引空間")
    _require_rag_vector_schema(db)
    from deploy.proxy import _get_proxy_secret
    api_key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not api_key: raise HTTPException(503, "未設定GEMINI_API_KEY")
    submissions = _rows(db.query(f"""SELECT s.id,s.data_type,s.title,s.topic_text,s.side,
            s.content_text,s.source_note,d.document_id AS existing_document_id
        FROM {TABLE_LLM_TRAINING_SUBMISSIONS} s
        LEFT JOIN {TABLE_RAG_DOCUMENTS} d ON d.submission_id=s.id
        WHERE s.status='accepted' AND s.anonymized=TRUE AND s.permission_confirmed=TRUE
          AND (d.document_id IS NULL OR d.status!='active'
            OR d.embedding_model IS DISTINCT FROM :model
            OR d.embedding_version IS DISTINCT FROM :version
            OR NOT EXISTS (SELECT 1 FROM {TABLE_RAG_CHUNKS} c WHERE c.document_id=d.document_id))
        ORDER BY (d.document_id IS NULL),s.id LIMIT :document_limit""",
        {"model": body.embedding_model, "version": body.embedding_version,
         "document_limit": RAG_REINDEX_MAX_DOCUMENTS + 1}))
    has_more = len(submissions) > RAG_REINDEX_MAX_DOCUMENTS
    submissions = submissions[:RAG_REINDEX_MAX_DOCUMENTS]
    db.execute(f"""UPDATE {TABLE_RAG_DOCUMENTS} SET status='withdrawn'
        WHERE submission_id IN
        (SELECT id FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE status!='accepted')""")
    active_count_frame = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_RAG_DOCUMENTS} WHERE status='active'")
    active_document_count = int(active_count_frame.iloc[0]["n"] or 0) if not active_count_frame.empty else 0
    indexed = 0; documents_indexed = 0
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{body.embedding_model}:embedContent"
    async with httpx.AsyncClient(timeout=AI_TRAINING_PROVIDER_TIMEOUT_SECONDS) as client:
        for submission in submissions:
            existing_document_id = submission.get("existing_document_id")
            is_new_document = (existing_document_id is None or
                               str(existing_document_id).strip().lower() in {"", "nan", "none"})
            if is_new_document and active_document_count >= RAG_DOCUMENT_MAX_TOTAL:
                has_more = True
                continue
            document_id = f"llm-{submission['id']}"
            content_sha = hashlib.sha256(str(submission["content_text"]).encode("utf-8")).hexdigest()
            chunks = _rag_chunks(submission["content_text"])
            if indexed and indexed + len(chunks) > RAG_REINDEX_MAX_CHUNKS:
                has_more = True
                break
            if len(chunks) > RAG_REINDEX_MAX_CHUNKS:
                raise HTTPException(413, "單一文件分段數超過RAG保護上限，請先拆分文字再提交。")

            async def embed_chunk(chunk):
                async with RAG_EMBED_SEMAPHORE:
                    response_data = await post_json_bounded(client, endpoint,
                        params={"key":api_key}, json={
                        "content":{"parts":[{"text":chunk}]}, "outputDimensionality":768})
                    values = ((response_data.get("embedding") or {}).get("values") or [])
                    if len(values) != 768:
                        raise HTTPException(502, "Embedding API回傳維度不正確")
                    return values

            vectors = await asyncio.gather(*(embed_chunk(chunk) for chunk in chunks))
            now = datetime.now()
            chunk_params = []
            for index, (chunk, values) in enumerate(zip(chunks, vectors)):
                chunk_id = f"{document_id}-{index:04d}-{hashlib.sha256(chunk.encode()).hexdigest()[:10]}"
                vector_text = "[" + ",".join(f"{float(x):.9g}" for x in values) + "]"
                chunk_params.append({"id":chunk_id,"doc":document_id,"idx":index,"content":chunk,
                    "tokens":max(1,len(chunk)//2),"model":body.embedding_model,
                    "version":body.embedding_version,"vector":vector_text,
                    "metadata":_json_param({"data_type":submission.get("data_type"),
                                               "topic_text":submission.get("topic_text"),
                                               "side":submission.get("side")})})

            with db.transaction() as conn:
                conn.execute(text(f"""INSERT INTO {TABLE_RAG_DOCUMENTS}
                (document_id,submission_id,title,data_type,topic_text,side,source_note,content_sha256,
                 status,embedding_model,embedding_version,indexed_at)
                VALUES(:doc,:submission,:title,:type,:topic,:side,:source,:sha,'active',:model,:version,:now)
                ON CONFLICT(document_id) DO UPDATE SET title=EXCLUDED.title,data_type=EXCLUDED.data_type,
                 topic_text=EXCLUDED.topic_text,side=EXCLUDED.side,source_note=EXCLUDED.source_note,
                 content_sha256=EXCLUDED.content_sha256,status='active',embedding_model=EXCLUDED.embedding_model,
                 embedding_version=EXCLUDED.embedding_version,indexed_at=EXCLUDED.indexed_at"""),
                {"doc":document_id,"submission":submission["id"],"title":submission.get("title"),
                 "type":submission.get("data_type"),"topic":submission.get("topic_text"),
                 "side":submission.get("side"),"source":submission.get("source_note"),"sha":content_sha,
                 "model":body.embedding_model,"version":body.embedding_version,"now":now})
                conn.execute(text(f"DELETE FROM {TABLE_RAG_CHUNKS} WHERE document_id=:doc"), {"doc": document_id})
                if chunk_params:
                    conn.execute(text(f"""INSERT INTO {TABLE_RAG_CHUNKS}
                    (chunk_id,document_id,chunk_index,content_text,token_estimate,embedding_model,
                     embedding_version,embedding,metadata_json)
                    VALUES(:id,:doc,:idx,:content,:tokens,:model,:version,
                     CAST(:vector AS vector),CAST(:metadata AS jsonb))"""), chunk_params)
            indexed += len(chunks); documents_indexed += 1
            if is_new_document:
                active_document_count += 1
    _audit(db, user, "rag_reindexed", "rag_index", body.embedding_version,
           {"documents": documents_indexed, "chunks": indexed, "has_more": has_more})
    return {"ok":True,"documents":documents_indexed,"chunks":indexed,"has_more":has_more,
            "embedding_model":body.embedding_model,"embedding_version":body.embedding_version}
