"""Durable PostgreSQL state transitions for the admin AI data factory.

Provider calls deliberately live outside this module.  Every function here is
either read-only or completes one short transaction, so a slow external model
never holds database locks open.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from schema import (
    TABLE_AI_FACTORY_ATTEMPTS,
    TABLE_AI_FACTORY_ITEM_TAGS,
    TABLE_AI_FACTORY_ITEMS,
    TABLE_AI_FACTORY_JOBS,
    TABLE_AI_FACTORY_RELEASE_ITEMS,
    TABLE_AI_FACTORY_RELEASES,
    TABLE_AI_FACTORY_SOURCES,
    TABLE_AI_FACTORY_TOPIC_TAGS,
    TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS,
    TABLE_AI_FACTORY_TRANSCRIPT_RUNS,
    TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS,
    TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS,
    TABLE_AI_FACTORY_TRANSCRIPTS,
    TABLE_AI_TRAINING_AUDIT,
    TABLE_LLM_TRAINING_SUBMISSIONS,
)
from system_limits import (
    AI_FACTORY_ATTEMPT_MAX,
    AI_FACTORY_CANDIDATE_MAX,
    AI_FACTORY_CONCURRENCY,
    AI_FACTORY_ITEM_MAX_TOTAL,
    AI_FACTORY_JOB_MAX_TOTAL,
    AI_FACTORY_MANAGER_CONCURRENCY,
    AI_FACTORY_PREVIEW_TTL_SECONDS,
    AI_FACTORY_RELEASE_MAX_BYTES,
    AI_FACTORY_RELEASE_MAX_ITEMS,
    AI_FACTORY_RELEASE_MAX_TOTAL,
    AI_FACTORY_SOURCE_MAX_CHARS,
    AI_FACTORY_SOURCE_NOTE_MAX_CHARS,
    AI_FACTORY_SOURCE_MAX_TOTAL,
    AI_FACTORY_TOPIC_TAG_MAX,
    AI_FACTORY_TOPIC_TAG_MAX_CHARS,
    AI_FACTORY_TOPIC_TAG_MAX_TOTAL,
)


FACTORY_TABLES = (
    TABLE_AI_FACTORY_SOURCES,
    TABLE_AI_FACTORY_JOBS,
    TABLE_AI_FACTORY_ATTEMPTS,
    TABLE_AI_FACTORY_ITEMS,
    TABLE_AI_FACTORY_TOPIC_TAGS,
    TABLE_AI_FACTORY_ITEM_TAGS,
    TABLE_AI_FACTORY_RELEASES,
    TABLE_AI_FACTORY_RELEASE_ITEMS,
    TABLE_AI_FACTORY_TRANSCRIPTS,
    TABLE_AI_FACTORY_TRANSCRIPT_RUNS,
    TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS,
    TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS,
    TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS,
)
SOURCE_KINDS = frozenset(("llm_submission", "admin_paste"))
RIGHTS_BASES = frozenset(
    ("own_work", "permission", "open_license", "public_domain", "other")
)
SOURCE_LANGUAGES = frozenset(("yue-Hant-HK", "zh-Hant", "en", "mixed", "other"))
RECIPE_KINDS = {
    "rag_knowledge_card_v1": "rag",
    "rag_argument_decomposition_v1": "rag",
    "sft_speech_critique_v1": "sft",
    "sft_attack_defence_v1": "sft",
}


class FactoryStoreError(ValueError):
    """Expected state/validation error carrying an HTTP-compatible status."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = int(status_code)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    """Normalise driver/test datetimes before comparing an expiry boundary."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def normalized_content(value: str) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _database_value(value):
    """Normalise nullable values returned through the pandas query wrapper."""
    if value is None:
        return None
    value_type = type(value)
    if (
        value_type.__module__.startswith("pandas.")
        and value_type.__name__ in {"NAType", "NaTType"}
    ):
        return None
    if isinstance(value, dict):
        return {key: _database_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_database_value(item) for item in value]
    try:
        if value != value:
            return None
    except (TypeError, ValueError):
        pass
    return value


def _row(frame):
    if getattr(frame, "empty", True):
        return None
    return _database_value(dict(frame.iloc[0]))


def _rows(frame):
    return [_database_value(dict(item)) for item in frame.to_dict(orient="records")]


def _audit(conn, actor, action, target_type, target_id, details=None):
    encoded = canonical_json(details or {})
    conn.execute(
        text(
            f"""INSERT INTO {TABLE_AI_TRAINING_AUDIT}
                (actor_user_id,action,target_type,target_id,details_json)
                VALUES(:actor,:action,:target_type,:target_id,CAST(:details AS jsonb))"""
        ),
        {
            "actor": str(actor or "")[:200],
            "action": str(action or "")[:100],
            "target_type": str(target_type or "")[:100],
            "target_id": str(target_id or "")[:300],
            "details": encoded,
        },
    )


def create_pasted_source(
    db,
    actor: str,
    *,
    title: str,
    content_text: str,
    source_note: str,
    rights_basis: str,
    language_code: str,
    data_type: str = "admin_paste",
    topic_text: str = "",
    side: str = "not_applicable",
    source_group_id: str = "",
    supersedes_source_id: str | None = None,
) -> dict:
    title = str(title or "").strip()
    content = normalized_content(content_text)
    source_note = str(source_note or "").strip()
    rights_basis = str(rights_basis or "").strip()
    language_code = str(language_code or "").strip()
    if not title:
        raise FactoryStoreError(400, "請填寫來源標題")
    if not source_note:
        raise FactoryStoreError(400, "請填寫來源或使用權說明")
    if len(source_note) > AI_FACTORY_SOURCE_NOTE_MAX_CHARS:
        raise FactoryStoreError(400, "來源或使用權說明超過字數上限")
    if not content:
        raise FactoryStoreError(400, "請貼上來源內容")
    if len(content) > AI_FACTORY_SOURCE_MAX_CHARS:
        raise FactoryStoreError(413, "來源內容超過資料工廠字數上限")
    if rights_basis not in RIGHTS_BASES:
        raise FactoryStoreError(400, "來源使用權類型不正確")
    if language_code not in SOURCE_LANGUAGES:
        raise FactoryStoreError(400, "來源語言不正確")
    now = utc_now()
    source_id = new_id("src")
    group_id = str(source_group_id or "").strip() or new_id("source")
    with db.transaction() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('ai_factory_source_capacity'))")
        )
        source_total = int(
            conn.execute(text(f"SELECT COUNT(*) FROM {TABLE_AI_FACTORY_SOURCES}")).scalar()
            or 0
        )
        if source_total >= AI_FACTORY_SOURCE_MAX_TOTAL:
            raise FactoryStoreError(409, "資料來源已達保護上限，請先整理現有資料")
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"ai_factory_source:{group_id}"},
        )
        revision_no = 1
        if supersedes_source_id:
            previous = conn.execute(
                text(
                    f"""SELECT id,source_group_id,revision_no,withdrawn_at
                        FROM {TABLE_AI_FACTORY_SOURCES}
                        WHERE id=:id FOR UPDATE"""
                ),
                {"id": supersedes_source_id},
            ).mappings().first()
            if previous is None:
                raise FactoryStoreError(404, "找不到要修訂的來源")
            if str(previous["source_group_id"]) != group_id:
                raise FactoryStoreError(409, "來源修訂鏈不一致")
            revision_no = int(previous["revision_no"]) + 1
        conn.execute(
            text(
                f"""INSERT INTO {TABLE_AI_FACTORY_SOURCES}(
                    id,source_group_id,revision_no,supersedes_source_id,source_kind,
                    origin_submission_id,data_type,title,topic_text,side,source_note,
                    language_code,rights_basis,rights_confirmed_by,rights_confirmed_at,
                    content_text,content_sha256,created_by,created_at
                ) VALUES(
                    :id,:group_id,:revision,:supersedes,'admin_paste',NULL,:data_type,
                    :title,:topic,:side,:source_note,:language,:rights,:actor,:now,
                    :content,:sha,:actor,:now
                )"""
            ),
            {
                "id": source_id,
                "group_id": group_id,
                "revision": revision_no,
                "supersedes": supersedes_source_id,
                "data_type": str(data_type or "admin_paste")[:80],
                "title": title[:200],
                "topic": str(topic_text or "")[:500],
                "side": str(side or "not_applicable")[:40],
                "source_note": source_note,
                "language": language_code,
                "rights": rights_basis,
                "actor": str(actor),
                "now": now,
                "content": content,
                "sha": sha256_text(content),
            },
        )
        _audit(
            conn,
            actor,
            "factory_source_created",
            "ai_factory_source",
            source_id,
            {"kind": "admin_paste", "revision": revision_no, "content_sha256": sha256_text(content)},
        )
    return get_source(db, source_id)


