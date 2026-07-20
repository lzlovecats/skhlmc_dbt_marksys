"""Durable state transitions for full-transcript structure extraction."""

from __future__ import annotations

import json
from datetime import timedelta, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from core.ai_data_factory import FactoryContractError
from core.ai_factory_store import (
    FactoryStoreError,
    _reap_stale_attempts_in_transaction,
    _withdraw_sources_in_transaction,
    canonical_json,
    new_id,
    sha256_text,
    utc_now,
)
from core.ai_transcript_factory import (
    TRANSCRIPT_STRUCTURE_RECIPE,
    build_segment_payloads,
    validate_reviewed_segment,
)
from schema import (
    TABLE_AI_FACTORY_ATTEMPTS,
    TABLE_AI_FACTORY_JOBS,
    TABLE_AI_FACTORY_SOURCES,
    TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS,
    TABLE_AI_FACTORY_TRANSCRIPT_RUNS,
    TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS,
    TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS,
    TABLE_AI_FACTORY_TRANSCRIPTS,
    TABLE_AI_TRAINING_AUDIT,
)
from system_limits import (
    AI_FACTORY_ATTEMPT_MAX,
    AI_FACTORY_CONCURRENCY,
    AI_FACTORY_MANAGER_CONCURRENCY,
    AI_FACTORY_PREVIEW_TTL_SECONDS,
    AI_FACTORY_SOURCE_MAX_TOTAL,
    AI_FACTORY_TRANSCRIPT_MAX_TOTAL,
    AI_FACTORY_TRANSCRIPT_RUN_MAX_TOTAL,
    AI_FACTORY_TRANSCRIPT_SEGMENT_MAX_TOTAL,
)


TRANSCRIPT_FACTORY_TABLES = (
    TABLE_AI_FACTORY_TRANSCRIPTS,
    TABLE_AI_FACTORY_TRANSCRIPT_RUNS,
    TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS,
    TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS,
    TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS,
)


