"""Direct-HTML data API for the AI training workspace."""
import base64
import csv
import hashlib
import io
import json
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from api.pagination import PAGE_SIZE, bounds, json_safe, payload, scalar_count

from schema import (
    CREATE_AI_DATASET_SNAPSHOTS, CREATE_AI_DATASET_SNAPSHOT_ITEMS,
    CREATE_AI_EVAL_CASES, CREATE_AI_EVAL_RUNS, CREATE_AI_MODEL_VERSIONS,
    CREATE_AI_TRAINING_AUDIT, CREATE_RAG_CHUNKS, CREATE_RAG_DOCUMENTS,
    CREATE_LLM_TRAINING_SUBMISSIONS, CREATE_TTS_LEXICON, CREATE_TTS_SCRIPTS,
    CREATE_TTS_VOICE_CONSENTS, CREATE_TTS_VOICE_RECORDINGS,
    TABLE_AI_DATASET_SNAPSHOTS, TABLE_AI_DATASET_SNAPSHOT_ITEMS,
    TABLE_AI_EVAL_CASES, TABLE_AI_EVAL_RUNS, TABLE_AI_MODEL_VERSIONS,
    TABLE_AI_TRAINING_AUDIT, TABLE_RAG_CHUNKS, TABLE_RAG_DOCUMENTS,
    TABLE_LLM_TRAINING_SUBMISSIONS, TABLE_TTS_LEXICON, TABLE_TTS_SCRIPTS,
    TABLE_TTS_VOICE_CONSENTS, TABLE_TTS_VOICE_RECORDINGS,
)
from prompts import (
    TTS_COVERAGE_SYSTEM_PROMPT, TTS_REGENERATE_SYSTEM_PROMPT,
    build_tts_coverage_prompt, build_tts_regenerate_prompt,
)

router = APIRouter(prefix="/api/ai-training", tags=["ai-training"])
CONSENT_VERSION = "tts_voice_v2_2026_07"
CONSENT_TEXT = "我同意聖呂中辯收集本人錄音，用作內部廣東話 TTS、讀音檢查及建立可生成近似本人聲音的語音模型；資料可交由受控雲端 GPU／AI 服務處理，但不會公開原始錄音或 checkpoint。我可撤回未來使用；撤回後錄音不再納入新資料集，使用過該錄音的 checkpoint會被停止部署並安排排除資料重訓。未成年錄音者須另有家長／學校授權。"
ALLOWED_KEY, REVIEWERS_KEY = "tts_recording_allowed_users", "tts_recording_reviewers"
MANUSCRIPT_SEGMENT_MAX_LEN = 35
ADMIN_RECORDING_PAGE_SIZE = 5
SUPPORTED_AUDIO_MIMES = {"audio/webm", "audio/mp4", "audio/mpeg", "audio/wav", "audio/ogg"}
MAX_AUDIO_BYTES = 10 * 1024 * 1024

# Kept here (rather than in the browser) so every fresh database is usable.
DEFAULT_SCRIPT_BANK = [
    ("free_001","Free De","你呢個講法最大問題係冇證明因果關係。"),("free_002","Free De","我想追問你，政策成本由邊個承擔？"),("free_003","Free De","如果你承認有例外，咁你個標準其實已經唔穩陣。"),
    ("mock_001","Mock","多謝主席，各位評判、各位同學，今日我方立場非常清晰。"),("mock_002","Mock","總結我方三個重點，第一係可行性，第二係公平性，第三係長遠影響。"),("mock_003","Mock","對方一直避開核心問題，就係制度本身會否製造更大不公。"),
    ("question_001","追問","你可唔可以畀一個具體例子，證明呢個方法真係有效？"),("question_002","追問","如果資源有限，你會優先幫邊一類人，點解？"),("rebuttal_001","反駁","對方將相關性講成因果性，呢個係明顯嘅邏輯跳步。"),("rebuttal_002","反駁","你嘅例子只係個別情況，唔足以支持一個普遍政策。"),
    ("numbers_001","數字讀法","二零二六年，我哋預計有百分之三十五嘅學生受影響。"),("numbers_002","數字讀法","如果滿分係一百分，呢個方案最多只可以攞六十五分。"),("terms_002","術語/英文","自由辯論最重要唔係講得長，而係追問要準、反駁要快。"),("feedback_001","評語","你頭先嘅主線清楚，但回應對方追問時可以再直接啲。"),("feedback_002","評語","整體台風穩陣，不過個別位收得太急，畀評判嘅印象會扣分。"),("feedback_003","評語","你嘅論點有數據支持，值得欣賞，下次記得同時交代數據嚟源。"),
    ("numbers_003","數字讀法","呢場比賽最後比數係四十八比五十二，我哋以四分之差落敗。"),("numbers_004","數字讀法","報名人數由二百三十七人升到一千零五人，升幅超過三倍。"),("numbers_005","數字讀法","第一、第二同第三名分別攞到九十五、八十八同八十一分。"),("numbers_006","數字讀法","聯絡電話係二五二八，三六七九，有問題可以隨時致電查詢。"),
    ("date_001","日期時間","決賽定於二零二六年七月十九號，星期日下晝三點半喺禮堂舉行。"),("date_002","日期時間","報名截止日期係下個月八號，逾期恕不受理，請各位隊伍準時提交。"),("date_003","日期時間","每節限時四分三十秒，夠三分鐘會響第一次鈴，夠鐘就響兩下。"),
    ("terms_004","術語/英文","OK，我哋而家開始 free debate 環節，計時交由 timer 負責。"),("terms_005","術語/英文","呢個 argument 嘅 logic 有斷層，你需要補返個 example 先撐得住。"),("terms_006","術語/英文","AI 辯論易會用 GPT 同 Gemini 兩個模型，分別做評語同即時回應。"),
    ("poly_001","多音字","佢嘅行為好有問題，但銀行嗰行細字就冇人為意。"),("poly_002","多音字","呢點好重要，所以我哋要重新檢視成個制度嘅設計。"),("poly_003","多音字","校長話長遠嚟講，同學嘅成長比一時嘅長短更加關鍵。"),("poly_004","多音字","呢部分嘅分數唔高，但佢反映嘅身分認同問題就唔可以忽視。"),("poly_005","多音字","佢好奇點解一個好人會做出咁嘅選擇，我覺得值得深究。"),("poly_006","多音字","快樂同音樂表面相似，實際上係兩種完全唔同嘅體驗。"),
    ("tone_001","聲調覆蓋","詩、史、試、時、市、事，呢六個字聲調各有不同，要讀得分明。"),("tone_002","聲調覆蓋","三分鐘、九十九分、五十蚊、一百萬，數字讀音要清清楚楚。"),("prosody_001","長句韻律","各位評判、各位老師、各位同學，多謝大家喺一個咁繁忙嘅星期日，抽時間出席今日呢場意義重大嘅辯論比賽。"),("prosody_002","長句韻律","我方認為，無論係從公平、效率，定係從長遠嘅社會影響嚟睇，呢個政策都應該經過更充分嘅諮詢先至推行。"),("prosody_003","長句韻律","如果我哋只係睇短期數字，好容易忽略咗背後真正需要幫助嘅人，而呢啲人往往就係最冇聲音嗰班。"),
]


