"""Turn-based local-AI debate practice API."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Literal
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ai_model_config import resolve_lmc_ai_mode_options
from ai_name import LMC_AI_MODEL_LABEL
from api.access import require_interactive_features_available, require_page_user
from core.funds_logic import log_ai_usage
from core.media_probe import MediaProbeError, audio_extension, canonical_audio_mime
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
    LOCAL_PRACTICE_AUDIO_MAX_BYTES,
    LOCAL_PRACTICE_AUDIO_MAX_SECONDS,
    LOCAL_PRACTICE_CONTEXT_MAX_CHARS,
    LMC_AI_MESSAGE_MAX_CHARS,
    LOCAL_PRACTICE_FEEDBACK_MAX_CHARS,
    LOCAL_PRACTICE_MEDIA_PRUNE_INTERVAL_SECONDS,
    LOCAL_PRACTICE_REPLY_MAX_CHARS,
    LOCAL_PRACTICE_TURN_MAX,
    R2_DOWNLOAD_URL_TTL_SECONDS,
    R2_OBJECT_CACHE_MAX_AGE_SECONDS,
    WORKSTATION_TTS_OUTPUT_MAX_BYTES,
)


router = APIRouter(prefix="/api/ai-coach/local-practice", tags=["local-ai-practice"])
STORE = LocalPracticeStore()
_SESSION_RE = re.compile(r"[0-9a-f]{32,64}")
logger = logging.getLogger(__name__)


async def local_practice_media_retention_loop(db_factory) -> None:
    """Continuously enforce temporary-R2 TTLs without request traffic.

    The loop is started by the application lifespan.  It performs no work when
    R2 is not configured and contains failures so serving practice is not tied
    to a best-effort cleanup pass.
    """
    from core import r2_storage

    while True:
        try:
            if r2_storage.configured():
                db = db_factory()
                for cleaner in (
                    r2_storage.prune_local_practice_media,
                    r2_storage.prune_workstation_r2_health_probes,
                ):
                    try:
                        await asyncio.to_thread(cleaner, db)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # One media lifecycle must not starve the other. Never
                        # log provider details, object keys or owner IDs.
                        logger.warning("Temporary R2 cleanup pass failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Provider exceptions may contain object identifiers.  Keep the
            # periodic failure observable without putting media keys or member
            # identifiers into application logs.
            logger.warning("Temporary R2 media retention failed")
        await asyncio.sleep(LOCAL_PRACTICE_MEDIA_PRUNE_INTERVAL_SECONDS)


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


class RecordingIntentBody(TurnStartBody):
    mime_type: str = Field(max_length=80)
    byte_size: int = Field(gt=0, le=LOCAL_PRACTICE_AUDIO_MAX_BYTES)
    sha256: str = Field(min_length=64, max_length=64)


class RecordingCompleteBody(TurnStartBody):
    intent_id: str = Field(min_length=32, max_length=64)


class AudioTurnBody(RecordingCompleteBody):
    pass


@router.post("/asr/warmup")
async def local_practice_asr_warmup(body: TurnStartBody, request: Request):
    owner_id = _context(request)
    session_id = _valid_session_id(body.session_id)
    session = _store_call(STORE.snapshot, session_id, owner_id)
    if (
        session["state"] != "user_speaking"
        or session["turn_index"] != body.expected_turn
        or not session.get("voice_reserved")
    ):
        raise HTTPException(409, "目前回合未能預載語音辨識。")
    from core.lmc_ai_client import LocalAIError, run_workstation_job
    from deploy.proxy import get_vote_db

    try:
        result = await run_workstation_job(
            get_vote_db(),
            operation_id=f"asr-prewarm.{uuid.uuid4().hex}",
            job_kind="asr.prepare",
            session_id=session_id,
            turn_id=f"turn-{body.expected_turn}",
            stage="asr_model_load",
            payload={},
        )
    except LocalAIError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "prepared": bool(result.get("prepared"))}


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
    from core.lmc_ai_client import local_ai_availability, workstation_capabilities
    from deploy import proxy

    status = await local_ai_availability(db)
    try:
        workstation = await workstation_capabilities(db)
    except Exception:
        workstation = {}
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
    return {
        "text": bool(status.get("available") and fast and fast.get("available")),
        "workstation": bool(workstation.get("workstation")),
        "asr": bool(workstation.get("asr")),
        "local_tts": bool(workstation.get("local_tts")),
        "rag": bool(workstation.get("rag")),
        "azure_tts": azure_ready,
        "status": status.get("state") or "unavailable",
        "message": status.get("message") or "自家 AI 暫時未準備好。",
        "mode": "fast",
        "mode_label": resolve_lmc_ai_mode_options()["fast"]["label"],
        "audio_max_bytes": LOCAL_PRACTICE_AUDIO_MAX_BYTES,
        "audio_max_seconds": LOCAL_PRACTICE_AUDIO_MAX_SECONDS,
    }


async def _generate(owner_id: str, session: dict, *, stage: str) -> str:
    from core.lmc_ai_client import LocalAIError, generate_local_text, run_workstation_job
    from deploy.proxy import get_vote_db

    prompt_session = _store_call(
        STORE.prompt_context, session["session_id"], owner_id
    )
    if stage == "opening":
        user_prompt = build_opening_user_prompt(prompt_session)
    elif stage == "feedback":
        user_prompt = build_feedback_user_prompt(prompt_session)
    else:
        user_prompt = build_reply_user_prompt(prompt_session)
    attempted = False

    def mark_attempted():
        nonlocal attempted
        attempted = True

    operation_id = f"local-practice:{session['session_id']}:{stage}:{session['turn_index']}"
    db = get_vote_db()
    try:
        if prompt_session.get("voice_reserved"):
            result = await run_workstation_job(
                db,
                operation_id=operation_id,
                job_kind="voice_text",
                session_id=session["session_id"],
                turn_id=f"turn-{session['turn_index']}",
                stage=stage,
                payload={
                    "messages": [
                        {"role": "system", "content": build_system_prompt(prompt_session)},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                on_stage=lambda current: mark_attempted()
                if current == "provider_started" and not attempted else None,
            )
            answer = str(result.get("text") or "").strip()
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            if not answer:
                raise LocalAIError("自家 AI 未有產生有效回覆。")
        else:
            answer, usage = await generate_local_text(
                db,
                actor_id=owner_id,
                system_prompt=build_system_prompt(prompt_session),
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
    completed = _store_call(
        STORE.complete_feedback, session["session_id"], owner_id, feedback
    )
    await _release_workstation(owner_id, completed)
    return completed


async def _release_workstation(owner_id: str, session: dict) -> None:
    if not session.get("voice_reserved"):
        return
    from core.lmc_ai_client import LocalAIError, run_workstation_job
    from deploy.proxy import get_vote_db

    try:
        await run_workstation_job(
            get_vote_db(),
            operation_id=f"local-practice.release.{session['session_id']}",
            job_kind="voice.release",
            session_id=session["session_id"],
            stage="release",
            payload={},
        )
        _store_call(
            STORE.mark_workstation_reserved,
            session["session_id"],
            owner_id,
            False,
        )
    except (LocalAIError, HTTPException):
        # The Manager reservation has its own hard expiry and remains fail-closed.
        logger.warning("local practice Workstation release will rely on manager expiry")


async def _reserve_workstation(
    owner_id: str, session: dict, capabilities: dict, db
) -> dict:
    if not capabilities.get("workstation"):
        return session
    from core.lmc_ai_client import LocalAIError, run_workstation_job

    try:
        await run_workstation_job(
            db,
            operation_id=f"local-practice.reserve.{session['session_id']}",
            job_kind="voice.reserve",
            session_id=session["session_id"],
            stage="reserve",
            payload={
                "session_expires_epoch": int(time.time())
                + max(60, int(session.get("session_remaining_seconds") or 0)),
            },
        )
    except LocalAIError as exc:
        raise HTTPException(503, str(exc)) from exc
    return _store_call(
        STORE.mark_workstation_reserved,
        session["session_id"],
        owner_id,
        True,
    )


def _research_brief(result: dict) -> str:
    parts: list[str] = []
    local = result.get("local") if isinstance(result.get("local"), dict) else {}
    for item in (local.get("results") or ())[:8]:
        if not isinstance(item, dict):
            continue
        payload = json.dumps({
            "citation": str(item.get("citation") or "")[:300],
            "title": str(item.get("title") or "")[:300],
            "url": str(item.get("source_url") or "")[:1000],
            "text": str(item.get("text") or "")[:3000],
        }, ensure_ascii=False, separators=(",", ":"))
        parts.append(
            "<local_rag_source>\n"
            + payload.replace("<", "\\u003c").replace(">", "\\u003e")
            + "\n</local_rag_source>"
        )
    return "\n".join(parts)[:LOCAL_PRACTICE_CONTEXT_MAX_CHARS]


async def _prepare_research(
    owner_id: str, session: dict, capabilities: dict, db
) -> dict:
    if not session.get("voice_reserved") or not capabilities.get("rag"):
        return _store_call(
            STORE.set_research_brief,
            session["session_id"],
            owner_id,
            brief="",
            status="unavailable",
        )
    from core.lmc_ai_client import LocalAIError, run_workstation_job

    try:
        result = await run_workstation_job(
            db,
            operation_id=f"local-practice.rag.{session['session_id']}",
            job_kind="rag",
            session_id=session["session_id"],
            stage="retrieving",
            payload={"query": session["topic"], "top_k": 6},
        )
        brief = _research_brief(result)
        return _store_call(
            STORE.set_research_brief,
            session["session_id"],
            owner_id,
            brief=brief,
            status="local_ready",
        )
    except Exception:
        return _store_call(
            STORE.set_research_brief,
            session["session_id"],
            owner_id,
            brief="",
            status="unavailable",
        )


def _safe_owner(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value))[:48] or "member"


def _verified_media_intent(
    db,
    *,
    owner_id: str,
    intent_id: str,
    media_kind: str,
    session_id: str,
    turn_index: int,
    require_status: str,
) -> tuple[dict, str, dict]:
    from core import r2_storage

    intent = r2_storage.get_upload_intent(
        db, intent_id, owner_id, media_kind,
    )
    if not intent or str(intent.get("status") or "") != require_status:
        raise HTTPException(409, "錄音或語音工作狀態已更新。")
    metadata = intent.get("intent_metadata") or {}
    keys = intent.get("object_keys") or []
    try:
        metadata_turn = int(metadata.get("turn_index"))
    except (TypeError, ValueError):
        metadata_turn = -1
    if (
        len(keys) != 1
        or str(metadata.get("session_id") or "") != session_id
        or metadata_turn != int(turn_index)
    ):
        raise HTTPException(400, "媒體 intent 同目前回合不一致。")
    try:
        remote = r2_storage.head(keys[0])
    except Exception as exc:
        raise HTTPException(400, "私人媒體檔案不存在，請重新處理。") from exc
    expected_size = int(intent.get("declared_bytes") or 0)
    expected_sha = str(metadata.get("sha256") or "").lower()
    expected_mime = str(metadata.get("mime_type") or "").lower()
    if (
        int(remote.get("ContentLength") or 0) != expected_size
        or str((remote.get("Metadata") or {}).get("sha256") or "").lower()
        != expected_sha
        or str(remote.get("ContentType") or "").split(";", 1)[0].lower()
        != expected_mime
    ):
        raise HTTPException(400, "私人媒體檔案大小、SHA256 或 MIME 驗證失敗。")
    return intent, keys[0], remote


def _delete_intent_best_effort(db, intent_id: str, keys: list[str] | tuple[str, ...]) -> None:
    from core import r2_storage

    try:
        if not r2_storage.delete_intent_objects(db, intent_id, keys):
            logger.warning("local practice R2 intent cleanup remains pending")
    except Exception:
        logger.warning("local practice R2 intent cleanup failed")


def _response(session: dict, capabilities: dict | None = None) -> JSONResponse:
    payload = {"ok": True, "session": session}
    if capabilities is not None:
        payload["capabilities"] = capabilities
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


async def _advance_after_user(owner_id: str, session_id: str, claim: dict) -> dict:
    session = claim["session"]
    if claim["action"] == "feedback":
        return await _finish_feedback(owner_id, session)
    reply = await _generate(owner_id, session, stage="reply")
    session = _store_call(STORE.complete_ai_turn, session_id, owner_id, reply)
    if session["state"] == "generating_feedback":
        session = await _finish_feedback(owner_id, session)
    return session


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

    db = get_vote_db()
    capabilities = await _capabilities(db)
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
    try:
        session = await _reserve_workstation(owner_id, session, capabilities, db)
        session = await _prepare_research(owner_id, session, capabilities, db)
        if session["state"] == "generating_ai" and not session["transcript"]:
            opening = await _generate(owner_id, session, stage="opening")
            session = _store_call(
                STORE.complete_ai_turn, session_id, owner_id, opening
            )
    except HTTPException as exc:
        failed = _store_call(STORE.fail, session_id, owner_id, str(exc.detail))
        await _release_workstation(owner_id, failed)
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
        return _response(await _advance_after_user(owner_id, session_id, claim))
    except HTTPException as exc:
        failed = _store_call(STORE.fail, session_id, owner_id, str(exc.detail))
        await _release_workstation(owner_id, failed)
        raise


@router.post("/recording-intent")
async def local_practice_recording_intent(
    body: RecordingIntentBody, request: Request
):
    owner_id = _context(request)
    session_id = _valid_session_id(body.session_id)
    session = _store_call(STORE.snapshot, session_id, owner_id)
    if session["state"] != "user_speaking" or session["turn_index"] != body.expected_turn:
        raise HTTPException(409, "請先開始目前回合，再錄音。")
    from core import r2_storage
    from deploy.proxy import get_vote_db

    db = get_vote_db()
    capabilities = await _capabilities(db)
    if not capabilities.get("asr") or not session.get("voice_reserved"):
        raise HTTPException(503, "自家語音辨識暫時未能使用。")
    if not r2_storage.configured():
        raise HTTPException(503, "Cloudflare R2 尚未完成設定。")
    try:
        mime = canonical_audio_mime(body.mime_type)
    except MediaProbeError as exc:
        raise HTTPException(400, str(exc)) from exc
    sha = body.sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise HTTPException(400, "錄音 SHA256 格式無效。")
    storage = await asyncio.to_thread(
        r2_storage.storage_budget_status, db, refresh=True
    )
    if storage["blocked"]:
        raise HTTPException(429, "R2 已達全系統儲存保護上限。")
    intent_id = uuid.uuid4().hex
    key = (
        f"pending/local-practice/input/{_safe_owner(owner_id)}/"
        f"{session_id}/{body.expected_turn}/{intent_id}.{audio_extension(mime)}"
    )
    reserved, _scope = await asyncio.to_thread(
        r2_storage.reserve_upload_intent,
        db,
        intent_id=intent_id,
        user_id=owner_id,
        media_kind="local_practice_input",
        object_keys=[key],
        declared_bytes=body.byte_size,
        metadata={
            "sha256": sha,
            "mime_type": mime,
            "session_id": session_id,
            "turn_index": body.expected_turn,
        },
    )
    if not reserved:
        raise HTTPException(429, "R2 已達全系統儲存保護上限。")
    try:
        url = await asyncio.to_thread(
            r2_storage.presign_put, key, mime, sha, body.byte_size
        )
    except Exception as exc:
        await asyncio.to_thread(
            _delete_intent_best_effort, db, intent_id, [key]
        )
        raise HTTPException(503, "暫時未能建立錄音上載連結。") from exc
    return {
        "ok": True,
        "intent_id": intent_id,
        "upload": {
            "url": url,
            "headers": {
                "Content-Type": mime,
                "Cache-Control": f"private, max-age={R2_OBJECT_CACHE_MAX_AGE_SECONDS}",
                "x-amz-meta-sha256": sha,
            },
        },
    }


@router.post("/recording-complete")
async def local_practice_recording_complete(
    body: RecordingCompleteBody, request: Request
):
    owner_id = _context(request)
    session_id = _valid_session_id(body.session_id)
    session = _store_call(STORE.snapshot, session_id, owner_id)
    if session["turn_index"] != body.expected_turn:
        raise HTTPException(409, "錄音回合狀態已更新。")
    from core import r2_storage
    from deploy.proxy import get_vote_db

    db = get_vote_db()
    await asyncio.to_thread(
        _verified_media_intent,
        db,
        owner_id=owner_id,
        intent_id=body.intent_id,
        media_kind="local_practice_input",
        session_id=session_id,
        turn_index=body.expected_turn,
        require_status="issued",
    )
    changed = await asyncio.to_thread(
        r2_storage.complete_upload_intent,
        db,
        body.intent_id,
        user_id=owner_id,
        media_kind="local_practice_input",
    )
    if not changed:
        raise HTTPException(409, "錄音 intent 已完成或已被使用。")
    return {"ok": True, "intent_id": body.intent_id}


@router.post("/turn/audio")
async def submit_local_practice_audio_turn(
    body: AudioTurnBody, request: Request
):
    owner_id = _context(request)
    session_id = _valid_session_id(body.session_id)
    from core import r2_storage
    from core.lmc_ai_client import LocalAIError, run_workstation_job
    from deploy.proxy import get_vote_db

    db = get_vote_db()
    capabilities = await _capabilities(db)
    if not capabilities.get("asr"):
        raise HTTPException(503, "自家語音辨識暫時未能使用。")
    intent, key, _remote = await asyncio.to_thread(
        _verified_media_intent,
        db,
        owner_id=owner_id,
        intent_id=body.intent_id,
        media_kind="local_practice_input",
        session_id=session_id,
        turn_index=body.expected_turn,
        require_status="completed",
    )
    if not await asyncio.to_thread(
        r2_storage.claim_completed_upload_intent,
        db,
        body.intent_id,
        user_id=owner_id,
        media_kind="local_practice_input",
    ):
        raise HTTPException(409, "錄音已經轉錄緊或已被使用。")
    try:
        _store_call(
            STORE.begin_audio_processing,
            session_id,
            owner_id,
            expected_turn=body.expected_turn,
            intent_id=body.intent_id,
        )
    except HTTPException:
        await asyncio.to_thread(
            r2_storage.release_processing_upload_intent,
            db,
            body.intent_id,
            user_id=owner_id,
            media_kind="local_practice_input",
        )
        raise
    metadata = intent.get("intent_metadata") or {}
    try:
        download_url = await asyncio.to_thread(
            r2_storage.presign_get,
            key,
            mime_type=str(metadata.get("mime_type") or "application/octet-stream"),
            expires=R2_DOWNLOAD_URL_TTL_SECONDS,
        )
        result = await run_workstation_job(
            db,
            operation_id=(
                f"local-practice.asr.{session_id}.{body.expected_turn}.{body.intent_id}"
            ),
            job_kind="asr",
            session_id=session_id,
            turn_id=f"turn-{body.expected_turn}",
            stage="transcribing",
            payload={
                "download": {
                    "url": download_url,
                    "byte_size": int(intent.get("declared_bytes") or 0),
                    "sha256": str(metadata.get("sha256") or ""),
                },
                "mime_type": str(metadata.get("mime_type") or ""),
                "file_ext": audio_extension(str(metadata.get("mime_type") or "")),
            },
            on_stage=lambda value: _store_call(
                STORE.set_workstation_stage,
                session_id,
                owner_id,
                value,
            ),
        )
        transcript = str(result.get("transcript") or "").strip()
        media = result.get("media") if isinstance(result.get("media"), dict) else {}
        transfer = result.get("transfer") if isinstance(result.get("transfer"), dict) else {}
        if (
            not transcript
            or len(transcript) > LMC_AI_MESSAGE_MAX_CHARS
            or str(media.get("mime_type") or "") != str(metadata.get("mime_type") or "")
            or not 1 <= float(media.get("duration_seconds") or 0) <= LOCAL_PRACTICE_AUDIO_MAX_SECONDS
            or int(transfer.get("byte_size") or 0) != int(intent.get("declared_bytes") or 0)
            or str(transfer.get("sha256") or "").lower()
            != str(metadata.get("sha256") or "").lower()
        ):
            raise LocalAIError("自家語音辨識回傳資料未能通過驗證。")
        claim = _store_call(
            STORE.complete_audio_transcript,
            session_id,
            owner_id,
            expected_turn=body.expected_turn,
            intent_id=body.intent_id,
            text=transcript,
        )
    except Exception as exc:
        await asyncio.to_thread(
            r2_storage.release_processing_upload_intent,
            db,
            body.intent_id,
            user_id=owner_id,
            media_kind="local_practice_input",
        )
        _store_call(
            STORE.resume_audio_input,
            session_id,
            owner_id,
            intent_id=body.intent_id,
        )
        detail = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        raise HTTPException(
            503,
            (detail or "自家語音辨識未能完成。")
            + " 你可以重試相同錄音，或者改用文字。",
        ) from exc
    try:
        await asyncio.to_thread(
            r2_storage.mark_processing_upload_cleanup_pending,
            db,
            body.intent_id,
            user_id=owner_id,
            media_kind="local_practice_input",
        )
    except Exception:
        logger.warning("local practice successful ASR cleanup marker failed")
    await asyncio.to_thread(_delete_intent_best_effort, db, body.intent_id, [key])
    try:
        return _response(await _advance_after_user(owner_id, session_id, claim))
    except HTTPException as exc:
        failed = _store_call(STORE.fail, session_id, owner_id, str(exc.detail))
        await _release_workstation(owner_id, failed)
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
        failed = _store_call(STORE.fail, session_id, owner_id, str(exc.detail))
        await _release_workstation(owner_id, failed)
        raise


@router.post("/tts/local")
async def local_practice_tts(body: LocalTtsBody, request: Request):
    owner_id = _context(request)
    session_id = _valid_session_id(body.session_id)
    session = _store_call(STORE.snapshot, session_id, owner_id)
    if session.get("local_tts_disabled"):
        raise HTTPException(503, "自家讀音模型暫時未能使用。")
    text = _store_call(
        STORE.ai_turn_text,
        session_id,
        owner_id,
        turn_index=body.turn_index,
    )
    from core import r2_storage
    from core.lmc_ai_client import LocalAIError, run_workstation_job
    from deploy.proxy import get_vote_db

    db = get_vote_db()
    cached = _store_call(
        STORE.cached_tts_output,
        session_id,
        owner_id,
        turn_index=body.turn_index,
    )
    if cached:
        try:
            await asyncio.to_thread(
                _verified_media_intent,
                db,
                owner_id=owner_id,
                intent_id=str(cached["intent_id"]),
                media_kind="local_practice_tts_output",
                session_id=session_id,
                turn_index=body.turn_index,
                require_status="completed",
            )
            audio_url = await asyncio.to_thread(
                r2_storage.presign_get,
                str(cached["key"]),
                mime_type=str(cached["mime_type"]),
                expires=R2_DOWNLOAD_URL_TTL_SECONDS,
            )
            return JSONResponse(
                {"ok": True, "audio_url": audio_url, "cached": True},
                headers={"Cache-Control": "no-store"},
            )
        except Exception:
            _store_call(STORE.disable_local_tts, session_id, owner_id)
            raise HTTPException(503, "自家讀音模型暫時未能使用。")

    capabilities = await _capabilities(db)
    if not capabilities.get("local_tts"):
        _store_call(STORE.disable_local_tts, session_id, owner_id)
        raise HTTPException(503, "自家讀音模型暫時未能使用。")
    transient_reservation = False
    if not session.get("voice_reserved"):
        try:
            session = await _reserve_workstation(owner_id, session, capabilities, db)
            transient_reservation = True
        except HTTPException as exc:
            _store_call(STORE.disable_local_tts, session_id, owner_id)
            raise HTTPException(503, "自家讀音模型暫時未能使用。") from exc

    created: dict = {}

    def reserve_output(media: dict) -> dict:
        if created:
            raise ValueError("output upload was already authorized")
        mime = str(media.get("mime_type") or "").lower()
        sha = str(media.get("sha256") or "").lower()
        size = int(media.get("byte_size") or 0)
        duration = float(media.get("duration_seconds") or 0)
        if (
            mime != "audio/wav"
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or not 1 <= size <= WORKSTATION_TTS_OUTPUT_MAX_BYTES
            or not 0 < duration <= LOCAL_PRACTICE_AUDIO_MAX_SECONDS
        ):
            raise ValueError("generated audio metadata is invalid")
        intent_id = uuid.uuid4().hex
        key = (
            f"pending/local-practice/output/{_safe_owner(owner_id)}/"
            f"{session_id}/{body.turn_index}/{intent_id}.wav"
        )
        reserved, _scope = r2_storage.reserve_upload_intent(
            db,
            intent_id=intent_id,
            user_id=owner_id,
            media_kind="local_practice_tts_output",
            object_keys=[key],
            declared_bytes=size,
            metadata={
                "sha256": sha,
                "mime_type": mime,
                "session_id": session_id,
                "turn_index": body.turn_index,
                "duration_seconds": duration,
                "model_version": str(media.get("model_version") or "")[:200],
            },
        )
        if not reserved:
            raise ValueError("R2 storage gate blocked generated audio")
        try:
            url = r2_storage.presign_put(key, mime, sha, size)
        except Exception:
            _delete_intent_best_effort(db, intent_id, [key])
            raise
        created.update({
            "intent_id": intent_id,
            "key": key,
            "mime_type": mime,
            "sha256": sha,
            "byte_size": size,
            "duration_seconds": duration,
        })
        return {
            "intent_id": intent_id,
            "upload": {
                "url": url,
                "headers": {
                    "Content-Type": mime,
                    "Cache-Control": f"private, max-age={R2_OBJECT_CACHE_MAX_AGE_SECONDS}",
                    "x-amz-meta-sha256": sha,
                },
            },
        }

    async def authorize_upload(_job, media: dict) -> dict:
        return await asyncio.to_thread(reserve_output, media)

    async def verify_uploaded_output(_job, result: dict) -> dict:
        output = result.get("output") if isinstance(result.get("output"), dict) else {}
        if (
            not created
            or str(output.get("intent_id") or "") != created["intent_id"]
            or int(output.get("byte_size") or 0) != created["byte_size"]
            or str(output.get("sha256") or "").lower() != created["sha256"]
        ):
            raise LocalAIError("自家讀音上載結果未能通過驗證。")
        await asyncio.to_thread(
            _verified_media_intent,
            db,
            owner_id=owner_id,
            intent_id=created["intent_id"],
            media_kind="local_practice_tts_output",
            session_id=session_id,
            turn_index=body.turn_index,
            require_status="issued",
        )
        if not await asyncio.to_thread(
            r2_storage.complete_upload_intent,
            db,
            created["intent_id"],
            user_id=owner_id,
            media_kind="local_practice_tts_output",
        ):
            raise LocalAIError("自家讀音 intent 狀態已更新。")
        return dict(result)

    try:
        result = await run_workstation_job(
            db,
            operation_id=(
                f"local-practice.tts.{session_id}.{body.turn_index}"
            ),
            job_kind="tts",
            session_id=session_id,
            turn_id=f"turn-{body.turn_index}",
            stage="synthesizing",
            payload={"text": text},
            upload_callback=authorize_upload,
            upload_finish_callback=verify_uploaded_output,
            on_stage=lambda value: _store_call(
                STORE.set_workstation_stage,
                session_id,
                owner_id,
                value,
            ),
        )
        output = result.get("output") if isinstance(result.get("output"), dict) else {}
        if (
            not created
            or str(output.get("intent_id") or "") != created["intent_id"]
            or int(output.get("byte_size") or 0) != created["byte_size"]
            or str(output.get("sha256") or "").lower() != created["sha256"]
        ):
            raise LocalAIError("自家讀音上載結果未能通過驗證。")
        await asyncio.to_thread(
            _verified_media_intent,
            db,
            owner_id=owner_id,
            intent_id=created["intent_id"],
            media_kind="local_practice_tts_output",
            session_id=session_id,
            turn_index=body.turn_index,
            require_status="completed",
        )
        _store_call(
            STORE.cache_tts_output,
            session_id,
            owner_id,
            turn_index=body.turn_index,
            output=created,
        )
        audio_url = await asyncio.to_thread(
            r2_storage.presign_get,
            created["key"],
            mime_type=created["mime_type"],
            expires=R2_DOWNLOAD_URL_TTL_SECONDS,
        )
        if transient_reservation:
            await _release_workstation(owner_id, session)
        _store_call(STORE.set_workstation_stage, session_id, owner_id, "")
        return JSONResponse(
            {"ok": True, "audio_url": audio_url, "cached": False},
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        if created:
            await asyncio.to_thread(
                _delete_intent_best_effort,
                db,
                created["intent_id"],
                [created["key"]],
            )
        _store_call(STORE.disable_local_tts, session_id, owner_id)
        _store_call(STORE.set_workstation_stage, session_id, owner_id, "")
        if transient_reservation:
            await _release_workstation(owner_id, session)
        raise HTTPException(503, "自家讀音模型暫時未能使用。") from exc