def _audit(conn, actor: str, action: str, target_type: str, target_id: str, details=None) -> None:
    conn.execute(
        text(
            f"""INSERT INTO {TABLE_AI_TRAINING_AUDIT}
                (actor_user_id,action,target_type,target_id,details_json)
                VALUES(:actor,:action,:target_type,:target_id,CAST(:details AS jsonb))"""
        ),
        {
            "actor": str(actor)[:200],
            "action": str(action)[:100],
            "target_type": str(target_type)[:100],
            "target_id": str(target_id)[:300],
            "details": canonical_json(details or {}),
        },
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _provider_identity(usage: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    value = usage or {}
    request_id = str(value.get("provider_request_id") or "").strip()[:300] or None
    resolved = str(value.get("resolved_provider_model") or "").strip()[:200] or None
    return request_id, resolved


def _aware_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _settle_usage(conn, actor: str, attempt: Mapping[str, Any], *, success: bool, usage=None, error_code="") -> None:
    if usage is None:
        return
    from core.funds_logic import log_ai_usage_in_transaction

    recorded = dict(usage or {})
    recorded.update({
        "model_label": str(attempt["model_label"]),
        "provider": str(attempt["provider"]),
        "operation_id": str(attempt["run_id"]),
        "operation_stage": (
            f"window_{int(attempt['window_ordinal'])}_attempt_{int(attempt['attempt_no'])}"
        ),
    })
    log_ai_usage_in_transaction(
        conn,
        actor,
        "data_factory_generation",
        success,
        recorded,
        error_message=str(error_code or "")[:100],
    )


def create_transcript_preview(
    db,
    actor: str,
    *,
    transcript_id: str,
    run_id: str,
    title: str,
    topic_text: str,
    source_note: str,
    language_code: str,
    rights_basis: str,
    content_text: str,
    content_sha256: str,
    model_label: str,
    provider: str,
    provider_model: str,
    prompt_version: str,
    prompt_template_sha256: str,
    instruction_text: str,
    manifest_sha256: str,
    preview_expires_at,
    estimated_cost_hkd: float,
    window_previews: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not window_previews:
        raise FactoryStoreError(400, "逐字稿沒有可處理視窗")
    transcript_id = str(transcript_id)
    run_id = str(run_id)
    now = utc_now()
    with db.transaction() as conn:
        conn.execute(text(
            "SELECT pg_advisory_xact_lock(hashtext('ai_factory_transcript_capacity'))"
        ))
        transcript_total = int(conn.execute(text(
            f"SELECT COUNT(*) FROM {TABLE_AI_FACTORY_TRANSCRIPTS}"
        )).scalar() or 0)
        if transcript_total >= AI_FACTORY_TRANSCRIPT_MAX_TOTAL:
            raise FactoryStoreError(409, "完整逐字稿已達保護上限，請先整理現有資料")
        run_total = int(conn.execute(text(
            f"SELECT COUNT(*) FROM {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}"
        )).scalar() or 0)
        if run_total >= AI_FACTORY_TRANSCRIPT_RUN_MAX_TOTAL:
            raise FactoryStoreError(409, "逐字稿處理工作已達保護上限")
        conn.execute(text(
            f"""INSERT INTO {TABLE_AI_FACTORY_TRANSCRIPTS}(
                id,title,topic_text,source_note,language_code,rights_basis,
                rights_confirmed_by,rights_confirmed_at,content_text,content_sha256,
                created_by,created_at
            ) VALUES(
                :id,:title,:topic,:note,:language,:rights,:actor,:now,:content,:sha,
                :actor,:now
            )"""
        ), {
            "id": transcript_id,
            "title": title,
            "topic": topic_text or None,
            "note": source_note,
            "language": language_code,
            "rights": rights_basis,
            "actor": str(actor),
            "now": now,
            "content": content_text,
            "sha": content_sha256,
        })
        conn.execute(text(
            f"""INSERT INTO {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}(
                id,transcript_id,recipe_key,model_label,provider,provider_model,
                prompt_version,prompt_template_sha256,instruction_text,window_count,
                estimated_cost_hkd,status,preview_manifest_sha256,preview_expires_at,
                created_by,created_at,updated_at
            ) VALUES(
                :id,:transcript_id,:recipe,:model,:provider,:provider_model,
                :prompt_version,:template_sha,:instruction,:window_count,
                :estimated_cost,'draft',:manifest_sha,:expires,:actor,:now,:now
            )"""
        ), {
            "id": run_id,
            "transcript_id": transcript_id,
            "recipe": TRANSCRIPT_STRUCTURE_RECIPE,
            "model": model_label,
            "provider": provider,
            "provider_model": provider_model,
            "prompt_version": prompt_version,
            "template_sha": prompt_template_sha256,
            "instruction": instruction_text,
            "window_count": len(window_previews),
            "estimated_cost": float(estimated_cost_hkd),
            "manifest_sha": manifest_sha256,
            "expires": preview_expires_at,
            "actor": str(actor),
            "now": now,
        })
        stored_windows = []
        for item in window_previews:
            window_id = new_id("transcript_window")
            stored_windows.append({**dict(item), "id": window_id})
            conn.execute(text(
                f"""INSERT INTO {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}(
                    id,run_id,ordinal,context_start,context_end,core_start,core_end,
                    prompt_sha256,input_sha256,preview_sha256,status,attempt_count
                ) VALUES(
                    :id,:run_id,:ordinal,:context_start,:context_end,:core_start,:core_end,
                    :prompt_sha,:input_sha,:preview_sha,'pending',0
                )"""
            ), {
                "id": window_id,
                "run_id": run_id,
                "ordinal": int(item["ordinal"]),
                "context_start": int(item["context_start"]),
                "context_end": int(item["context_end"]),
                "core_start": int(item["core_start"]),
                "core_end": int(item["core_end"]),
                "prompt_sha": str(item["prompt_sha256"]),
                "input_sha": str(item["input_sha256"]),
                "preview_sha": str(item["preview_sha256"]),
            })
        _audit(
            conn,
            actor,
            "factory_source_created",
            "ai_factory_transcript",
            transcript_id,
            {
                "run_id": run_id,
                "content_sha256": content_sha256,
                "window_count": len(stored_windows),
                "recipe_key": TRANSCRIPT_STRUCTURE_RECIPE,
            },
        )
    return {
        "transcript_id": transcript_id,
        "run_id": run_id,
        "windows": stored_windows,
    }


def _lock_transcript_run_lineage(conn, run_id: str):
    identity = conn.execute(text(
        f"""SELECT transcript_id FROM {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
            WHERE id=:id"""
    ), {"id": str(run_id)}).mappings().first()
    if identity is None:
        raise FactoryStoreError(404, "找不到逐字稿處理工作")
    transcript = conn.execute(text(
        f"""SELECT * FROM {TABLE_AI_FACTORY_TRANSCRIPTS}
            WHERE id=:id FOR UPDATE"""
    ), {"id": str(identity["transcript_id"])}).mappings().first()
    run = conn.execute(text(
        f"""SELECT * FROM {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
            WHERE id=:id FOR UPDATE"""
    ), {"id": str(run_id)}).mappings().first()
    if (
        transcript is None
        or run is None
        or str(run["transcript_id"]) != str(transcript["id"])
    ):
        raise FactoryStoreError(409, "逐字稿處理工作關聯已改變")
    return transcript, run


def confirm_transcript_run(
    db,
    actor: str,
    run_id: str,
    *,
    manifest_sha256: str,
    confirmation_version: str,
    anonymization_confirmed: bool,
    rights_confirmed: bool,
    third_party_confirmed: bool,
    pii_warning_count: int,
    pii_override_reason: str,
) -> dict[str, Any]:
    if not (anonymization_confirmed and rights_confirmed and third_party_confirmed):
        raise FactoryStoreError(400, "處理前必須確認匿名化、使用權及第三方 AI 傳送警告")
    warning_count = max(0, int(pii_warning_count or 0))
    reason = str(pii_override_reason or "").strip()
    if warning_count and not reason:
        raise FactoryStoreError(400, "逐字稿有個人資料警告，必須填寫覆寫理由")
    now = utc_now()
    with db.transaction() as conn:
        transcript, locked_run = _lock_transcript_run_lineage(conn, str(run_id))
        run = dict(locked_run)
        run["withdrawn_at"] = transcript["withdrawn_at"]
        if str(run["created_by"]) != str(actor):
            raise FactoryStoreError(403, "只有建立工作嘅管理員可以開始處理")
        if run["withdrawn_at"] is not None:
            raise FactoryStoreError(410, "逐字稿已撤回")
        if run["status"] != "draft":
            raise FactoryStoreError(409, "逐字稿處理工作已經確認或開始")
        if _aware_utc(run["preview_expires_at"]) < now:
            raise FactoryStoreError(409, "精確預覽已過期，請重新建立工作")
        if str(run["preview_manifest_sha256"]) != str(manifest_sha256):
            raise FactoryStoreError(409, "逐字稿預覽內容已改變")
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
                SET status='processing',confirmation_version=:version,
                    anonymization_confirmed=TRUE,rights_confirmed=TRUE,
                    third_party_confirmed=TRUE,pii_warning_count=:warning_count,
                    pii_override_reason=:reason,confirmed_by=:actor,confirmed_at=:now,
                    updated_at=:now
                WHERE id=:id"""
        ), {
            "version": str(confirmation_version),
            "warning_count": warning_count,
            "reason": reason[:1000] if warning_count else None,
            "actor": str(actor),
            "now": now,
            "id": str(run_id),
        })
        _audit(
            conn,
            actor,
            "factory_generation_confirmed",
            "ai_factory_transcript_run",
            str(run_id),
            {
                "manifest_sha256": manifest_sha256,
                "window_count": int(run["window_count"]),
                "pii_warning_count": warning_count,
            },
        )
    return {"id": str(run_id), "status": "processing"}


def withdraw_transcript(
    db,
    actor: str,
    transcript_id: str,
    reason: str,
) -> dict[str, Any]:
    reason = str(reason or "").strip()
    if not reason:
        raise FactoryStoreError(400, "請填寫逐字稿撤回原因")
    now = utc_now()
    with db.transaction() as conn:
        transcript = conn.execute(text(
            f"""SELECT id,withdrawn_at FROM {TABLE_AI_FACTORY_TRANSCRIPTS}
                WHERE id=:id FOR UPDATE"""
        ), {"id": str(transcript_id)}).mappings().first()
        if transcript is None:
            raise FactoryStoreError(404, "找不到完整逐字稿")
        if transcript["withdrawn_at"] is not None:
            return {
                "ok": True,
                "changed": False,
                "transcript_id": str(transcript_id),
                "runs": [],
                "claimed_attempts": [],
                "sources": [],
                "items": [],
                "releases": [],
            }
        run_rows = conn.execute(text(
            f"""SELECT id,status FROM {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
                WHERE transcript_id=:transcript_id ORDER BY id FOR UPDATE"""
        ), {"transcript_id": str(transcript_id)}).mappings().all()
        run_ids = [str(row["id"]) for row in run_rows]
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPTS}
                SET withdrawn_by=:actor,withdrawn_at=:now,withdrawal_reason=:reason
                WHERE id=:id AND withdrawn_at IS NULL"""
        ), {
            "actor": str(actor),
            "now": now,
            "reason": reason[:1000],
            "id": str(transcript_id),
        })
        claimed_attempt_ids: list[str] = []
        if run_ids:
            conn.execute(text(
                f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
                    SET status='invalidated',
                        invalidated_by=COALESCE(invalidated_by,:actor),
                        invalidated_at=COALESCE(invalidated_at,:now),
                        invalidation_reason=COALESCE(invalidation_reason,:reason),
                        updated_at=:now
                    WHERE id=ANY(:ids)"""
            ), {
                "actor": str(actor),
                "now": now,
                "reason": reason[:1000],
                "ids": run_ids,
            })
            claimed_rows = conn.execute(text(
                f"""SELECT id,window_id FROM {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}
                    WHERE run_id=ANY(:run_ids) AND status='claimed'
                    ORDER BY run_id,window_id,id FOR UPDATE"""
            ), {"run_ids": run_ids}).mappings().all()
            claimed_attempt_ids = [str(row["id"]) for row in claimed_rows]
            claimed_window_ids = sorted({
                str(row["window_id"]) for row in claimed_rows
            })
            if claimed_attempt_ids:
                conn.execute(text(
                    f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}
                        SET status='discarded',error_code='transcript_withdrawn',
                            completed_at=:now
                        WHERE id=ANY(:ids) AND status='claimed'"""
                ), {"now": now, "ids": claimed_attempt_ids})
                conn.execute(text(
                    f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}
                        SET status='discarded',error_code='transcript_withdrawn',
                            completed_at=:now
                        WHERE id=ANY(:ids) AND status='processing'"""
                ), {"now": now, "ids": claimed_window_ids})
        source_rows = conn.execute(text(
            f"""SELECT DISTINCT approved_source_id
                FROM {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS}
                WHERE transcript_id=:transcript_id
                  AND approved_source_id IS NOT NULL
                ORDER BY approved_source_id"""
        ), {"transcript_id": str(transcript_id)}).mappings().all()
        source_ids = [str(row["approved_source_id"]) for row in source_rows]
        cascaded = _withdraw_sources_in_transaction(
            conn,
            str(actor),
            source_ids,
            reason,
            now,
        )
        _audit(
            conn,
            actor,
            "factory_transcript_withdrawn",
            "ai_factory_transcript",
            str(transcript_id),
            {
                "run_count": len(run_ids),
                "claimed_attempt_count": len(claimed_attempt_ids),
                "source_count": len(cascaded["sources"]),
                "item_count": len(cascaded["items"]),
                "release_count": len(cascaded["releases"]),
            },
        )
    return {
        "ok": True,
        "changed": True,
        "transcript_id": str(transcript_id),
        "runs": run_ids,
        "claimed_attempts": claimed_attempt_ids,
        **cascaded,
    }


def _reap_stale_transcript_attempts(conn, now) -> int:
    stale_before = now - timedelta(seconds=AI_FACTORY_PREVIEW_TTL_SECONDS)
    candidates = conn.execute(text(
        f"""SELECT a.id FROM {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS} a
            WHERE a.status IN ('claimed','running')
              AND COALESCE(a.provider_attempted_at,a.created_at)<:cutoff
            ORDER BY a.id"""
    ), {"cutoff": stale_before}).mappings().all()
    reaped = 0
    for candidate in candidates:
        _transcript, run, window, attempt = _lock_transcript_attempt_lineage(
            conn, str(candidate["id"])
        )
        attempted_at = attempt.get("provider_attempted_at")
        stale_at = attempted_at or attempt.get("created_at")
        if (
            attempt["status"] not in ("claimed", "running")
            or stale_at is None
            or _aware_utc(stale_at) >= stale_before
        ):
            continue
        previous_status = str(attempt["status"])
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}
                SET status='failed',error_code='orphaned_attempt',completed_at=:now
                WHERE id=:id"""
        ), {"id": str(attempt["id"]), "now": now})
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}
                SET status='failed',error_code='orphaned_attempt',completed_at=:now
                WHERE id=:id AND status='processing'"""
        ), {"id": str(window["id"]), "now": now})
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
                SET status='failed',updated_at=:now
                WHERE id=:id AND status='processing'"""
        ), {"id": str(run["id"]), "now": now})
        if previous_status == "running":
            estimated_hkd = max(
                0.0, float(attempt.get("estimated_cost_hkd") or 0)
            )
            _settle_usage(
                conn,
                str(attempt["confirmed_by"]),
                attempt,
                success=False,
                usage={
                    "estimated_cost_usd": round(estimated_hkd / 7.8, 8),
                    "estimated_cost_hkd": estimated_hkd,
                    "cost_source": "factory_preview_estimate_orphaned_running",
                },
                error_code="orphaned_attempt",
            )
        reaped += 1
    return reaped