class ConsentBody(BaseModel):
    agreed: bool
    # The page presents CONSENT_TEXT as one indivisible agreement. Defaults keep
    # older direct-HTML clients compatible while the stored columns remain explicit.
    voice_cloning_confirmed: bool = True
    cloud_processing_confirmed: bool = True
    is_minor: bool = False
    guardian_confirmed: bool = False


class RecordingBody(BaseModel):
    script_id: str
    audio_base64: str
    mime_type: str = "audio/webm"
    duration_seconds: int = 0
    manual_review: bool = False
    ai_review: dict | None = None


class LlmBody(BaseModel):
    data_type: str
    side: str = "不適用"
    title: str = ""
    topic_text: str = ""
    content_text: str
    source_note: str = ""
    anonymized: bool = False
    permission_confirmed: bool = False
    manual_review: bool = False


class LexiconBody(BaseModel):
    lexicon_id: str = ""
    term: str
    reading: str
    jyutping: str = ""
    example: str = ""
    note: str = ""
    category: str = ""


class ScriptBody(BaseModel):
    script_id: str = ""
    category: str
    text: str
    sort_order: int = 0


class ReviewBody(BaseModel):
    status: str
    note: str = ""


class ActiveBody(BaseModel):
    active: bool


class SuggestionsBody(BaseModel):
    items: list[dict]
    deactivate_ids: list[str] = Field(default_factory=list)


class ManuscriptBody(BaseModel):
    title: str
    text: str
    category: str = "完整稿"
    active: bool = True


class SnapshotBody(BaseModel):
    dataset_kind: str
    speaker_user_id: str = ""


class ModelMetricsBody(BaseModel):
    metrics: dict = Field(default_factory=dict)
    status: str | None = None


class ModelRegisterBody(BaseModel):
    model_id: str
    model_type: str
    base_model: str
    dataset_snapshot_id: str | None = None
    artifact_uri: str = ""
    config: dict = Field(default_factory=dict)


class RagReindexBody(BaseModel):
    embedding_model: str = "gemini-embedding-2"
    embedding_version: str = "gemini-embedding-2@2026-04"


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
    user = _require_committee_user(request); db = get_vote_db()
    db.execute(CREATE_TTS_VOICE_CONSENTS); db.execute(CREATE_TTS_VOICE_RECORDINGS)
    db.execute(CREATE_TTS_SCRIPTS); db.execute(CREATE_TTS_LEXICON); db.execute(CREATE_LLM_TRAINING_SUBMISSIONS)
    for ddl in (
        CREATE_AI_DATASET_SNAPSHOTS, CREATE_AI_DATASET_SNAPSHOT_ITEMS,
        CREATE_AI_MODEL_VERSIONS, CREATE_AI_EVAL_CASES, CREATE_AI_EVAL_RUNS,
        CREATE_RAG_DOCUMENTS, CREATE_RAG_CHUNKS, CREATE_AI_TRAINING_AUDIT,
    ):
        db.execute(ddl)
    eval_path = Path(__file__).resolve().parents[1] / "assets" / "ai_eval_cases_v0.json"
    if eval_path.exists():
        for case in json.loads(eval_path.read_text(encoding="utf-8")):
            db.execute(f"""INSERT INTO {TABLE_AI_EVAL_CASES}
                (case_id,task_type,title,input_json,rubric_json,reference_text,is_active)
                VALUES(:id,:task,:title,CAST(:input AS jsonb),CAST(:rubric AS jsonb),:reference,TRUE)
                ON CONFLICT(case_id) DO NOTHING""",
                {"id":case["case_id"],"task":case["task_type"],"title":case["title"],
                 "input":_json_param(case["input"]),"rubric":_json_param(case["rubric"]),
                 "reference":case.get("reference_text")})
    for ddl in (
        f"ALTER TABLE {TABLE_TTS_VOICE_CONSENTS} ADD COLUMN IF NOT EXISTS voice_cloning_confirmed BOOLEAN DEFAULT FALSE",
        f"ALTER TABLE {TABLE_TTS_VOICE_CONSENTS} ADD COLUMN IF NOT EXISTS cloud_processing_confirmed BOOLEAN DEFAULT FALSE",
        f"ALTER TABLE {TABLE_TTS_VOICE_CONSENTS} ADD COLUMN IF NOT EXISTS is_minor BOOLEAN DEFAULT FALSE",
        f"ALTER TABLE {TABLE_TTS_VOICE_CONSENTS} ADD COLUMN IF NOT EXISTS guardian_confirmed BOOLEAN DEFAULT FALSE",
        f"ALTER TABLE {TABLE_TTS_VOICE_RECORDINGS} ADD COLUMN IF NOT EXISTS audio_sha256 TEXT",
        f"ALTER TABLE {TABLE_TTS_VOICE_RECORDINGS} ADD COLUMN IF NOT EXISTS measured_duration_seconds NUMERIC",
        f"ALTER TABLE {TABLE_TTS_VOICE_RECORDINGS} ADD COLUMN IF NOT EXISTS sample_rate_hz INTEGER",
        f"ALTER TABLE {TABLE_TTS_VOICE_RECORDINGS} ADD COLUMN IF NOT EXISTS channel_count INTEGER",
        f"ALTER TABLE {TABLE_TTS_VOICE_RECORDINGS} ADD COLUMN IF NOT EXISTS detected_format TEXT",
    ):
        db.execute(ddl)
    count = db.query(f"SELECT COUNT(*) AS n FROM {TABLE_TTS_SCRIPTS}")
    if count.empty or int(count.iloc[0]["n"] or 0) == 0:
        for order, (script_id, category, value) in enumerate(DEFAULT_SCRIPT_BANK):
            db.execute(f"""INSERT INTO {TABLE_TTS_SCRIPTS}(script_id,category,text,is_active,sort_order,created_by)
                VALUES(:id,:category,:text,TRUE,:sort,'system') ON CONFLICT(script_id) DO NOTHING""",
                {"id":script_id,"category":category,"text":value,"sort":order})
    return user, db


