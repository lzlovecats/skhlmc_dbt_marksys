"""Direct-HTML data API for the AI training workspace."""
import base64
import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from api.pagination import PAGE_SIZE, bounds, json_safe, payload, scalar_count

from schema import (
    CREATE_LLM_TRAINING_SUBMISSIONS, CREATE_TTS_LEXICON, CREATE_TTS_SCRIPTS,
    CREATE_TTS_VOICE_CONSENTS, CREATE_TTS_VOICE_RECORDINGS,
    TABLE_LLM_TRAINING_SUBMISSIONS, TABLE_TTS_LEXICON, TABLE_TTS_SCRIPTS,
    TABLE_TTS_VOICE_CONSENTS, TABLE_TTS_VOICE_RECORDINGS,
)

router = APIRouter(prefix="/api/ai-training", tags=["ai-training"])
CONSENT_VERSION = "tts_voice_v1_2026_07"
CONSENT_TEXT = "我同意聖呂中辯收集本人在本頁提交的錄音，用作廣東話 TTS（文字轉語音）、讀音檢查及相關 AI 研究測試。錄音可能用於分析本人聲線及建立語音模型。我明白可向開發者要求撤回未來使用授權。"
ALLOWED_KEY, REVIEWERS_KEY = "tts_recording_allowed_users", "tts_recording_reviewers"
MANUSCRIPT_SEGMENT_MAX_LEN = 35

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


class ManuscriptBody(BaseModel):
    title: str
    text: str
    category: str = "完整稿"
    active: bool = True


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
    result = {"user_id": user, "is_allowed": allowed, "is_admin": admin, "consented": not consent.empty, "consent_text": CONSENT_TEXT, "scripts": scripts, "lexicon":lexicon, "my_recordings":mine, "my_llm":llm}
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
    _user, db = _admin(request); page, _, offset = bounds(page)
    clauses, params = ["1=1"], {}
    if status != "all": clauses.append("status=:status"); params["status"] = status
    if speaker.strip(): clauses.append("speaker_user_id=:speaker"); params["speaker"] = speaker.strip()
    where = " AND ".join(clauses); total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE {where}", params)
    params.update(limit=PAGE_SIZE, offset=offset)
    rows = _rows(db.query(f"SELECT id,speaker_user_id,script_id,prompt_text,mime_type,size_bytes,duration_seconds,status,ai_review_status,ai_review_json,ai_transcript,review_note,created_at FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE {where} ORDER BY created_at DESC LIMIT :limit OFFSET :offset", params))
    return payload(rows, page, total)


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
    db.execute(f"""INSERT INTO {TABLE_TTS_VOICE_CONSENTS}(user_id,consent_version,consent_text,consented_at,withdrawn_at)
                   VALUES(:user,:version,:consent,:now,NULL)
                   ON CONFLICT(user_id,consent_version) DO UPDATE SET consent_text=EXCLUDED.consent_text,consented_at=EXCLUDED.consented_at,withdrawn_at=NULL""", {"user":user,"version":CONSENT_VERSION,"consent":CONSENT_TEXT,"now":datetime.now()})
    return {"ok": True}


@router.delete("/consent")
def withdraw(request: Request):
    user, db = _ctx(request)
    db.execute(f"UPDATE {TABLE_TTS_VOICE_CONSENTS} SET withdrawn_at=:now WHERE user_id=:user AND consent_version=:version AND withdrawn_at IS NULL", {"user":user,"version":CONSENT_VERSION,"now":datetime.now()})
    db.execute(f"UPDATE {TABLE_TTS_VOICE_RECORDINGS} SET status='withdrawn' WHERE speaker_user_id=:user AND status!='withdrawn'", {"user":user})
    return {"ok":True}


