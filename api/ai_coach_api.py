"""Direct-HTML API for AI coach.

The old page kept all provider calls in a Streamlit session.  This module keeps
the same model choices and prompts, but keeps credentials and accounting on the
server so the browser never receives either.
"""
import base64
import secrets
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ai_model_config import AI_MODEL_OPTIONS, DEFAULT_AI_MODEL
from prompts import (
    FACT_CHECK_SYSTEM_PROMPT, QA_REVIEW_SYSTEM_PROMPT, SPEECH_REVIEW_SYSTEM_PROMPT,
    WEB_RESEARCH_SYSTEM_PROMPT, build_fact_check_user_prompt, build_strategy_prompt,
    build_strategy_user_prompt, build_web_research_user_prompt,
)

router = APIRouter(prefix="/api/ai-coach", tags=["ai-coach"])
_LIVE_BRIEFS = {}


class CoachRequest(BaseModel):
    feature: str
    model_label: str = DEFAULT_AI_MODEL
    topic: str = ""
    side: str = "正方"
    debate_format: str = "校園隨想"
    text: str = ""
    position: int = 1
    research_need: str = ""
    audio_base64: str = ""
    audio_mime: str = "audio/webm"

class LivePrepareRequest(BaseModel):
    topic: str
    side: str = "正方"
    debate_format: str = "校園隨想"
    mode: str = "free"
    model_label: str = DEFAULT_AI_MODEL

def consume_live_brief(brief_id, user_id):
    row=_LIVE_BRIEFS.pop(str(brief_id or ""),None)
    return row["brief"] if row and row["user_id"]==user_id else ""

def record_live_usage(user_id, feature, duration_minutes):
    from deploy.proxy import get_vote_db
    config={"provider":"gemini","input_price_per_million":0,"output_price_per_million":0}
    try:
        get_vote_db().execute("""INSERT INTO ai_fund_usage_logs(user_id,feature,model_label,provider,estimated_cost_usd,estimated_cost_hkd,input_tokens,output_tokens,audio_tokens,search_calls,cost_source,status,error_message,created_at) VALUES(:user,:feature,:label,'gemini',:usd,:hkd,0,0,:audio,0,'estimate','success','',:now)""",{"user":user_id,"feature":feature,"label":"Gemini Live","usd":round(float(duration_minutes)*0.01,4),"hkd":round(float(duration_minutes)*0.078,4),"audio":int(float(duration_minutes)*60*25),"now":datetime.now().isoformat(sep=" ",timespec="seconds")})
    except Exception: pass


def _context(request):
    from deploy.proxy import _require_committee_user
    return _require_committee_user(request)


def _config(label):
    if label not in AI_MODEL_OPTIONS:
        raise HTTPException(400, "不支援的 AI 模型")
    return AI_MODEL_OPTIONS[label]


def _estimate(feature, config, has_audio=False):
    costs={"speech_review":(2500,1800),"strategy":(1200,2500),"web_research":(1500,2500),"fact_check":(1500,2500)}
    inp,out=costs.get(feature,(0,0));audio=1200 if has_audio else 0
    usd=(inp*(config.get("input_price_per_million") or 0)+audio*(config.get("audio_input_price_per_million") or config.get("input_price_per_million") or 0)+out*(config.get("output_price_per_million") or 0))/1_000_000
    if feature in ("web_research","fact_check"):usd+=config.get("web_search_price_per_call") or 0
    return {"usd":round(usd,4),"hkd":round(usd*7.8,4)}