def claim_transcript_window(db, actor: str, run_id: str) -> dict[str, Any]:
    now = utc_now()
    attempt_id = new_id("transcript_attempt")
    with db.transaction() as conn:
        conn.execute(text(
            "SELECT pg_advisory_xact_lock(hashtext('ai_factory_provider_capacity'))"
        ))
        _reap_stale_attempts_in_transaction(conn, now)
        _reap_stale_transcript_attempts(conn, now)
        transcript, locked_run = _lock_transcript_run_lineage(conn, str(run_id))
        run = dict(locked_run)
        run.update({
            "content_text": transcript["content_text"],
            "content_sha256": transcript["content_sha256"],
            "withdrawn_at": transcript["withdrawn_at"],
        })
        if str(run["created_by"]) != str(actor):
            raise FactoryStoreError(403, "只有建立工作嘅管理員可以繼續處理")
        if run["withdrawn_at"] is not None or run["invalidated_at"] is not None:
            raise FactoryStoreError(410, "逐字稿或處理工作已失效")
        if run["status"] in ("awaiting_review", "reviewed"):
            return {"done": True, "run_id": str(run_id), "status": str(run["status"])}
        if run["status"] not in ("processing", "failed") or run["confirmed_at"] is None:
            raise FactoryStoreError(409, "逐字稿處理工作尚未完成預覽確認")
        window = conn.execute(text(
            f"""SELECT * FROM {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}
                WHERE run_id=:run_id AND status<>'succeeded'
                ORDER BY ordinal LIMIT 1 FOR UPDATE"""
        ), {"run_id": str(run_id)}).mappings().first()
        if window is None:
            return {"done": True, "run_id": str(run_id), "status": str(run["status"])}
        if window["status"] == "processing":
            raise FactoryStoreError(409, "此逐字稿視窗正在處理")
        previous_count = int(window["attempt_count"] or 0)
        if previous_count >= AI_FACTORY_ATTEMPT_MAX:
            raise FactoryStoreError(409, "此逐字稿視窗已達手動重試上限")
        global_active = int(conn.execute(text(
            f"""SELECT
                (SELECT COUNT(*) FROM {TABLE_AI_FACTORY_ATTEMPTS}
                    WHERE status IN ('claimed','running'))
                +
                (SELECT COUNT(*) FROM {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}
                    WHERE status IN ('claimed','running'))"""
        )).scalar() or 0)
        manager_active = int(conn.execute(text(
            f"""SELECT
                (SELECT COUNT(*) FROM {TABLE_AI_FACTORY_ATTEMPTS} a
                    JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=a.job_id
                    WHERE a.status IN ('claimed','running') AND j.created_by=:actor)
                +
                (SELECT COUNT(*) FROM {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS} a
                    JOIN {TABLE_AI_FACTORY_TRANSCRIPT_RUNS} r ON r.id=a.run_id
                    WHERE a.status IN ('claimed','running') AND r.created_by=:actor)"""
        ), {"actor": str(actor)}).scalar() or 0)
        if global_active >= AI_FACTORY_CONCURRENCY:
            raise FactoryStoreError(429, "資料工廠正處理其他生成工作，請稍後再試")
        if manager_active >= AI_FACTORY_MANAGER_CONCURRENCY:
            raise FactoryStoreError(429, "你已有一個生成工作正在處理")
        attempt_no = previous_count + 1
        conn.execute(text(
            f"""INSERT INTO {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}(
                id,run_id,window_id,attempt_no,operation_id,model_label,provider,
                provider_model,prompt_version,prompt_sha256,input_sha256,preview_sha256,
                estimated_cost_hkd,confirmed_by,confirmed_at,status,created_at
            ) VALUES(
                :id,:run_id,:window_id,:attempt_no,:run_id,:model,:provider,
                :provider_model,:prompt_version,:prompt_sha,:input_sha,:preview_sha,
                :estimated_cost,:confirmed_by,:confirmed_at,'claimed',:now
            )"""
        ), {
            "id": attempt_id,
            "run_id": str(run_id),
            "window_id": str(window["id"]),
            "attempt_no": attempt_no,
            "model": str(run["model_label"]),
            "provider": str(run["provider"]),
            "provider_model": str(run["provider_model"]),
            "prompt_version": str(run["prompt_version"]),
            "prompt_sha": str(window["prompt_sha256"]),
            "input_sha": str(window["input_sha256"]),
            "preview_sha": str(window["preview_sha256"]),
            "estimated_cost": float(run["estimated_cost_hkd"] or 0) / int(run["window_count"]),
            "confirmed_by": str(run["confirmed_by"]),
            "confirmed_at": run["confirmed_at"],
            "now": now,
        })
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}
                SET status='processing',attempt_count=:attempt_count,error_code=NULL,
                    started_at=:now,completed_at=NULL,boundary_json=NULL,boundary_sha256=NULL
                WHERE id=:id"""
        ), {
            "attempt_count": attempt_no,
            "now": now,
            "id": str(window["id"]),
        })
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
                SET status='processing',updated_at=:now WHERE id=:id"""
        ), {"now": now, "id": str(run_id)})
    return {
        "done": False,
        "attempt_id": attempt_id,
        "attempt_no": attempt_no,
        "run_id": str(run_id),
        "window_id": str(window["id"]),
        "window_ordinal": int(window["ordinal"]),
        "context_start": int(window["context_start"]),
        "context_end": int(window["context_end"]),
        "core_start": int(window["core_start"]),
        "core_end": int(window["core_end"]),
        "prompt_sha256": str(window["prompt_sha256"]),
        "input_sha256": str(window["input_sha256"]),
        "preview_sha256": str(window["preview_sha256"]),
        "model_label": str(run["model_label"]),
        "provider": str(run["provider"]),
        "provider_model": str(run["provider_model"]),
        "prompt_version": str(run["prompt_version"]),
        "prompt_template_sha256": str(run["prompt_template_sha256"]),
        "instruction_text": str(run["instruction_text"] or ""),
        "transcript_id": str(run["transcript_id"]),
        "content_text": str(run["content_text"]),
        "content_sha256": str(run["content_sha256"]),
        "window_count": int(run["window_count"]),
    }


