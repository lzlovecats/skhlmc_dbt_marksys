"""Authenticated API for the collaborative Competition Prep workspace."""

from __future__ import annotations

from datetime import date
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ai_model_config import get_lmc_ai_feature_mode
from api.access import require_interactive_features_available, require_page_user
from core import competition_prep_logic as logic
from system_limits import COMPETITION_PREP_MANUSCRIPT_MAX_CHARS

router = APIRouter(prefix="/api/competition-prep", tags=["competition-prep"])


class ProjectBody(BaseModel):
    title: str = Field(default="", max_length=200)
    recent_match_id: int | None = None
    topic_text: str = Field(default="", max_length=500)
    our_side: Literal["pro", "con"] = "pro"
    debate_format: Literal["校園隨想", "聯中", "星島", "基本法盃"] = "校園隨想"
    opponent: str = Field(default="", max_length=200)
    match_date: date | None = None
    match_time: str = Field(default="", max_length=8)
    revision: int = Field(default=0, ge=0)


class MemberBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    role: Literal["editor", "viewer"]


class ManuscriptBody(BaseModel):
    id: int | None = None
    slot: Literal["main", "dep1", "dep2", "dep3", "closing", "interaction", "other"]
    title: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=COMPETITION_PREP_MANUSCRIPT_MAX_CHARS)
    assigned_user_id: str = Field(default="", max_length=200)
    status: Literal["draft", "reviewed", "final"] = "draft"
    revision: int = Field(default=0, ge=0)


class StrategyCardBody(BaseModel):
    kind: Literal[
        "mainline", "definition", "standard", "burden", "argument",
        "opponent_argument", "attack", "opponent_answer", "rebuttal",
        "defence_floor", "concession", "question",
    ] = "argument"
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=10_000)
    assigned_slot: str = Field(default="", max_length=20)
    priority: int = Field(default=2, ge=1, le=3)
    sort_order: int = Field(default=0, ge=-10_000, le=10_000)


class EvidenceCardBody(BaseModel):
    claim_text: str = Field(min_length=1, max_length=500)
    excerpt: str = Field(default="", max_length=20_000)
    source_url: str = Field(default="", max_length=2000)
    source_name: str = Field(default="", max_length=200)
    published_date: date | None = None
    accessed_date: date | None = None
    region: str = Field(default="", max_length=100)
    source_type: Literal[
        "government", "academic", "news", "ngo", "industry", "ai_research", "other",
    ] = "other"
    side_scope: Literal["our", "opponent", "both"] = "both"
    limitations: str = Field(default="", max_length=5000)


class WeaknessBody(BaseModel):
    source_type: Literal["manual", "audit", "speech", "strategy"] = "manual"
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    category: Literal["logic", "evidence", "definition", "response", "delivery", "coordination"] = "logic"
    assigned_user_id: str = Field(default="", max_length=200)
    priority: int = Field(default=2, ge=1, le=3)


class WeaknessStatusBody(BaseModel):
    status: Literal["open", "practicing", "passed"]
    revision: int = Field(ge=1)


class AiRunBody(BaseModel):
    run_type: Literal["team_audit", "strategy_attack"]
    model_label: str = Field(default="", max_length=120)
    local_mode: Literal["fast", "daily", "deep", "complex"] = (
        get_lmc_ai_feature_mode("ai_coach")
    )
    operation_id: str = Field(min_length=16, max_length=200)


def _db():
    from deploy.proxy import get_vote_db

    return get_vote_db()


def _user(request):
    return require_page_user(request, "competition_prep")


def _payload(model):
    data = model.model_dump()
    for key, value in tuple(data.items()):
        if isinstance(value, date):
            data[key] = value.isoformat()
    return data


def _handle(action):
    try:
        return action()
    except logic.PrepError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("/data")
def data(request: Request):
    result = _handle(lambda: logic.list_workspace(_db(), _user(request)))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/projects")