def _usage(db, user_id, feature, label, config, success, error=""):
    # Same ledger table as Streamlit.  Use a conservative, transparent estimate;
    # provider billing remains the source of truth in AI基金.
    costs = {"speech_review": (2500, 1800), "strategy": (1200, 2500),
             "web_research": (1500, 2500), "fact_check": (1500, 2500)}
    inp, out = costs.get(feature, (0, 0))
    usd = ((inp * config.get("input_price_per_million", 0) + out * config.get("output_price_per_million", 0)) / 1_000_000)
    if feature in ("web_research", "fact_check"):
        usd += config.get("web_search_price_per_call") or 0
    try:
        db.execute(
            """INSERT INTO ai_fund_usage_logs
               (user_id, feature, model_label, provider, estimated_cost_usd, estimated_cost_hkd,
                input_tokens, output_tokens, audio_tokens, search_calls, cost_source, status, error_message, created_at)
               VALUES (:user_id,:feature,:label,:provider,:usd,:hkd,:inp,:out,0,:search,'estimate',:status,:error,:now)""",
            {"user_id": user_id, "feature": feature, "label": label,
             "provider": config.get("provider", ""), "usd": usd if success else 0,
             "hkd": usd * 7.8 if success else 0, "inp": inp if success else 0,
             "out": out if success else 0, "search": 1 if feature in ("web_research", "fact_check") and success else 0,
             "status": "success" if success else "failed", "error": str(error)[:500],
             "now": datetime.now().isoformat(sep=" ", timespec="seconds")},
        )
    except Exception:
        # A missing optional accounting table must never make coaching unusable.
        pass


def _message(body: CoachRequest):
    feature = body.feature
    if feature == "strategy":
        return build_strategy_prompt(body.debate_format), build_strategy_user_prompt(body.topic, body.side, body.debate_format)
    if feature == "speech_review":
        position = {1: "主辯", 2: "一副", 3: "二副", 4: "結辯", 5: "三副"}.get(body.position, "")
        system = QA_REVIEW_SYSTEM_PROMPT if "台下發問練習" in body.text or "交互答問練習" in body.text else SPEECH_REVIEW_SYSTEM_PROMPT
        return system, f"辯題：{body.topic}\n立場：{body.side}{position}\n\n## 我嘅演辭內容\n{body.text or '請按錄音內容分析。'}"
    today = datetime.now().strftime("%Y-%m-%d")
    if feature == "web_research":
        return WEB_RESEARCH_SYSTEM_PROMPT, build_web_research_user_prompt(today, body.topic, body.research_need)
    if feature == "fact_check":
        return FACT_CHECK_SYSTEM_PROMPT, build_fact_check_user_prompt(today, body.text)
    raise HTTPException(400, "不支援的 AI 功能")