def mark_transcript_provider_started(db, attempt_id: str) -> None:
    now = utc_now()
    with db.transaction() as conn:
        identity = conn.execute(text(
            f"""SELECT a.id,a.run_id,a.window_id,r.transcript_id
                FROM {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS} a
                JOIN {TABLE_AI_FACTORY_TRANSCRIPT_RUNS} r ON r.id=a.run_id
                WHERE a.id=:id"""
        ), {"id": str(attempt_id)}).mappings().first()
        if identity is None:
            raise FactoryStoreError(404, "找不到逐字稿生成 attempt")
        transcript = conn.execute(text(
            f"SELECT id,withdrawn_at FROM {TABLE_AI_FACTORY_TRANSCRIPTS} WHERE id=:id FOR UPDATE"
        ), {"id": str(identity["transcript_id"])}).mappings().first()
        run = conn.execute(text(
            f"SELECT id,invalidated_at FROM {TABLE_AI_FACTORY_TRANSCRIPT_RUNS} WHERE id=:id FOR UPDATE"
        ), {"id": str(identity["run_id"])}).mappings().first()
        window = conn.execute(text(
            f"SELECT id,status FROM {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS} WHERE id=:id FOR UPDATE"
        ), {"id": str(identity["window_id"])}).mappings().first()
        attempt = conn.execute(text(
            f"SELECT id,status FROM {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS} WHERE id=:id FOR UPDATE"
        ), {"id": str(attempt_id)}).mappings().first()
        if not all((transcript, run, window, attempt)):
            raise FactoryStoreError(409, "逐字稿生成關聯已改變")
        if transcript["withdrawn_at"] is not None or run["invalidated_at"] is not None:
            raise FactoryStoreError(410, "逐字稿已撤回，沒有呼叫 AI provider")
        if attempt["status"] != "claimed" or window["status"] != "processing":
            raise FactoryStoreError(409, "逐字稿生成 attempt 狀態已改變")
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}
                SET status='running',provider_attempted_at=:now
                WHERE id=:id AND status='claimed'"""
        ), {"now": now, "id": str(attempt_id)})


def _lock_transcript_attempt_lineage(conn, attempt_id: str):
    identity = conn.execute(text(
        f"""SELECT a.run_id,a.window_id,r.transcript_id
            FROM {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS} a
            JOIN {TABLE_AI_FACTORY_TRANSCRIPT_RUNS} r ON r.id=a.run_id
            WHERE a.id=:id"""
    ), {"id": str(attempt_id)}).mappings().first()
    if identity is None:
        raise FactoryStoreError(404, "找不到逐字稿生成 attempt")
    transcript = conn.execute(text(
        f"SELECT * FROM {TABLE_AI_FACTORY_TRANSCRIPTS} WHERE id=:id FOR UPDATE"
    ), {"id": str(identity["transcript_id"])}).mappings().first()
    run = conn.execute(text(
        f"SELECT * FROM {TABLE_AI_FACTORY_TRANSCRIPT_RUNS} WHERE id=:id FOR UPDATE"
    ), {"id": str(identity["run_id"])}).mappings().first()
    window = conn.execute(text(
        f"SELECT * FROM {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS} WHERE id=:id FOR UPDATE"
    ), {"id": str(identity["window_id"])}).mappings().first()
    attempt = conn.execute(text(
        f"SELECT * FROM {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS} WHERE id=:id FOR UPDATE"
    ), {"id": str(attempt_id)}).mappings().first()
    if not all((transcript, run, window, attempt)):
        raise FactoryStoreError(409, "逐字稿生成關聯已改變")
    value = dict(attempt)
    value.update({
        "window_ordinal": int(window["ordinal"]),
        "transcript_withdrawn_at": transcript["withdrawn_at"],
        "run_invalidated_at": run["invalidated_at"],
        "content_text": str(transcript["content_text"]),
    })
    return transcript, run, window, value


def fail_transcript_attempt(
    db,
    actor: str,
    attempt_id: str,
    *,
    error_code: str,
    response_sha256: str = "",
    response_bytes: int = 0,
    provider_called: bool = True,
    usage: Mapping[str, Any] | None = None,
) -> None:
    now = utc_now()
    request_id, resolved_model = _provider_identity(usage)
    with db.transaction() as conn:
        _transcript, run, window, attempt = _lock_transcript_attempt_lineage(
            conn, str(attempt_id)
        )
        if attempt["status"] not in ("claimed", "running"):
            return
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}
                SET status='failed',response_sha256=:sha,response_bytes=:bytes,
                    error_code=:error,provider_request_id=:request_id,
                    resolved_provider_model=:resolved_model,completed_at=:now
                WHERE id=:id"""
        ), {
            "sha": response_sha256 or None,
            "bytes": max(0, int(response_bytes)),
            "error": str(error_code or "provider_error")[:120],
            "request_id": request_id,
            "resolved_model": resolved_model,
            "now": now,
            "id": str(attempt_id),
        })
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}
                SET status='failed',error_code=:error,completed_at=:now
                WHERE id=:id"""
        ), {
            "error": str(error_code or "provider_error")[:120],
            "now": now,
            "id": str(window["id"]),
        })
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
                SET status=CASE WHEN invalidated_at IS NULL THEN 'failed' ELSE 'invalidated' END,
                    updated_at=:now WHERE id=:id"""
        ), {"now": now, "id": str(run["id"])})
        if provider_called:
            _settle_usage(
                conn, actor, attempt, success=False, usage=usage or {},
                error_code=error_code,
            )


