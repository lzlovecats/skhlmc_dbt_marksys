"""Organiser results and the official AI third-judge workflow."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from account_access import KIOSK_ACCOUNT_ID
from api.access import require_competition_staff
from system_limits import (
    AI_PROVIDER_RESPONSE_MAX_BYTES,
    OFFICIAL_AI_JUDGE_CONCURRENCY,
    OFFICIAL_AI_JUDGE_PROMPT_MAX_CHARS,
    OFFICIAL_AI_JUDGE_TIMEOUT_SECONDS,
)

router = APIRouter(prefix="/api/management", tags=["management"])
OFFICIAL_AI_JUDGE_SEMAPHORE = asyncio.Semaphore(OFFICIAL_AI_JUDGE_CONCURRENCY)
logger = logging.getLogger(__name__)


class OfficialAiJudgeBody(BaseModel):
    match_id: str = Field(min_length=1, max_length=200)
    model_label: str = Field(min_length=1, max_length=200)


def _require_admin(request: Request):
    return require_competition_staff(request)


def _runtime_model_options(db) -> list[dict]:
    from ai_model_config import (
        AI_MODEL_OPTIONS,
        OFFICIAL_AI_JUDGE_DEFAULT_MODEL,
        resolve_interactive_model_settings,
    )
    from core.config_store import get_configs
    from deploy.proxy import _get_proxy_secret

    try:
        configured = get_configs(db, ("ai_enabled_providers", "ai_default_model"))
    except Exception:
        configured = {}
    enabled, _default = resolve_interactive_model_settings(
        configured.get("ai_enabled_providers"), configured.get("ai_default_model")
    )
    options = []
    for label, config in AI_MODEL_OPTIONS.items():
        if config.get("provider") not in enabled:
            continue
        key_name = str(config.get("api_key") or "")
        options.append({
            "label": label,
            "provider": str(config.get("provider") or ""),
            "selection_label": str(config.get("selection_label") or ""),
            "pricing_label": str(config.get("pricing_label") or ""),
            "available": bool(key_name and _get_proxy_secret(key_name).strip()),
            "is_default": label == OFFICIAL_AI_JUDGE_DEFAULT_MODEL,
        })
    return options


def _ai_state(match_id: str, db) -> dict:
    from api.projector_ai_api import load_official_ai_judge_evidence
    from core.official_ai_judge import state_data

    state = state_data(match_id, db=db)
    state["model_options"] = _runtime_model_options(db)
    pinned_session = str((state.get("run") or {}).get("projector_session_id") or "")
    try:
        evidence = load_official_ai_judge_evidence(
            match_id, session_id=pinned_session, db=db
        )
        state["transcript_available"] = True
        state["transcript_expires_at"] = evidence["result_expires_at"]
    except ValueError:
        state["transcript_available"] = False
        state["transcript_expires_at"] = ""
    return state


def _usage_metadata(config, usage) -> dict:
    actual = usage if isinstance(usage, dict) else {}
    input_tokens = int(actual.get("input_tokens") or 0)
    output_tokens = int(actual.get("output_tokens") or 0)
    usd = (
        input_tokens * float(config.get("input_price_per_million") or 0)
        + output_tokens * float(config.get("output_price_per_million") or 0)
    ) / 1_000_000
    return {
        "provider": config.get("provider") or "other",
        "estimated_cost_usd": usd,
        "estimated_cost_hkd": usd * 7.8,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "audio_tokens": 0,
        "search_calls": 0,
        "cost_source": actual.get("cost_source") or (
            "estimate"
            if input_tokens or output_tokens
            else "provider_attempt_usage_unavailable"
        ),
    }


@router.get("/data")
def data(request: Request, match_id: str | None = None):
    from core.results_logic import results_data
    from deploy.proxy import get_vote_db
    _require_admin(request)
    try:
        db = get_vote_db()
        payload = results_data(match_id, db=db)
        selected = str(payload.get("selected_match_id") or "")
        payload["official_ai_judge"] = _ai_state(selected, db) if selected else None
        return payload
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("management result load failed match=%s", match_id or "")
        raise HTTPException(503, "讀取評分暫時失敗，請稍後再試。") from exc


@router.post("/ai-judge/attempt")
async def official_ai_judge_attempt(body: OfficialAiJudgeBody, request: Request):
    from ai_model_config import get_official_ai_judge_model
    from api.projector_ai_api import load_official_ai_judge_evidence
    from core.ai_provider import generate_text
    from core.official_ai_judge import (
        fail_attempt,
        finalize_success,
        load_match_context,
        mark_provider_attempted,
        parse_ai_score_json,
        reserve_attempt,
        state_data,
    )
    from core.config_store import get_configs
    from deploy.proxy import (
        _bandwidth_essential_gate_error,
        _get_proxy_secret,
        get_vote_db,
    )
    from prompts import OFFICIAL_AI_JUDGE_SYSTEM_PROMPT, build_official_ai_judge_prompt
    from ai_model_config import resolve_interactive_model_settings

    user_id = _require_admin(request)
    db = get_vote_db()
    budget_error = _bandwidth_essential_gate_error()
    if budget_error:
        raise HTTPException(429, budget_error)
    model_label, config = get_official_ai_judge_model(body.model_label)
    try:
        configured = get_configs(db, ("ai_enabled_providers", "ai_default_model"))
    except Exception:
        configured = {}
    enabled, _default = resolve_interactive_model_settings(
        configured.get("ai_enabled_providers"), configured.get("ai_default_model")
    )
    if config.get("provider") not in enabled:
        raise HTTPException(400, "所選模型的 provider 未獲系統啟用。")
    key_name = str(config.get("api_key") or "")
    api_key = _get_proxy_secret(key_name).strip() if key_name else ""
    if not api_key:
        raise HTTPException(503, "所選模型尚未設定 provider key。")

    before = state_data(body.match_id, db=db)
    pinned_session = str((before.get("run") or {}).get("projector_session_id") or "")
    try:
        evidence = load_official_ai_judge_evidence(
            body.match_id, session_id=pinned_session, db=db
        )
        context = load_match_context(body.match_id, db=db)
        claim = reserve_attempt(
            body.match_id,
            evidence["session_id"],
            model_label,
            user_id,
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    prompt = build_official_ai_judge_prompt(context, evidence["transcript"])
    provider_attempted = False
    usage = {}

    def mark_attempt_started():
        nonlocal provider_attempted
        mark_provider_attempted(claim, db=db)
        provider_attempted = True

    try:
        async with OFFICIAL_AI_JUDGE_SEMAPHORE:
            raw_result, usage = await generate_text(
                config,
                OFFICIAL_AI_JUDGE_SYSTEM_PROMPT,
                prompt,
                api_key=api_key,
                web_search=False,
                max_prompt_chars=OFFICIAL_AI_JUDGE_PROMPT_MAX_CHARS,
                timeout_seconds=OFFICIAL_AI_JUDGE_TIMEOUT_SECONDS,
                temperature=0.1,
                require_complete=True,
                structured_json=True,
                preserve_text=True,
                max_response_bytes=AI_PROVIDER_RESPONSE_MAX_BYTES,
                on_provider_attempt=mark_attempt_started,
            )
        result = parse_ai_score_json(raw_result, context, claim["deductions"])
        usage_record = _usage_metadata(config, usage)
        judge_name = finalize_success(
            claim,
            result,
            db=db,
            usage_actor=KIOSK_ACCOUNT_ID,
            usage=usage_record,
        )
    except Exception as exc:
        logger.exception(
            "official AI judge attempt failed match=%s attempt=%s model=%s",
            body.match_id,
            claim["attempt_no"],
            model_label,
        )
        public_error = "AI 模型未能完成有效分紙。"
        response_usage = getattr(exc, "usage", None)
        if isinstance(response_usage, dict):
            usage = dict(response_usage)
        usage_record = _usage_metadata(config, usage) if provider_attempted else None
        status = fail_attempt(
            claim,
            public_error,
            db=db,
            usage_actor=KIOSK_ACCOUNT_ID,
            usage=usage_record,
        )
        return {
            "ok": False,
            "status": status,
            "message": (
                "AI provider 尚未開始，今次不計作一次嘗試，請重新提交。"
                if status == "ready"
                else (
                    "第一次 AI 評分失敗，請轉用另一個模型重試一次。"
                    if status == "retryable"
                    else "兩次 AI 評分均失敗，賽果會直接按真人評判分紙公布。"
                )
            ),
            "official_ai_judge": _ai_state(body.match_id, db),
        }
    return {
        "ok": True,
        "judge_name": judge_name,
        "official_ai_judge": _ai_state(body.match_id, db),
    }
