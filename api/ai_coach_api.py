"""API for AI coach practice, speech review and strategy planning.

This module keeps model choices and prompts server-side with credentials and accounting on the
server so the browser never receives either.
"""
import asyncio
import base64
import math
import os
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ai_model_config import AI_MODEL_OPTIONS, DEFAULT_AI_MODEL
from prompts import (
    FACT_CHECK_SYSTEM_PROMPT, QA_REVIEW_SYSTEM_PROMPT, SPEECH_REVIEW_SYSTEM_PROMPT,
    WEB_RESEARCH_SYSTEM_PROMPT, build_fact_check_user_prompt, build_strategy_prompt,
    build_strategy_user_prompt, build_web_research_user_prompt,
)
from schema import (
    TABLE_AI_COACH_LIVE_BRIEFS,
    TABLE_AI_DATASET_SNAPSHOTS,
    TABLE_AI_DATASET_SNAPSHOT_ITEMS,
    TABLE_AI_MODEL_VERSIONS,
)
from system_limits import (
    AI_COACH_CONCURRENCY, AI_COACH_MATCH_LIMIT, AI_COACH_MAX_AUDIO_BYTES,
    AI_COACH_MAX_AUDIO_SECONDS, AI_COACH_TOPIC_LIMIT, GEMINI_RELAY_MAX_BYTES,
    LIVE_BRIEF_MAX_CHARS, LIVE_BRIEF_TTL_MINUTES, LIVE_FREE_MAX_MINUTES,
    MAX_ROOMS, MULTIPLAYER_FREE_MONTHLY_ROOMS, MULTIPLAYER_MOCK_MONTHLY_ROOMS,
    PREPARE_LIVE_USAGE_RETENTION_DAYS, PREPARE_LIVE_USER_DAILY_LIMIT,
    PREPARE_LIVE_USER_HOURLY_LIMIT, SOLO_FREE_MONTHLY_LIMIT,
    SOLO_MOCK_MONTHLY_LIMIT,
)

router = APIRouter(prefix="/api/ai-coach", tags=["ai-coach"])
_LIVE_BRIEF_TABLE = TABLE_AI_COACH_LIVE_BRIEFS
FEATURE_TOKEN_ESTIMATES = {"speech_review": (2500, 1800), "strategy": (1200, 2500),
                           "web_research": (1500, 2500), "fact_check": (1500, 2500)}
MAX_COACH_AUDIO_BYTES = AI_COACH_MAX_AUDIO_BYTES
AI_COACH_SEMAPHORE = asyncio.Semaphore(AI_COACH_CONCURRENCY)
DIFFICULTY_OPTIONS = {1: "Lv1 — 概念日常", 2: "Lv2 — 一般議題", 3: "Lv3 — 進階專業"}


class CoachRequest(BaseModel):
    feature: str = Field(max_length=40)
    model_label: str = Field(default=DEFAULT_AI_MODEL, max_length=120)
    topic: str = Field(default="", max_length=500)
    side: str = Field(default="正方", max_length=20)
    debate_format: str = Field(default="校園隨想", max_length=80)
    text: str = Field(default="", max_length=20_000)
    position: int = 1
    research_need: str = Field(default="", max_length=2000)
    audio_base64: str = Field(default="", max_length=3_000_000)
    audio_mime: str = Field(default="audio/webm", max_length=80)
    match_id: str = Field(default="", max_length=100)

class LivePrepareRequest(BaseModel):
    topic: str = Field(max_length=500)
    side: str = Field(default="正方", max_length=20)
    debate_format: str = Field(default="校園隨想", max_length=80)
    mode: str = Field(default="free", max_length=20)
    model_label: str = Field(default=DEFAULT_AI_MODEL, max_length=120)