def snapshot_submission_source(db, actor: str, submission_id: int) -> dict:
    now = utc_now()
    with db.transaction() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('ai_factory_source_capacity'))")
        )
        submission = conn.execute(
            text(
                f"""SELECT id,submitted_by,data_type,title,topic_text,side,content_text,
                    source_note,status,anonymized,permission_confirmed,created_at
                    FROM {TABLE_LLM_TRAINING_SUBMISSIONS}
                    WHERE id=:id FOR UPDATE"""
            ),
            {"id": int(submission_id)},
        ).mappings().first()
        if submission is None:
            raise FactoryStoreError(404, "找不到已接受的 LLM 來源")
        if (
            submission["status"] != "accepted"
            or not submission["anonymized"]
            or not submission["permission_confirmed"]
        ):
            raise FactoryStoreError(409, "來源必須已接受、已匿名化並確認使用權")
        existing = conn.execute(
            text(
                f"""SELECT id,withdrawn_at FROM {TABLE_AI_FACTORY_SOURCES}
                    WHERE source_kind='llm_submission' AND origin_submission_id=:id
                    ORDER BY revision_no DESC LIMIT 1 FOR UPDATE"""
            ),
            {"id": int(submission_id)},
        ).mappings().first()
        if existing is not None:
            if existing["withdrawn_at"] is not None:
                raise FactoryStoreError(410, "此來源已由資料工廠撤回")
            source_id = str(existing["id"])
        else:
            source_total = int(
                conn.execute(text(f"SELECT COUNT(*) FROM {TABLE_AI_FACTORY_SOURCES}")).scalar()
                or 0
            )
            if source_total >= AI_FACTORY_SOURCE_MAX_TOTAL:
                raise FactoryStoreError(409, "資料來源已達保護上限，請先整理現有資料")
            content = normalized_content(submission["content_text"])
            if not content or len(content) > AI_FACTORY_SOURCE_MAX_CHARS:
                raise FactoryStoreError(413, "來源內容不符合資料工廠字數限制")
            source_id = new_id("src")
            group_id = f"submission_{int(submission_id)}"
            rights_actor = str(submission["submitted_by"] or actor)
            rights_confirmed_at = submission["created_at"]
            if rights_confirmed_at is None:
                raise FactoryStoreError(409, "來源缺少可核對嘅使用權確認時間")
            conn.execute(
                text(
                    f"""INSERT INTO {TABLE_AI_FACTORY_SOURCES}(
                        id,source_group_id,revision_no,supersedes_source_id,source_kind,
                        origin_submission_id,data_type,title,topic_text,side,source_note,
                        language_code,rights_basis,rights_confirmed_by,rights_confirmed_at,
                        content_text,content_sha256,created_by,created_at
                    ) VALUES(
                        :id,:group_id,1,NULL,'llm_submission',:submission_id,:data_type,
                        :title,:topic,:side,:source_note,'yue-Hant-HK','submission_confirmed',
                        :rights_actor,:rights_confirmed_at,:content,:sha,:actor,:now
                    )"""
                ),
                {
                    "id": source_id,
                    "group_id": group_id,
                    "submission_id": int(submission_id),
                    "data_type": (str(submission["data_type"] or "").strip() or "llm_submission")[:80],
                    "title": str(submission["title"] or "")[:200],
                    "topic": str(submission["topic_text"] or "")[:500],
                    "side": str(submission["side"] or "not_applicable")[:40],
                    "source_note": str(submission["source_note"] or "")[
                        :AI_FACTORY_SOURCE_NOTE_MAX_CHARS
                    ],
                    "rights_actor": rights_actor,
                    "rights_confirmed_at": rights_confirmed_at,
                    "now": now,
                    "content": content,
                    "sha": sha256_text(content),
                    "actor": str(actor),
                },
            )
            _audit(
                conn,
                actor,
                "factory_source_created",
                "ai_factory_source",
                source_id,
                {"kind": "llm_submission", "submission_id": int(submission_id), "content_sha256": sha256_text(content)},
            )
    return get_source(db, source_id)


def get_source(db, source_id: str) -> dict:
    row = _row(
        db.query(
            f"""SELECT * FROM {TABLE_AI_FACTORY_SOURCES} WHERE id=:id""",
            {"id": str(source_id)},
        )
    )
    if row is None:
        raise FactoryStoreError(404, "找不到資料來源")
    return row


def create_or_refresh_job_preview(
    db,
    actor: str,
    *,
    source_id: str,
    recipe_key: str,
    requested_count: int,
    instruction_text: str,
    preview_model_label: str,
    preview_provider: str,
    preview_provider_model: str,
    preview_prompt_sha256: str,
    preview_input_sha256: str,
    preview_sha256: str,
    preview_expires_at: datetime,
    job_id: str = "",
) -> dict:
    if recipe_key not in RECIPE_KINDS:
        raise FactoryStoreError(400, "資料配方不正確")
    count = int(requested_count)
    if not 1 <= count <= AI_FACTORY_CANDIDATE_MAX:
        raise FactoryStoreError(400, "候選數量不正確")
    now = utc_now()
    with db.transaction() as conn:
        source = conn.execute(
            text(
                f"""SELECT id,withdrawn_at FROM {TABLE_AI_FACTORY_SOURCES}
                    WHERE id=:id FOR UPDATE"""
            ),
            {"id": str(source_id)},
        ).mappings().first()
        if source is None:
            raise FactoryStoreError(404, "找不到資料來源")
        if source["withdrawn_at"] is not None:
            raise FactoryStoreError(410, "資料來源已撤回")
        if job_id:
            job = conn.execute(
                text(
                    f"""SELECT id,source_id,recipe_key,requested_count,instruction_text,
                        status,created_by,invalidated_at
                        FROM {TABLE_AI_FACTORY_JOBS} WHERE id=:id FOR UPDATE"""
                ),
                {"id": str(job_id)},
            ).mappings().first()
            if job is None:
                raise FactoryStoreError(404, "找不到生成工作")
            if str(job["created_by"]) != str(actor):
                raise FactoryStoreError(403, "只有建立工作嘅管理員可以重試")
            if job["invalidated_at"] is not None:
                raise FactoryStoreError(410, "生成工作已失效")
            if job["status"] not in ("draft", "failed"):
                raise FactoryStoreError(409, "目前工作狀態不可重新預覽")
            if (
                str(job["source_id"]) != str(source_id)
                or str(job["recipe_key"]) != recipe_key
                or int(job["requested_count"]) != count
                or str(job["instruction_text"] or "") != str(instruction_text or "")
            ):
                raise FactoryStoreError(409, "重試不可更改來源、配方或候選設定")
            resolved_job_id = str(job_id)
            conn.execute(
                text(
                    f"""UPDATE {TABLE_AI_FACTORY_JOBS} SET
                        status='draft',
                        preview_model_label=:label,preview_provider=:provider,
                        preview_provider_model=:provider_model,
                        preview_prompt_sha256=:prompt_sha,preview_input_sha256=:input_sha,
                        preview_sha256=:preview_sha,preview_expires_at=:expires,
                        updated_at=:now WHERE id=:id"""
                ),
                {
                    "label": preview_model_label,
                    "provider": preview_provider,
                    "provider_model": preview_provider_model,
                    "prompt_sha": preview_prompt_sha256,
                    "input_sha": preview_input_sha256,
                    "preview_sha": preview_sha256,
                    "expires": preview_expires_at,
                    "now": now,
                    "id": resolved_job_id,
                },
            )
        else:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('ai_factory_job_capacity'))")
            )
            job_total = int(
                conn.execute(text(f"SELECT COUNT(*) FROM {TABLE_AI_FACTORY_JOBS}")).scalar()
                or 0
            )
            if job_total >= AI_FACTORY_JOB_MAX_TOTAL:
                raise FactoryStoreError(409, "生成工作已達保護上限，請先整理現有資料")
            resolved_job_id = new_id("job")
            conn.execute(
                text(
                    f"""INSERT INTO {TABLE_AI_FACTORY_JOBS}(
                        id,source_id,recipe_key,requested_count,instruction_text,status,
                        preview_model_label,preview_provider,preview_provider_model,
                        preview_prompt_sha256,preview_input_sha256,preview_sha256,
                        preview_expires_at,created_by,created_at,updated_at
                    ) VALUES(
                        :id,:source_id,:recipe,:count,:instruction,'draft',:label,
                        :provider,:provider_model,:prompt_sha,:input_sha,:preview_sha,
                        :expires,:actor,:now,:now
                    )"""
                ),
                {
                    "id": resolved_job_id,
                    "source_id": str(source_id),
                    "recipe": recipe_key,
                    "count": count,
                    "instruction": str(instruction_text or ""),
                    "label": preview_model_label,
                    "provider": preview_provider,
                    "provider_model": preview_provider_model,
                    "prompt_sha": preview_prompt_sha256,
                    "input_sha": preview_input_sha256,
                    "preview_sha": preview_sha256,
                    "expires": preview_expires_at,
                    "actor": str(actor),
                    "now": now,
                },
            )
    return _row(
        db.query(f"SELECT * FROM {TABLE_AI_FACTORY_JOBS} WHERE id=:id", {"id": resolved_job_id})
    )