@router.post("/recordings")
def recording(body: RecordingBody, request: Request):
    user, db = _ctx(request)
    if user not in _users(db, ALLOWED_KEY): raise HTTPException(403, "你未獲加入 TTS 錄音收集名單")
    active = db.query(f"SELECT 1 FROM {TABLE_TTS_VOICE_CONSENTS} WHERE user_id=:user AND consent_version=:version AND withdrawn_at IS NULL", {"user":user,"version":CONSENT_VERSION})
    if active.empty: raise HTTPException(400, "請先確認錄音同意")
    script = db.query(f"SELECT text FROM {TABLE_TTS_SCRIPTS} WHERE script_id=:id AND is_active=TRUE", {"id":body.script_id})
    if script.empty: raise HTTPException(404, "錄音句子不存在或已停用")
    try: audio = base64.b64decode(body.audio_base64)
    except Exception as exc: raise HTTPException(400, "錄音資料無法讀取") from exc
    if not audio or len(audio)>25*1024*1024: raise HTTPException(400, "錄音大小不正確")
    review_status = "error" if body.manual_review else "passed"
    db.execute(f"""INSERT INTO {TABLE_TTS_VOICE_RECORDINGS}(speaker_user_id,script_id,prompt_text,audio_data,mime_type,file_ext,size_bytes,duration_seconds,ai_review_status,ai_review_json,ai_transcript,status,created_at)
                   VALUES(:user,:script,:prompt,:audio,:mime,:ext,:size,:duration,:review_status,:review_json,:transcript,'pending',:now)""", {"user":user,"script":body.script_id,"prompt":script.iloc[0]["text"],"audio":audio,"mime":body.mime_type,"ext":"webm","size":len(audio),"duration":max(0,body.duration_seconds),"review_status":review_status,"review_json":json.dumps(body.ai_review or {},ensure_ascii=False),"transcript":str((body.ai_review or {}).get("transcript") or ""),"now":datetime.now()})
    return {"ok":True, "message":"錄音已提交，等待人工審核。"}


@router.post("/recordings/quality-check")
async def recording_quality_check(body: RecordingBody, request: Request):
    """Run the deterministic gate before the provider-assisted/manual review.

    Browsers do not reliably report duration, so duration zero is accepted; the
    byte-size and supported-container checks still catch empty/corrupt captures.
    """
    user, db = _ctx(request)
    if user not in _users(db, ALLOWED_KEY): raise HTTPException(403, "你未獲加入 TTS 錄音收集名單")
    try: audio = base64.b64decode(body.audio_base64, validate=True)
    except Exception as exc: raise HTTPException(400, "錄音資料無法讀取") from exc
    mime = (body.mime_type or "").split(";", 1)[0].lower()
    supported = {"audio/webm", "audio/mp4", "audio/mpeg", "audio/wav", "audio/ogg"}
    problems = []
    if mime not in supported: problems.append("錄音格式不受支援")
    if len(audio) < 1000: problems.append("錄音太短或沒有聲音資料")
    if len(audio) > 25 * 1024 * 1024: problems.append("錄音超過 25MB")
    if problems:
        return {"ok": False, "status": "failed", "problems": problems, "message": "；".join(problems)}
    from deploy.proxy import _get_proxy_secret
    key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not key:
        raise HTTPException(503, "未設定 GEMINI_API_KEY，暫時未能進行 AI 音質檢查")
    prompt = "以廣東話 TTS 資料審核員身份檢查錄音清晰度、雜音、截斷及內容可用性。只回覆 JSON：{\"passed\":true,\"transcript\":\"\",\"problems\":[],\"note\":\"\"}"
    payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": body.mime_type, "data": body.audio_base64}}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    try:
        async with httpx.AsyncClient(timeout=60) as client: response = await client.post(url, json=payload)
        response.raise_for_status(); raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        review = json.loads(raw)
    except Exception as exc:
        raise HTTPException(502, f"AI 音質檢查失敗：{str(exc)[:160]}") from exc
    passed = bool(review.get("passed"))
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
        if not key: raise HTTPException(503,"AI 預檢暫時未能完成；可確認後選擇略過 AI 檢查")
        prompt = "審核以下香港粵語辯論訓練文字，只回覆 JSON，含 passed(boolean), reason, relevance, quality, anonymization, permission_risk。\n" + normalized
        try:
            url=f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
            async with httpx.AsyncClient(timeout=60) as client: response=await client.post(url,json={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"responseMimeType":"application/json","temperature":0}})
            response.raise_for_status(); review=json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"]); review["fingerprint"]=fingerprint
        except Exception as exc: raise HTTPException(503,"AI 預檢暫時未能完成；可確認後選擇略過 AI 檢查") from exc
        if not bool(review.get("passed")): return {"ok":False,"status":"failed","message":review.get("reason") or "AI 預檢未通過", "review":review}
    db.execute(f"""INSERT INTO {TABLE_LLM_TRAINING_SUBMISSIONS}(submitted_by,data_type,title,topic_text,side,content_text,source_note,anonymized,permission_confirmed,ai_review_status,ai_review_json,status,created_at)
                   VALUES(:user,:type,:title,:topic,:side,:content,:source,TRUE,TRUE,:ai_status,:review,'pending',:now)""", {"user":user,"type":body.data_type,"title":body.title.strip() or None,"topic":body.topic_text.strip() or None,"side":body.side,"content":body.content_text.strip(),"source":body.source_note.strip() or None,"ai_status":review_status,"review":json.dumps(review,ensure_ascii=False),"now":datetime.now()})
    return {"ok":True,"message":"資料已提交，等待人工審核。"}