def _reserve_prepare_live(db, user_id: str) -> str | None:
    """Atomically cap repeated pre-live research before provider tokens burn."""
    now_hk = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    hour_start = now_hk.replace(minute=0, second=0, microsecond=0)
    day_start = now_hk.replace(hour=0, minute=0, second=0, microsecond=0)
    now_utc = now_hk.astimezone(timezone.utc).replace(tzinfo=None)
    hour_utc = hour_start.astimezone(timezone.utc).replace(tzinfo=None)
    day_utc = day_start.astimezone(timezone.utc).replace(tzinfo=None)
    with db.transaction() as conn:
        from sqlalchemy import text
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('ai_coach_prepare_live_quota'))"))
        conn.execute(text("DELETE FROM ai_coach_prepare_usage WHERE created_at<:cutoff"),
                     {"cutoff": day_utc - timedelta(days=PREPARE_LIVE_USAGE_RETENTION_DAYS)})
        hourly = int(conn.execute(text("""SELECT COUNT(*) FROM ai_coach_prepare_usage
            WHERE user_id=:user AND created_at>=:start"""), {
            "user": user_id, "start": hour_utc,
        }).scalar() or 0)
        if hourly >= PREPARE_LIVE_USER_HOURLY_LIMIT:
            return "賽前研究每人每小時只可執行一次，請稍後再試。"
        daily = int(conn.execute(text("""SELECT COUNT(*) FROM ai_coach_prepare_usage
            WHERE user_id=:user AND created_at>=:start"""), {
            "user": user_id, "start": day_utc,
        }).scalar() or 0)
        if daily >= PREPARE_LIVE_USER_DAILY_LIMIT:
            return f"賽前研究每人每日最多{PREPARE_LIVE_USER_DAILY_LIMIT}次，請翌日再試。"
        conn.execute(text("""INSERT INTO ai_coach_prepare_usage(user_id,created_at)
            VALUES(:user,:now)"""), {"user": user_id, "now": now_utc})
    return None


def consume_live_brief(brief_id, user_id):
    from deploy.proxy import get_vote_db
    db = get_vote_db()
    key = str(brief_id or "")
    rows = db.query(
        f"SELECT brief,user_id,expires_at FROM {_LIVE_BRIEF_TABLE} WHERE brief_id=:brief_id",
        {"brief_id": key},
    )
    db.execute(f"DELETE FROM {_LIVE_BRIEF_TABLE} WHERE brief_id=:brief_id OR expires_at<:now", {
        "brief_id": key, "now": datetime.now().isoformat(sep=" ", timespec="seconds"),
    })
    if rows.empty:
        return ""
    row = rows.iloc[0]
    now_text = datetime.now().isoformat(sep=" ", timespec="seconds")
    return str(row["brief"]) if str(row["user_id"]) == str(user_id) and str(row["expires_at"]) >= now_text else ""

def _context(request):
    from deploy.proxy import _require_committee_user
    return _require_committee_user(request)


def _config(label, db=None):
    if label == "自家辯論 LLM":
        from deploy.proxy import _get_proxy_secret
        base_url = _get_proxy_secret("CUSTOM_LLM_BASE_URL").strip().rstrip("/")
        model = _get_proxy_secret("CUSTOM_LLM_MODEL").strip()
        api_key = _get_proxy_secret("CUSTOM_LLM_API_KEY").strip()
        if not base_url or not model or not api_key:
            raise HTTPException(503, "自家LLM尚未完成設定")
        if db is not None:
            from core.schema_features import READY, feature_bundle_state

            try:
                model_schema_ready = feature_bundle_state(
                    db, "dataset_model", (
                        TABLE_AI_DATASET_SNAPSHOTS,
                        TABLE_AI_DATASET_SNAPSHOT_ITEMS,
                        TABLE_AI_MODEL_VERSIONS,
                    )
                ) == READY
            except Exception as exc:
                raise HTTPException(503, "自家LLM模型schema狀態暫時無法驗證") from exc
            if not model_schema_ready:
                raise HTTPException(503, "自家LLM模型registry尚未由正式migration啟用")
            registered = db.query(
                f"""SELECT 1 FROM {TABLE_AI_MODEL_VERSIONS}
                    WHERE model_id=:model AND model_type='llm' AND status='deployable'""",
                {"model": model},
            )
            if registered.empty:
                raise HTTPException(503, "自家LLM未通過deployable評估gate")
        return {"provider":"custom","model":model,"base_url":base_url,
                "api_key":"CUSTOM_LLM_API_KEY","supports_audio":False,"supports_web_search":False,
                "input_price_per_million":0,"output_price_per_million":0,"web_search_price_per_call":0,
                "pricing_note":"自家OpenAI-compatible endpoint。","paid_rate_note":"成本由本地／GPU服務承擔。",
                "selection_label":"自家模型","pricing_label":"自家","is_premium":False}
    if label not in AI_MODEL_OPTIONS:
        raise HTTPException(400, "不支援的 AI 模型")
    return AI_MODEL_OPTIONS[label]