def _stale_value(row, key: str, index: int):
    try:
        return row[key]
    except (KeyError, TypeError):
        return row[index]


def _lock_job_lineage(conn, job_id: str):
    """Lock one job in the canonical source -> job order."""
    identity = conn.execute(
        text(
            f"""SELECT source_id FROM {TABLE_AI_FACTORY_JOBS}
                WHERE id=:id"""
        ),
        {"id": str(job_id)},
    ).mappings().first()
    if identity is None:
        raise FactoryStoreError(404, "找不到生成工作")
    source = conn.execute(
        text(
            f"""SELECT id,withdrawn_at,content_sha256
                FROM {TABLE_AI_FACTORY_SOURCES}
                WHERE id=:id FOR UPDATE"""
        ),
        {"id": str(identity["source_id"])},
    ).mappings().first()
    job = conn.execute(
        text(f"SELECT * FROM {TABLE_AI_FACTORY_JOBS} WHERE id=:id FOR UPDATE"),
        {"id": str(job_id)},
    ).mappings().first()
    if source is None or job is None or str(job["source_id"]) != str(source["id"]):
        raise FactoryStoreError(409, "生成工作關聯已改變，請重新載入")
    locked = dict(job)
    locked["source_withdrawn_at"] = source["withdrawn_at"]
    locked["current_source_sha"] = source["content_sha256"]
    return locked


def _lock_attempt_lineage(conn, attempt_id: str):
    """Lock one attempt in the canonical source -> job -> attempt order."""
    identity = conn.execute(
        text(
            f"""SELECT a.job_id,j.source_id
                FROM {TABLE_AI_FACTORY_ATTEMPTS} a
                JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=a.job_id
                WHERE a.id=:id"""
        ),
        {"id": str(attempt_id)},
    ).mappings().first()
    if identity is None:
        raise FactoryStoreError(404, "找不到生成 attempt")
    source = conn.execute(
        text(
            f"""SELECT id,withdrawn_at FROM {TABLE_AI_FACTORY_SOURCES}
                WHERE id=:id FOR UPDATE"""
        ),
        {"id": str(identity["source_id"])},
    ).mappings().first()
    job = conn.execute(
        text(
            f"""SELECT id,source_id,invalidated_at FROM {TABLE_AI_FACTORY_JOBS}
                WHERE id=:id FOR UPDATE"""
        ),
        {"id": str(identity["job_id"])},
    ).mappings().first()
    attempt = conn.execute(
        text(f"SELECT * FROM {TABLE_AI_FACTORY_ATTEMPTS} WHERE id=:id FOR UPDATE"),
        {"id": str(attempt_id)},
    ).mappings().first()
    if source is None or job is None or attempt is None:
        raise FactoryStoreError(409, "生成工作關聯已改變，請重新載入")
    if (
        str(job["source_id"]) != str(source["id"])
        or str(attempt["job_id"]) != str(job["id"])
    ):
        raise FactoryStoreError(409, "生成工作關聯已改變，請重新載入")
    locked = dict(attempt)
    locked["job_invalidated_at"] = job["invalidated_at"]
    locked["source_withdrawn_at"] = source["withdrawn_at"]
    return locked


def _lock_item_lineage(conn, item_id: str):
    """Lock one review item in the canonical source -> job -> item order."""
    identity = conn.execute(
        text(
            f"""SELECT i.job_id,j.source_id
                FROM {TABLE_AI_FACTORY_ITEMS} i
                JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=i.job_id
                WHERE i.id=:id"""
        ),
        {"id": str(item_id)},
    ).mappings().first()
    if identity is None:
        raise FactoryStoreError(404, "找不到候選資料")
    source = conn.execute(
        text(
            f"""SELECT id,withdrawn_at FROM {TABLE_AI_FACTORY_SOURCES}
                WHERE id=:id FOR UPDATE"""
        ),
        {"id": str(identity["source_id"])},
    ).mappings().first()
    job = conn.execute(
        text(
            f"""SELECT id,source_id,recipe_key,created_by,invalidated_at
                FROM {TABLE_AI_FACTORY_JOBS}
                WHERE id=:id FOR UPDATE"""
        ),
        {"id": str(identity["job_id"])},
    ).mappings().first()
    item = conn.execute(
        text(f"SELECT * FROM {TABLE_AI_FACTORY_ITEMS} WHERE id=:id FOR UPDATE"),
        {"id": str(item_id)},
    ).mappings().first()
    if source is None or job is None or item is None:
        raise FactoryStoreError(409, "候選資料關聯已改變，請重新載入")
    if (
        str(job["source_id"]) != str(source["id"])
        or str(item["job_id"]) != str(job["id"])
    ):
        raise FactoryStoreError(409, "候選資料關聯已改變，請重新載入")
    locked = dict(item)
    locked["source_id"] = job["source_id"]
    locked["recipe_key"] = job["recipe_key"]
    locked["created_by"] = job["created_by"]
    locked["job_invalidated_at"] = job["invalidated_at"]
    locked["source_withdrawn_at"] = source["withdrawn_at"]
    return locked


def _reap_stale_attempts_in_transaction(conn, now: datetime) -> int:
    cutoff = now - timedelta(seconds=AI_FACTORY_PREVIEW_TTL_SECONDS)
    candidates = conn.execute(
        text(
            f"""SELECT a.id,a.job_id,j.source_id
                FROM {TABLE_AI_FACTORY_ATTEMPTS} a
                JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=a.job_id
                WHERE a.status IN ('claimed','running') AND a.created_at<:cutoff
                ORDER BY j.source_id,a.job_id,a.id"""
        ),
        {"cutoff": cutoff},
    ).fetchall()
    if not candidates:
        return 0
    candidate_by_id = {
        str(_stale_value(row, "id", 0)): (
            str(_stale_value(row, "job_id", 1)),
            str(_stale_value(row, "source_id", 2)),
        )
        for row in candidates
    }
    candidate_ids = sorted(candidate_by_id)
    source_ids = sorted({source_id for _, source_id in candidate_by_id.values()})
    job_ids = sorted({job_id for job_id, _ in candidate_by_id.values()})
    locked_sources = conn.execute(
        text(
            f"""SELECT id FROM {TABLE_AI_FACTORY_SOURCES}
                WHERE id=ANY(:ids) ORDER BY id FOR UPDATE"""
        ),
        {"ids": source_ids},
    ).fetchall()
    if {str(row[0]) for row in locked_sources} != set(source_ids):
        raise FactoryStoreError(409, "生成工作關聯已改變，請重新載入")
    locked_jobs = conn.execute(
        text(
            f"""SELECT id,source_id FROM {TABLE_AI_FACTORY_JOBS}
                WHERE id=ANY(:ids) ORDER BY id FOR UPDATE"""
        ),
        {"ids": job_ids},
    ).fetchall()
    locked_job_sources = {str(row[0]): str(row[1]) for row in locked_jobs}
    if set(locked_job_sources) != set(job_ids) or any(
        locked_job_sources.get(job_id) != source_id
        for job_id, source_id in candidate_by_id.values()
    ):
        raise FactoryStoreError(409, "生成工作關聯已改變，請重新載入")
    stale_rows = conn.execute(
        text(
            f"""SELECT id,job_id,status,attempt_no,model_label,provider,
                estimated_cost_hkd,confirmed_by
                FROM {TABLE_AI_FACTORY_ATTEMPTS}
                WHERE id=ANY(:ids) AND status IN ('claimed','running')
                  AND created_at<:cutoff
                ORDER BY id FOR UPDATE"""
        ),
        {"ids": candidate_ids, "cutoff": cutoff},
    ).fetchall()
    for row in stale_rows:
        stale_id = str(_stale_value(row, "id", 0))
        if candidate_by_id.get(stale_id, (None,))[0] != str(
            _stale_value(row, "job_id", 1)
        ):
            raise FactoryStoreError(409, "生成工作關聯已改變，請重新載入")
    if not stale_rows:
        return 0
    stale_ids = sorted(str(_stale_value(row, "id", 0)) for row in stale_rows)
    stale_jobs = sorted(
        {str(_stale_value(row, "job_id", 1)) for row in stale_rows}
    )
    conn.execute(
        text(
            f"""UPDATE {TABLE_AI_FACTORY_ATTEMPTS} SET status='failed',
                error_code='orphaned_attempt',completed_at=:now
                WHERE id=ANY(:ids) AND status IN ('claimed','running')"""
        ),
        {"ids": stale_ids, "now": now},
    )
    conn.execute(
        text(
            f"""UPDATE {TABLE_AI_FACTORY_JOBS} SET status='failed',updated_at=:now
                WHERE id=ANY(:ids) AND status='processing'
                  AND invalidated_at IS NULL"""
        ),
        {"ids": stale_jobs, "now": now},
    )
    for row in stale_rows:
        if str(_stale_value(row, "status", 2)) != "running":
            continue
        reserved_hkd = max(
            0.0, float(_stale_value(row, "estimated_cost_hkd", 6) or 0)
        )
        attempt = {
            "job_id": str(_stale_value(row, "job_id", 1)),
            "attempt_no": int(_stale_value(row, "attempt_no", 3)),
            "model_label": str(_stale_value(row, "model_label", 4)),
            "provider": str(_stale_value(row, "provider", 5)),
        }
        _settle_usage_in_transaction(
            conn,
            str(_stale_value(row, "confirmed_by", 7)),
            attempt,
            success=False,
            usage={
                "estimated_cost_usd": round(reserved_hkd / 7.8, 8),
                "estimated_cost_hkd": reserved_hkd,
                "cost_source": "factory_preview_estimate_orphaned_running",
            },
            error_code="orphaned_attempt",
        )
    return len(stale_rows)


