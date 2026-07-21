"""Phase-2 fixed local-AI generation and blind-review HTTP boundary."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ai_model_config import LMC_AI_MODE_OPTIONS, LMC_AI_MODEL_PROFILE_VERSION
from api.access import require_interactive_features_available, require_page_user_or_developer
from api.pagination import json_safe
from core.lmc_ai_eval import build_eval_prompt, validate_review_payload
from core.lmc_ai_eval_store import (
    campaign_progress, claim_next_output, close_campaign, create_campaign,
    invalidate_campaign, latest_campaign, list_campaigns, list_pending_assignments,
    manager_export, mark_output_started, next_assignment, open_review,
    preview_assignment, purge_campaign, record_campaign_export, release_assignment,
    release_unstarted_claim, require_eval_schema, settle_output, submit_review,
)
from core.lmc_ai_runtime import (
    BackendChangedError, NodeUnavailableError, QueueFullError, RUNTIME,
    backend_fingerprint,
)
from core.lmc_ai_store import get_active_node_id
from core.roles import is_ai_manager
from schema import TABLE_AI_EVAL_CAMPAIGNS
from system_limits import (
    LMC_AI_EVAL_CAMPAIGN_MAX, LMC_AI_EVAL_EXPORT_MAX_BYTES, LMC_AI_EVAL_OUTPUT_MAX_BYTES,
    LMC_AI_EVAL_REVIEW_NOTE_MAX_CHARS,
)


router = APIRouter(prefix="/api/lmc-ai/ab-tests", tags=["lmc-ai-eval"])


class CampaignCreateBody(BaseModel):
    note: str = Field(default="", max_length=LMC_AI_EVAL_REVIEW_NOTE_MAX_CHARS)


class ReviewBody(BaseModel):
    overall: str = Field(max_length=20)
    cantonese: str = Field(max_length=20)
    reasoning: str = Field(max_length=20)
    usefulness: str = Field(max_length=20)
    factual: str = Field(max_length=20)
    privacy: str = Field(max_length=20)
    note: str = Field(default="", max_length=LMC_AI_EVAL_REVIEW_NOTE_MAX_CHARS)


class InvalidateBody(BaseModel):
    reason: str = Field(min_length=1, max_length=LMC_AI_EVAL_REVIEW_NOTE_MAX_CHARS)


class ReleaseBody(BaseModel):
    reason: str = Field(min_length=1, max_length=LMC_AI_EVAL_REVIEW_NOTE_MAX_CHARS)


class PurgeBody(BaseModel):
    confirmation: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=LMC_AI_EVAL_REVIEW_NOTE_MAX_CHARS)


def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()


def _context(request: Request) -> tuple[str, object, bool]:
    actor = require_page_user_or_developer(request, "lmc_ai")
    db = _db()
    try:
        require_eval_schema(db)
        manager = bool(is_ai_manager(actor, db=db))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return actor, db, manager


def _require_manager(request: Request) -> tuple[str, object]:
    actor, db, manager = _context(request)
    if not manager:
        raise HTTPException(403, "只有 AI 管理員或 Developer 可以管理 A/B Test。")
    return actor, db


def _no_store(value: object, status_code: int = 200) -> JSONResponse:
    return JSONResponse(json_safe(value), status_code=status_code, headers={"Cache-Control": "private, no-store"})


def _public_campaign(campaign: dict | None) -> dict | None:
    if not campaign:
        return None
    return {
        "campaign_id": campaign["campaign_id"], "status": campaign["status"],
        "created_at": campaign.get("created_at"), "reviewing_at": campaign.get("reviewing_at"),
        "closed_at": campaign.get("closed_at"),
        "invalidation_reason": (
            "呢個campaign已作廢；資料會保留，但不會再派發盲評。"
            if campaign["status"] == "invalidated" else ""
        ),
    }


@router.get("/bootstrap")
def bootstrap(request: Request):
    actor, db, manager = _context(request)
    campaign = latest_campaign(db)
    progress = campaign_progress(db, campaign["campaign_id"], "" if actor == "developer" else actor) if campaign else None
    history = list_campaigns(db) if manager else []
    payload = {
        "identity": {"manager": manager, "can_review": actor != "developer"},
        "campaign": _public_campaign(campaign), "progress": progress,
    }
    if manager:
        payload["campaigns"] = history
        payload["can_create_campaign"] = (
            len(history) < LMC_AI_EVAL_CAMPAIGN_MAX
            and not any(item["status"] in {"generating", "reviewing"} for item in history)
        )
    if manager and campaign:
        payload["manager"] = {
            "bound_node_id": campaign["bound_node_id"], "note": campaign.get("note") or "",
            "summary": campaign.get("summary_json") if campaign["status"] == "closed" else None,
            "summary_hash": campaign.get("summary_hash"),
            "exported_at": campaign.get("exported_at"),
            "invalidation_reason": campaign.get("invalidation_reason") if campaign["status"] == "invalidated" else "",
        }
    return _no_store(payload)


@router.post("/campaigns")
async def campaigns(body: CampaignCreateBody, request: Request):
    actor, db = _require_manager(request)
    require_interactive_features_available(request)
    node_id = get_active_node_id(db)
    snapshot = await RUNTIME.snapshot(node_id) if node_id else None
    if not node_id or not snapshot:
        raise HTTPException(409, "請先選擇已連線嘅自家 AI 電腦。")
    try:
        value = await asyncio.to_thread(
            create_campaign, db, actor_id=actor, node_id=node_id,
            snapshot=snapshot, note=body.note.strip(),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return _no_store(value, 201)


def _validate_bound_identity(db, claim: dict, snapshot: dict | None) -> dict:
    campaign = claim["campaign"]
    if get_active_node_id(db) != campaign["bound_node_id"]:
        raise ValueError("active node已經切換，campaign拒絕混合backend。")
    if not snapshot or not snapshot.get("online") or not snapshot.get("ready") or snapshot.get("draining"):
        raise ValueError("綁定 AI 電腦未 ready。")
    if snapshot.get("busy") or int(snapshot.get("queue_length") or 0):
        raise ValueError("AI 電腦必須完全空閒先可以生成下一題。")
    manifest = campaign["model_manifest"]
    for mode, expected in manifest.items():
        if expected["model"] not in set(snapshot.get("models") or []) or (snapshot.get("model_digests") or {}).get(expected["model"]) != expected["digest"]:
            raise ValueError("model tag或exact digest已改變，campaign必須invalidate。")
        if (snapshot.get("runtime") != expected["runtime"]
                or snapshot.get("runtime_version") != expected["runtime_version"]):
            raise ValueError("runtime或runtime version已改變，campaign必須invalidate。")
    expected = manifest[claim["mode"]]
    fingerprint = backend_fingerprint(
        campaign["bound_node_id"], expected["model"], expected["thinking"],
        model_digest=expected["digest"],
    )
    return {
        "model": expected["model"], "digest": expected["digest"],
        "thinking": expected["thinking"], "fingerprint": fingerprint,
        "runtime": snapshot.get("runtime"), "runtime_version": snapshot.get("runtime_version"),
    }


@router.post("/campaigns/{campaign_id}/generate-next")
async def generate_next(campaign_id: str, request: Request):
    _actor, db = _require_manager(request)
    require_interactive_features_available(request)
    try:
        claim = await asyncio.to_thread(claim_next_output, db, campaign_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if claim is None:
        return _no_store({"ok": True, "message": "冇可生成或可重試嘅答案。", "progress": campaign_progress(db, campaign_id)})
    node_id = claim["campaign"]["bound_node_id"]
    snapshot = await RUNTIME.snapshot(node_id)
    try:
        identity = _validate_bound_identity(db, claim, snapshot)
        prompt = build_eval_prompt(claim["task_type"], claim["input_json"])
    except (ValueError, RuntimeError) as exc:
        await asyncio.to_thread(release_unstarted_claim, db, claim, str(exc))
        raise HTTPException(409, str(exc)) from exc
    finished = {}

    async def started(job):
        recorded = await asyncio.to_thread(mark_output_started, db, claim)
        if not recorded:
            raise RuntimeError("eval attempt lease已失效")

    async def finish(job, success: bool, usage: dict, error: str):
        try:
            final_success = bool(success)
            final_error = str(error or "")
            try:
                current = await RUNTIME.snapshot(node_id)
                current_identity = _validate_bound_identity(db, claim, {
                    **(current or {}), "busy": False, "queue_length": 0,
                })
                if current_identity["fingerprint"] != identity["fingerprint"] or str(usage.get("model") or identity["model"]) != identity["model"]:
                    raise ValueError("generation期間model identity已改變。")
            except ValueError as exc:
                final_success = False
                final_error = str(exc)
            saved = await asyncio.to_thread(
                settle_output, db, claim, success=final_success,
                answer=job.collected_text, usage=usage, identity=identity,
                error=final_error,
            )
            finished.update({"saved": saved, "success": final_success, "error": final_error})
        except Exception:
            # Keep the processing lease for restart-safe reclaim. Provider
            # details and DB errors must not be exposed through this endpoint.
            finished.update({"saved": False, "success": False, "error": "未能保存generation結果；lease到期後可安全重試。"})

    try:
        job, position = await RUNTIME.submit(
            node_id=node_id, expected_fingerprint=identity["fingerprint"],
            actor_id="lmc_ai_eval", usage_user_id=None,
            operation_stage=f"attempt_{claim['active_attempt']}",
            messages=[{"role": "user", "content": prompt}],
            finish_callback=finish, start_callback=started,
            model=identity["model"], thinking_enabled=identity["thinking"],
            operation_id=claim["operation_id"], require_idle=True,
            output_max_bytes=LMC_AI_EVAL_OUTPUT_MAX_BYTES,
        )
        if position != 0:
            raise RuntimeError("eval工作不可排入FIFO queue")
    except (BackendChangedError, NodeUnavailableError, QueueFullError, RuntimeError) as exc:
        await asyncio.to_thread(release_unstarted_claim, db, claim, str(exc))
        raise HTTPException(409, str(exc)) from exc
    while True:
        event, payload = await job.events.get()
        if event in {"complete", "error"}:
            break
    if not job.provider_attempted:
        await asyncio.to_thread(
            release_unstarted_claim, db, claim,
            str(payload.get("message") or "generation未真正開始"),
        )
    progress = await asyncio.to_thread(campaign_progress, db, campaign_id)
    return _no_store({
        "ok": bool(finished.get("saved") and finished.get("success")),
        "case_id": claim["case_id"], "mode": claim["mode"],
        "attempt": claim["active_attempt"], "error": finished.get("error") or "",
        "progress": progress,
    })


@router.post("/campaigns/{campaign_id}/open-review")
async def open_campaign_review(campaign_id: str, request: Request):
    _actor, db = _require_manager(request)
    try:
        await asyncio.to_thread(open_review, db, campaign_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _no_store({"ok": True})


@router.get("/campaigns/{campaign_id}/reviews/next")
async def review_next(campaign_id: str, request: Request):
    actor, db, _manager = _context(request)
    if actor == "developer":
        value = await asyncio.to_thread(preview_assignment, db, campaign_id)
        return _no_store({"assignment": value})
    try:
        value = await asyncio.to_thread(next_assignment, db, campaign_id, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _no_store({"assignment": value})


@router.get("/campaigns/{campaign_id}/assignments")
def campaign_assignments(campaign_id: str, request: Request):
    _actor, db = _require_manager(request)
    return _no_store({"assignments": list_pending_assignments(db, campaign_id)})


@router.post("/campaigns/{campaign_id}/assignments/{review_id}/release")
async def campaign_assignment_release(
    campaign_id: str, review_id: str, body: ReleaseBody, request: Request,
):
    actor, db = _require_manager(request)
    try:
        await asyncio.to_thread(
            release_assignment, db, campaign_id, review_id, actor, body.reason.strip(),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _no_store({"ok": True})


@router.post("/reviews/{review_id}")
async def review_submit(review_id: str, body: ReviewBody, request: Request):
    actor, db, _manager = _context(request)
    if actor == "developer":
        raise HTTPException(403, "Developer票唔會計入正式結果。")
    try:
        choices = validate_review_payload(body.model_dump())
        created = await asyncio.to_thread(
            submit_review, db, review_id, actor, choices, body.note.strip(),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _no_store({"ok": True, "created": created})


@router.post("/campaigns/{campaign_id}/close")
async def campaign_close(campaign_id: str, request: Request):
    _actor, db = _require_manager(request)
    try:
        value = await asyncio.to_thread(close_campaign, db, campaign_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _no_store(value)


@router.post("/campaigns/{campaign_id}/invalidate")
async def campaign_invalidate(campaign_id: str, body: InvalidateBody, request: Request):
    actor, db = _require_manager(request)
    try:
        await asyncio.to_thread(invalidate_campaign, db, campaign_id, actor, body.reason.strip())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _no_store({"ok": True})


@router.get("/campaigns/{campaign_id}/results")
def results(campaign_id: str, request: Request):
    _actor, db = _require_manager(request)
    campaign = latest_campaign(db)
    if not campaign or campaign["campaign_id"] != campaign_id:
        rows = db.query(f"SELECT campaign_id,status,summary_json,summary_hash FROM {TABLE_AI_EVAL_CAMPAIGNS} WHERE campaign_id=:campaign", {"campaign": campaign_id})
        if rows.empty:
            raise HTTPException(404, "找不到campaign。")
        campaign = dict(rows.iloc[0])
    if campaign["status"] not in {"closed", "invalidated"}:
        raise HTTPException(409, "campaign完成或作廢後先可以查看結果。")
    return _no_store({"status": campaign["status"], "summary": campaign.get("summary_json"), "summary_hash": campaign.get("summary_hash")})


@router.post("/campaigns/{campaign_id}/export.json")
def export_json(campaign_id: str, request: Request):
    actor, db = _require_manager(request)
    try:
        value = json_safe(manager_export(db, campaign_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > LMC_AI_EVAL_EXPORT_MAX_BYTES:
        raise HTTPException(413, "audit export超過安全上限。")
    try:
        record_campaign_export(db, campaign_id, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(
        content=encoded, media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="lmc-ai-eval-{campaign_id}.json"',
        },
    )


@router.post("/campaigns/{campaign_id}/purge")
async def campaign_purge(campaign_id: str, body: PurgeBody, request: Request):
    actor, db = _require_manager(request)
    try:
        value = await asyncio.to_thread(
            purge_campaign, db, campaign_id, actor,
            body.confirmation.strip(), body.reason.strip(),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _no_store({"ok": True, **value})