def _estimate(feature, config, has_audio=False):
    inp,out=FEATURE_TOKEN_ESTIMATES.get(feature,(0,0));audio=1200 if has_audio else 0
    usd=(inp*(config.get("input_price_per_million") or 0)+audio*(config.get("audio_input_price_per_million") or config.get("input_price_per_million") or 0)+out*(config.get("output_price_per_million") or 0))/1_000_000
    if feature in ("web_research","fact_check"):usd+=config.get("web_search_price_per_call") or 0
    return {"usd":round(usd,4),"hkd":round(usd*7.8,4)}


def _usage(db, user_id, feature, label, config, success, error="", actual=None, has_audio=False):
    # Use a conservative, transparent ledger estimate;
    # provider billing remains the source of truth in AI基金.
    estimate_inp, estimate_out = FEATURE_TOKEN_ESTIMATES.get(feature, (0, 0))
    actual = actual or {}
    inp = int(actual.get("input_tokens") or estimate_inp)
    out = int(actual.get("output_tokens") or estimate_out)
    audio = int(actual.get("audio_tokens") or (1200 if has_audio else 0))
    search = int(actual.get("search_calls") or (feature in ("web_research", "fact_check")))
    usd = (
        inp * (config.get("input_price_per_million") or 0)
        + audio * (config.get("audio_input_price_per_million") or config.get("input_price_per_million") or 0)
        + out * (config.get("output_price_per_million") or 0)
    ) / 1_000_000
    if search:
        usd += search * (config.get("web_search_price_per_call") or 0)
    try:
        from core.funds_logic import log_ai_usage

        log_ai_usage(
            user_id,
            feature,
            success,
            usage={
                "model_label": label,
                "provider": config.get("provider", ""),
                "estimated_cost_usd": usd,
                "estimated_cost_hkd": usd * 7.8,
                "input_tokens": inp,
                "output_tokens": out,
                "audio_tokens": audio,
                "search_calls": search,
                "cost_source": actual.get("cost_source") or "estimate",
            },
            error_message=error,
            db=db,
        )
    except Exception:
        # A missing optional accounting table must never make coaching unusable.
        pass


def _match_context(db, match_id):
    if not match_id:
        return ""
    from schema import TABLE_DEBATERS, TABLE_MATCHES, TABLE_SCORES
    matches = db.query(
        f"SELECT match_id,topic_text,pro_team,con_team FROM {TABLE_MATCHES} WHERE match_id=:match_id",
        {"match_id": match_id},
    )
    if matches.empty:
        return ""
    row = matches.iloc[0]
    lines = ["## 比賽資料", f"- 場次：{match_id}"]
    if row.get("topic_text"):
        lines.append(f"- 辯題：{row['topic_text']}")
    debaters = db.query(
        f"SELECT side,position,debater_name FROM {TABLE_DEBATERS} WHERE match_id=:match_id ORDER BY side,position",
        {"match_id": match_id},
    )
    for side, team in (("pro", row.get("pro_team")), ("con", row.get("con_team"))):
        names = [str(x).strip() for x in debaters[debaters["side"] == side]["debater_name"].tolist()
                 if x is not None and str(x).strip()] if not debaters.empty else []
        label = "正方" if side == "pro" else "反方"
        if team:
            lines.append(f"- {label}：{team}（{', '.join(filter(None, names))}）" if names else f"- {label}：{team}")
    scores = db.query(
        f"SELECT AVG(pro_total_score) pro_avg,AVG(con_total_score) con_avg FROM {TABLE_SCORES} WHERE match_id=:match_id",
        {"match_id": match_id},
    )
    pro_avg = scores.iloc[0].get("pro_avg") if not scores.empty else None
    con_avg = scores.iloc[0].get("con_avg") if not scores.empty else None
    if pro_avg is not None and con_avg is not None and math.isfinite(float(pro_avg)) and math.isfinite(float(con_avg)):
        lines.extend(["", "## 歷史評分參考",
                      f"- 正方平均總分：{float(pro_avg):.1f}",
                      f"- 反方平均總分：{float(con_avg):.1f}"])
    return "\n".join(lines)