def _users(db, key):
    rows = db.query("SELECT value FROM system_config WHERE key=:key", {"key": key})
    if rows.empty or not rows.iloc[0]["value"]:
        return []
    try: return [str(x) for x in json.loads(rows.iloc[0]["value"])]
    except Exception: return [x.strip() for x in str(rows.iloc[0]["value"]).split(",") if x.strip()]


def _is_admin(db, user): return user in _users(db, REVIEWERS_KEY)


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
                capture_output=True, text=True, timeout=10, check=False,
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
    if not 1 <= duration <= 60.5:
        raise HTTPException(400, "錄音實際長度必須為 1 至 60 秒")
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


def _audio_payload(body):
    try:
        audio = base64.b64decode(body.audio_base64, validate=True)
    except Exception as exc:
        raise HTTPException(400, "錄音資料無法讀取") from exc
    mime = (body.mime_type or "").split(";", 1)[0].lower()
    if mime not in SUPPORTED_AUDIO_MIMES:
        raise HTTPException(400, "錄音格式不受支援")
    if len(audio) < 1000:
        raise HTTPException(400, "錄音太短或沒有聲音資料")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(400, "錄音超過 10MB")
    if not 1 <= int(body.duration_seconds or 0) <= 60:
        raise HTTPException(400, "錄音長度必須為 1 至 60 秒")
    return audio, mime, _probe_audio(audio, mime, body.duration_seconds)


def _audit(db, actor, action, target_type, target_id="", details=None):
    db.execute(
        f"INSERT INTO {TABLE_AI_TRAINING_AUDIT}(actor_user_id,action,target_type,target_id,details_json) "
        "VALUES(:actor,:action,:target_type,:target_id,CAST(:details AS jsonb))",
        {"actor": actor, "action": action, "target_type": target_type,
         "target_id": str(target_id or ""),
         "details": json.dumps(details or {}, ensure_ascii=False, default=str)},
    )


def _json_param(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


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


def _audio_ext(mime):
    return {"audio/webm": "webm", "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/ogg": "ogg"}.get(mime, "webm")


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
    consent = db.query(f"SELECT 1 FROM {TABLE_TTS_VOICE_CONSENTS} WHERE user_id=:user AND consent_version=:version AND withdrawn_at IS NULL", {"user": user, "version": CONSENT_VERSION})
    scripts = _rows(db.query(f"SELECT script_id AS id,category,text,is_active,sort_order,COALESCE(script_type,'short') AS script_type,manuscript_id,manuscript_title FROM {TABLE_TTS_SCRIPTS} WHERE is_active=TRUE ORDER BY category,sort_order,script_id"))
    lexicon = []
    # Recorder selection only needs one status per script; full history is paged below.
    mine = _rows(db.query(f"SELECT DISTINCT ON (script_id) id,script_id,status,created_at FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE speaker_user_id=:user ORDER BY script_id,created_at DESC", {"user":user}))
    llm = []
    plan_path = Path(__file__).resolve().parents[1] / "assets" / "tts_rd_plan.md"
    try: rd_plan = plan_path.read_text(encoding="utf-8").strip()
    except OSError: rd_plan = "研發計劃書暫時未能讀取。"
    result = {"user_id": user, "is_allowed": allowed, "is_admin": admin, "consented": not consent.empty, "consent_text": CONSENT_TEXT, "rd_plan":rd_plan, "scripts": scripts, "lexicon":lexicon, "my_recordings":mine, "my_llm":llm}
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
        f"SELECT speaker_user_id,audio_data,mime_type FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE id=:id",
        {"id": record_id},
    )
    if row.empty:
        raise HTTPException(404, "找不到錄音")
    owner = str(row.iloc[0]["speaker_user_id"] or "").strip()
    if owner != str(user).strip() and not _is_admin(db, user):
        raise HTTPException(403, "你無權播放此錄音")
    audio = row.iloc[0]["audio_data"]
    if isinstance(audio, memoryview):
        audio = audio.tobytes()
    return Response(content=bytes(audio), media_type=row.iloc[0]["mime_type"] or "audio/webm")


@router.post("/consent")
def consent(body: ConsentBody, request: Request):
    user, db = _ctx(request)
    if not body.agreed: raise HTTPException(400, "必須同意錄音用途及授權安排")
    if not body.voice_cloning_confirmed or not body.cloud_processing_confirmed:
        raise HTTPException(400, "必須確認聲線模型及受控雲端處理用途")
    if body.is_minor and not body.guardian_confirmed:
        raise HTTPException(400, "未成年錄音者必須確認已取得家長／學校授權")
    db.execute(f"""INSERT INTO {TABLE_TTS_VOICE_CONSENTS}
                   (user_id,consent_version,consent_text,consented_at,withdrawn_at,
                    voice_cloning_confirmed,cloud_processing_confirmed,is_minor,guardian_confirmed)
                   VALUES(:user,:version,:consent,:now,NULL,TRUE,TRUE,:minor,:guardian)
                   ON CONFLICT(user_id,consent_version) DO UPDATE SET
                    consent_text=EXCLUDED.consent_text,consented_at=EXCLUDED.consented_at,
                    withdrawn_at=NULL,voice_cloning_confirmed=TRUE,cloud_processing_confirmed=TRUE,
                    is_minor=EXCLUDED.is_minor,guardian_confirmed=EXCLUDED.guardian_confirmed""",
               {"user":user,"version":CONSENT_VERSION,"consent":CONSENT_TEXT,"now":datetime.now(),
                "minor":body.is_minor,"guardian":body.guardian_confirmed})
    _audit(db, user, "consent_granted", "tts_consent", CONSENT_VERSION,
           {"is_minor": body.is_minor, "guardian_confirmed": body.guardian_confirmed})
    return {"ok": True}