def complete_transcript_attempt(
    db,
    actor: str,
    attempt_id: str,
    *,
    boundaries: Sequence[Mapping[str, Any]],
    response_sha256: str,
    response_bytes: int,
    usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    now = utc_now()
    request_id, resolved_model = _provider_identity(usage)
    with db.transaction() as conn:
        conn.execute(text(
            "SELECT pg_advisory_xact_lock(hashtext('ai_factory_transcript_segment_capacity'))"
        ))
        transcript, run, window, attempt = _lock_transcript_attempt_lineage(
            conn, str(attempt_id)
        )
        if attempt["status"] != "running" or window["status"] != "processing":
            raise FactoryStoreError(409, "逐字稿生成 attempt 狀態已改變")
        if transcript["withdrawn_at"] is not None or run["invalidated_at"] is not None:
            conn.execute(text(
                f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}
                    SET status='discarded',response_sha256=:sha,response_bytes=:bytes,
                        provider_request_id=:request_id,
                        resolved_provider_model=:resolved_model,
                        error_code='transcript_withdrawn',completed_at=:now
                    WHERE id=:id"""
            ), {
                "sha": response_sha256,
                "bytes": int(response_bytes),
                "request_id": request_id,
                "resolved_model": resolved_model,
                "now": now,
                "id": str(attempt_id),
            })
            conn.execute(text(
                f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}
                    SET status='discarded',error_code='transcript_withdrawn',completed_at=:now
                    WHERE id=:id"""
            ), {"now": now, "id": str(window["id"])})
            _settle_usage(conn, actor, attempt, success=True, usage=usage)
            return {"discarded": True, "done": False}
        boundary_list = [dict(item) for item in boundaries]
        encoded = canonical_json(boundary_list)
        boundary_sha = sha256_text(encoded)
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}
                SET status='succeeded',response_sha256=:sha,response_bytes=:bytes,
                    provider_request_id=:request_id,resolved_provider_model=:resolved_model,
                    completed_at=:now,error_code=NULL WHERE id=:id"""
        ), {
            "sha": response_sha256,
            "bytes": int(response_bytes),
            "request_id": request_id,
            "resolved_model": resolved_model,
            "now": now,
            "id": str(attempt_id),
        })
        conn.execute(text(
            f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}
                SET status='succeeded',boundary_json=CAST(:boundaries AS jsonb),
                    boundary_sha256=:boundary_sha,error_code=NULL,completed_at=:now
                WHERE id=:id"""
        ), {
            "boundaries": encoded,
            "boundary_sha": boundary_sha,
            "now": now,
            "id": str(window["id"]),
        })
        remaining = int(conn.execute(text(
            f"""SELECT COUNT(*) FROM {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}
                WHERE run_id=:run_id AND status<>'succeeded'"""
        ), {"run_id": str(run["id"])}).scalar() or 0)
        segment_ids = []
        if remaining == 0:
            window_rows = conn.execute(text(
                f"""SELECT id,boundary_json FROM {TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS}
                    WHERE run_id=:run_id ORDER BY ordinal"""
            ), {"run_id": str(run["id"])}).mappings().all()
            boundary_windows = [{
                "id": str(item["id"]),
                "boundaries": _json_value(item["boundary_json"]),
            } for item in window_rows]
            payloads = build_segment_payloads(
                str(transcript["content_text"]), boundary_windows,
            )
            total = int(conn.execute(text(
                f"SELECT COUNT(*) FROM {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS}"
            )).scalar() or 0)
            if total + len(payloads) > AI_FACTORY_TRANSCRIPT_SEGMENT_MAX_TOTAL:
                raise FactoryStoreError(409, "逐字稿段落已達保護上限，沒有建立不完整批次")
            for item in payloads:
                payload = item["payload"]
                segment_id = new_id("transcript_segment")
                segment_ids.append(segment_id)
                payload_json = canonical_json(payload)
                conn.execute(text(
                    f"""INSERT INTO {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS}(
                        id,run_id,transcript_id,origin_window_id,start_offset,end_offset,
                        original_json,original_sha256,review_status,created_at
                    ) VALUES(
                        :id,:run_id,:transcript_id,:window_id,:start_offset,:end_offset,
                        CAST(:payload AS jsonb),:sha,'pending',:now
                    )"""
                ), {
                    "id": segment_id,
                    "run_id": str(run["id"]),
                    "transcript_id": str(transcript["id"]),
                    "window_id": str(item["origin_window_id"]),
                    "start_offset": int(payload["start_offset"]),
                    "end_offset": int(payload["end_offset"]),
                    "payload": payload_json,
                    "sha": sha256_text(payload_json),
                    "now": now,
                })
            conn.execute(text(
                f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
                    SET status='awaiting_review',updated_at=:now WHERE id=:id"""
            ), {"now": now, "id": str(run["id"])})
            _audit(
                conn,
                actor,
                "factory_generation_completed",
                "ai_factory_transcript_run",
                str(run["id"]),
                {"segment_count": len(segment_ids), "window_count": int(run["window_count"])},
            )
        else:
            conn.execute(text(
                f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
                    SET status='processing',updated_at=:now WHERE id=:id"""
            ), {"now": now, "id": str(run["id"])})
        _settle_usage(conn, actor, attempt, success=True, usage=usage)
    return {
        "discarded": False,
        "done": remaining == 0,
        "remaining_windows": remaining,
        "segment_ids": segment_ids,
    }