def _topic_context(db, topic):
    if not topic:
        return ""
    from schema import TABLE_TOPICS
    rows = db.query(f"SELECT category,difficulty FROM {TABLE_TOPICS} WHERE topic_text=:topic", {"topic": topic})
    if rows.empty:
        return ""
    row = rows.iloc[0]
    parts = []
    if row.get("category"):
        parts.append(f"類別：{row['category']}")
    if row.get("difficulty") in DIFFICULTY_OPTIONS:
        parts.append(f"難度：{DIFFICULTY_OPTIONS[row['difficulty']]}")
    return "辯題資料：" + "，".join(parts) if parts else ""


def _message(body: CoachRequest, db=None):
    feature = body.feature
    if feature == "strategy":
        return build_strategy_prompt(body.debate_format), build_strategy_user_prompt(
            body.topic, body.side, body.debate_format, _topic_context(db, body.topic) if db else ""
        )
    if feature == "speech_review":
        position = {1: "主辯", 2: "一副", 3: "二副", 4: "結辯", 5: "三副"}.get(body.position, "")
        system = QA_REVIEW_SYSTEM_PROMPT if "台下發問練習" in body.text or "交互答問練習" in body.text else SPEECH_REVIEW_SYSTEM_PROMPT
        lines = [f"我嘅辯位：{body.side}{position}"]
        context = _match_context(db, body.match_id) if db and body.match_id else ""
        if context:
            lines.append(context)
        else:
            lines.extend([f"辯題：{body.topic}", f"立場：{body.side}"])
        lines.append(f"\n## 我嘅演辭內容\n{body.text or '以下係我嘅演辭錄音，請分析：'}")
        return system, "\n".join(lines)
    today = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d")
    if feature == "web_research":
        return WEB_RESEARCH_SYSTEM_PROMPT, build_web_research_user_prompt(today, body.topic, body.research_need)
    if feature == "fact_check":
        return FACT_CHECK_SYSTEM_PROMPT, build_fact_check_user_prompt(today, body.text)
    raise HTTPException(400, "不支援的 AI 功能")


async def _generate(config, system, user, body, user_id=""):
    from deploy.proxy import _get_proxy_secret
    key_name = config.get("api_key") or ("OPENROUTER_API_KEY" if config["provider"] == "openrouter" else "GEMINI_API_KEY")
    key = _get_proxy_secret(key_name).strip()
    if not key:
        raise HTTPException(503, f"未設定 {key_name}")
    if body.audio_base64:
        if not config.get("supports_audio"):
            raise HTTPException(400, "所選模型不支援錄音分析，請改貼逐字稿或選 Gemini。")
        try:
            audio_bytes = base64.b64decode(body.audio_base64, validate=True)
        except Exception as exc:
            raise HTTPException(400, "錄音資料無法讀取") from exc
        if len(audio_bytes) > MAX_COACH_AUDIO_BYTES:
            raise HTTPException(413, "錄音不可超過2MB")
    from core.ai_provider import generate_text
    async with AI_COACH_SEMAPHORE:
        try:
            if body.audio_base64:
                from deploy.proxy import record_bandwidth_usage
                await asyncio.to_thread(
                    record_bandwidth_usage, "ai_coach_audio_provider",
                    len(body.audio_base64.encode("ascii")), str(user_id),
                    aggregate_key=f"user={str(user_id)[:120]}",
                )
            return await generate_text(config, system, user, api_key=key,
                audio_base64=body.audio_base64, audio_mime=body.audio_mime,
                web_search=body.feature in ("web_research", "fact_check"))
        except Exception as exc:
            raise HTTPException(502, f"AI 服務錯誤：{str(exc)[:300]}") from exc