def reap_stale_attempts(db) -> dict:
    """Recover crashed in-process work before an administrator retries it."""
    now = utc_now()
    with db.transaction() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('ai_factory_provider_capacity'))")
        )
        count = _reap_stale_attempts_in_transaction(conn, now)
    return {"reaped": count}


def claim_attempt(
    db,
    actor: str,
    *,
    job_id: str,
    preview_sha256: str,
    model_label: str,
    provider: str,
    provider_model: str,
    recipe_version: str,
    source_sha256: str,
    prompt_sha256: str,
    input_sha256: str,
    candidate_count: int,
    confirmation_version: str,
    anonymization_confirmed: bool,
    rights_confirmed: bool,
    third_party_confirmed: bool = False,
    pii_warning_count: int = 0,
    pii_override_reason: str = "",
    estimated_cost_hkd: float = 0,
) -> dict:
    if not anonymization_confirmed or not rights_confirmed or not third_party_confirmed:
        raise FactoryStoreError(400, "生成前必須確認匿名化、使用權及第三方 AI 傳送警告")
    warning_count = max(0, int(pii_warning_count or 0))
    override_reason = str(pii_override_reason or "").strip()
    if warning_count and not override_reason:
        raise FactoryStoreError(400, "來源有個人資料警告，必須填寫覆寫理由")
    try:
        estimated_cost = float(estimated_cost_hkd or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FactoryStoreError(400, "生成成本估算不正確") from exc
    if not 0 <= estimated_cost <= 9_999:
        raise FactoryStoreError(400, "生成成本估算不正確")
    now = utc_now()
    attempt_id = new_id("attempt")
    with db.transaction() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('ai_factory_provider_capacity'))")
        )
        _reap_stale_attempts_in_transaction(conn, now)
        job = _lock_job_lineage(conn, str(job_id))
        if str(job["created_by"]) != str(actor):
            raise FactoryStoreError(403, "只有建立工作嘅管理員可以生成或重試")
        if job["invalidated_at"] is not None or job["source_withdrawn_at"] is not None:
            raise FactoryStoreError(410, "來源或生成工作已失效")
        if job["status"] != "draft":
            raise FactoryStoreError(409, "生成工作已經處理或正在處理")
        if (
            job["preview_expires_at"] is None
            or _aware_utc(job["preview_expires_at"]) < now
        ):
            raise FactoryStoreError(409, "精確預覽已過期，請重新預覽")
        pinned = (
            str(job["preview_sha256"] or ""),
            str(job["preview_model_label"] or ""),
            str(job["preview_provider"] or ""),
            str(job["preview_provider_model"] or ""),
            str(job["preview_prompt_sha256"] or ""),
            str(job["preview_input_sha256"] or ""),
            str(job["current_source_sha"] or ""),
        )
        supplied = (
            preview_sha256,
            model_label,
            provider,
            provider_model,
            prompt_sha256,
            input_sha256,
            source_sha256,
        )
        if pinned != supplied:
            raise FactoryStoreError(409, "預覽內容或模型已改變，請重新預覽")
        previous_count = int(
            conn.execute(
                text(f"SELECT COUNT(*) FROM {TABLE_AI_FACTORY_ATTEMPTS} WHERE job_id=:id"),
                {"id": str(job_id)},
            ).scalar()
            or 0
        )
        if previous_count >= AI_FACTORY_ATTEMPT_MAX:
            raise FactoryStoreError(409, "此工作已達手動重試上限")
        global_active = int(
            conn.execute(
                text(
                    f"""SELECT
                        (SELECT COUNT(*) FROM {TABLE_AI_FACTORY_ATTEMPTS}
                            WHERE status IN ('claimed','running'))
                        +
                        (SELECT COUNT(*) FROM {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS}
                            WHERE status IN ('claimed','running'))"""
                )
            ).scalar()
            or 0
        )
        manager_active = int(
            conn.execute(
                text(
                    f"""SELECT
                        (SELECT COUNT(*) FROM {TABLE_AI_FACTORY_ATTEMPTS} a
                            JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=a.job_id
                            WHERE a.status IN ('claimed','running')
                              AND j.created_by=:actor)
                        +
                        (SELECT COUNT(*) FROM {TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS} a
                            JOIN {TABLE_AI_FACTORY_TRANSCRIPT_RUNS} r ON r.id=a.run_id
                            WHERE a.status IN ('claimed','running')
                              AND r.created_by=:actor)"""
                ),
                {"actor": str(actor)},
            ).scalar()
            or 0
        )
        if global_active >= AI_FACTORY_CONCURRENCY:
            raise FactoryStoreError(429, "資料工廠正處理其他生成工作，請稍後再試")
        if manager_active >= AI_FACTORY_MANAGER_CONCURRENCY:
            raise FactoryStoreError(429, "你已有一個生成工作正在處理")
        attempt_no = previous_count + 1
        conn.execute(
            text(
                f"""INSERT INTO {TABLE_AI_FACTORY_ATTEMPTS}(
                    id,job_id,attempt_no,operation_id,model_label,provider,provider_model,
                    recipe_key,recipe_version,candidate_count,estimated_cost_hkd,
                    source_sha256,prompt_sha256,
                    input_sha256,preview_sha256,previewed_at,preview_expires_at,
                    confirmation_version,anonymization_confirmed,rights_confirmed,
                    third_party_confirmed,pii_warning_count,pii_override_reason,
                    confirmed_by,confirmed_at,status,created_at
                ) VALUES(
                    :id,:job_id,:attempt_no,:job_id,:label,:provider,:provider_model,
                    :recipe,:recipe_version,:count,:estimated_cost_hkd,
                    :source_sha,:prompt_sha,:input_sha,
                    :preview_sha,:previewed_at,:expires,:confirmation,TRUE,TRUE,TRUE,
                    :pii_warning_count,:pii_override_reason,:actor,:now,'claimed',:now
                )"""
            ),
            {
                "id": attempt_id,
                "job_id": str(job_id),
                "attempt_no": attempt_no,
                "label": model_label,
                "provider": provider,
                "provider_model": provider_model,
                "recipe": str(job["recipe_key"]),
                "recipe_version": recipe_version,
                "count": int(candidate_count),
                "estimated_cost_hkd": estimated_cost,
                "source_sha": source_sha256,
                "prompt_sha": prompt_sha256,
                "input_sha": input_sha256,
                "preview_sha": preview_sha256,
                "previewed_at": job["updated_at"],
                "expires": job["preview_expires_at"],
                "confirmation": confirmation_version,
                "pii_warning_count": warning_count,
                "pii_override_reason": override_reason[:1000] if warning_count else None,
                "actor": str(actor),
                "now": now,
            },
        )
        conn.execute(
            text(
                f"""UPDATE {TABLE_AI_FACTORY_JOBS}
                    SET status='processing',updated_at=:now WHERE id=:id"""
            ),
            {"now": now, "id": str(job_id)},
        )
        _audit(
            conn,
            actor,
            "factory_generation_confirmed",
            "ai_factory_attempt",
            attempt_id,
            {
                "confirmation_version": str(confirmation_version)[:80],
                "anonymization_confirmed": True,
                "rights_confirmed": True,
                "third_party_confirmed": True,
                "pii_warning_count": warning_count,
                "pii_override_reason": override_reason[:1000],
                "preview_sha256": preview_sha256,
            },
        )
    return {"id": attempt_id, "job_id": str(job_id), "attempt_no": attempt_no}