@router.delete("/consent")
def withdraw(request: Request):
    user, db = _ctx(request)
    db.execute(f"UPDATE {TABLE_TTS_VOICE_CONSENTS} SET withdrawn_at=:now WHERE user_id=:user AND consent_version=:version AND withdrawn_at IS NULL", {"user":user,"version":CONSENT_VERSION,"now":datetime.now()})
    db.execute(f"UPDATE {TABLE_TTS_VOICE_RECORDINGS} SET status='withdrawn' WHERE speaker_user_id=:user AND status!='withdrawn'", {"user":user})
    db.execute(f"""UPDATE {TABLE_AI_DATASET_SNAPSHOTS} SET status='withdrawn'
                   WHERE dataset_kind='tts' AND speaker_user_id=:user AND status IN ('draft','ready')""", {"user": user})
    db.execute(f"""UPDATE {TABLE_AI_MODEL_VERSIONS} SET status='blocked',updated_at=:now
                   WHERE dataset_snapshot_id IN (SELECT snapshot_id FROM {TABLE_AI_DATASET_SNAPSHOTS}
                   WHERE dataset_kind='tts' AND speaker_user_id=:user) AND status!='retired'""",
               {"user": user, "now": datetime.now()})
    _audit(db, user, "consent_withdrawn", "tts_consent", CONSENT_VERSION)
    return {"ok":True}


@router.post("/recordings")
def recording(body: RecordingBody, request: Request):
    user, db = _ctx(request)
    if user not in _users(db, ALLOWED_KEY): raise HTTPException(403, "你未獲加入 TTS 錄音收集名單")
    active = db.query(f"SELECT 1 FROM {TABLE_TTS_VOICE_CONSENTS} WHERE user_id=:user AND consent_version=:version AND withdrawn_at IS NULL", {"user":user,"version":CONSENT_VERSION})
    if active.empty: raise HTTPException(400, "請先確認錄音同意")
    script = db.query(f"SELECT text FROM {TABLE_TTS_SCRIPTS} WHERE script_id=:id AND is_active=TRUE", {"id":body.script_id})
    if script.empty: raise HTTPException(404, "錄音句子不存在或已停用")
    audio, mime, probe = _audio_payload(body)
    duplicate = db.query(
        f"SELECT 1 FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE speaker_user_id=:user AND script_id=:script "
        "AND status IN ('pending','accepted') LIMIT 1",
        {"user": user, "script": body.script_id},
    )
    if not duplicate.empty: raise HTTPException(409, "此句已有待審核或已接受錄音，請勿重複提交")
    review = body.ai_review or {}
    provider_review = review.get("review") if isinstance(review.get("review"), dict) else {}
    if not body.manual_review and not (
        review.get("ok") is True and review.get("status") == "passed"
        and provider_review.get("passed") is True and provider_review.get("matches_prompt") is True
    ):
        raise HTTPException(400, "錄音未通過 AI 音質及稿件一致性檢查")
    review_status = "error" if body.manual_review else "passed"
    db.execute(f"""INSERT INTO {TABLE_TTS_VOICE_RECORDINGS}
                   (speaker_user_id,script_id,prompt_text,audio_data,mime_type,file_ext,size_bytes,
                    duration_seconds,audio_sha256,measured_duration_seconds,sample_rate_hz,
                    channel_count,detected_format,ai_review_status,ai_review_json,ai_transcript,status,created_at)
                   VALUES(:user,:script,:prompt,:audio,:mime,:ext,:size,:duration,:sha,:measured,
                    :sample_rate,:channels,:detected,:review_status,:review_json,:transcript,'pending',:now)""",
               {"user":user,"script":body.script_id,"prompt":script.iloc[0]["text"],"audio":audio,
                "mime":mime,"ext":_audio_ext(mime),"size":len(audio),"duration":int(body.duration_seconds),
                "sha":probe["sha256"],"measured":probe["duration"],"sample_rate":probe["sample_rate"],
                "channels":probe["channels"],"detected":probe["format"],"review_status":review_status,
                "review_json":json.dumps(body.ai_review or {},ensure_ascii=False),
                "transcript":str((body.ai_review or {}).get("transcript") or ""),"now":datetime.now()})
    return {"ok":True, "message":"錄音已提交，等待人工審核。"}


@router.post("/recordings/quality-check")
async def recording_quality_check(body: RecordingBody, request: Request):
    """Run the deterministic gate before the provider-assisted/manual review.

    Manual review may bypass a provider outage, but never these deterministic
    format, duration and byte-size safeguards.
    """
    user, db = _ctx(request)
    if user not in _users(db, ALLOWED_KEY): raise HTTPException(403, "你未獲加入 TTS 錄音收集名單")
    audio, mime, _probe = _audio_payload(body)
    consent = db.query(f"SELECT 1 FROM {TABLE_TTS_VOICE_CONSENTS} WHERE user_id=:user AND consent_version=:version AND withdrawn_at IS NULL", {"user": user, "version": CONSENT_VERSION})
    if consent.empty: raise HTTPException(400, "請先確認錄音同意")
    script = db.query(f"SELECT text FROM {TABLE_TTS_SCRIPTS} WHERE script_id=:id AND is_active=TRUE", {"id": body.script_id})
    if script.empty: raise HTTPException(404, "錄音句子不存在或已停用")
    duplicate = db.query(
        f"SELECT 1 FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE speaker_user_id=:user AND script_id=:script "
        "AND status IN ('pending','accepted') LIMIT 1",
        {"user": user, "script": body.script_id},
    )
    if not duplicate.empty: raise HTTPException(409, "此句已有待審核或已接受錄音，毋須再作 AI 檢查")
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
    payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": body.audio_base64}}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    try:
        async with httpx.AsyncClient(timeout=60) as client: response = await client.post(url, json=payload)
        response.raise_for_status(); response_data = response.json(); raw = response_data["candidates"][0]["content"]["parts"][0]["text"]
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
    return {"ok": passed, "status": "passed" if passed else "failed", "problems": review.get("problems") or [],
            "transcript": review.get("transcript") or "", "review": review,
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
            async with httpx.AsyncClient(timeout=60) as client: response=await client.post(url,json={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"responseMimeType":"application/json","temperature":0}})
            response.raise_for_status(); response_data=response.json(); review=json.loads(response_data["candidates"][0]["content"]["parts"][0]["text"]); review["fingerprint"]=fingerprint
            _log_ai(user, db, "llm_review", True, response_data=response_data)
        except Exception as exc:
            _log_ai(user, db, "llm_review", False, error=str(exc))
            raise HTTPException(503,"AI 預檢暫時未能完成；可確認後選擇略過 AI 檢查") from exc
        if not bool(review.get("passed")): return {"ok":False,"status":"failed","message":review.get("reason") or "AI 預檢未通過", "review":review}
    db.execute(f"""INSERT INTO {TABLE_LLM_TRAINING_SUBMISSIONS}(submitted_by,data_type,title,topic_text,side,content_text,source_note,anonymized,permission_confirmed,ai_review_status,ai_review_json,status,created_at)
                   VALUES(:user,:type,:title,:topic,:side,:content,:source,TRUE,TRUE,:ai_status,:review,'pending',:now)""", {"user":user,"type":body.data_type,"title":body.title.strip() or None,"topic":body.topic_text.strip() or None,"side":body.side,"content":body.content_text.strip(),"source":body.source_note.strip() or None,"ai_status":review_status,"review":json.dumps(review,ensure_ascii=False),"now":datetime.now()})
    return {"ok":True,"message":"資料已提交，等待人工審核。"}