@router.get("/data")
def data(request: Request):
    _context(request)
    from deploy.proxy import get_vote_db, _get_proxy_secret
    from schema import TABLE_TOPICS, TABLE_MATCHES
    from debate_timing import (DEBATE_FORMATS, full_mock_total_seconds,
                               get_debate_timer_config, get_full_mock_sequence,
                               split_mock_into_sessions)
    db=get_vote_db()
    topics=db.query(f"SELECT topic_text,category,difficulty FROM {TABLE_TOPICS} ORDER BY category,topic_text LIMIT :topic_limit", {"topic_limit": AI_COACH_TOPIC_LIMIT})
    matches=db.query(f"SELECT match_id,topic_text,pro_team,con_team FROM {TABLE_MATCHES} ORDER BY match_id DESC LIMIT :match_limit", {"match_limit": AI_COACH_MATCH_LIMIT})
    balance=db.query("SELECT COALESCE(SUM(CASE WHEN transaction_type='member_deposit' THEN amount_hkd WHEN transaction_type='provider_topup' THEN -amount_hkd WHEN transaction_type IN ('refund','provider_refund') THEN amount_hkd WHEN transaction_type='member_refund' THEN -amount_hkd WHEN transaction_type='adjustment' THEN amount_hkd ELSE 0 END),0) balance FROM ai_fund_transactions WHERE status='confirmed'")
    from core.config_store import get_config
    low_balance_hkd = float(get_config(db, "ai_fund_low_balance_hkd", 100) or 100)
    models=[]
    for label,item in AI_MODEL_OPTIONS.items():
        key_name="OPENROUTER_API_KEY" if item["provider"]=="openrouter" else "GEMINI_API_KEY"
        estimates={f:_estimate(f,item) for f in ("strategy","speech_review","web_research","fact_check")}
        estimates["speech_review_audio"]=_estimate("speech_review",item,has_audio=True)
        models.append({"label":label,"selection_label":item.get("selection_label",""),"supports_audio":item["supports_audio"],"supports_web_search":item["supports_web_search"],"note":f"{item['pricing_note']} {item.get('paid_rate_note','')}".strip(),"pricing_label":item.get("pricing_label",""),"is_premium":bool(item.get("is_premium")),"api_key_name":key_name,"available":bool(_get_proxy_secret(key_name)),"estimates":estimates})
    try:
        custom = _config("自家辯論 LLM", db)
        models.append({"label":"自家辯論 LLM","selection_label":"自家模型","supports_audio":False,
            "supports_web_search":False,"note":custom["pricing_note"],"pricing_label":"自家",
            "is_premium":False,"api_key_name":"CUSTOM_LLM_API_KEY","available":True,
            "estimates":{feature:{"usd":0,"hkd":0} for feature in ("strategy","speech_review","web_research","fact_check","speech_review_audio")}})
    except HTTPException:
        pass
    mock_formats = {}
    for name in DEBATE_FORMATS:
        segments = get_full_mock_sequence(name, free_debate_minutes=5 if name == "聯中" else None)
        mock_formats[name] = {
            "segments": segments,
            "session_count": len(split_mock_into_sessions(segments)),
            "total_minutes": full_mock_total_seconds(segments) / 60,
        }
    from deploy.proxy import bandwidth_budget_status
    return {
        "models": models, "default_model": DEFAULT_AI_MODEL,
        "topics": [dict(x) for x in topics.to_dict("records")],
        "matches": [dict(x) for x in matches.to_dict("records")],
        "formats": {name: get_debate_timer_config(name) for name in DEBATE_FORMATS},
        "mock_formats": mock_formats,
        "fund": {
            "balance_hkd": float(balance.iloc[0]["balance"] or 0),
            "low_balance_hkd": low_balance_hkd,
        },
        "azure_tts": bool(
            _get_proxy_secret("AZURE_SPEECH_KEY")
            and _get_proxy_secret("AZURE_SPEECH_REGION")
        ),
        "bandwidth_budget": bandwidth_budget_status(notify=True),
        "resource_limits": {
            "audio_max_bytes": AI_COACH_MAX_AUDIO_BYTES,
            "audio_max_seconds": AI_COACH_MAX_AUDIO_SECONDS,
            "solo_free_monthly": SOLO_FREE_MONTHLY_LIMIT,
            "solo_mock_monthly": SOLO_MOCK_MONTHLY_LIMIT,
            "multiplayer_free_monthly": MULTIPLAYER_FREE_MONTHLY_ROOMS,
            "multiplayer_mock_monthly": MULTIPLAYER_MOCK_MONTHLY_ROOMS,
            "free_max_minutes": LIVE_FREE_MAX_MINUTES,
            "max_rooms": MAX_ROOMS,
            "relay_max_bytes": GEMINI_RELAY_MAX_BYTES,
        },
    }


@router.get("/mock-plan")
def mock_plan(request: Request):
    _context(request)
    from debate_timing import (DEBATE_FORMATS, full_mock_total_seconds,
                               get_full_mock_sequence, split_mock_into_sessions)
    debate_format = request.query_params.get("format") or DEBATE_FORMATS[0]
    if debate_format not in DEBATE_FORMATS:
        raise HTTPException(400, "不支援的賽制")
    try:
        minutes = float(request.query_params.get("minutes") or 5)
    except ValueError as exc:
        raise HTTPException(400, "自由辯論時間無效") from exc
    minutes = min(10, max(2, minutes))
    segments = get_full_mock_sequence(
        debate_format, free_debate_minutes=minutes if debate_format == "聯中" else None
    )
    return {"segments": segments, "session_count": len(split_mock_into_sessions(segments)),
            "total_minutes": full_mock_total_seconds(segments) / 60}