def mark_provider_started(db, attempt_id: str) -> None:
    with db.transaction() as conn:
        identity = conn.execute(
            text(
                f"""SELECT a.job_id,j.source_id
                    FROM {TABLE_AI_FACTORY_ATTEMPTS} a
                    JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=a.job_id
                    WHERE a.id=:id"""
            ),
            {"id": str(attempt_id)},
        ).mappings().first()
        if identity is None:
            raise FactoryStoreError(404, "找不到生成 attempt")
        # Match withdrawal's source -> job -> attempt lock order.  Whichever
        # transaction wins the source lock is the linearization point deciding
        # whether any source text may leave the server.
        source = conn.execute(
            text(
                f"""SELECT id,withdrawn_at FROM {TABLE_AI_FACTORY_SOURCES}
                    WHERE id=:id FOR UPDATE"""
            ),
            {"id": str(identity["source_id"])},
        ).mappings().first()
        job = conn.execute(
            text(
                f"""SELECT id,source_id,invalidated_at FROM {TABLE_AI_FACTORY_JOBS}
                    WHERE id=:id FOR UPDATE"""
            ),
            {"id": str(identity["job_id"])},
        ).mappings().first()
        attempt = conn.execute(
            text(
                f"""SELECT id,job_id,status
                    FROM {TABLE_AI_FACTORY_ATTEMPTS}
                    WHERE id=:id FOR UPDATE"""
            ),
            {"id": str(attempt_id)},
        ).mappings().first()
        if source is None or job is None or attempt is None:
            raise FactoryStoreError(409, "生成工作關聯已改變")
        if (
            str(job["source_id"]) != str(source["id"])
            or str(attempt["job_id"]) != str(job["id"])
        ):
            raise FactoryStoreError(409, "生成工作關聯已改變")
        if source["withdrawn_at"] is not None or job["invalidated_at"] is not None:
            raise FactoryStoreError(410, "來源或生成工作已撤回，沒有呼叫 AI provider")
        if attempt["status"] != "claimed":
            raise FactoryStoreError(409, "生成 attempt 狀態已改變")
        now = utc_now()
        conn.execute(
            text(
                f"""UPDATE {TABLE_AI_FACTORY_ATTEMPTS}
                    SET status='running',provider_attempted_at=:now
                    WHERE id=:id AND status='claimed'"""
            ),
            {"id": str(attempt_id), "now": now},
        )


def _settle_usage_in_transaction(
    conn,
    actor: str,
    attempt,
    *,
    success: bool,
    usage: dict | None,
    error_code: str = "",
) -> None:
    if usage is None:
        return
    from core.funds_logic import log_ai_usage_in_transaction

    recorded = dict(usage)
    recorded.update(
        {
            "model_label": str(attempt["model_label"]),
            "provider": str(attempt["provider"]),
            "operation_id": str(attempt["job_id"]),
            "operation_stage": f"attempt_{int(attempt['attempt_no'])}",
        }
    )
    log_ai_usage_in_transaction(
        conn,
        actor,
        "data_factory_generation",
        success,
        recorded,
        error_message=error_code,
    )


def _provider_identity_from_usage(usage: dict | None) -> tuple[str | None, str | None]:
    """Bound optional provider lineage before storing terminal metadata."""
    values = usage or {}

    def bounded(name: str, maximum: int) -> str | None:
        value = str(values.get(name) or "").strip()
        return value[:maximum] or None

    return (
        bounded("provider_request_id", 300),
        bounded("resolved_provider_model", 200),
    )


def fail_attempt(
    db,
    actor: str,
    attempt_id: str,
    *,
    error_code: str,
    response_sha256: str = "",
    response_bytes: int = 0,
    provider_called: bool = True,
    usage: dict | None = None,
) -> None:
    now = utc_now()
    provider_request_id, resolved_provider_model = _provider_identity_from_usage(
        usage
    )
    with db.transaction() as conn:
        attempt = _lock_attempt_lineage(conn, str(attempt_id))
        if attempt["status"] not in ("claimed", "running"):
            return
        conn.execute(
            text(
                f"""UPDATE {TABLE_AI_FACTORY_ATTEMPTS} SET status='failed',
                    response_sha256=:sha,response_bytes=:bytes,error_code=:error,
                    provider_request_id=:provider_request_id,
                    resolved_provider_model=:resolved_provider_model,
                    completed_at=:now WHERE id=:id"""
            ),
            {
                "sha": response_sha256 or None,
                "bytes": max(0, int(response_bytes)),
                "error": str(error_code or "provider_error")[:100],
                "provider_request_id": provider_request_id,
                "resolved_provider_model": resolved_provider_model,
                "now": now,
                "id": str(attempt_id),
            },
        )
        conn.execute(
            text(
                f"""UPDATE {TABLE_AI_FACTORY_JOBS}
                    SET status=CASE WHEN invalidated_at IS NULL THEN 'failed' ELSE 'invalidated' END,
                        updated_at=:now WHERE id=:id"""
            ),
            {"now": now, "id": str(attempt["job_id"])},
        )
        if provider_called:
            _settle_usage_in_transaction(
                conn,
                actor,
                attempt,
                success=False,
                usage=usage or {},
                error_code=str(error_code or "provider_error")[:100],
            )