def create_project(body: ProjectBody, request: Request):
    user_id = _user(request)
    project_id = _handle(lambda: logic.create_project(_db(), user_id, _payload(body)))
    return JSONResponse({"ok": True, "project_id": project_id}, headers={"Cache-Control": "no-store"})


@router.get("/projects/{project_id}")
def get_project(project_id: int, request: Request):
    result = _handle(lambda: logic.project_bundle(_db(), project_id, _user(request)))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.patch("/projects/{project_id}")
def update_project(project_id: int, body: ProjectBody, request: Request):
    _handle(lambda: logic.update_project(_db(), project_id, _user(request), _payload(body)))
    return {"ok": True}


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, request: Request):
    _handle(lambda: logic.delete_project(_db(), project_id, _user(request)))
    return {"ok": True}


@router.put("/projects/{project_id}/members")
def set_member(project_id: int, body: MemberBody, request: Request):
    _handle(lambda: logic.set_member(_db(), project_id, _user(request), body.user_id, body.role))
    return {"ok": True}


@router.delete("/projects/{project_id}/members/{member_id}")
def remove_member(project_id: int, member_id: str, request: Request):
    _handle(lambda: logic.remove_member(_db(), project_id, _user(request), member_id))
    return {"ok": True}


@router.post("/projects/{project_id}/manuscripts")
def save_manuscript(project_id: int, body: ManuscriptBody, request: Request):
    item_id = _handle(lambda: logic.save_manuscript(_db(), project_id, _user(request), _payload(body)))
    return {"ok": True, "id": item_id}


@router.post("/projects/{project_id}/strategy-cards")
def save_strategy_card(project_id: int, body: StrategyCardBody, request: Request):
    item_id = _handle(lambda: logic.save_strategy_card(_db(), project_id, _user(request), _payload(body)))
    return {"ok": True, "id": item_id}


@router.post("/projects/{project_id}/evidence-cards")
def save_evidence_card(project_id: int, body: EvidenceCardBody, request: Request):
    item_id = _handle(lambda: logic.save_evidence_card(_db(), project_id, _user(request), _payload(body)))
    return {"ok": True, "id": item_id}


@router.post("/projects/{project_id}/weaknesses")
def save_weakness(project_id: int, body: WeaknessBody, request: Request):
    item_id = _handle(lambda: logic.save_weakness(_db(), project_id, _user(request), _payload(body)))
    return {"ok": True, "id": item_id}


@router.patch("/projects/{project_id}/weaknesses/{weakness_id}")
def update_weakness(project_id: int, weakness_id: int, body: WeaknessStatusBody, request: Request):
    _handle(lambda: logic.set_weakness_status(
        _db(), project_id, weakness_id, _user(request), body.status, body.revision,
    ))
    return {"ok": True}


@router.delete("/projects/{project_id}/{collection}/{item_id}")
def delete_item(project_id: int, collection: str, item_id: int, request: Request):
    _handle(lambda: logic.delete_item(_db(), project_id, _user(request), collection, item_id))
    return {"ok": True}


@router.get("/projects/{project_id}/export")
def export_project(project_id: int, request: Request):
    result = _handle(lambda: logic.export_project(_db(), project_id, _user(request)))
    return JSONResponse(
        result,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="competition-prep-{project_id}.json"',
        },
    )