@router.post("/run")
async def run(body: CoachRequest, request: Request):
    user_id = _context(request)
    from deploy.proxy import get_vote_db, _get_proxy_secret, _bandwidth_essential_gate_error
    budget_error = _bandwidth_essential_gate_error()
    if budget_error: raise HTTPException(429, budget_error)
    db = get_vote_db()
    config = _config(body.model_label, db)
    key_name=config.get("api_key") or ("OPENROUTER_API_KEY" if config["provider"]=="openrouter" else "GEMINI_API_KEY")
    if not _get_proxy_secret(key_name): raise HTTPException(503,f"未設定 {key_name}")
    system, user = _message(body, db)
    if body.feature in ("speech_review", "strategy"):
        try:
            from core.rag import retrieve_rag_context
            rag = await retrieve_rag_context(db, _get_proxy_secret("GEMINI_API_KEY").strip(),
                "\n".join(x for x in (body.topic, body.text, body.side, body.research_need) if x))
            if rag: user += "\n\n" + rag
        except Exception:
            pass
    try:
        result, actual = await _generate(config, system, user, body, user_id)
    except HTTPException as exc:
        if config.get("provider") == "custom":
            fallback = AI_MODEL_OPTIONS[DEFAULT_AI_MODEL]
            result, actual = await _generate(fallback, system, user, body, user_id)
            config = fallback
        else:
            _usage(db, user_id, body.feature, body.model_label, config, False, exc.detail)
            raise
    _usage(db, user_id, body.feature, body.model_label, config, True,
           actual=actual, has_audio=bool(body.audio_base64))
    return {"ok": True, "markdown": result}

@router.post("/prepare-live")
async def prepare_live(body:LivePrepareRequest,request:Request):
    user_id=_context(request);proxy=__import__('deploy.proxy',fromlist=['get_vote_db','_solo_live_quota_error']);db=proxy.get_vote_db();config=_config(body.model_label,db)
    quota_error=proxy._solo_live_quota_error(user_id,body.mode)
    if quota_error: raise HTTPException(429,quota_error)
    prepare_error=_reserve_prepare_live(db,user_id)
    if prepare_error: raise HTTPException(429,prepare_error)
    if not config.get("supports_web_search"):
        config=AI_MODEL_OPTIONS[DEFAULT_AI_MODEL]
    need=f"為{body.mode}練習準備正反雙方最新事實、數據、例子、攻防位及可靠來源。賽制：{body.debate_format}；使用者立場：{body.side}。"
    system,user=_message(CoachRequest(feature="web_research",model_label=body.model_label,topic=body.topic,research_need=need))
    try:
        from core.rag import retrieve_rag_context
        from deploy.proxy import _get_proxy_secret
        rag=await retrieve_rag_context(db,_get_proxy_secret("GEMINI_API_KEY").strip(),body.topic+"\n"+need)
        if rag:user += "\n\n"+rag
    except Exception:pass
    actual = None
    try:brief,actual=await _generate(config,system,user,CoachRequest(feature="web_research",model_label=body.model_label,topic=body.topic,research_need=need))
    except Exception:brief=""
    _usage(__import__('deploy.proxy',fromlist=['get_vote_db']).get_vote_db(),user_id,"web_research",body.model_label,config,bool(brief),actual=actual)
    key=secrets.token_urlsafe(18);now=datetime.now()
    db.execute(f"DELETE FROM {_LIVE_BRIEF_TABLE} WHERE expires_at<:now", {"now": now.isoformat(sep=" ",timespec="seconds")})
    db.execute(f"INSERT INTO {_LIVE_BRIEF_TABLE}(brief_id,user_id,brief,expires_at,created_at) VALUES(:key,:user,:brief,:expires,:created)",{"key":key,"user":user_id,"brief":brief[:LIVE_BRIEF_MAX_CHARS],"expires":(now+timedelta(minutes=LIVE_BRIEF_TTL_MINUTES)).isoformat(sep=" ",timespec="seconds"),"created":now.isoformat(sep=" ",timespec="seconds")});return {"ok":True,"brief_id":key,"research_ready":bool(brief)}