@router.delete("/llm/{submission_id}")
def withdraw_llm(submission_id: int, request: Request):
    user, db = _ctx(request)
    row=db.query(f"SELECT status FROM {TABLE_LLM_TRAINING_SUBMISSIONS} WHERE id=:id AND submitted_by=:user",{"id":submission_id,"user":user})
    if row.empty: raise HTTPException(404,"找不到提交")
    if row.iloc[0]["status"] != "pending": raise HTTPException(409,"只有待審核資料可以撤回")
    db.execute(f"UPDATE {TABLE_LLM_TRAINING_SUBMISSIONS} SET status='withdrawn' WHERE id=:id",{"id":submission_id})
    return {"ok":True}


@router.post("/lexicon")
def lexicon(body: LexiconBody, request: Request):
    user, db = _ctx(request)
    if not _is_admin(db,user): raise HTTPException(403,"只有管理員可修改讀音字典")
    lid = body.lexicon_id.strip()
    if not lid:
        existing=_rows(db.query(f"SELECT lexicon_id FROM {TABLE_TTS_LEXICON} WHERE lexicon_id LIKE 'lex_%'")); nums=[int(m.group(1)) for x in existing if (m:=re.match(r"lex_(\\d+)$",str(x['lexicon_id'])))]
        lid=f"lex_{max(nums,default=0)+1:04d}"
    db.execute(f"""INSERT INTO {TABLE_TTS_LEXICON}(lexicon_id,term,reading,jyutping,example,note,category,is_active,created_by,updated_at)
                   VALUES(:id,:term,:reading,:jyutping,:example,:note,:category,TRUE,:user,:now)
                   ON CONFLICT(lexicon_id) DO UPDATE SET term=EXCLUDED.term,reading=EXCLUDED.reading,jyutping=EXCLUDED.jyutping,example=EXCLUDED.example,note=EXCLUDED.note,category=EXCLUDED.category,updated_at=EXCLUDED.updated_at""", {"id":lid,"term":body.term.strip(),"reading":body.reading.strip(),"jyutping":body.jyutping.strip(),"example":body.example.strip(),"note":body.note.strip(),"category":body.category.strip(),"user":user,"now":datetime.now()})
    return {"ok":True}