def complete_attempt(
    db,
    actor: str,
    attempt_id: str,
    *,
    payloads: list[dict],
    response_sha256: str,
    response_bytes: int,
    usage: dict | None = None,
) -> dict:
    now = utc_now()
    provider_request_id, resolved_provider_model = _provider_identity_from_usage(
        usage
    )
    if not 1 <= len(payloads) <= AI_FACTORY_CANDIDATE_MAX:
        raise FactoryStoreError(400, "候選數量不正確")
    with db.transaction() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('ai_factory_item_capacity'))")
        )
        attempt = _lock_attempt_lineage(conn, str(attempt_id))
        if attempt["status"] != "running":
            raise FactoryStoreError(409, "生成 attempt 狀態已改變")
        if attempt["job_invalidated_at"] is not None or attempt["source_withdrawn_at"] is not None:
            conn.execute(
                text(
                    f"""UPDATE {TABLE_AI_FACTORY_ATTEMPTS} SET status='discarded',
                        response_sha256=:sha,response_bytes=:bytes,
                        provider_request_id=:provider_request_id,
                        resolved_provider_model=:resolved_provider_model,
                        error_code='source_withdrawn',completed_at=:now WHERE id=:id"""
                ),
                {
                    "sha": response_sha256,
                    "bytes": int(response_bytes),
                    "provider_request_id": provider_request_id,
                    "resolved_provider_model": resolved_provider_model,
                    "now": now,
                    "id": str(attempt_id),
                },
            )
            conn.execute(
                text(
                    f"""UPDATE {TABLE_AI_FACTORY_JOBS} SET status='invalidated',
                        updated_at=:now WHERE id=:id"""
                ),
                {"now": now, "id": str(attempt["job_id"])},
            )
            _settle_usage_in_transaction(
                conn, actor, attempt, success=True, usage=usage
            )
            return {"discarded": True, "items": []}
        if len(payloads) != int(attempt["candidate_count"]):
            raise FactoryStoreError(400, "Provider 回傳候選數量不符")
        item_total = int(
            conn.execute(text(f"SELECT COUNT(*) FROM {TABLE_AI_FACTORY_ITEMS}")).scalar()
            or 0
        )
        if item_total + len(payloads) > AI_FACTORY_ITEM_MAX_TOTAL:
            raise FactoryStoreError(409, "候選資料已達保護上限，請先整理現有資料")
        item_ids = []
        for ordinal, payload in enumerate(payloads, 1):
            encoded = canonical_json(payload)
            item_id = new_id("item")
            item_ids.append(item_id)
            conn.execute(
                text(
                    f"""INSERT INTO {TABLE_AI_FACTORY_ITEMS}(
                        id,job_id,attempt_id,ordinal,original_json,original_sha256,
                        reviewed_json,reviewed_sha256,review_status,created_at
                    ) VALUES(
                        :id,:job_id,:attempt_id,:ordinal,CAST(:payload AS jsonb),:sha,
                        NULL,NULL,'pending',:now
                    )"""
                ),
                {
                    "id": item_id,
                    "job_id": str(attempt["job_id"]),
                    "attempt_id": str(attempt_id),
                    "ordinal": ordinal,
                    "payload": encoded,
                    "sha": sha256_text(encoded),
                    "now": now,
                },
            )
        conn.execute(
            text(
                f"""UPDATE {TABLE_AI_FACTORY_ATTEMPTS} SET status='succeeded',
                    response_sha256=:sha,response_bytes=:bytes,error_code=NULL,
                    provider_request_id=:provider_request_id,
                    resolved_provider_model=:resolved_provider_model,
                    completed_at=:now WHERE id=:id"""
            ),
            {
                "sha": response_sha256,
                "bytes": int(response_bytes),
                "provider_request_id": provider_request_id,
                "resolved_provider_model": resolved_provider_model,
                "now": now,
                "id": str(attempt_id),
            },
        )
        conn.execute(
            text(
                f"""UPDATE {TABLE_AI_FACTORY_JOBS}
                    SET status='awaiting_review',updated_at=:now WHERE id=:id"""
            ),
            {"now": now, "id": str(attempt["job_id"])},
        )
        _audit(
            conn,
            actor,
            "factory_generation_completed",
            "ai_factory_job",
            str(attempt["job_id"]),
            {"attempt_id": str(attempt_id), "item_count": len(item_ids), "response_sha256": response_sha256},
        )
        _settle_usage_in_transaction(
            conn, actor, attempt, success=True, usage=usage
        )
    return {"discarded": False, "items": item_ids}


def _normalise_tag(value: str) -> tuple[str, str]:
    label = re.sub(r"\s+", " ", str(value or "").strip())
    if not 1 <= len(label) <= AI_FACTORY_TOPIC_TAG_MAX_CHARS:
        raise FactoryStoreError(400, "主題標籤長度不正確")
    normalized = label.casefold()
    if not 1 <= len(normalized) <= AI_FACTORY_TOPIC_TAG_MAX_CHARS:
        raise FactoryStoreError(400, "主題標籤正規化後長度不正確")
    return label, normalized


def review_item(
    db,
    actor: str,
    item_id: str,
    *,
    decision: str,
    reviewed_payload: dict | None,
    reviewed_sha256: str,
    note: str,
    topic_tags: list[str],
) -> dict:
    if decision not in ("approved", "rejected"):
        raise FactoryStoreError(400, "審核狀態不正確")
    if decision == "rejected" and not str(note or "").strip():
        raise FactoryStoreError(400, "拒絕候選時必須填寫原因")
    if len(topic_tags) > AI_FACTORY_TOPIC_TAG_MAX:
        raise FactoryStoreError(400, "主題標籤數量超過上限")
    now = utc_now()
    with db.transaction() as conn:
        item = _lock_item_lineage(conn, str(item_id))
        if item["review_status"] != "pending":
            raise FactoryStoreError(409, "此候選已完成審核")
        if item["invalidated_at"] is not None or item["job_invalidated_at"] is not None or item["source_withdrawn_at"] is not None:
            raise FactoryStoreError(410, "候選或來源已失效")
        reviewed_encoded = canonical_json(reviewed_payload) if reviewed_payload is not None else None
        if decision == "approved":
            if reviewed_payload is None or sha256_text(reviewed_encoded) != reviewed_sha256:
                raise FactoryStoreError(409, "人工修訂內容雜湊不一致")
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"ai_factory_approved_hash:{reviewed_sha256}"},
            )
            duplicate = conn.execute(
                text(
                    f"""SELECT id FROM {TABLE_AI_FACTORY_ITEMS}
                        WHERE id<>:id AND review_status='approved'
                          AND COALESCE(reviewed_sha256,original_sha256)=:sha LIMIT 1"""
                ),
                {"id": str(item_id), "sha": reviewed_sha256},
            ).first()
            if duplicate is not None:
                raise FactoryStoreError(409, "完全相同嘅資料已經批准")
        conn.execute(
            text(
                f"""UPDATE {TABLE_AI_FACTORY_ITEMS} SET
                    reviewed_json=CASE WHEN :payload IS NULL THEN NULL ELSE CAST(:payload AS jsonb) END,
                    reviewed_sha256=:sha,review_status=:status,review_note=:note,
                    reviewed_by=:actor,reviewed_at=:now WHERE id=:id"""
            ),
            {
                "payload": reviewed_encoded,
                "sha": reviewed_sha256 if decision == "approved" else None,
                "status": decision,
                "note": str(note or "")[:2000],
                "actor": str(actor),
                "now": now,
                "id": str(item_id),
            },
        )
        if decision == "approved":
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('ai_factory_topic_tag_capacity'))")
            )
            for raw_tag in topic_tags:
                label, normalized = _normalise_tag(raw_tag)
                tag_id = "tag_" + sha256_text(normalized)[:24]
                active_tag = conn.execute(
                    text(
                        f"""SELECT id,retired_at FROM {TABLE_AI_FACTORY_TOPIC_TAGS}
                            WHERE normalized_label=:normalized FOR UPDATE"""
                    ),
                    {"normalized": normalized},
                ).mappings().first()
                if active_tag is None:
                    tag_total = int(
                        conn.execute(
                            text(f"SELECT COUNT(*) FROM {TABLE_AI_FACTORY_TOPIC_TAGS}")
                        ).scalar()
                        or 0
                    )
                    if tag_total >= AI_FACTORY_TOPIC_TAG_MAX_TOTAL:
                        raise FactoryStoreError(409, "主題標籤已達保護上限")
                    conn.execute(
                        text(
                            f"""INSERT INTO {TABLE_AI_FACTORY_TOPIC_TAGS}(
                                id,label,normalized_label,approved_by,approved_at
                            ) VALUES(:id,:label,:normalized,:actor,:now)"""
                        ),
                        {
                            "id": tag_id,
                            "label": label,
                            "normalized": normalized,
                            "actor": str(actor),
                            "now": now,
                        },
                    )
                    active_tag = {"id": tag_id, "retired_at": None}
                    _audit(
                        conn,
                        actor,
                        "factory_topic_tag_approved",
                        "ai_factory_topic_tag",
                        tag_id,
                        {"label": label},
                    )
                if active_tag["retired_at"] is not None:
                    raise FactoryStoreError(409, f"主題標籤「{label}」已停用")
                conn.execute(
                    text(
                        f"""INSERT INTO {TABLE_AI_FACTORY_ITEM_TAGS}
                            (item_id,tag_id,assigned_by,assigned_at)
                            VALUES(:item_id,:tag_id,:actor,:now)
                            ON CONFLICT(item_id,tag_id) DO NOTHING"""
                    ),
                    {"item_id": str(item_id), "tag_id": str(active_tag["id"]), "actor": str(actor), "now": now},
                )
        pending = int(
            conn.execute(
                text(
                    f"""SELECT COUNT(*) FROM {TABLE_AI_FACTORY_ITEMS}
                        WHERE job_id=:job AND review_status='pending' AND invalidated_at IS NULL"""
                ),
                {"job": str(item["job_id"])},
            ).scalar()
            or 0
        )
        if pending == 0:
            conn.execute(
                text(
                    f"""UPDATE {TABLE_AI_FACTORY_JOBS}
                        SET status='reviewed',updated_at=:now WHERE id=:id"""
                ),
                {"now": now, "id": str(item["job_id"])},
            )
        _audit(
            conn,
            actor,
            "factory_item_reviewed",
            "ai_factory_item",
            str(item_id),
            {"decision": decision, "reviewed_sha256": reviewed_sha256 if decision == "approved" else None},
        )
    return {"ok": True, "status": decision}