@router.post("/projects/{project_id}/ai-run")
async def ai_run(project_id: int, body: AiRunBody, request: Request):
    user_id = _user(request)
    require_interactive_features_available(request)
    from api import ai_coach_api as coach
    from deploy import proxy
    from prompts import (
        COMPETITION_PREP_STRATEGY_ATTACK_SYSTEM_PROMPT,
        COMPETITION_PREP_TEAM_AUDIT_SYSTEM_PROMPT,
        build_competition_prep_user_prompt,
    )

    budget_error = proxy._bandwidth_essential_gate_error()
    if budget_error:
        raise HTTPException(429, budget_error)
    db = _db()
    bundle = _handle(lambda: logic.project_bundle(db, project_id, user_id))
    if bundle["role"] not in logic.EDIT_ROLES:
        raise HTTPException(403, "只有項目擁有者或編輯者可以執行 AI 分析。")
    context = logic.build_ai_context(bundle)
    systems = {
        "team_audit": COMPETITION_PREP_TEAM_AUDIT_SYSTEM_PROMPT,
        "strategy_attack": COMPETITION_PREP_STRATEGY_ATTACK_SYSTEM_PROMPT,
    }
    labels = {"team_audit": "全隊稿件審查", "strategy_attack": "AI模擬攻擊"}
    enabled_providers, runtime_default = coach._runtime_model_settings(db)
    from ai_name import LMC_AI_MODEL_LABEL
    model_label = body.model_label or LMC_AI_MODEL_LABEL
    config = coach._config(model_label, db)
    coach._require_enabled_model(model_label, config, enabled_providers)
    await coach._require_local_model_available(config, db, body.local_mode)
    operation_id = body.operation_id
    snapshot = {
        "project_revision": bundle["project"]["revision"],
        "input_sha256": logic.ai_input_fingerprint({"context": context}),
    }
    claim = _handle(lambda: logic.claim_ai_run(
        db, project_id, user_id, operation_id, body.run_type, model_label, snapshot,
    ))
    if claim["state"] == "completed":
        return JSONResponse(
            {
                "ok": True, "run_id": operation_id,
                "markdown": claim["output"], "cached": True,
            },
            headers={"Cache-Control": "no-store"},
        )
    provider_attempted = False

    def mark_attempt():
        nonlocal provider_attempted
        provider_attempted = True

    provider_body = coach.CoachRequest(
        feature="strategy",
        model_label=model_label,
        local_mode=body.local_mode,
    )
    try:
        markdown, actual = await coach._generate(
            config, systems[body.run_type],
            build_competition_prep_user_prompt(context, labels[body.run_type]),
            provider_body, user_id, on_provider_attempt=mark_attempt,
        )
    except HTTPException as exc:
        try:
            logic.release_ai_run(db, project_id, user_id, operation_id, body.run_type)
        except Exception:
            pass
        if provider_attempted:
            coach._usage(
                db, user_id, "competition_prep", model_label, config, False,
                str(exc.detail)[:300], operation_id=operation_id,
                operation_stage=body.run_type,
            )
        raise
    _handle(lambda: logic.complete_ai_run(
        db, project_id, user_id, operation_id, body.run_type, model_label, markdown,
    ))
    coach._usage(
        db, user_id, "competition_prep", model_label, config, True,
        actual=actual, operation_id=operation_id, operation_stage=body.run_type,
    )
    return JSONResponse(
        {"ok": True, "run_id": operation_id, "markdown": markdown},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/projects/{project_id}/weaknesses/{weakness_id}/prepare-live")
def prepare_weakness_live(project_id: int, weakness_id: int, request: Request):
    user_id = _user(request)
    require_interactive_features_available(request)
    from deploy import proxy

    db = _db()
    _bundle, weakness = _handle(
        lambda: logic.weakness_context(db, project_id, weakness_id, user_id)
    )
    country = proxy._solo_live_country_status(request)
    if not country["supported"]:
        raise HTTPException(403, country["message"])
    budget_error = proxy._bandwidth_essential_gate_error()
    if budget_error:
        raise HTTPException(429, budget_error)
    practice_id = proxy._new_live_practice_claim(user_id, "free")
    if not practice_id:
        raise HTTPException(503, "伺服器未能簽發練習授權，請稍後再試。")
    _handle(lambda: logic.set_weakness_status(
        db, project_id, weakness_id, user_id, "practicing", weakness["revision"],
    ))
    query = urlencode({
        "mode": "free", "source": "competition-prep",
        "prep_project_id": project_id, "weakness_id": weakness_id,
        "practice_id": practice_id,
    })
    return JSONResponse(
        {"ok": True, "url": f"/practice/ai-debate/live?{query}"},
        headers={"Cache-Control": "no-store"},
    )