@router.post("/scripts")
def script(body: ScriptBody, request: Request):
    user, db = _ctx(request)
    if not _is_admin(db,user): raise HTTPException(403,"只有管理員可修改句庫")
    sid=body.script_id.strip() or f"custom_{int(datetime.now().timestamp())}"
    db.execute(f"""INSERT INTO {TABLE_TTS_SCRIPTS}(script_id,category,text,is_active,sort_order,created_by,updated_at) VALUES(:id,:cat,:text,TRUE,:sort,:user,:now)
                   ON CONFLICT(script_id) DO UPDATE SET category=EXCLUDED.category,text=EXCLUDED.text,sort_order=EXCLUDED.sort_order,updated_at=EXCLUDED.updated_at""", {"id":sid,"cat":body.category.strip(),"text":body.text.strip(),"sort":body.sort_order,"user":user,"now":datetime.now()})
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
    for index, value in enumerate(segments, 1):
        db.execute(f"""INSERT INTO {TABLE_TTS_SCRIPTS}
            (script_id,category,text,is_active,sort_order,script_type,manuscript_id,manuscript_title,created_by,updated_at)
            VALUES(:id,:category,:text,:active,:sort,'full',:mid,:title,:user,:now)""",
            {"id": f"{manuscript_id}_{index:03d}", "category": body.category.strip() or "完整稿",
             "text": value, "active": body.active, "sort": index, "mid": manuscript_id,
             "title": title, "user": user, "now": datetime.now()})
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
      ON r.script_id=s.script_id AND r.status IN ('pending','accepted') AND r.speaker_user_id=ANY(:users)
      WHERE s.is_active=TRUE GROUP BY s.script_id HAVING COUNT(DISTINCT r.speaker_user_id)>=:required""",{"users":allowed,"required":len(allowed)})
    ids=[str(x) for x in rows["script_id"].tolist()] if not rows.empty else []
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
    return {"ok":True,"added":added}


@router.post("/regenerate-suggestions")
async def regenerate_suggestions(request: Request):
    _user, db = _admin(request)
    from deploy.proxy import _get_proxy_secret
    key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not key: raise HTTPException(503, "未設定 GEMINI_API_KEY，暫時未能重出句庫")
    rows = _rows(db.query(f"SELECT category,text FROM {TABLE_TTS_SCRIPTS} WHERE is_active=TRUE ORDER BY category,sort_order"))
    sample = "\n".join(f"[{x['category']}] {x['text']}" for x in rows[:120])
    prompt = "為香港粵語辯論 TTS 句庫提出最多20條補充句，覆蓋數字、專名、語氣及辯論用語。避免重複。只回覆 JSON array，每項含 category,text。現有句庫：\n" + sample
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": .4}}
    try:
        async with httpx.AsyncClient(timeout=60) as client: response = await client.post(url, json=payload)
        response.raise_for_status(); raw=response.json()["candidates"][0]["content"]["parts"][0]["text"]
        suggestions=json.loads(raw)
    except Exception as exc: raise HTTPException(502, f"AI 重出句庫失敗：{str(exc)[:160]}") from exc
    return {"items": suggestions if isinstance(suggestions, list) else []}


@router.get("/export/recordings.zip")
def export_recordings(request: Request, speaker: str = ""):
    _user, db = _admin(request)
    where, params = "status='accepted'", {}
    if speaker.strip(): where += " AND speaker_user_id=:speaker"; params["speaker"] = speaker.strip()
    rows = _rows(db.query(f"SELECT id,speaker_user_id,script_id,prompt_text,audio_data,mime_type,file_ext FROM {TABLE_TTS_VOICE_RECORDINGS} WHERE {where} ORDER BY id", params))
    output = io.BytesIO(); manifest = []
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            audio = row.pop("audio_data"); audio = audio.tobytes() if isinstance(audio, memoryview) else bytes(audio)
            ext = re.sub(r"[^a-z0-9]", "", str(row.get("file_ext") or "webm").lower()) or "webm"
            name = f"audio/{row['id']}_{row['script_id']}.{ext}"; archive.writestr(name, audio)
            manifest.append({**row, "file": name})
        csv_buffer=io.StringIO(); fields=["id","speaker_user_id","script_id","prompt_text","mime_type","file_ext","file"]
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
    db.execute(f"UPDATE {TABLE_TTS_VOICE_RECORDINGS} SET status=:status,review_note=:note,reviewed_by=:user,reviewed_at=:now WHERE id=:id", {"status":body.status,"note":body.note,"user":user,"now":datetime.now(),"id":record_id}); return {"ok":True}


@router.post("/llm/{submission_id}/review")
def review_llm(submission_id:int, body:ReviewBody, request:Request):
    user,db=_ctx(request)
    if not _is_admin(db,user): raise HTTPException(403,"只有管理員可審核")
    if body.status not in ('accepted','rejected','withdrawn'): raise HTTPException(400,"狀態不正確")
    db.execute(f"UPDATE {TABLE_LLM_TRAINING_SUBMISSIONS} SET status=:status,review_note=:note,reviewed_by=:user,reviewed_at=:now WHERE id=:id", {"status":body.status,"note":body.note,"user":user,"now":datetime.now(),"id":submission_id}); return {"ok":True}