def _invalidate_releases_for_items(conn, item_ids: list[str], actor: str, reason: str, now: datetime) -> list[str]:
    if not item_ids:
        return []
    item_ids = sorted({str(item_id) for item_id in item_ids})
    rows = conn.execute(
        text(
            f"""SELECT r.id FROM {TABLE_AI_FACTORY_RELEASES} r
                WHERE r.invalidated_at IS NULL AND EXISTS (
                    SELECT 1 FROM {TABLE_AI_FACTORY_RELEASE_ITEMS} ri
                    WHERE ri.release_id=r.id AND ri.item_id=ANY(:item_ids)
                )
                ORDER BY r.id FOR UPDATE OF r"""
        ),
        {"item_ids": item_ids},
    ).fetchall()
    release_ids = sorted(str(row[0]) for row in rows)
    if release_ids:
        conn.execute(
            text(
                f"""UPDATE {TABLE_AI_FACTORY_RELEASES}
                    SET invalidated_by=:actor,invalidated_at=:now,invalidation_reason=:reason
                    WHERE id=ANY(:ids) AND invalidated_at IS NULL"""
            ),
            {"actor": str(actor), "now": now, "reason": str(reason)[:1000], "ids": release_ids},
        )
        for release_id in release_ids:
            _audit(
                conn,
                actor,
                "factory_release_invalidated",
                "ai_factory_release",
                release_id,
                {"reason": str(reason)[:300]},
            )
    return release_ids


def withdraw_item(db, actor: str, item_id: str, reason: str) -> dict:
    reason = str(reason or "").strip()
    if not reason:
        raise FactoryStoreError(400, "請填寫撤回原因")
    now = utc_now()
    with db.transaction() as conn:
        item = conn.execute(
            text(
                f"""SELECT id,review_status,invalidated_at
                    FROM {TABLE_AI_FACTORY_ITEMS} WHERE id=:id FOR UPDATE"""
            ),
            {"id": str(item_id)},
        ).mappings().first()
        if item is None:
            raise FactoryStoreError(404, "找不到資料項目")
        if item["review_status"] != "approved":
            raise FactoryStoreError(409, "只可撤回已批准資料；待審或已拒絕資料請保留原審核流程")
        changed = item["invalidated_at"] is None
        if changed:
            conn.execute(
                text(
                    f"""UPDATE {TABLE_AI_FACTORY_ITEMS} SET invalidated_by=:actor,
                        invalidated_at=:now,invalidation_reason=:reason WHERE id=:id"""
                ),
                {"actor": str(actor), "now": now, "reason": reason[:1000], "id": str(item_id)},
            )
            release_ids = _invalidate_releases_for_items(conn, [str(item_id)], actor, reason, now)
            _audit(conn, actor, "factory_item_withdrawn", "ai_factory_item", item_id, {"affected_releases": release_ids})
        else:
            release_ids = []
    return {"ok": True, "changed": changed, "affected_releases": release_ids}


def _withdraw_sources_in_transaction(conn, actor: str, source_ids: list[str], reason: str, now: datetime) -> dict:
    source_ids = sorted({str(source_id) for source_id in source_ids})
    if not source_ids:
        return {"sources": [], "items": [], "releases": []}
    locked = conn.execute(
        text(
            f"""SELECT id FROM {TABLE_AI_FACTORY_SOURCES}
                WHERE id=ANY(:ids) AND withdrawn_at IS NULL
                ORDER BY id FOR UPDATE"""
        ),
        {"ids": source_ids},
    ).fetchall()
    active_ids = sorted(str(row[0]) for row in locked)
    if not active_ids:
        return {"sources": [], "items": [], "releases": []}
    conn.execute(
        text(
            f"""UPDATE {TABLE_AI_FACTORY_SOURCES} SET withdrawn_by=:actor,
                withdrawn_at=:now,withdrawal_reason=:reason
                WHERE id=ANY(:ids) AND withdrawn_at IS NULL"""
        ),
        {"actor": str(actor), "now": now, "reason": str(reason)[:1000], "ids": active_ids},
    )
    job_rows = conn.execute(
        text(
            f"""SELECT id FROM {TABLE_AI_FACTORY_JOBS}
                WHERE source_id=ANY(:ids) ORDER BY source_id,id FOR UPDATE"""
        ),
        {"ids": active_ids},
    ).fetchall()
    job_ids = sorted(str(row[0]) for row in job_rows)
    if job_ids:
        conn.execute(
            text(
                f"""UPDATE {TABLE_AI_FACTORY_JOBS} SET status='invalidated',
                    invalidated_by=COALESCE(invalidated_by,:actor),
                    invalidated_at=COALESCE(invalidated_at,:now),
                    invalidation_reason=COALESCE(invalidation_reason,:reason),updated_at=:now
                    WHERE id=ANY(:ids)"""
            ),
            {"actor": str(actor), "now": now, "reason": str(reason)[:1000], "ids": job_ids},
        )
        attempt_rows = conn.execute(
            text(
                f"""SELECT id FROM {TABLE_AI_FACTORY_ATTEMPTS}
                    WHERE job_id=ANY(:ids) AND status='claimed'
                    ORDER BY job_id,id FOR UPDATE"""
            ),
            {"ids": job_ids},
        ).fetchall()
        attempt_ids = sorted(str(row[0]) for row in attempt_rows)
        if attempt_ids:
            conn.execute(
                text(
                    f"""UPDATE {TABLE_AI_FACTORY_ATTEMPTS} SET status='discarded',
                        error_code='source_withdrawn',completed_at=COALESCE(completed_at,:now)
                        WHERE id=ANY(:ids) AND status='claimed'"""
                ),
                {"ids": attempt_ids, "now": now},
            )
    item_rows = conn.execute(
        text(
            f"""SELECT id FROM {TABLE_AI_FACTORY_ITEMS}
                WHERE job_id=ANY(:jobs) AND invalidated_at IS NULL
                ORDER BY job_id,id FOR UPDATE"""
        ),
        {"jobs": job_ids or ["__none__"]},
    ).fetchall()
    item_ids = sorted(str(row[0]) for row in item_rows)
    if item_ids:
        conn.execute(
            text(
                f"""UPDATE {TABLE_AI_FACTORY_ITEMS} SET invalidated_by=:actor,
                    invalidated_at=:now,invalidation_reason=:reason
                    WHERE id=ANY(:ids) AND invalidated_at IS NULL"""
            ),
            {"actor": str(actor), "now": now, "reason": str(reason)[:1000], "ids": item_ids},
        )
    release_ids = _invalidate_releases_for_items(conn, item_ids, actor, reason, now)
    for source_id in active_ids:
        _audit(
            conn,
            actor,
            "factory_source_withdrawn",
            "ai_factory_source",
            source_id,
            {"affected_items": len(item_ids), "affected_releases": release_ids},
        )
    return {"sources": active_ids, "items": item_ids, "releases": release_ids}


def withdraw_source(db, actor: str, source_id: str, reason: str) -> dict:
    reason = str(reason or "").strip()
    if not reason:
        raise FactoryStoreError(400, "請填寫撤回原因")
    with db.transaction() as conn:
        result = _withdraw_sources_in_transaction(conn, actor, [str(source_id)], reason, utc_now())
    return {"ok": True, "changed": bool(result["sources"]), **result}


def withdraw_submission_sources_in_transaction(conn, actor: str, submission_id: int, reason: str, now=None) -> dict:
    rows = conn.execute(
        text(
            f"""SELECT id FROM {TABLE_AI_FACTORY_SOURCES}
                WHERE source_kind='llm_submission' AND origin_submission_id=:submission_id
                  AND withdrawn_at IS NULL ORDER BY id"""
        ),
        {"submission_id": int(submission_id)},
    ).fetchall()
    return _withdraw_sources_in_transaction(
        conn,
        actor,
        [str(row[0]) for row in rows],
        str(reason or "原始 LLM 投稿已撤回"),
        now or utc_now(),
    )