async def _generate(config, system, user, body):
    from deploy.proxy import _get_proxy_secret
    if config["provider"] == "openrouter":
        key = _get_proxy_secret("OPENROUTER_API_KEY").strip()
        if not key:
            raise HTTPException(503, "未設定 OPENROUTER_API_KEY")
        payload = {"model": config["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        async with httpx.AsyncClient(timeout=70) as client:
            response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=payload)
            response.raise_for_status(); data = response.json()
        return data["choices"][0]["message"]["content"]

    key = _get_proxy_secret("GEMINI_API_KEY").strip()
    if not key:
        raise HTTPException(503, "未設定 GEMINI_API_KEY")
    parts = [{"text": user}]
    if body.audio_base64:
        if not config.get("supports_audio"):
            raise HTTPException(400, "所選模型不支援錄音分析，請改貼逐字稿或選 Gemini。")
        try:
            base64.b64decode(body.audio_base64, validate=True)
        except Exception as exc:
            raise HTTPException(400, "錄音資料無法讀取") from exc
        parts.append({"inline_data": {"mime_type": body.audio_mime or "audio/webm", "data": body.audio_base64}})
    payload = {"system_instruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0.35}}
    if body.feature in ("web_research", "fact_check"):
        payload["tools"] = [{"google_search": {}}]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config['model']}:generateContent?key={key}"
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(url, json=payload)
        if response.status_code >= 400:
            raise HTTPException(502, f"Gemini 服務錯誤：{response.text[:300]}")
        data = response.json()
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
    except (IndexError, KeyError, TypeError) as exc:
        raise HTTPException(502, "AI 未有回傳可讀結果") from exc


@router.get("/data")
def data(request: Request):
    _context(request)
    from deploy.proxy import get_vote_db, _get_proxy_secret
    from schema import TABLE_TOPICS, TABLE_MATCHES
    from debate_timing import DEBATE_FORMATS, get_debate_timer_config
    db=get_vote_db()
    from core.funds_logic import _ensure_ai
    _ensure_ai(db)
    topics=db.query(f"SELECT topic_text,category,difficulty FROM {TABLE_TOPICS} ORDER BY category,topic_text")
    matches=db.query(f"SELECT match_id,topic_text,pro_team,con_team FROM {TABLE_MATCHES} ORDER BY match_id DESC")
    balance=db.query("SELECT COALESCE(SUM(CASE WHEN transaction_type='member_deposit' THEN amount_hkd WHEN transaction_type='provider_topup' THEN -amount_hkd WHEN transaction_type IN ('refund','adjustment') THEN amount_hkd ELSE 0 END),0) balance FROM ai_fund_transactions WHERE status='confirmed'")
    low=db.query("SELECT value FROM system_config WHERE key='ai_fund_low_balance_hkd'")
    models=[]
    for label,item in AI_MODEL_OPTIONS.items():
        key_name="OPENROUTER_API_KEY" if item["provider"]=="openrouter" else "GEMINI_API_KEY"
        models.append({"label":label,"supports_audio":item["supports_audio"],"supports_web_search":item["supports_web_search"],"note":f"{item['pricing_note']} {item.get('paid_rate_note','')}".strip(),"pricing_label":item.get("pricing_label",""),"is_premium":bool(item.get("is_premium")),"api_key_name":key_name,"available":bool(_get_proxy_secret(key_name)),"estimates":{f:_estimate(f,item) for f in ("strategy","speech_review","web_research","fact_check")}})
    return {"models":models,"default_model":DEFAULT_AI_MODEL,"topics":[dict(x) for x in topics.to_dict("records")],"matches":[dict(x) for x in matches.to_dict("records")],"formats":{name:get_debate_timer_config(name) for name in DEBATE_FORMATS},"fund":{"balance_hkd":float(balance.iloc[0]["balance"] or 0),"low_balance_hkd":float(low.iloc[0]["value"] or 100) if not low.empty else 100},"azure_tts":bool(_get_proxy_secret("AZURE_SPEECH_KEY") and _get_proxy_secret("AZURE_SPEECH_REGION"))}


@router.post("/run")
async def run(body: CoachRequest, request: Request):
    user_id = _context(request)
    config = _config(body.model_label)
    key_name="OPENROUTER_API_KEY" if config["provider"]=="openrouter" else "GEMINI_API_KEY"
    from deploy.proxy import _get_proxy_secret
    if not _get_proxy_secret(key_name): raise HTTPException(503,f"未設定 {key_name}")
    system, user = _message(body)
    from deploy.proxy import get_vote_db
    try:
        result = await _generate(config, system, user, body)
    except HTTPException as exc:
        _usage(get_vote_db(), user_id, body.feature, body.model_label, config, False, exc.detail)
        raise
    _usage(get_vote_db(), user_id, body.feature, body.model_label, config, True)
    return {"ok": True, "markdown": result}

@router.post("/prepare-live")
async def prepare_live(body:LivePrepareRequest,request:Request):
    user_id=_context(request);config=_config(body.model_label)
    need=f"為{body.mode}練習準備正反雙方最新事實、數據、例子、攻防位及可靠來源。賽制：{body.debate_format}；使用者立場：{body.side}。"
    system,user=_message(CoachRequest(feature="web_research",model_label=body.model_label,topic=body.topic,research_need=need))
    try:brief=await _generate(config,system,user,CoachRequest(feature="web_research",model_label=body.model_label,topic=body.topic,research_need=need))
    except Exception:brief=""
    _usage(__import__('deploy.proxy',fromlist=['get_vote_db']).get_vote_db(),user_id,"web_research",body.model_label,config,bool(brief))
    key=secrets.token_urlsafe(18);_LIVE_BRIEFS[key]={"user_id":user_id,"brief":brief[:4500]};return {"ok":True,"brief_id":key,"research_ready":bool(brief)}