@router.delete("/llm/{submission_id}")
def withdraw_llm(submission_id: int, request: Request):
    user, db = _ctx(request)
    row=db.query(f"SELECT status FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE id=:id AND submitted_by=:user",{"id":submission_id,"user":user})
    if row.empty: raise HTTPException(404,"找不到提交")
    if row.iloc[0]["status"] not in ("pending", "accepted"): raise HTTPException(409,"只有待審核或已接受資料可以撤回")
    db.execute(f"UPDATE {TABLE_LLM_TRAINING_SUBMISSIONS} SET status='withdrawn' WHERE id=:id",{"id":submission_id})
    db.execute(f"UPDATE {TABLE_RAG_DOCUMENTS} SET status='withdrawn' WHERE submission_id=:id", {"id": submission_id})
    _audit(db, user, "submission_withdrawn", "llm_submission", submission_id)
    return {"ok":True}


@router.post("/lexicon")
def lexicon(body: LexiconBody, request: Request):
    user, db = _ctx(request)
    if not _is_admin(db,user): raise HTTPException(403,"只有管理員可修改讀音字典")
    term, reading = body.term.strip(), body.reading.strip()
    if not term or not reading: raise HTTPException(400, "詞語與讀法都必須填寫")
    lid = body.lexicon_id.strip()
    if not lid:
        existing=_rows(db.query(f"SELECT lexicon_id FROM {TABLE_TTS_LEXICON} WHERE lexicon_id LIKE 'lex_%'")); nums=[int(m.group(1)) for x in existing if (m:=re.match(r"lex_(\\d+)$",str(x['lexicon_id'])))]
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
        ORDER BY s.category,s.script_id"""))
    grouped = {}
    for row in rows:
        item = grouped.setdefault(row["script_id"], {"category":row["category"], "text":row["text"], "accepted":0, "pending":0})
        if row.get("status") in ("accepted", "pending"): item[row["status"]] = int(row.get("n") or 0)
    summary = "\n".join(f"[{x['category']}] {sid}｜accepted={x['accepted']}｜pending={x['pending']}｜{x['text']}" for sid,x in grouped.items()) or "（句庫為空）"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    body = {"systemInstruction":{"parts":[{"text":TTS_COVERAGE_SYSTEM_PROMPT}]}, "contents":[{"parts":[{"text":build_tts_coverage_prompt(summary)}]}], "generationConfig":{"responseMimeType":"application/json","temperature":.4}}
    try:
        async with httpx.AsyncClient(timeout=60) as client: response = await client.post(url, json=body)
        response.raise_for_status(); response_data=response.json()
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
    scripts = _rows(db.query(f"SELECT script_id AS id,category,text,is_active,script_type,manuscript_id,manuscript_title,sort_order FROM {TABLE_TTS_SCRIPTS} ORDER BY category,sort_order,script_id"))
    lexicon = _rows(db.query(f"SELECT lexicon_id AS id,term,reading,is_active,category FROM {TABLE_TTS_LEXICON} ORDER BY category,term"))
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
    active = _rows(db.query(f"SELECT script_id,script_type,manuscript_id FROM {TABLE_TTS_SCRIPTS} WHERE is_active=TRUE"))
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
    for item in body.items[:50]:
        category=str(item.get("category") or "AI 建議").strip(); value=str(item.get("text") or "").strip()
        if not value: continue
        sid=f"ai_{int(datetime.now().timestamp()*1000)}_{added:02d}"
        db.execute(f"INSERT INTO {TABLE_TTS_SCRIPTS}(script_id,category,text,is_active,sort_order,created_by,updated_at) VALUES(:id,:cat,:text,TRUE,0,:user,:now)",{"id":sid,"cat":category,"text":value,"user":user,"now":datetime.now()}); added+=1
    locked = _rows(db.query(f"SELECT DISTINCT script_id FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE status IN ('pending','accepted')"))
    locked_ids = {str(x["script_id"]) for x in locked}
    deactivate = [str(x) for x in body.deactivate_ids[:50] if str(x) not in locked_ids]
    deactivated = 0
    if deactivate:
        deactivated = db.execute_count(f"UPDATE {TABLE_TTS_SCRIPTS} SET is_active=FALSE,updated_at=:now WHERE script_id=ANY(:ids) AND is_active=TRUE", {"ids":deactivate,"now":datetime.now()})
    return {"ok":True,"added":added,"deactivated":deactivated}


@router.post("/regenerate-suggestions")
async def regenerate_suggestions(request: Request):
    user, db = _admin(request)
    from deploy.proxy import _get_proxy_secret
    key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not key:
        _log_ai(user, db, "tts_script_analysis", False, error="GEMINI_API_KEY missing")
        raise HTTPException(503, "未設定 GEMINI_API_KEY，暫時未能重出句庫")
    rows = _rows(db.query(f"SELECT script_id AS id,category,text FROM {TABLE_TTS_SCRIPTS} WHERE is_active=TRUE ORDER BY category,sort_order"))
    locked_rows = _rows(db.query(f"SELECT DISTINCT script_id FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE status IN ('pending','accepted')"))
    locked_ids = {str(x["script_id"]) for x in locked_rows}
    locked = "\n".join(f"[{x['category']}] {x['id']}｜{x['text']}" for x in rows if str(x["id"]) in locked_ids) or "（暫時冇已錄音句子）"
    unlocked = "\n".join(f"[{x['category']}] {x['id']}｜{x['text']}" for x in rows if str(x["id"]) not in locked_ids) or "（暫時冇未錄音句子）"
    prompt = build_tts_regenerate_prompt(locked, unlocked)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    payload = {"systemInstruction":{"parts":[{"text":TTS_REGENERATE_SYSTEM_PROMPT}]}, "contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": .5}}
    try:
        async with httpx.AsyncClient(timeout=60) as client: response = await client.post(url, json=payload)
        response.raise_for_status(); response_data=response.json(); raw=response_data["candidates"][0]["content"]["parts"][0]["text"]
        plan=json.loads(raw)
        if not isinstance(plan, dict): raise ValueError("AI 回覆格式不正確")
        plan["deactivate_candidates"] = [x for x in (plan.get("deactivate_candidates") or []) if str(x.get("script_id")) not in locked_ids]
        _log_ai(user, db, "tts_script_analysis", True, response_data=response_data)
    except Exception as exc:
        _log_ai(user, db, "tts_script_analysis", False, error=str(exc))
        raise HTTPException(502, f"AI 重出句庫失敗：{str(exc)[:160]}") from exc
    return {"plan": plan}


@router.get("/export/recordings.zip")
def export_recordings(request: Request, speaker: str = ""):
    _user, db = _admin(request)
    where, params = "status='accepted'", {}
    if speaker.strip(): where += " AND speaker_user_id=:speaker"; params["speaker"] = speaker.strip()
    rows = _rows(db.query(f"""SELECT r.id,r.speaker_user_id,r.script_id,r.prompt_text,r.audio_data,
        r.mime_type,r.file_ext,r.audio_sha256,
        COALESCE(r.measured_duration_seconds,r.duration_seconds) AS duration_seconds,
        r.sample_rate_hz,r.channel_count,r.detected_format,r.ai_transcript,
        s.manuscript_id,s.manuscript_title,s.category
        FROM {TABLE_TTS_VOICE_RECORDINGS} r LEFT JOIN {TABLE_TTS_SCRIPTS} s ON s.script_id=r.script_id
        WHERE {where.replace('status', 'r.status').replace('speaker_user_id', 'r.speaker_user_id')}
        ORDER BY r.id""", params))
    output = io.BytesIO(); manifest = []
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            audio = row.pop("audio_data"); audio = audio.tobytes() if isinstance(audio, memoryview) else bytes(audio)
            ext = re.sub(r"[^a-z0-9]", "", str(row.get("file_ext") or "webm").lower()) or "webm"
            name = f"audio/{row['id']}_{row['script_id']}.{ext}"; archive.writestr(name, audio)
            manifest.append({**row, "file": name})
        csv_buffer=io.StringIO(); fields=["id","speaker_user_id","script_id","prompt_text","mime_type","file_ext","file",
            "audio_sha256","duration_seconds","sample_rate_hz","channel_count","detected_format","ai_transcript",
            "manuscript_id","manuscript_title","category"]
        writer=csv.DictWriter(csv_buffer,fieldnames=fields); writer.writeheader()
        for item in manifest: writer.writerow({key:item.get(key,"") for key in fields})
        archive.writestr("metadata.csv", csv_buffer.getvalue().encode("utf-8-sig"))
    output.seek(0)
    return StreamingResponse(output, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=tts-accepted.zip"})


@router.get("/export/llm.jsonl")
def export_llm(request: Request):
    _user, db = _admin(request)
    rows = _rows(db.query(f"SELECT id,submitted_by,data_type,title,topic_text,side,content_text,source_note,created_at FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE status='accepted' ORDER BY id"))
    content = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    return Response(content=content, media_type="application/x-ndjson; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=llm-accepted.jsonl"})


@router.post("/recordings/{record_id}/review")
def review_recording(record_id:int, body:ReviewBody, request:Request):
    user,db=_ctx(request)
    if not _is_admin(db,user): raise HTTPException(403,"只有管理員可審核")
    if body.status not in ('accepted','rejected'): raise HTTPException(400,"狀態不正確")
    row = db.query(f"SELECT status,script_id FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE id=:id", {"id": record_id})
    if row.empty: raise HTTPException(404, "找不到錄音")
    if row.iloc[0]["status"] != "pending": raise HTTPException(409, "只有待審核錄音可以更新")
    db.execute(f"UPDATE {TABLE_TTS_VOICE_RECORDINGS} SET status=:status,review_note=:note,reviewed_by=:user,reviewed_at=:now WHERE id=:id", {"status":body.status,"note":body.note,"user":user,"now":datetime.now(),"id":record_id})
    if body.status == "rejected":
        db.execute(f"UPDATE {TABLE_TTS_SCRIPTS} SET is_active=TRUE,updated_at=:now WHERE script_id=:script",
                   {"script": row.iloc[0]["script_id"], "now": datetime.now()})
    _audit(db, user, "recording_reviewed", "tts_recording", record_id, {"status": body.status})
    return {"ok":True}


@router.post("/llm/{submission_id}/review")
def review_llm(submission_id:int, body:ReviewBody, request:Request):
    user,db=_ctx(request)
    if not _is_admin(db,user): raise HTTPException(403,"只有管理員可審核")
    if body.status not in ('accepted','rejected'): raise HTTPException(400,"狀態不正確")
    row = db.query(f"SELECT status FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE id=:id", {"id": submission_id})
    if row.empty: raise HTTPException(404, "找不到提交")
    if row.iloc[0]["status"] != "pending": raise HTTPException(409, "只有待審核資料可以更新")
    db.execute(f"UPDATE {TABLE_LLM_TRAINING_SUBMISSIONS} SET status=:status,review_note=:note,reviewed_by=:user,reviewed_at=:now WHERE id=:id", {"status":body.status,"note":body.note,"user":user,"now":datetime.now(),"id":submission_id})
    if body.status == "rejected":
        db.execute(f"UPDATE {TABLE_RAG_DOCUMENTS} SET status='archived' WHERE submission_id=:id", {"id": submission_id})
    _audit(db, user, "submission_reviewed", "llm_submission", submission_id, {"status": body.status})
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
      LEFT JOIN {TABLE_TTS_VOICE_CONSENTS} c ON c.user_id=r.speaker_user_id
      GROUP BY r.speaker_user_id ORDER BY accepted_minutes DESC""", {"version": CONSENT_VERSION}))
    lexicon = db.query(f"SELECT COUNT(*) n FROM {TABLE_TTS_LEXICON} WHERE is_active=TRUE")
    llm = _rows(db.query(f"""SELECT data_type,COUNT(*) docs,COALESCE(SUM(LENGTH(content_text)),0) chars
        FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE status='accepted'
        AND anonymized=TRUE AND permission_confirmed=TRUE GROUP BY data_type ORDER BY docs DESC"""))
    active_lexicon = int(lexicon.iloc[0]["n"] or 0) if not lexicon.empty else 0
    eval_rows = db.query(f"SELECT COUNT(*) n FROM {TABLE_AI_EVAL_CASES} WHERE is_active=TRUE")
    eval_count = int(eval_rows.iloc[0]["n"] or 0) if not eval_rows.empty else 0
    return json_safe({"consent_version": CONSENT_VERSION, "speakers": speakers,
        "active_lexicon": active_lexicon, "llm_by_type": llm, "active_eval_cases": eval_count,
        "gates": {"tts_min_train_minutes": 30, "tts_target_collected_minutes": 40,
                  "tts_min_lexicon": 50, "llm_eval_cases": 30,
                  "llm_min_instruction_pairs_for_lora": 500}})