def review_transcript_segment(
    db,
    actor: str,
    segment_id: str,
    *,
    decision: str,
    reviewed_payload: Mapping[str, Any] | None,
    note: str = "",
) -> dict[str, Any]:
    if decision not in ("approved", "rejected"):
        raise FactoryStoreError(400, "逐字稿段落審核決定不正確")
    if decision == "rejected" and not str(note or "").strip():
        raise FactoryStoreError(400, "拒絕逐字稿段落時必須填寫原因")
    now = utc_now()
    with db.transaction() as conn:
        identity = conn.execute(text(
            f"""SELECT run_id,transcript_id
                FROM {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS} WHERE id=:id"""
        ), {"id": str(segment_id)}).mappings().first()
        if identity is None:
            raise FactoryStoreError(404, "找不到逐字稿段落")
        transcript = conn.execute(text(
            f"""SELECT * FROM {TABLE_AI_FACTORY_TRANSCRIPTS}
                WHERE id=:id FOR UPDATE"""
        ), {"id": str(identity["transcript_id"])}).mappings().first()
        run = conn.execute(text(
            f"""SELECT * FROM {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
                WHERE id=:id FOR UPDATE"""
        ), {"id": str(identity["run_id"])}).mappings().first()
        segment = conn.execute(text(
            f"""SELECT * FROM {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS}
                WHERE id=:id FOR UPDATE"""
        ), {"id": str(segment_id)}).mappings().first()
        if (
            transcript is None
            or run is None
            or segment is None
            or str(run["transcript_id"]) != str(transcript["id"])
            or str(segment["run_id"]) != str(run["id"])
            or str(segment["transcript_id"]) != str(transcript["id"])
        ):
            raise FactoryStoreError(409, "逐字稿段落關聯已改變")
        row = dict(segment)
        row.update({
            "run_status": run["status"],
            "invalidated_at": run["invalidated_at"],
            "title": transcript["title"],
            "topic_text": transcript["topic_text"],
            "source_note": transcript["source_note"],
            "language_code": transcript["language_code"],
            "rights_basis": transcript["rights_basis"],
            "rights_confirmed_by": transcript["rights_confirmed_by"],
            "rights_confirmed_at": transcript["rights_confirmed_at"],
            "content_text": transcript["content_text"],
            "withdrawn_at": transcript["withdrawn_at"],
        })
        if row["withdrawn_at"] is not None or row["invalidated_at"] is not None:
            raise FactoryStoreError(410, "逐字稿或處理工作已失效")
        if row["review_status"] != "pending":
            raise FactoryStoreError(409, "此逐字稿段落已完成審核")
        if row["run_status"] != "awaiting_review":
            raise FactoryStoreError(409, "逐字稿處理工作目前不可審核")
        source_id = None
        reviewed_json = None
        reviewed_sha = None
        if decision == "approved":
            try:
                reviewed = validate_reviewed_segment(
                    reviewed_payload or {}, transcript_text=str(row["content_text"]),
                )
            except FactoryContractError as exc:
                raise FactoryStoreError(400, str(exc)) from exc
            original = _json_value(row["original_json"])
            if (
                not isinstance(original, Mapping)
                or reviewed["sequence_no"] != int(original.get("sequence_no") or 0)
            ):
                raise FactoryStoreError(400, "發言次序由原文位置決定，不可修改")
            if (
                reviewed["start_offset"] != original.get("start_offset")
                or reviewed["end_offset"] != original.get("end_offset")
            ):
                raise FactoryStoreError(400, "原文邊界由逐字稿結構決定，不可逐段修改")
            conn.execute(text(
                "SELECT pg_advisory_xact_lock(hashtext('ai_factory_source_capacity'))"
            ))
            source_total = int(conn.execute(text(
                f"SELECT COUNT(*) FROM {TABLE_AI_FACTORY_SOURCES}"
            )).scalar() or 0)
            if source_total >= AI_FACTORY_SOURCE_MAX_TOTAL:
                raise FactoryStoreError(409, "來源快照已達保護上限")
            source_id = new_id("source")
            source_group_id = new_id("source_group")
            reviewed_json = canonical_json(reviewed)
            reviewed_sha = sha256_text(reviewed_json)
            source_title = f"{row['title']}｜{reviewed['speaker_label']}"
            provenance_note = (
                f"完整逐字稿 {row['transcript_id']} 字元 "
                f"{reviewed['start_offset']} 至 {reviewed['end_offset']}。"
                f"{str(row['source_note'] or '')}"
            )[:1000]
            conn.execute(text(
                f"""INSERT INTO {TABLE_AI_FACTORY_SOURCES}(
                    id,source_group_id,revision_no,supersedes_source_id,source_kind,
                    origin_submission_id,data_type,title,topic_text,side,source_note,
                    language_code,rights_basis,rights_confirmed_by,rights_confirmed_at,
                    content_text,content_sha256,created_by,created_at
                ) VALUES(
                    :id,:group_id,1,NULL,'admin_paste',NULL,'transcript_segment',
                    :title,:topic,:side,:note,:language,:rights,:rights_by,:rights_at,
                    :content,:content_sha,:actor,:now
                )"""
            ), {
                "id": source_id,
                "group_id": source_group_id,
                "title": source_title[:500],
                "topic": row["topic_text"],
                "side": reviewed["side"],
                "note": provenance_note,
                "language": row["language_code"],
                "rights": row["rights_basis"],
                "rights_by": row["rights_confirmed_by"],
                "rights_at": row["rights_confirmed_at"],
                "content": reviewed["full_text"],
                "content_sha": sha256_text(reviewed["full_text"]),
                "actor": str(actor),
                "now": now,
            })
            conn.execute(text(
                f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS}
                    SET reviewed_json=CAST(:payload AS jsonb),reviewed_sha256=:sha,
                        review_status='approved',review_note=:note,reviewed_by=:actor,
                        reviewed_at=:now,approved_source_id=:source_id
                    WHERE id=:id"""
            ), {
                "payload": reviewed_json,
                "sha": reviewed_sha,
                "note": str(note or "")[:2000] or None,
                "actor": str(actor),
                "now": now,
                "source_id": source_id,
                "id": str(segment_id),
            })
            _audit(
                conn,
                actor,
                "factory_source_created",
                "ai_factory_source",
                source_id,
                {
                    "transcript_id": str(row["transcript_id"]),
                    "transcript_segment_id": str(segment_id),
                    "content_sha256": sha256_text(reviewed["full_text"]),
                },
            )
        else:
            conn.execute(text(
                f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS}
                    SET review_status='rejected',review_note=:note,reviewed_by=:actor,
                        reviewed_at=:now WHERE id=:id"""
            ), {
                "note": str(note or "")[:2000] or None,
                "actor": str(actor),
                "now": now,
                "id": str(segment_id),
            })
        pending = int(conn.execute(text(
            f"""SELECT COUNT(*) FROM {TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS}
                WHERE run_id=:run_id AND review_status='pending'"""
        ), {"run_id": str(row["run_id"])}).scalar() or 0)
        if pending == 0:
            conn.execute(text(
                f"""UPDATE {TABLE_AI_FACTORY_TRANSCRIPT_RUNS}
                    SET status='reviewed',updated_at=:now WHERE id=:id"""
            ), {"now": now, "id": str(row["run_id"])})
        _audit(
            conn,
            actor,
            "factory_item_reviewed",
            "ai_factory_transcript_segment",
            str(segment_id),
            {
                "decision": decision,
                "reviewed_sha256": reviewed_sha,
                "approved_source_id": source_id,
            },
        )
    return {
        "id": str(segment_id),
        "status": decision,
        "approved_source_id": source_id,
    }


__all__ = [
    "TRANSCRIPT_FACTORY_TABLES",
    "claim_transcript_window",
    "complete_transcript_attempt",
    "confirm_transcript_run",
    "create_transcript_preview",
    "fail_transcript_attempt",
    "mark_transcript_provider_started",
    "review_transcript_segment",
    "withdraw_transcript",
]