def _lock_release_item_lineages(conn, item_ids: list[str]):
    """Lock release inputs in source -> job -> item order across all lineages."""
    requested_ids = [str(item_id) for item_id in item_ids]
    locators = conn.execute(
        text(
            f"""SELECT i.id,i.job_id,j.source_id
                FROM {TABLE_AI_FACTORY_ITEMS} i
                JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=i.job_id
                WHERE i.id=ANY(:ids) ORDER BY i.id"""
        ),
        {"ids": requested_ids},
    ).mappings().all()
    locator_by_id = {str(row["id"]): row for row in locators}
    if set(locator_by_id) != set(requested_ids):
        raise FactoryStoreError(404, "部分已批准資料不存在")
    source_ids = sorted({str(row["source_id"]) for row in locators})
    job_ids = sorted({str(row["job_id"]) for row in locators})
    sources = conn.execute(
        text(
            f"""SELECT id,withdrawn_at FROM {TABLE_AI_FACTORY_SOURCES}
                WHERE id=ANY(:ids) ORDER BY id FOR UPDATE"""
        ),
        {"ids": source_ids},
    ).mappings().all()
    jobs = conn.execute(
        text(
            f"""SELECT id,source_id,recipe_key,invalidated_at
                FROM {TABLE_AI_FACTORY_JOBS}
                WHERE id=ANY(:ids) ORDER BY id FOR UPDATE"""
        ),
        {"ids": job_ids},
    ).mappings().all()
    items = conn.execute(
        text(
            f"""SELECT id,job_id,
                    COALESCE(reviewed_sha256,original_sha256) AS item_sha,
                    review_status,invalidated_at
                FROM {TABLE_AI_FACTORY_ITEMS}
                WHERE id=ANY(:ids) ORDER BY id FOR UPDATE"""
        ),
        {"ids": requested_ids},
    ).mappings().all()
    source_by_id = {str(row["id"]): row for row in sources}
    job_by_id = {str(row["id"]): row for row in jobs}
    item_by_id = {str(row["id"]): row for row in items}
    if (
        set(source_by_id) != set(source_ids)
        or set(job_by_id) != set(job_ids)
        or set(item_by_id) != set(requested_ids)
    ):
        raise FactoryStoreError(409, "資料關聯已改變，請重新建立發布預覽")
    locked = {}
    for item_id in requested_ids:
        locator = locator_by_id[item_id]
        job = job_by_id[str(locator["job_id"])]
        item = item_by_id[item_id]
        source = source_by_id[str(locator["source_id"])]
        if (
            str(job["source_id"]) != str(source["id"])
            or str(item["job_id"]) != str(job["id"])
        ):
            raise FactoryStoreError(409, "資料關聯已改變，請重新建立發布預覽")
        row = dict(item)
        row["recipe_key"] = job["recipe_key"]
        row["job_invalidated"] = job["invalidated_at"]
        row["source_withdrawn"] = source["withdrawn_at"]
        locked[item_id] = row
    return locked


def create_release(
    db,
    actor: str,
    *,
    release_kind: str,
    item_ids: list[str],
    schema_version: str,
    jsonl_text: str,
    manifest: dict,
    jsonl_line_hashes: list[str],
    item_hashes: list[str],
) -> dict:
    if release_kind not in ("rag", "sft"):
        raise FactoryStoreError(400, "發布類型不正確")
    if not 1 <= len(item_ids) <= AI_FACTORY_RELEASE_MAX_ITEMS:
        raise FactoryStoreError(400, "發布項目數量不正確")
    if len(set(item_ids)) != len(item_ids):
        raise FactoryStoreError(400, "發布項目不可重複")
    encoded = str(jsonl_text)
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) > AI_FACTORY_RELEASE_MAX_BYTES:
        raise FactoryStoreError(413, "發布檔案超過大小上限")
    if len(jsonl_line_hashes) != len(item_ids) or len(item_hashes) != len(item_ids):
        raise FactoryStoreError(400, "發布 manifest 不完整")
    now = utc_now()
    with db.transaction() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": "ai_factory_release_capacity"},
        )
        release_total = int(
            conn.execute(text(f"SELECT COUNT(*) FROM {TABLE_AI_FACTORY_RELEASES}")).scalar()
            or 0
        )
        if release_total >= AI_FACTORY_RELEASE_MAX_TOTAL:
            raise FactoryStoreError(409, "發布版本已達保護上限")
        by_id = _lock_release_item_lineages(conn, item_ids)
        for index, item_id in enumerate(item_ids):
            row = by_id[item_id]
            if row["review_status"] != "approved" or row["invalidated_at"] is not None or row["job_invalidated"] is not None or row["source_withdrawn"] is not None:
                raise FactoryStoreError(409, "發布只可包含仍然有效嘅已批准資料")
            if RECIPE_KINDS.get(str(row["recipe_key"])) != release_kind:
                raise FactoryStoreError(409, "RAG 與 SFT 資料不可放入同一個發布版本")
            if str(row["item_sha"]) != str(item_hashes[index]):
                raise FactoryStoreError(409, "資料項目已改變，請重新建立發布預覽")
        version_no = int(
            conn.execute(
                text(
                    f"SELECT COALESCE(MAX(version_no),0)+1 FROM {TABLE_AI_FACTORY_RELEASES} WHERE release_kind=:kind"
                ),
                {"kind": release_kind},
            ).scalar()
            or 1
        )
        release_id = f"{release_kind}-v{version_no:06d}"
        final_manifest = dict(manifest or {})
        final_manifest.update(
            {
                "release_id": release_id,
                "release_kind": release_kind,
                "version_no": version_no,
                "schema_version": str(schema_version)[:80],
                "jsonl_sha256": sha256_text(encoded),
                "jsonl_bytes": len(encoded_bytes),
                "item_count": len(item_ids),
                "published_at": now.isoformat(),
            }
        )
        manifest_text = canonical_json(final_manifest)
        conn.execute(
            text(
                f"""INSERT INTO {TABLE_AI_FACTORY_RELEASES}(
                    id,release_kind,version_no,schema_version,jsonl_text,jsonl_sha256,
                    jsonl_bytes,manifest_json,manifest_sha256,item_count,published_by,published_at
                ) VALUES(
                    :id,:kind,:version,:schema,:jsonl,:jsonl_sha,:bytes,
                    CAST(:manifest AS jsonb),:manifest_sha,:count,:actor,:now
                )"""
            ),
            {
                "id": release_id,
                "kind": release_kind,
                "version": version_no,
                "schema": str(schema_version)[:80],
                "jsonl": encoded,
                "jsonl_sha": sha256_text(encoded),
                "bytes": len(encoded_bytes),
                "manifest": manifest_text,
                "manifest_sha": sha256_text(manifest_text),
                "count": len(item_ids),
                "actor": str(actor),
                "now": now,
            },
        )
        for ordinal, item_id in enumerate(item_ids, 1):
            conn.execute(
                text(
                    f"""INSERT INTO {TABLE_AI_FACTORY_RELEASE_ITEMS}(
                        release_id,item_id,ordinal,item_sha256,jsonl_line_sha256
                    ) VALUES(:release,:item,:ordinal,:item_sha,:line_sha)"""
                ),
                {
                    "release": release_id,
                    "item": item_id,
                    "ordinal": ordinal,
                    "item_sha": item_hashes[ordinal - 1],
                    "line_sha": jsonl_line_hashes[ordinal - 1],
                },
            )
        _audit(
            conn,
            actor,
            "factory_release_published",
            "ai_factory_release",
            release_id,
            {"kind": release_kind, "version": version_no, "item_count": len(item_ids), "manifest_sha256": sha256_text(manifest_text)},
        )
    return {
        "id": release_id,
        "release_kind": release_kind,
        "version_no": version_no,
        "jsonl_sha256": sha256_text(encoded),
        "manifest_sha256": sha256_text(manifest_text),
        "item_count": len(item_ids),
    }


def get_release_for_download(db, release_id: str) -> dict:
    release = _row(
        db.query(
            f"""SELECT * FROM {TABLE_AI_FACTORY_RELEASES} WHERE id=:id""",
            {"id": str(release_id)},
        )
    )
    if release is None:
        raise FactoryStoreError(404, "找不到發布版本")
    if release.get("invalidated_at") is not None:
        raise FactoryStoreError(410, "發布版本已失效，請建立新版本")
    invalid = _row(
        db.query(
            f"""SELECT 1 AS invalid FROM {TABLE_AI_FACTORY_RELEASE_ITEMS} ri
                JOIN {TABLE_AI_FACTORY_ITEMS} i ON i.id=ri.item_id
                JOIN {TABLE_AI_FACTORY_JOBS} j ON j.id=i.job_id
                JOIN {TABLE_AI_FACTORY_SOURCES} s ON s.id=j.source_id
                WHERE ri.release_id=:id AND (
                    i.review_status!='approved' OR i.invalidated_at IS NOT NULL
                    OR j.invalidated_at IS NOT NULL OR s.withdrawn_at IS NOT NULL
                ) LIMIT 1""",
            {"id": str(release_id)},
        )
    )
    if invalid is not None:
        raise FactoryStoreError(410, "發布版本包含已撤回資料，禁止下載")
    return release