@router.get("/eval/cases")
def eval_cases(request: Request):
    _user, db = _admin(request)
    return json_safe({"items": _rows(db.query(f"""SELECT case_id,task_type,title,input_json,
        rubric_json,reference_text FROM {TABLE_AI_EVAL_CASES} WHERE is_active=TRUE
        ORDER BY task_type,case_id"""))})


@router.post("/eval/runs")
async def eval_baseline(request: Request):
    user, db = _admin(request)
    payload_body = await request.json()
    model_label = str(payload_body.get("model_label") or "Gemini 2.5 Flash")
    cases = _rows(db.query(f"SELECT case_id,task_type,title,input_json,rubric_json FROM {TABLE_AI_EVAL_CASES} WHERE is_active=TRUE ORDER BY case_id"))
    _audit(db, user, "eval_baseline_requested", "eval_run", model_label, {"cases": len(cases)})
    return {"ok":True,"model_label":model_label,"case_count":len(cases),
            "message":"評估題已鎖定；請由受控eval worker逐題執行並寫入ai_eval_runs，API不會在單一HTTP request內長時間批量呼叫模型。"}


@router.post("/datasets/snapshots")
def create_snapshot(body: SnapshotBody, request: Request):
    user, db = _admin(request)
    if body.dataset_kind not in ("tts", "llm"):
        raise HTTPException(400, "dataset_kind只可為tts或llm")
    items = []
    total_seconds = 0.0
    if body.dataset_kind == "tts":
        speaker = body.speaker_user_id.strip()
        if not speaker:
            raise HTTPException(400, "TTS snapshot必須指定單一speaker")
        rows = _rows(db.query(f"""SELECT r.id,r.script_id,r.prompt_text,r.audio_data,r.audio_sha256,
                COALESCE(r.measured_duration_seconds,r.duration_seconds,0) duration_seconds,
                s.manuscript_id,s.category,c.consent_version
            FROM {TABLE_TTS_VOICE_RECORDINGS} r
            JOIN {TABLE_TTS_SCRIPTS} s ON s.script_id=r.script_id
            JOIN {TABLE_TTS_VOICE_CONSENTS} c ON c.user_id=r.speaker_user_id
              AND c.consent_version=:version AND c.withdrawn_at IS NULL
              AND c.voice_cloning_confirmed=TRUE AND c.cloud_processing_confirmed=TRUE
              AND (c.is_minor=FALSE OR c.guardian_confirmed=TRUE)
            WHERE r.status='accepted' AND r.speaker_user_id=:speaker ORDER BY r.id""",
            {"speaker": speaker, "version": CONSENT_VERSION}))
        for row in rows:
            raw = row.pop("audio_data")
            raw = raw.tobytes() if isinstance(raw, memoryview) else bytes(raw)
            sha = row.get("audio_sha256") or hashlib.sha256(raw).hexdigest()
            group = str(row.get("manuscript_id") or row["script_id"])
            seconds = float(row.get("duration_seconds") or 0)
            total_seconds += seconds
            items.append({"source_table": TABLE_TTS_VOICE_RECORDINGS, "source_id": str(row["id"]),
                "source_sha256": sha, "consent_version": CONSENT_VERSION,
                "split_name": _split_name(group), "metadata": {"script_id": row["script_id"],
                "prompt_text": row["prompt_text"], "manuscript_id": row.get("manuscript_id"),
                "category": row.get("category"), "duration_seconds": seconds}})
    else:
        speaker = ""
        rows = _rows(db.query(f"""SELECT id,data_type,title,topic_text,side,content_text,source_note
            FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE status='accepted'
            AND anonymized=TRUE AND permission_confirmed=TRUE ORDER BY id"""))
        for row in rows:
            sha = hashlib.sha256(str(row["content_text"]).encode("utf-8")).hexdigest()
            group = str(row.get("topic_text") or row["id"])
            items.append({"source_table": TABLE_LLM_TRAINING_SUBMISSIONS, "source_id": str(row["id"]),
                "source_sha256": sha, "consent_version": None, "split_name": _split_name(group),
                "metadata": row})
    if not items:
        raise HTTPException(400, "沒有符合授權及審核條件的資料")
    manifest = {"dataset_kind": body.dataset_kind, "speaker_user_id": speaker,
                "consent_version": CONSENT_VERSION if body.dataset_kind == "tts" else None,
                "items": items}
    manifest_json = _json_param(manifest)
    digest = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    snapshot_id = f"{body.dataset_kind}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{digest[:10]}"
    db.execute(f"""INSERT INTO {TABLE_AI_DATASET_SNAPSHOTS}
        (snapshot_id,dataset_kind,speaker_user_id,consent_version,item_count,total_seconds,
         manifest_sha256,manifest_json,status,created_by)
        VALUES(:id,:kind,:speaker,:consent,:count,:seconds,:sha,CAST(:manifest AS jsonb),'ready',:user)""",
        {"id":snapshot_id,"kind":body.dataset_kind,"speaker":speaker or None,
         "consent":CONSENT_VERSION if body.dataset_kind == "tts" else None,"count":len(items),
         "seconds":total_seconds,"sha":digest,"manifest":manifest_json,"user":user})
    for item in items:
        db.execute(f"""INSERT INTO {TABLE_AI_DATASET_SNAPSHOT_ITEMS}
            (snapshot_id,source_table,source_id,source_sha256,consent_version,split_name,metadata_json)
            VALUES(:snapshot,:table,:source,:sha,:consent,:split,CAST(:metadata AS jsonb))""",
            {"snapshot":snapshot_id,"table":item["source_table"],"source":item["source_id"],
             "sha":item["source_sha256"],"consent":item["consent_version"],
             "split":item["split_name"],"metadata":_json_param(item["metadata"])})
    _audit(db, user, "snapshot_created", "dataset_snapshot", snapshot_id,
           {"kind": body.dataset_kind, "items": len(items), "manifest_sha256": digest})
    return {"ok":True,"snapshot_id":snapshot_id,"manifest_sha256":digest,
            "item_count":len(items),"total_seconds":round(total_seconds,3)}


