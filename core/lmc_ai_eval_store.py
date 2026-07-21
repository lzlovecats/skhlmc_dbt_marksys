"""Transactional persistence for Phase-2 local-AI evaluation campaigns."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets

from sqlalchemy import text

from ai_model_config import LMC_AI_MODEL_PROFILE_VERSION
from core.ai_eval_defaults import EVAL_SUITE_ID, EVAL_SUITE_VERSION, load_eval_cases, suite_hash
from core.lmc_ai_eval import (
    EVAL_MODES, EVAL_PAIRS, EVAL_PROMPT_VERSION, REVIEW_DIMENSIONS,
    aggregate_campaign, generation_order, prompt_fingerprint,
)
from core.lmc_ai_runtime import PERSONA_VERSION
from core.funds_logic import log_ai_usage_in_transaction
from core.schema_features import READY, feature_bundle_state
from schema import (
    TABLE_AI_EVAL_CAMPAIGNS, TABLE_AI_EVAL_CASES, TABLE_AI_EVAL_OUTPUTS,
    TABLE_AI_EVAL_REVIEWS,
)
from system_limits import (
    LMC_AI_EVAL_CAMPAIGN_MAX, LMC_AI_EVAL_GENERATION_ATTEMPT_MAX,
    LMC_AI_EVAL_PROCESSING_LEASE_SECONDS, LMC_AI_EVAL_REVIEWS_PER_PAIR,
)


EVAL_TABLE_BUNDLE = (
    TABLE_AI_EVAL_CASES, TABLE_AI_EVAL_CAMPAIGNS,
    TABLE_AI_EVAL_OUTPUTS, TABLE_AI_EVAL_REVIEWS,
)


def _json(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dicts(result) -> list[dict]:
    return [dict(row._mapping) for row in result.fetchall()]


def require_eval_schema(db) -> None:
    if feature_bundle_state(db, "eval", EVAL_TABLE_BUNDLE) != READY:
        raise RuntimeError("Phase 2 A/B Test database migration 尚未套用。")


def latest_campaign(db) -> dict | None:
    rows = db.query(f"""SELECT * FROM {TABLE_AI_EVAL_CAMPAIGNS}
        ORDER BY CASE WHEN status IN ('generating','reviewing') THEN 0 ELSE 1 END,
        created_at DESC LIMIT 1""")
    if rows.empty:
        return None
    value = dict(rows.iloc[0])
    value["model_manifest"] = _json(value.get("model_manifest"))
    value["summary_json"] = _json(value.get("summary_json"))
    return value


def campaign_progress(db, campaign_id: str, reviewer_user_id: str = "") -> dict:
    generation = db.query(f"""SELECT status,COUNT(*) n FROM {TABLE_AI_EVAL_OUTPUTS}
        WHERE campaign_id=:campaign GROUP BY status""", {"campaign": campaign_id})
    generation_counts = {str(row["status"]): int(row["n"]) for _, row in generation.iterrows()}
    quorum = db.query(f"""SELECT COUNT(*) FILTER (WHERE submitted_at IS NOT NULL) submitted,
        COUNT(DISTINCT (case_id,pair_key)) FILTER (WHERE submitted_at IS NOT NULL) covered_pairs
        FROM {TABLE_AI_EVAL_REVIEWS} WHERE campaign_id=:campaign""", {"campaign": campaign_id})
    submitted = int(quorum.iloc[0]["submitted"] or 0) if not quorum.empty else 0
    covered = int(quorum.iloc[0]["covered_pairs"] or 0) if not quorum.empty else 0
    mine = 0
    if reviewer_user_id:
        rows = db.query(f"""SELECT COUNT(*) n FROM {TABLE_AI_EVAL_REVIEWS}
            WHERE campaign_id=:campaign AND reviewer_user_id=:reviewer
              AND submitted_at IS NOT NULL""", {"campaign": campaign_id, "reviewer": reviewer_user_id})
        mine = int(rows.iloc[0]["n"] or 0) if not rows.empty else 0
    return {
        "generation": {**generation_counts, "total": sum(generation_counts.values())},
        "quorum": {"submitted": submitted, "required": 90 * LMC_AI_EVAL_REVIEWS_PER_PAIR, "covered_pairs": covered, "total_pairs": 90},
        "reviewer_completed": mine,
    }


def create_campaign(db, *, actor_id: str, node_id: str, snapshot: dict, note: str) -> dict:
    digests = snapshot.get("model_digests") if isinstance(snapshot, dict) else None
    available = set(snapshot.get("models") or []) if isinstance(snapshot, dict) else set()
    manifest = {}
    from ai_model_config import LMC_AI_MODE_OPTIONS
    for mode in EVAL_MODES:
        config = LMC_AI_MODE_OPTIONS[mode]
        model = str(config["model"])
        digest = str((digests or {}).get(model) or "")
        if model not in available or len(digest) != 64:
            raise ValueError("三個模式必須齊全並提供 exact model digest。")
        if any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("model digest格式無效。")
        manifest[mode] = {
            "model": model, "digest": digest, "thinking": bool(config["thinking"]),
            "runtime": str(snapshot.get("runtime") or ""),
            "runtime_version": str(snapshot.get("runtime_version") or ""),
        }
    if not snapshot.get("online") or not snapshot.get("ready") or snapshot.get("draining"):
        raise ValueError("所選 AI 電腦未 ready。")
    campaign_id = secrets.token_hex(16)
    cases = []
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('lmc_ai_eval_campaign'))"))
        retained = conn.execute(text(f"SELECT COUNT(*) FROM {TABLE_AI_EVAL_CAMPAIGNS}")).scalar_one()
        if int(retained) >= LMC_AI_EVAL_CAMPAIGN_MAX:
            raise ValueError("已保留10個campaign；Phase 2唔會自動刪除。")
        if conn.execute(text(f"SELECT 1 FROM {TABLE_AI_EVAL_CAMPAIGNS} WHERE status IN ('generating','reviewing') LIMIT 1 FOR UPDATE")).first():
            raise ValueError("同一時間只可以有一個進行中campaign。")
        cases = _dicts(conn.execute(text(f"""SELECT case_id,content_hash FROM {TABLE_AI_EVAL_CASES}
            WHERE suite_id=:suite AND suite_version=:version AND is_active=TRUE ORDER BY case_id FOR SHARE"""),
            {"suite": EVAL_SUITE_ID, "version": EVAL_SUITE_VERSION}))
        if len(cases) != 30:
            raise RuntimeError("固定eval suite唔完整，拒絕建立campaign。")
        expected_cases = [(item["case_id"], item["content_hash"]) for item in load_eval_cases()]
        if [(row["case_id"], row["content_hash"]) for row in cases] != sorted(expected_cases):
            raise RuntimeError("database eval suite hash同repo asset不一致。")
        conn.execute(text(f"""INSERT INTO {TABLE_AI_EVAL_CAMPAIGNS}(
            campaign_id,suite_id,suite_version,suite_hash,prompt_version,prompt_hash,
            persona_hash,model_profile_version,model_manifest,bound_node_id,status,
            created_by,note,required_votes,started_at
        ) VALUES(:campaign,:suite,:suite_version,:suite_hash,:prompt_version,:prompt_hash,
            :persona,:profile,CAST(:manifest AS JSONB),:node,'generating',:actor,:note,:votes,NOW())"""), {
            "campaign": campaign_id, "suite": EVAL_SUITE_ID,
            "suite_version": EVAL_SUITE_VERSION, "suite_hash": suite_hash(),
            "prompt_version": EVAL_PROMPT_VERSION, "prompt_hash": prompt_fingerprint(),
            "persona": PERSONA_VERSION, "profile": LMC_AI_MODEL_PROFILE_VERSION,
            "manifest": _canonical(manifest), "node": node_id, "actor": actor_id,
            "note": note, "votes": LMC_AI_EVAL_REVIEWS_PER_PAIR,
        })
        output_rows = []
        for case in cases:
            for order, mode in enumerate(generation_order(case["content_hash"])):
                output_rows.append({"campaign": campaign_id, "case": case["case_id"], "mode": mode, "order": order})
        conn.execute(text(f"""INSERT INTO {TABLE_AI_EVAL_OUTPUTS}
            (campaign_id,case_id,mode,generation_order) VALUES(:campaign,:case,:mode,:order)"""), output_rows)
    return {"campaign_id": campaign_id, "status": "generating", "outputs": len(cases) * 3}


def claim_next_output(db, campaign_id: str) -> dict | None:
    lease = secrets.token_hex(16)
    operation_id = "eval:" + campaign_id
    with db.transaction() as conn:
        campaign = conn.execute(text(f"SELECT * FROM {TABLE_AI_EVAL_CAMPAIGNS} WHERE campaign_id=:campaign FOR UPDATE"), {"campaign": campaign_id}).mappings().first()
        if not campaign:
            raise LookupError("找不到campaign。")
        if campaign["status"] != "generating":
            raise ValueError("只有generating campaign可以生成答案。")
        row = conn.execute(text(f"""SELECT o.*,c.task_type,c.input_json,c.content_hash
            FROM {TABLE_AI_EVAL_OUTPUTS} o JOIN {TABLE_AI_EVAL_CASES} c USING(case_id)
            WHERE o.campaign_id=:campaign AND (
                o.status='pending' OR
                (o.status='failed' AND o.attempt_count<:attempt_max) OR
                (o.status='processing' AND o.lease_expires_at<NOW())
            ) ORDER BY o.case_id,o.generation_order LIMIT 1 FOR UPDATE OF o SKIP LOCKED"""),
            {"campaign": campaign_id, "attempt_max": LMC_AI_EVAL_GENERATION_ATTEMPT_MAX}).mappings().first()
        if not row:
            return None
        next_attempt = int(row["attempt_count"] or 0) + 1
        operation = f"{operation_id}:{row['case_id']}:{row['mode']}"
        conn.execute(text(f"""UPDATE {TABLE_AI_EVAL_OUTPUTS} SET status='processing',
            active_attempt=:attempt,lease_token=:lease,
            lease_expires_at=NOW()+(:lease_seconds * INTERVAL '1 second'),
            operation_id=:operation,error_code='',error_message='',updated_at=NOW()
            WHERE campaign_id=:campaign AND case_id=:case AND mode=:mode"""), {
            "attempt": next_attempt, "lease": lease,
            "lease_seconds": LMC_AI_EVAL_PROCESSING_LEASE_SECONDS,
            "operation": operation, "campaign": campaign_id,
            "case": row["case_id"], "mode": row["mode"],
        })
        value = dict(row)
        value.update({
            "lease_token": lease, "active_attempt": next_attempt,
            "operation_id": operation, "input_json": _json(value["input_json"]),
            "campaign": dict(campaign),
        })
        value["campaign"]["model_manifest"] = _json(value["campaign"]["model_manifest"])
        return value


def mark_output_started(db, claim: dict) -> bool:
    with db.transaction() as conn:
        result = conn.execute(text(f"""UPDATE {TABLE_AI_EVAL_OUTPUTS}
            SET attempt_count=:attempt,started_at=NOW(),updated_at=NOW()
            WHERE campaign_id=:campaign AND case_id=:case AND mode=:mode
              AND status='processing' AND lease_token=:lease
              AND active_attempt=:attempt AND attempt_count=:previous"""), {
            "attempt": claim["active_attempt"], "previous": claim["active_attempt"] - 1,
            "campaign": claim["campaign_id"], "case": claim["case_id"],
            "mode": claim["mode"], "lease": claim["lease_token"],
        })
        if result.rowcount != 1:
            return False
        log_ai_usage_in_transaction(conn, None, "lmc_ai_eval", True, {
            "provider": "custom", "model_label": "自家 AI 固定盲評",
            "estimated_cost_usd": 0, "estimated_cost_hkd": 0,
            "operation_id": claim["operation_id"],
            "operation_stage": f"attempt_{claim['active_attempt']}",
            "cost_source": "local_zero_cost_attempt",
        })
        return True


def settle_output(db, claim: dict, *, success: bool, answer: str, usage: dict, identity: dict, error: str = "") -> bool:
    answer = str(answer or "")
    status = "succeeded" if success else "failed"
    answer_hash = hashlib.sha256(answer.encode("utf-8")).hexdigest() if success else None
    with db.transaction() as conn:
        result = conn.execute(text(f"""UPDATE {TABLE_AI_EVAL_OUTPUTS} SET
            status=:status,lease_token=NULL,lease_expires_at=NULL,
            model_tag=:model,model_digest=:digest,model_profile_version=:profile,
            runtime_name=:runtime,runtime_version=:runtime_version,
            backend_fingerprint=:fingerprint,persona_hash=:persona,prompt_hash=:prompt,
            thinking_enabled=:thinking,answer_text=:answer,answer_hash=:answer_hash,
            input_tokens=:input_tokens,output_tokens=:output_tokens,duration_ms=:duration,
            error_code=:error_code,error_message=:error,completed_at=NOW(),updated_at=NOW()
            WHERE campaign_id=:campaign AND case_id=:case AND mode=:mode
              AND status='processing' AND lease_token=:lease AND active_attempt=:attempt"""), {
            "status": status, "model": identity.get("model"), "digest": identity.get("digest"),
            "profile": LMC_AI_MODEL_PROFILE_VERSION, "runtime": identity.get("runtime"),
            "runtime_version": identity.get("runtime_version"), "fingerprint": identity.get("fingerprint"),
            "persona": PERSONA_VERSION, "prompt": prompt_fingerprint(),
            "thinking": identity.get("thinking"), "answer": answer if success else None,
            "answer_hash": answer_hash, "input_tokens": max(0, int(usage.get("input_tokens") or 0)),
            "output_tokens": max(0, int(usage.get("output_tokens") or 0)),
            "duration": max(0, int(usage.get("duration_ms") or 0)),
            "error_code": "" if success else "generation_failed", "error": str(error or "")[:500],
            "campaign": claim["campaign_id"], "case": claim["case_id"], "mode": claim["mode"],
            "lease": claim["lease_token"], "attempt": claim["active_attempt"],
        })
        return result.rowcount == 1


def release_unstarted_claim(db, claim: dict, error: str) -> None:
    db.execute(f"""UPDATE {TABLE_AI_EVAL_OUTPUTS} SET status='pending',active_attempt=NULL,
        lease_token=NULL,lease_expires_at=NULL,error_code='not_started',error_message=:error,
        updated_at=NOW() WHERE campaign_id=:campaign AND case_id=:case AND mode=:mode
        AND status='processing' AND lease_token=:lease AND attempt_count=:previous""", {
        "error": str(error or "")[:500], "campaign": claim["campaign_id"],
        "case": claim["case_id"], "mode": claim["mode"], "lease": claim["lease_token"],
        "previous": claim["active_attempt"] - 1,
    })


def open_review(db, campaign_id: str) -> None:
    with db.transaction() as conn:
        campaign = conn.execute(text(f"SELECT * FROM {TABLE_AI_EVAL_CAMPAIGNS} WHERE campaign_id=:campaign FOR UPDATE"), {"campaign": campaign_id}).mappings().first()
        if not campaign or campaign["status"] != "generating":
            raise ValueError("campaign唔係generating狀態。")
        rows = _dicts(conn.execute(text(f"SELECT * FROM {TABLE_AI_EVAL_OUTPUTS} WHERE campaign_id=:campaign ORDER BY case_id,mode FOR SHARE"), {"campaign": campaign_id}))
        if len(rows) != 90 or any(row["status"] != "succeeded" for row in rows):
            raise ValueError("90個固定答案全部成功後先可以開放盲評。")
        manifest = _json(campaign["model_manifest"])
        for row in rows:
            expected = manifest[row["mode"]]
            if (row["model_tag"] != expected["model"] or row["model_digest"] != expected["digest"]
                    or row["runtime_name"] != expected["runtime"]
                    or row["runtime_version"] != expected["runtime_version"]
                    or row["model_profile_version"] != campaign["model_profile_version"]
                    or row["persona_hash"] != campaign["persona_hash"]
                    or row["prompt_hash"] != campaign["prompt_hash"]):
                raise ValueError("答案身份唔一致，campaign必須invalidate。")
        conn.execute(text(f"UPDATE {TABLE_AI_EVAL_CAMPAIGNS} SET status='reviewing',reviewing_at=NOW() WHERE campaign_id=:campaign"), {"campaign": campaign_id})


def _assignment_payload(conn, review_id: str, reviewer: str) -> dict | None:
    row = conn.execute(text(f"""SELECT r.review_id,r.campaign_id,r.case_id,
        c.task_type,c.title,c.input_json,c.reference_text,
        lo.answer_text left_answer,ro.answer_text right_answer
        FROM {TABLE_AI_EVAL_REVIEWS} r
        JOIN {TABLE_AI_EVAL_CASES} c ON c.case_id=r.case_id
        JOIN {TABLE_AI_EVAL_OUTPUTS} lo ON lo.campaign_id=r.campaign_id AND lo.case_id=r.case_id AND lo.mode=r.left_mode
        JOIN {TABLE_AI_EVAL_OUTPUTS} ro ON ro.campaign_id=r.campaign_id AND ro.case_id=r.case_id AND ro.mode=r.right_mode
        WHERE r.review_id=:review AND r.reviewer_user_id=:reviewer"""), {"review": review_id, "reviewer": reviewer}).mappings().first()
    if not row:
        return None
    value = dict(row)
    value["input"] = _json(value.pop("input_json"))
    return value


def next_assignment(db, campaign_id: str, reviewer: str) -> dict | None:
    pairs_sql = "VALUES ('daily_complex','daily','complex'),('daily_deep','daily','deep'),('complex_deep','complex','deep')"
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:campaign))"), {"campaign": campaign_id})
        campaign = conn.execute(text(f"SELECT status,suite_id,suite_version FROM {TABLE_AI_EVAL_CAMPAIGNS} WHERE campaign_id=:campaign FOR SHARE"), {"campaign": campaign_id}).mappings().first()
        if not campaign or campaign["status"] != "reviewing":
            raise ValueError("campaign未開放盲評。")
        existing = conn.execute(text(f"""SELECT review_id FROM {TABLE_AI_EVAL_REVIEWS}
            WHERE campaign_id=:campaign AND reviewer_user_id=:reviewer AND submitted_at IS NULL
            ORDER BY assigned_at LIMIT 1 FOR UPDATE"""), {"campaign": campaign_id, "reviewer": reviewer}).scalar()
        if existing:
            return _assignment_payload(conn, existing, reviewer)
        candidate = conn.execute(text(f"""WITH pairs(pair_key,a,b) AS ({pairs_sql}), counts AS (
            SELECT c.case_id,p.pair_key,p.a,p.b,
              COUNT(r.review_id) assignments,
              COUNT(r.review_id) FILTER (WHERE r.submitted_at IS NOT NULL) votes
            FROM {TABLE_AI_EVAL_CASES} c CROSS JOIN pairs p
            LEFT JOIN {TABLE_AI_EVAL_REVIEWS} r ON r.campaign_id=:campaign
              AND r.case_id=c.case_id AND r.pair_key=p.pair_key
            WHERE c.is_active=TRUE AND c.suite_id=:suite AND c.suite_version=:suite_version
            GROUP BY c.case_id,p.pair_key,p.a,p.b
        ) SELECT * FROM counts x WHERE assignments<:votes AND NOT EXISTS (
            SELECT 1 FROM {TABLE_AI_EVAL_REVIEWS} mine WHERE mine.campaign_id=:campaign
              AND mine.case_id=x.case_id AND mine.pair_key=x.pair_key
              AND mine.reviewer_user_id=:reviewer
        ) ORDER BY votes,assignments,case_id,pair_key LIMIT 1"""), {
            "campaign": campaign_id, "reviewer": reviewer,
            "votes": LMC_AI_EVAL_REVIEWS_PER_PAIR,
            "suite": campaign["suite_id"], "suite_version": campaign["suite_version"],
        }).mappings().first()
        if not candidate:
            return None
        orientation = conn.execute(text(f"""SELECT
            COUNT(*) FILTER (WHERE left_mode=:a) a_left,
            COUNT(*) FILTER (WHERE left_mode=:b) b_left
            FROM {TABLE_AI_EVAL_REVIEWS} WHERE campaign_id=:campaign AND pair_key=:pair"""), {
            "a": candidate["a"], "b": candidate["b"],
            "campaign": campaign_id, "pair": candidate["pair_key"],
        }).mappings().one()
        if orientation["a_left"] < orientation["b_left"]:
            left, right = candidate["a"], candidate["b"]
        elif orientation["b_left"] < orientation["a_left"]:
            left, right = candidate["b"], candidate["a"]
        elif secrets.randbelow(2):
            left, right = candidate["a"], candidate["b"]
        else:
            left, right = candidate["b"], candidate["a"]
        review_id = secrets.token_hex(16)
        conn.execute(text(f"""INSERT INTO {TABLE_AI_EVAL_REVIEWS}(
            review_id,campaign_id,case_id,pair_key,reviewer_user_id,left_mode,right_mode
        ) VALUES(:review,:campaign,:case,:pair,:reviewer,:left,:right)"""), {
            "review": review_id, "campaign": campaign_id, "case": candidate["case_id"],
            "pair": candidate["pair_key"], "reviewer": reviewer, "left": left, "right": right,
        })
        return _assignment_payload(conn, review_id, reviewer)


def preview_assignment(db, campaign_id: str) -> dict | None:
    """Return one identity-free comparison without reserving or counting a vote."""
    rows = db.query(f"""SELECT c.case_id,c.task_type,c.title,c.input_json,c.reference_text,
        a.answer_text left_answer,b.answer_text right_answer
        FROM {TABLE_AI_EVAL_CAMPAIGNS} campaign
        JOIN {TABLE_AI_EVAL_CASES} c ON c.suite_id=campaign.suite_id
          AND c.suite_version=campaign.suite_version AND c.is_active=TRUE
        JOIN {TABLE_AI_EVAL_OUTPUTS} a ON a.campaign_id=campaign.campaign_id
          AND a.case_id=c.case_id AND a.mode='daily'
        JOIN {TABLE_AI_EVAL_OUTPUTS} b ON b.campaign_id=campaign.campaign_id
          AND b.case_id=c.case_id AND b.mode='complex'
        WHERE campaign.campaign_id=:campaign AND campaign.status='reviewing'
        ORDER BY c.case_id LIMIT 1""", {"campaign": campaign_id})
    if rows.empty:
        return None
    value = dict(rows.iloc[0])
    value["input"] = _json(value.pop("input_json"))
    value["preview"] = True
    return value


def submit_review(db, review_id: str, reviewer: str, choices: dict, note: str) -> bool:
    with db.transaction() as conn:
        row = conn.execute(text(f"SELECT * FROM {TABLE_AI_EVAL_REVIEWS} WHERE review_id=:review AND reviewer_user_id=:reviewer FOR UPDATE"), {"review": review_id, "reviewer": reviewer}).mappings().first()
        if not row:
            raise LookupError("找不到盲評assignment。")
        if row["submitted_at"] is not None:
            same = row["note"] == note and all(row[key] == choices[key] for key in REVIEW_DIMENSIONS)
            if not same:
                raise ValueError("盲評提交後不可修改。")
            return False
        status = conn.execute(text(f"SELECT status FROM {TABLE_AI_EVAL_CAMPAIGNS} WHERE campaign_id=:campaign FOR SHARE"), {"campaign": row["campaign_id"]}).scalar()
        if status != "reviewing":
            raise ValueError("campaign已經停止收票。")
        params = {**choices, "note": note, "review": review_id, "reviewer": reviewer}
        conn.execute(text(f"""UPDATE {TABLE_AI_EVAL_REVIEWS} SET overall=:overall,
            cantonese=:cantonese,reasoning=:reasoning,usefulness=:usefulness,
            factual=:factual,privacy=:privacy,note=:note,submitted_at=NOW()
            WHERE review_id=:review AND reviewer_user_id=:reviewer AND submitted_at IS NULL"""), params)
        return True


def close_campaign(db, campaign_id: str) -> dict:
    with db.transaction() as conn:
        campaign = conn.execute(text(f"SELECT * FROM {TABLE_AI_EVAL_CAMPAIGNS} WHERE campaign_id=:campaign FOR UPDATE"), {"campaign": campaign_id}).mappings().first()
        if not campaign or campaign["status"] != "reviewing":
            raise ValueError("campaign唔係reviewing狀態。")
        incomplete = conn.execute(text(f"""WITH pairs(pair_key) AS (VALUES ('daily_complex'),('daily_deep'),('complex_deep'))
            SELECT COUNT(*) FROM {TABLE_AI_EVAL_CASES} c CROSS JOIN pairs p
            WHERE c.is_active=TRUE AND c.suite_id=:suite AND c.suite_version=:suite_version
              AND (SELECT COUNT(*) FROM {TABLE_AI_EVAL_REVIEWS} r
              WHERE r.campaign_id=:campaign AND r.case_id=c.case_id AND r.pair_key=p.pair_key
                AND r.submitted_at IS NOT NULL)<>:votes"""), {
            "campaign": campaign_id, "votes": LMC_AI_EVAL_REVIEWS_PER_PAIR,
            "suite": campaign["suite_id"], "suite_version": campaign["suite_version"],
        }).scalar_one()
        if int(incomplete):
            raise ValueError("所有90組比較各有3票後先可以close。")
        reviews = _dicts(conn.execute(text(f"SELECT case_id,left_mode,right_mode,{','.join(REVIEW_DIMENSIONS)} FROM {TABLE_AI_EVAL_REVIEWS} WHERE campaign_id=:campaign AND submitted_at IS NOT NULL ORDER BY case_id,pair_key,review_id"), {"campaign": campaign_id}))
        outputs = _dicts(conn.execute(text(f"SELECT status,attempt_count,input_tokens,output_tokens,duration_ms FROM {TABLE_AI_EVAL_OUTPUTS} WHERE campaign_id=:campaign"), {"campaign": campaign_id}))
        case_rows = _dicts(conn.execute(text(f"SELECT case_id,task_type FROM {TABLE_AI_EVAL_CASES}")))
        summary = aggregate_campaign(reviews, outputs, {row["case_id"]: row for row in case_rows})
        summary["provenance"] = {
            "campaign_id": campaign_id, "suite_id": campaign["suite_id"],
            "suite_version": campaign["suite_version"], "suite_hash": campaign["suite_hash"],
            "prompt_version": campaign["prompt_version"], "prompt_hash": campaign["prompt_hash"],
            "persona_hash": campaign["persona_hash"], "model_profile_version": campaign["model_profile_version"],
            "models": _json(campaign["model_manifest"]), "node_id": campaign["bound_node_id"],
        }
        summary_json = _canonical(summary)
        summary_hash = hashlib.sha256(summary_json.encode("utf-8")).hexdigest()
        conn.execute(text(f"""UPDATE {TABLE_AI_EVAL_CAMPAIGNS} SET status='closed',
            closed_at=NOW(),summary_json=CAST(:summary AS JSONB),summary_hash=:hash
            WHERE campaign_id=:campaign"""), {"summary": summary_json, "hash": summary_hash, "campaign": campaign_id})
        return {"summary": summary, "summary_hash": summary_hash}


def invalidate_campaign(db, campaign_id: str, actor_id: str, reason: str) -> None:
    count = db.execute_count(f"""UPDATE {TABLE_AI_EVAL_CAMPAIGNS} SET status='invalidated',
        invalidation_reason=:reason,invalidated_by=:actor,invalidated_at=NOW()
        WHERE campaign_id=:campaign AND status IN ('generating','reviewing','closed')""", {
        "reason": reason, "actor": actor_id, "campaign": campaign_id,
    })
    if count != 1:
        raise ValueError("campaign已經invalidated或不存在。")


def manager_export(db, campaign_id: str) -> dict:
    campaign_rows = db.query(f"SELECT * FROM {TABLE_AI_EVAL_CAMPAIGNS} WHERE campaign_id=:campaign", {"campaign": campaign_id})
    if campaign_rows.empty:
        raise LookupError("找不到campaign。")
    campaign = dict(campaign_rows.iloc[0])
    if campaign["status"] not in {"closed", "invalidated"}:
        raise ValueError("campaign完成或作廢後先可以下載audit export。")
    for key in ("model_manifest", "summary_json"):
        campaign[key] = _json(campaign.get(key))
    output_rows = db.query(f"""SELECT case_id,mode,status,attempt_count,model_tag,model_digest,
        model_profile_version,runtime_name,runtime_version,backend_fingerprint,persona_hash,
        prompt_hash,thinking_enabled,answer_text,answer_hash,input_tokens,output_tokens,duration_ms,
        error_code,error_message,started_at,completed_at
        FROM {TABLE_AI_EVAL_OUTPUTS} WHERE campaign_id=:campaign ORDER BY case_id,mode""", {"campaign": campaign_id})
    review_rows = db.query(f"""SELECT case_id,pair_key,left_mode,right_mode,{','.join(REVIEW_DIMENSIONS)},note,assigned_at,submitted_at
        FROM {TABLE_AI_EVAL_REVIEWS} WHERE campaign_id=:campaign ORDER BY case_id,pair_key,review_id""", {"campaign": campaign_id})
    cases = db.query(f"""SELECT case_id,task_type,title,input_json,rubric_json,reference_text,content_hash
        FROM {TABLE_AI_EVAL_CASES} WHERE suite_id=:suite AND suite_version=:version ORDER BY case_id""", {"suite": campaign["suite_id"], "version": campaign["suite_version"]})
    return {
        "campaign": campaign,
        "cases": [dict(row) for _, row in cases.iterrows()],
        "outputs": [dict(row) for _, row in output_rows.iterrows()],
        # Deliberately excludes review_id and reviewer_user_id.
        "reviews": [dict(row) for _, row in review_rows.iterrows()],
    }
