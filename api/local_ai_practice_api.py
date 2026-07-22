"""Turn-based local-AI debate practice API."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ai_name import LMC_AI_MODEL_LABEL
from api.access import require_interactive_features_available, require_page_user
from core.funds_logic import log_ai_usage
from core.local_ai_practice import (
    LocalPracticeConflict,
    LocalPracticeStore,
    build_feedback_user_prompt,
    build_opening_user_prompt,
    build_reply_user_prompt,
    build_system_prompt,
)
from debate_timing import FREE_DEBATE_FORMATS
from system_limits import (
    LIVE_FREE_MAX_MINUTES,
    LMC_AI_MESSAGE_MAX_CHARS,
    LOCAL_PRACTICE_FEEDBACK_MAX_CHARS,
    LOCAL_PRACTICE_REPLY_MAX_CHARS,
    LOCAL_PRACTICE_TURN_MAX,
)


router = APIRouter(prefix="/api/ai-coach/local-practice", tags=["local-ai-practice"])
STORE = LocalPracticeStore()
_SESSION_RE = re.compile(r"[0-9a-f]{32,64}")
logger = logging.getLogger(__name__)


class StartBody(BaseModel):
    session_id: str = Field(min_length=32, max_length=64)
    topic: str = Field(min_length=1, max_length=500)
    side: Literal["正方", "反方"] = "正方"
    debate_format: Literal["校園隨想", "聯中"] = "校園隨想"
    minutes: float = Field(default=2.5, ge=0.5, le=LIVE_FREE_MAX_MINUTES)


class SessionBody(BaseModel):
    session_id: str = Field(min_length=32, max_length=64)


class TurnStartBody(SessionBody):
    expected_turn: int = Field(ge=0, le=LOCAL_PRACTICE_TURN_MAX)


class TurnBody(TurnStartBody):
    text: str = Field(min_length=1, max_length=LMC_AI_MESSAGE_MAX_CHARS)


class LocalTtsBody(SessionBody):
    turn_index: int = Field(ge=0, le=LOCAL_PRACTICE_TURN_MAX)


def _context(request: Request) -> str:
    user_id = require_page_user(request, "ai_coach")
    require_interactive_features_available(request)
    return str(user_id)


def _valid_session_id(value: str) -> str:
    clean = str(value or "").strip().lower()
    if not _SESSION_RE.fullmatch(clean):
        raise HTTPException(400, "練習識別碼無效。")
    return clean


def _store_call(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except LocalPracticeConflict as exc:
        raise HTTPException(409, str(exc)) from exc


async def _capabilities(db) -> dict:
    from core.lmc_ai_client import local_ai_availability
    from deploy import proxy

    status = await local_ai_availability(db)
    fast = next(
        (item for item in status.get("modes", []) if item.get("id") == "fast"),
        None,
    )
    bandwidth = proxy.bandwidth_budget_status(notify=True)
    azure_ready = bool(
        proxy.tts_provider_configured()
        and int(bandwidth.get("total_bytes") or 0)
        < int(bandwidth.get("stop_live_bytes") or 0)
    )
    # Protocol v1 advertises chat only.  The Workstation rollout will turn
    # these on only after its direct-R2 ASR/TTS contract passes preflight.
    return {
        "text": bool(status.get("available") and fast and fast.get("available")),
        "asr": False,
        "local_tts": False,
        "azure_tts": azure_ready,
        "status": status.get("state") or "unavailable",
        "message": status.get("message") or "自家 AI 暫時未準備好。",
        "mode": "fast",
        "mode_label": "快速回覆",
    }


async def _generate(owner_id: str, session: dict, *, stage: str) -> str:
    from core.lmc_ai_client import LocalAIError, generate_local_text
    from deploy.proxy import get_vote_db

    if stage == "opening":
        user_prompt = build_opening_user_prompt(session)
    elif stage == "feedback":
        user_prompt = build_feedback_user_prompt(session)
    else:
        user_prompt = build_reply_user_prompt(session)
    attempted = False

    def mark_attempted():
        nonlocal attempted
        attempted = True

    operation_id = f"local-practice:{session['session_id']}:{stage}:{session['turn_index']}"
    db = get_vote_db()
    try:
        answer, usage = await generate_local_text(
            db,
            actor_id=owner_id,
            system_prompt=build_system_prompt(session),
            user_prompt=user_prompt,
            mode="fast",
            operation_stage=f"local_practice_{stage}",
            on_provider_attempt=mark_attempted,
        )
    except LocalAIError as exc:
        if attempted:
            try:
                await asyncio.to_thread(
                    log_ai_usage,
                    owner_id,
                    "local_ai_practice",
                    False,
                    {
                        "provider": "other",
                        "model_label": LMC_AI_MODEL_LABEL,
                        "operation_id": operation_id,
                        "operation_stage": stage,
                    },
                    str(exc)[:300],
                    db,
                )
            except Exception:
                logger.warning("local practice usage ledger write failed")
        raise HTTPException(503, str(exc)) from exc
    answer_limit = (
        LOCAL_PRACTICE_FEEDBACK_MAX_CHARS
        if stage == "feedback"
        else LOCAL_PRACTICE_REPLY_MAX_CHARS
    )
    if len(answer) > answer_limit:
        try:
            await asyncio.to_thread(
                log_ai_usage,
                owner_id,
                "local_ai_practice",
                False,
                {
                    **usage,
                    "provider": "other",
                    "model_label": LMC_AI_MODEL_LABEL,
                    "operation_id": operation_id,
                    "operation_stage": stage,
                },
                "自家 AI 回覆超過安全長度。",
                db,
            )
        except Exception:
            logger.warning("local practice usage ledger write failed")
        raise HTTPException(502, "自家 AI 回覆超過安全長度，請重新開始練習。")
    try:
        await asyncio.to_thread(
            log_ai_usage,
            owner_id,
            "local_ai_practice",
            True,
            {
                **usage,
                "provider": "other",
                "model_label": LMC_AI_MODEL_LABEL,
                "operation_id": operation_id,
                "operation_stage": stage,
            },
            "",
            db,
        )
    except Exception:
        # Keep the answer visible during a temporary optional-ledger failure.
        logger.warning("local practice usage ledger write failed")
    return answer


async def _finish_feedback(owner_id: str, session: dict) -> dict:
    feedback = await _generate(owner_id, session, stage="feedback")
    return _store_call(
        STORE.complete_feedback, session["session_id"], owner_id, feedback
    )


def _response(session: dict, capabilities: dict | None = None) -> JSONResponse:
    payload = {"ok": True, "session": session}
    if capabilities is not None:
        payload["capabilities"] = capabilities
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/session/{session_id}")
async def local_practice_session(session_id: str, request: Request):
    owner_id = _context(request)
    from deploy.proxy import get_vote_db

    session = _store_call(
        STORE.snapshot, _valid_session_id(session_id), owner_id
    )
    return _response(session, await _capabilities(get_vote_db()))


@router.post("/start")
async def start_local_practice(body: StartBody, request: Request):
    owner_id = _context(request)
    session_id = _valid_session_id(body.session_id)
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(400, "請輸入辯題。")
    if body.debate_format not in FREE_DEBATE_FORMATS:
        raise HTTPException(400, "不支援的賽制。")
    minutes = 2.5 if body.debate_format == "校園隨想" else float(body.minutes)
    seconds_per_side = round(minutes * 60)

    from deploy.proxy import get_vote_db

    capabilities = await _capabilities(get_vote_db())
    if not capabilities["text"]:
        raise HTTPException(503, capabilities["message"])
    session = _store_call(
        STORE.create,
        session_id=session_id,
        owner_id=owner_id,
        topic=topic,
        user_side=body.side,
        debate_format=body.debate_format,
        seconds_per_side=seconds_per_side,
    )
    if session["state"] == "generating_ai" and not session["transcript"]:
        try:
            opening = await _generate(owner_id, session, stage="opening")
            session = _store_call(
                STORE.complete_ai_turn, session_id, owner_id, opening
            )
        except HTTPException as exc:
            _store_call(STORE.fail, session_id, owner_id, str(exc.detail))
            raise
    return _response(session, capabilities)


@router.post("/turn/start")
async def start_local_practice_turn(body: TurnStartBody, request: Request):
    owner_id = _context(request)
    session = _store_call(
        STORE.start_user_turn,
        _valid_session_id(body.session_id),
        owner_id,
        expected_turn=body.expected_turn,
    )
    return _response(session)


@router.post("/turn")
async def submit_local_practice_turn(body: TurnBody, request: Request):
    owner_id = _context(request)
    session_id = _valid_session_id(body.session_id)
    claim = _store_call(
        STORE.submit_user_turn,
        session_id,
        owner_id,
        expected_turn=body.expected_turn,
        text=body.text,
    )
    session = claim["session"]
    try:
        if claim["action"] == "feedback":
            return _response(await _finish_feedback(owner_id, session))
        reply = await _generate(owner_id, session, stage="reply")
        session = _store_call(STORE.complete_ai_turn, session_id, owner_id, reply)
        if session["state"] == "generating_feedback":
            session = await _finish_feedback(owner_id, session)
        return _response(session)
    except HTTPException as exc:
        _store_call(STORE.fail, session_id, owner_id, str(exc.detail))
        raise


@router.post("/stop")
async def stop_local_practice(body: SessionBody, request: Request):
    owner_id = _context(request)
    session_id = _valid_session_id(body.session_id)
    session = _store_call(STORE.reserve_feedback, session_id, owner_id)
    if session["state"] == "ended":
        return _response(session)
    try:
        return _response(await _finish_feedback(owner_id, session))
    except HTTPException as exc:
        _store_call(STORE.fail, session_id, owner_id, str(exc.detail))
        raise


@router.post("/tts/local")
async def local_practice_tts(body: LocalTtsBody, request: Request):
    owner_id = _context(request)
    _store_call(STORE.snapshot, _valid_session_id(body.session_id), owner_id)
    # Fail closed until a Workstation advertises the signed direct-R2 TTS
    # capability.  Never relay local audio bytes through Render.
    raise HTTPException(503, "自家讀音模型暫時未能使用。")