@router.get("/models")
def models(request: Request):
    _user, db = _admin(request)
    return json_safe({"items": _rows(db.query(f"""SELECT model_id,model_type,base_model,
        dataset_snapshot_id,artifact_uri,status,config_json,metrics_json,created_by,created_at,updated_at
        FROM {TABLE_AI_MODEL_VERSIONS} ORDER BY created_at DESC"""))})


@router.post("/models")
def register_model(body: ModelRegisterBody, request: Request):
    user, db = _admin(request)
    if body.model_type not in ("tts", "llm", "embedding") or not body.model_id.strip() or not body.base_model.strip():
        raise HTTPException(400, "模型資料不完整")
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
         "config":_json_param(body.config),"user":user})
    _audit(db, user, "model_registered", "model", body.model_id, {"type": body.model_type})
    return {"ok": True, "model_id": body.model_id}


@router.post("/models/{model_id}/metrics")
def model_metrics(model_id: str, body: ModelMetricsBody, request: Request):
    user, db = _admin(request)
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
               {"metrics":_json_param(body.metrics),"status":status,"now":datetime.now(),"id":model_id})
    _audit(db, user, "model_metrics_updated", "model", model_id, {"status": status})
    return {"ok":True,"status":status}


@router.post("/rag/reindex")
async def rag_reindex(body: RagReindexBody, request: Request):
    user, db = _admin(request)
    if body.embedding_model != "gemini-embedding-2":
        raise HTTPException(400, "目前只支援gemini-embedding-2，避免混用embedding space")
    from deploy.proxy import _get_proxy_secret
    api_key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not api_key: raise HTTPException(503, "未設定GEMINI_API_KEY")
    try:
        db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        db.execute(f"ALTER TABLE {TABLE_RAG_CHUNKS} ADD COLUMN IF NOT EXISTS embedding vector(768)")
    except Exception as exc:
        raise HTTPException(503, "Supabase尚未啟用pgvector extension") from exc
    submissions = _rows(db.query(f"""SELECT id,data_type,title,topic_text,side,content_text,source_note
        FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE status='accepted'
        AND anonymized=TRUE AND permission_confirmed=TRUE ORDER BY id"""))
    db.execute(f"""UPDATE {TABLE_RAG_DOCUMENTS} SET status='withdrawn'
        WHERE submission_id IN
        (SELECT id FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE status!='accepted')""")
    indexed = 0
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{body.embedding_model}:embedContent"
    async with httpx.AsyncClient(timeout=60) as client:
        for submission in submissions:
            document_id = f"llm-{submission['id']}"
            content_sha = hashlib.sha256(str(submission["content_text"]).encode("utf-8")).hexdigest()
            db.execute(f"""INSERT INTO {TABLE_RAG_DOCUMENTS}
                (document_id,submission_id,title,data_type,topic_text,side,source_note,content_sha256,
                 status,embedding_model,embedding_version,indexed_at)
                VALUES(:doc,:submission,:title,:type,:topic,:side,:source,:sha,'active',:model,:version,:now)
                ON CONFLICT(document_id) DO UPDATE SET title=EXCLUDED.title,data_type=EXCLUDED.data_type,
                 topic_text=EXCLUDED.topic_text,side=EXCLUDED.side,source_note=EXCLUDED.source_note,
                 content_sha256=EXCLUDED.content_sha256,status='active',embedding_model=EXCLUDED.embedding_model,
                 embedding_version=EXCLUDED.embedding_version,indexed_at=EXCLUDED.indexed_at""",
                {"doc":document_id,"submission":submission["id"],"title":submission.get("title"),
                 "type":submission.get("data_type"),"topic":submission.get("topic_text"),
                 "side":submission.get("side"),"source":submission.get("source_note"),"sha":content_sha,
                 "model":body.embedding_model,"version":body.embedding_version,"now":datetime.now()})
            db.execute(f"DELETE FROM {TABLE_RAG_CHUNKS} WHERE document_id=:doc", {"doc": document_id})
            for index, chunk in enumerate(_rag_chunks(submission["content_text"])):
                response = await client.post(endpoint, params={"key":api_key}, json={
                    "content":{"parts":[{"text":chunk}]}, "outputDimensionality":768})
                response.raise_for_status()
                values = ((response.json().get("embedding") or {}).get("values") or [])
                if len(values) != 768: raise HTTPException(502, "Embedding API回傳維度不正確")
                chunk_id = f"{document_id}-{index:04d}-{hashlib.sha256(chunk.encode()).hexdigest()[:10]}"
                vector_text = "[" + ",".join(f"{float(x):.9g}" for x in values) + "]"
                db.execute(f"""INSERT INTO {TABLE_RAG_CHUNKS}
                    (chunk_id,document_id,chunk_index,content_text,token_estimate,embedding_model,
                     embedding_version,embedding_json,embedding,metadata_json)
                    VALUES(:id,:doc,:idx,:content,:tokens,:model,:version,CAST(:json AS jsonb),
                     CAST(:vector AS vector),CAST(:metadata AS jsonb))""",
                    {"id":chunk_id,"doc":document_id,"idx":index,"content":chunk,
                     "tokens":max(1,len(chunk)//2),"model":body.embedding_model,
                     "version":body.embedding_version,"json":_json_param(values),"vector":vector_text,
                     "metadata":_json_param({"data_type":submission.get("data_type"),
                                               "topic_text":submission.get("topic_text"),
                                               "side":submission.get("side")})})
                indexed += 1
    _audit(db, user, "rag_reindexed", "rag_index", body.embedding_version,
           {"documents": len(submissions), "chunks": indexed})
    return {"ok":True,"documents":len(submissions),"chunks":indexed,
            "embedding_model":body.embedding_model,"embedding_version":body.embedding_version}
