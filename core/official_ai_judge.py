"""Durable workflow for an AI score sheet used as the official third judge."""

from __future__ import annotations

import datetime as dt
import json
import math
import secrets
import uuid
from collections.abc import Mapping

from sqlalchemy import text

from account_access import KIOSK_ACCOUNT_ID
from ai_model_config import (
    OFFICIAL_AI_JUDGE_DEFAULT_MODEL,
    OFFICIAL_AI_JUDGE_MODEL_LABELS,
    get_official_ai_judge_model,
)
from core.judging_logic import _serialize, normalise_side_data
from core.vote_logic import _resolve_db
from scoring import (
    FREE_DEBATE_CRITERIA,
    SPEECH_CRITERIA,
    derive_debater_ranks,
    free_debate_col,
    is_valid_competition_ranking,
    speech_col,
)
from schema import (
    TABLE_BEST_DEBATER_RANKINGS,
    TABLE_DEBATERS,
    TABLE_DEBATER_SCORES,
    TABLE_MATCHES,
    TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS,
    TABLE_OFFICIAL_AI_JUDGE_RUNS,
    TABLE_SCORE_DRAFTS,
    TABLE_SCORES,
)
from system_limits import JUDGE_MAX_PER_MATCH, OFFICIAL_AI_JUDGE_CLAIM_TTL_SECONDS


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _clean_db_text(value) -> str:
    if value is None:
        return ""
    text_value = str(value).strip()
    return "" if text_value.lower() in {"nan", "nat", "none", "<na>"} else text_value


def official_judge_name(model_label: str) -> str:
    if model_label not in OFFICIAL_AI_JUDGE_MODEL_LABELS:
        raise ValueError("正式 AI 評判模型不正確。")
    return f"AI 評判（{model_label}）"


def eligible_human_judge_count(value) -> bool:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return False
    # Leave one slot for the official AI sheet within the global match limit.
    return 2 <= count < JUDGE_MAX_PER_MATCH and count % 2 == 0


def require_all_expected_human_judges(expected, submitted) -> int:
    """Require every planned human judge before one official AI sheet."""
    try:
        expected_count = int(expected)
        submitted_count = int(submitted)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("請先在場次管理設定原定真人評判數目。") from exc
    if not 1 <= expected_count < JUDGE_MAX_PER_MATCH:
        raise ValueError("原定真人評判數目不正確。")
    if not eligible_human_judge_count(expected_count):
        raise ValueError("只有原定真人評判數目為雙數時才加入正式 AI 評判。")
    if submitted_count != expected_count:
        raise ValueError(
            f"原定真人評判共 {expected_count} 位，目前只有 {submitted_count} 份真人分紙。"
        )
    return expected_count


def attempt_number_for_run(run, model_label: str, projector_session_id: str) -> int:
    """Validate the model-switch policy after any stale claim is settled."""
    get_official_ai_judge_model(model_label)
    if run is None:
        return 1
    status = str(run.get("status") or "")
    if status in {"succeeded", "fallback"}:
        raise ValueError("本場正式 AI 第三評判流程已完結。")
    attempt_count = int(run.get("attempt_count") or 0)
    if status == "ready" and attempt_count == 0:
        if str(run.get("projector_session_id") or "") != str(projector_session_id or ""):
            raise ValueError("重新提交必須沿用原本的完整比賽逐字稿。")
        return 1
    if status != "retryable" or attempt_count != 1:
        raise ValueError("本場正式 AI 第三評判狀態不容許重試。")
    if model_label == str(run.get("current_model_label") or ""):
        raise ValueError("重試必須轉用另一個 AI 模型。")
    if str(run.get("projector_session_id") or "") != str(projector_session_id or ""):
        raise ValueError("重試必須沿用第一次的完整比賽逐字稿。")
    return 2


def combined_human_deduction(*deductions) -> int:
    """Return the upward-rounded mean of all human-judge deductions."""
    if len(deductions) < 2 or len(deductions) % 2:
        raise ValueError("真人評判扣分必須來自雙數評判。")
    try:
        values = [int(value) for value in deductions]
    except (TypeError, ValueError) as exc:
        raise ValueError("真人評判扣分資料不完整。") from exc
    if any(value < 0 for value in values):
        raise ValueError("真人評判扣分資料不正確。")
    return values[0] if len(set(values)) == 1 else math.ceil(sum(values) / len(values))


def _settle_usage(conn, actor: str, claim: dict, success: bool, usage, error="") -> None:
    if usage is None:
        return
    from core.funds_logic import log_ai_usage_in_transaction

    recorded = dict(usage)
    recorded.update({
        "model_label": claim["model_label"],
        "operation_id": claim["operation_id"],
        "operation_stage": f"score_attempt_{claim['attempt_no']}",
    })
    log_ai_usage_in_transaction(
        conn,
        actor,
        "official_ai_judge",
        success,
        recorded,
        error_message=str(error or "")[:300],
    )


def load_match_context(match_id: str, db=None) -> dict:
    db = _resolve_db(db)
    matches = db.query(
        f"""SELECT match_id,topic_text,pro_team,con_team
            FROM {TABLE_MATCHES} WHERE match_id=:match_id LIMIT 1""",
        {"match_id": str(match_id or "")},
    )
    if matches.empty:
        raise ValueError("找不到指定比賽場次。")
    row = matches.iloc[0]
    roster_rows = db.query(
        f"""SELECT side,position,debater_name FROM {TABLE_DEBATERS}
            WHERE match_id=:match_id ORDER BY side,position""",
        {"match_id": str(match_id)},
    )
    roster = []
    names = {}
    for _, debater in roster_rows.iterrows():
        side = str(debater.get("side") or "").strip()
        try:
            position = int(debater.get("position"))
        except (TypeError, ValueError):
            continue
        if side not in {"pro", "con"} or not 1 <= position <= 4:
            continue
        name = str(debater.get("debater_name") or "").strip()
        names[(side, position)] = name
        roster.append({"side": side, "position": position, "debater_name": name})
    expected = {(side, position) for side in ("pro", "con") for position in range(1, 5)}
    if set(names) != expected:
        raise ValueError("正式辯員名單未完整包含正反方各四個辯位。")
    return {
        "match_id": str(row.get("match_id") or ""),
        "topic": str(row.get("topic_text") or ""),
        "pro_team": str(row.get("pro_team") or "正方"),
        "con_team": str(row.get("con_team") or "反方"),
        "roster": roster,
        "names": names,
    }


def _integer(value, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必須是整數。")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必須是整數。") from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError(f"{field} 必須是整數。")
    integer = int(numeric)
    if not minimum <= integer <= maximum:
        raise ValueError(f"{field} 必須介乎 {minimum} 至 {maximum}。")
    return integer


def _parse_side(side_key: str, payload, context: dict, deduction: int) -> dict:
    if not isinstance(payload, Mapping) or set(payload) != {
        "speeches", "free_debate", "coherence"
    }:
        raise ValueError(f"{side_key} 評分格式不完整。")
    speeches = payload.get("speeches")
    if not isinstance(speeches, list) or len(speeches) != 4:
        raise ValueError(f"{side_key} 必須包含四位辯員評分。")
    by_position = {}
    expected_speech_keys = {criterion["key"] for criterion in SPEECH_CRITERIA}
    for item in speeches:
        if not isinstance(item, Mapping) or set(item) != {"position", "scores"}:
            raise ValueError(f"{side_key} 辯員評分格式不正確。")
        position = _integer(item.get("position"), "辯位", 1, 4)
        scores = item.get("scores")
        if not isinstance(scores, Mapping) or set(scores) != expected_speech_keys:
            raise ValueError(f"{side_key} 台上發言評分欄位不完整。")
        if position in by_position:
            raise ValueError(f"{side_key} 辯位重複。")
        row = {
            "姓名": context["names"][(side_key, position)],
        }
        for criterion in SPEECH_CRITERIA:
            row[speech_col(criterion)] = _integer(
                scores.get(criterion["key"]),
                f"{side_key}第{position}位{criterion['key']}",
                0,
                criterion["max"],
            )
        by_position[position] = row
    if set(by_position) != {1, 2, 3, 4}:
        raise ValueError(f"{side_key} 辯位資料不完整。")

    free = payload.get("free_debate")
    expected_free_keys = {criterion["key"] for criterion in FREE_DEBATE_CRITERIA}
    if not isinstance(free, Mapping) or set(free) != expected_free_keys:
        raise ValueError(f"{side_key} 自由辯論評分欄位不完整。")
    free_row = {
        free_debate_col(criterion): _integer(
            free.get(criterion["key"]),
            f"{side_key}自由辯論{criterion['key']}",
            0,
            criterion["max"],
        )
        for criterion in FREE_DEBATE_CRITERIA
    }
    side_label = "正方" if side_key == "pro" else "反方"
    team_key = "pro_team" if side_key == "pro" else "con_team"
    return normalise_side_data(
        side_label,
        {
            "team_name": context[team_key],
            "raw_df_a": [by_position[position] for position in range(1, 5)],
            "raw_df_b": [free_row],
            "coherence": _integer(payload.get("coherence"), "內容連貫", 0, 5),
            "deduction": deduction,
        },
    )


def parse_ai_score_json(raw_result: str, context: dict, deductions: dict) -> dict:
    """Strictly validate provider JSON and convert it to normal score payloads."""
    try:
        payload = json.loads(str(raw_result or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("AI 未有回傳完整 JSON 分紙。") from exc
    if not isinstance(payload, Mapping) or set(payload) != {
        "pro", "con", "rankings", "decision_reason"
    }:
        raise ValueError("AI 分紙頂層格式不完整。")
    pro = _parse_side("pro", payload["pro"], context, int(deductions["pro"]))
    con = _parse_side("con", payload["con"], context, int(deductions["con"]))

    rankings = payload.get("rankings")
    if not isinstance(rankings, list) or len(rankings) != 8:
        raise ValueError("AI 最佳辯論員排名必須包含八個辯位。")
    clean_rankings = []
    for item in rankings:
        if not isinstance(item, Mapping) or set(item) != {"side", "position", "rank"}:
            raise ValueError("AI 最佳辯論員排名格式不正確。")
        side = str(item.get("side") or "").strip()
        if side not in {"pro", "con"}:
            raise ValueError("AI 最佳辯論員排名方別不正確。")
        clean_rankings.append({
            "side": side,
            "position": _integer(item.get("position"), "辯位", 1, 4),
            "rank": _integer(item.get("rank"), "名次", 1, 8),
        })
    slots = {(item["side"], item["position"]) for item in clean_rankings}
    expected = {(side, position) for side in ("pro", "con") for position in range(1, 5)}
    if slots != expected or not is_valid_competition_ranking(
        [item["rank"] for item in clean_rankings]
    ):
        raise ValueError("AI 排名必須完整並使用標準競賽排名（例如 1、1、3）。")
    derived_rankings = derive_debater_ranks(pro["ind_scores"], con["ind_scores"])
    if any(
        item["rank"] != derived_rankings[(item["side"], item["position"])]
        for item in clean_rankings
    ):
        raise ValueError("AI 排名必須與八個辯位的個人分一致。")
    reason = str(payload.get("decision_reason") or "").strip()
    if not reason or len(reason) > 2000:
        raise ValueError("AI 判決理由必須為 1 至 2000 字。")
    return {"pro": pro, "con": con, "rankings": clean_rankings, "decision_reason": reason}


def state_data(match_id: str, db=None) -> dict:
    db = _resolve_db(db)
    expected_rows = db.query(
        f"""SELECT expected_human_judge_count FROM {TABLE_MATCHES}
            WHERE match_id=:match_id LIMIT 1""",
        {"match_id": str(match_id or "")},
    )
    expected_count = None
    if not expected_rows.empty:
        raw_expected = expected_rows.iloc[0].get("expected_human_judge_count")
        try:
            expected_count = int(raw_expected) if _clean_db_text(raw_expected) else None
        except (TypeError, ValueError, OverflowError):
            expected_count = None
    scores = db.query(
        f"""SELECT judge_name,judge_kind FROM {TABLE_SCORES}
            WHERE match_id=:match_id ORDER BY judge_name""",
        {"match_id": str(match_id or "")},
    )
    humans = []
    ai_names = []
    if not scores.empty:
        for _, row in scores.iterrows():
            name = str(row.get("judge_name") or "")
            if str(row.get("judge_kind") or "human") == "ai":
                ai_names.append(name)
            else:
                humans.append(name)
    runs = db.query(
        f"""SELECT match_id,projector_session_id,operation_id,status,attempt_count,
                   current_model_label,final_model_label,final_judge_name,last_error,
                   claim_expires_at,completed_at
            FROM {TABLE_OFFICIAL_AI_JUDGE_RUNS}
            WHERE match_id=:match_id LIMIT 1""",
        {"match_id": str(match_id or "")},
    )
    run = None
    if not runs.empty:
        row = runs.iloc[0]
        run = {
            "match_id": _clean_db_text(row.get("match_id")),
            "projector_session_id": _clean_db_text(row.get("projector_session_id")),
            "operation_id": _clean_db_text(row.get("operation_id")),
            "status": _clean_db_text(row.get("status")),
            "attempt_count": int(row.get("attempt_count") or 0),
            "current_model_label": _clean_db_text(row.get("current_model_label")),
            "final_model_label": _clean_db_text(row.get("final_model_label")),
            "final_judge_name": _clean_db_text(row.get("final_judge_name")),
            "last_error": _clean_db_text(row.get("last_error")),
            "claim_expires_at": _clean_db_text(row.get("claim_expires_at")),
            "completed_at": _clean_db_text(row.get("completed_at")),
        }
    if ai_names:
        status = "succeeded"
    elif run:
        status = str(run.get("status") or "")
    elif expected_count is None:
        status = "missing_expected_count"
    elif len(humans) > expected_count:
        status = "judge_count_mismatch"
    elif not eligible_human_judge_count(expected_count):
        status = "not_applicable"
    elif len(humans) < expected_count:
        status = "waiting_humans"
    else:
        status = "ready"
    return {
        "status": status,
        "human_judge_count": len(humans),
        "human_judge_names": humans,
        "expected_human_judge_count": expected_count,
        "all_expected_human_judges_submitted": (
            expected_count is not None and len(humans) == expected_count
        ),
        "ai_judge_count": len(ai_names),
        "ai_judge_names": ai_names,
        "run": run,
        "default_model_label": OFFICIAL_AI_JUDGE_DEFAULT_MODEL,
        "model_options": list(OFFICIAL_AI_JUDGE_MODEL_LABELS),
        "can_start": status == "ready",
        "can_retry": status == "retryable",
        "uses_human_result": status == "fallback",
    }


def reserve_attempt(
    match_id: str,
    projector_session_id: str,
    model_label: str,
    created_by: str,
    db=None,
) -> dict:
    db = _resolve_db(db)
    model_label, model_config = get_official_ai_judge_model(model_label)
    now = _now()
    claim_token = secrets.token_urlsafe(32)
    with db.transaction() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"judge_submit:{match_id}"},
        )
        score_rows = conn.execute(
            text(f"""SELECT judge_kind,pro_deduction_points,con_deduction_points
                FROM {TABLE_SCORES} WHERE match_id=:match_id
                ORDER BY judge_name FOR UPDATE"""),
            {"match_id": match_id},
        ).fetchall()
        humans = [row._mapping for row in score_rows if row._mapping["judge_kind"] == "human"]
        expected_row = conn.execute(
            text(f"""SELECT expected_human_judge_count FROM {TABLE_MATCHES}
                WHERE match_id=:match_id FOR UPDATE"""),
            {"match_id": match_id},
        ).fetchone()
        if expected_row is None:
            raise ValueError("找不到指定比賽場次。")
        expected_count = require_all_expected_human_judges(
            expected_row._mapping.get("expected_human_judge_count"), len(humans)
        )
        if len(score_rows) != len(humans):
            raise ValueError("正式 AI 評判只可在雙數真人評判全部提交後使用。")

        row = conn.execute(
            text(f"""SELECT * FROM {TABLE_OFFICIAL_AI_JUDGE_RUNS}
                WHERE match_id=:match_id FOR UPDATE"""),
            {"match_id": match_id},
        ).fetchone()
        if row is not None:
            current = row._mapping
            status = str(current.get("status") or "")
            expires = current.get("claim_expires_at")
            if status == "processing" and expires is not None and expires > now:
                raise ValueError("AI 第三評判正在處理，請勿重複提交。")
            if status == "processing":
                stale_attempt = int(current.get("attempt_count") or 0)
                stale_row = conn.execute(
                    text(f"""SELECT provider_attempted,model_label,provider
                        FROM {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}
                        WHERE match_id=:match_id AND attempt_no=:attempt_no FOR UPDATE"""),
                    {"match_id": match_id, "attempt_no": stale_attempt},
                ).fetchone()
                provider_started = bool(
                    stale_row is not None and stale_row._mapping.get("provider_attempted")
                )
                if not provider_started:
                    conn.execute(
                        text(f"""DELETE FROM {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}
                            WHERE match_id=:match_id AND attempt_no=:attempt_no"""),
                        {"match_id": match_id, "attempt_no": stale_attempt},
                    )
                    remaining = max(0, stale_attempt - 1)
                    previous = conn.execute(
                        text(f"""SELECT model_label FROM {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}
                            WHERE match_id=:match_id AND attempt_no=:attempt_no"""),
                        {"match_id": match_id, "attempt_no": remaining},
                    ).fetchone() if remaining else None
                    previous_model = (
                        str(previous._mapping.get("model_label") or "")
                        if previous is not None else ""
                    )
                    status = "ready" if remaining == 0 else "retryable"
                    conn.execute(
                        text(f"""UPDATE {TABLE_OFFICIAL_AI_JUDGE_RUNS}
                            SET status=:status,attempt_count=:attempt_count,
                                current_model_label=:previous_model,last_error='',
                                current_claim_token=NULL,claim_expires_at=NULL,
                                updated_at=:now,completed_at=NULL
                            WHERE match_id=:match_id"""),
                        {
                            "match_id": match_id,
                            "status": status,
                            "attempt_count": remaining,
                            "previous_model": previous_model or None,
                            "now": now,
                        },
                    )
                    current = dict(current)
                    current["attempt_count"] = remaining
                    current["current_model_label"] = previous_model
                else:
                    next_status = "retryable" if stale_attempt == 1 else "fallback"
                    conn.execute(
                        text(f"""UPDATE {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}
                            SET status='failed',error_message='處理逾時',completed_at=:now
                            WHERE match_id=:match_id AND attempt_no=:attempt_no
                              AND status IN ('claimed','running')"""),
                        {"match_id": match_id, "attempt_no": stale_attempt, "now": now},
                    )
                    conn.execute(
                        text(f"""UPDATE {TABLE_OFFICIAL_AI_JUDGE_RUNS}
                            SET status=:status,last_error='處理逾時',current_claim_token=NULL,
                                claim_expires_at=NULL,updated_at=:now,
                                completed_at=CASE WHEN :status='fallback' THEN :now ELSE NULL END
                            WHERE match_id=:match_id"""),
                        {"match_id": match_id, "status": next_status, "now": now},
                    )
                    _settle_usage(
                        conn,
                        KIOSK_ACCOUNT_ID,
                        {
                            "model_label": str(stale_row._mapping.get("model_label") or ""),
                            "operation_id": str(current.get("operation_id") or ""),
                            "attempt_no": stale_attempt,
                        },
                        False,
                        {
                            "provider": str(stale_row._mapping.get("provider") or "other"),
                            "cost_source": "provider_attempt_usage_unavailable",
                        },
                        "處理逾時",
                    )
                    status = next_status
            normalized_run = dict(current)
            normalized_run["status"] = status
            attempt_no = attempt_number_for_run(
                normalized_run, model_label, projector_session_id
            )
            operation_id = str(current.get("operation_id") or "")
            conn.execute(
                text(f"""UPDATE {TABLE_OFFICIAL_AI_JUDGE_RUNS}
                    SET status='processing',attempt_count=:attempt_no,current_model_label=:model,
                        current_claim_token=:claim,claim_expires_at=:expires,
                        last_error='',updated_at=:now,completed_at=NULL
                    WHERE match_id=:match_id"""),
                {
                    "match_id": match_id,
                    "attempt_no": attempt_no,
                    "model": model_label,
                    "claim": claim_token,
                    "expires": now + dt.timedelta(seconds=OFFICIAL_AI_JUDGE_CLAIM_TTL_SECONDS),
                    "now": now,
                },
            )
        else:
            attempt_number_for_run(None, model_label, projector_session_id)
            operation_id = f"official-ai-judge:{uuid.uuid4().hex}"
            attempt_no = 1
            conn.execute(
                text(f"""INSERT INTO {TABLE_OFFICIAL_AI_JUDGE_RUNS}
                    (match_id,projector_session_id,operation_id,status,attempt_count,
                     current_model_label,current_claim_token,claim_expires_at,created_by,
                     created_at,updated_at)
                    VALUES(:match_id,:session,:operation,'processing',1,:model,:claim,
                           :expires,:created_by,:now,:now)"""),
                {
                    "match_id": match_id,
                    "session": projector_session_id,
                    "operation": operation_id,
                    "model": model_label,
                    "claim": claim_token,
                    "expires": now + dt.timedelta(seconds=OFFICIAL_AI_JUDGE_CLAIM_TTL_SECONDS),
                    "created_by": str(created_by or "")[:200],
                    "now": now,
                },
            )
        deductions = {
            "pro": combined_human_deduction(
                *(row["pro_deduction_points"] for row in humans)
            ),
            "con": combined_human_deduction(
                *(row["con_deduction_points"] for row in humans)
            ),
        }
        provider = str(model_config.get("provider") or "other")
        conn.execute(
            text(f"""INSERT INTO {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}
                (match_id,attempt_no,model_label,provider,human_judge_count,
                 pro_deduction,con_deduction,status,created_at)
                VALUES(:match_id,:attempt_no,:model,:provider,:human_count,
                       :pro_deduction,:con_deduction,'claimed',:now)"""),
            {
                "match_id": match_id,
                "attempt_no": attempt_no,
                "model": model_label,
                "provider": provider,
                "human_count": expected_count,
                "pro_deduction": deductions["pro"],
                "con_deduction": deductions["con"],
                "now": now,
            },
        )
    return {
        "match_id": match_id,
        "projector_session_id": projector_session_id,
        "operation_id": operation_id,
        "attempt_no": attempt_no,
        "model_label": model_label,
        "claim_token": claim_token,
        "human_judge_count": expected_count,
        "deductions": deductions,
    }


def mark_provider_attempted(claim: dict, db=None) -> None:
    db = _resolve_db(db)
    now = _now()
    with db.transaction() as conn:
        updated = conn.execute(
            text(f"""UPDATE {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS} AS attempt
                SET status='running',provider_attempted=TRUE,provider_attempted_at=:now
                FROM {TABLE_OFFICIAL_AI_JUDGE_RUNS} AS run
                WHERE attempt.match_id=:match_id AND attempt.attempt_no=:attempt_no
                  AND attempt.status='claimed' AND run.match_id=attempt.match_id
                  AND run.status='processing' AND run.current_claim_token=:claim"""),
            {
                "match_id": claim["match_id"],
                "attempt_no": claim["attempt_no"],
                "claim": claim["claim_token"],
                "now": now,
            },
        ).rowcount
        if updated != 1:
            raise ValueError("AI 第三評判處理權已失效。")


def fail_attempt(
    claim: dict,
    error: str,
    db=None,
    *,
    usage_actor: str = "",
    usage=None,
) -> str:
    db = _resolve_db(db)
    now = _now()
    next_status = "retryable" if int(claim["attempt_no"]) == 1 else "fallback"
    with db.transaction() as conn:
        row = conn.execute(
            text(f"""SELECT status,current_claim_token FROM {TABLE_OFFICIAL_AI_JUDGE_RUNS}
                WHERE match_id=:match_id FOR UPDATE"""),
            {"match_id": claim["match_id"]},
        ).fetchone()
        if row is None or str(row._mapping.get("current_claim_token") or "") != claim["claim_token"]:
            return str(row._mapping.get("status") or "") if row is not None else ""
        message = str(error or "AI 評分失敗")[:1000]
        attempt = conn.execute(
            text(f"""SELECT provider_attempted FROM {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}
                WHERE match_id=:match_id AND attempt_no=:attempt_no FOR UPDATE"""),
            {"match_id": claim["match_id"], "attempt_no": claim["attempt_no"]},
        ).fetchone()
        provider_started = bool(
            attempt is not None and attempt._mapping.get("provider_attempted")
        )
        if not provider_started:
            conn.execute(
                text(f"""DELETE FROM {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}
                    WHERE match_id=:match_id AND attempt_no=:attempt_no"""),
                {"match_id": claim["match_id"], "attempt_no": claim["attempt_no"]},
            )
            remaining = max(0, int(claim["attempt_no"]) - 1)
            previous = conn.execute(
                text(f"""SELECT model_label FROM {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}
                    WHERE match_id=:match_id AND attempt_no=:attempt_no"""),
                {"match_id": claim["match_id"], "attempt_no": remaining},
            ).fetchone() if remaining else None
            previous_model = (
                str(previous._mapping.get("model_label") or "")
                if previous is not None else ""
            )
            reset_status = "ready" if remaining == 0 else "retryable"
            conn.execute(
                text(f"""UPDATE {TABLE_OFFICIAL_AI_JUDGE_RUNS}
                    SET status=:status,attempt_count=:attempt_count,
                        current_model_label=:previous_model,last_error=:error,
                        current_claim_token=NULL,claim_expires_at=NULL,
                        updated_at=:now,completed_at=NULL
                    WHERE match_id=:match_id"""),
                {
                    "match_id": claim["match_id"],
                    "status": reset_status,
                    "attempt_count": remaining,
                    "previous_model": previous_model or None,
                    "error": message,
                    "now": now,
                },
            )
            return reset_status
        changed = conn.execute(
            text(f"""UPDATE {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}
                SET status='failed',error_message=:error,completed_at=:now
                WHERE match_id=:match_id AND attempt_no=:attempt_no
                  AND status IN ('claimed','running')"""),
            {
                "match_id": claim["match_id"],
                "attempt_no": claim["attempt_no"],
                "error": message,
                "now": now,
            },
        ).rowcount
        if changed != 1:
            raise ValueError("AI 第三評判 attempt 狀態已改變。")
        conn.execute(
            text(f"""UPDATE {TABLE_OFFICIAL_AI_JUDGE_RUNS}
                SET status=:status,last_error=:error,current_claim_token=NULL,
                    claim_expires_at=NULL,updated_at=:now,
                    completed_at=CASE WHEN :status='fallback' THEN :now ELSE NULL END
                WHERE match_id=:match_id"""),
            {
                "match_id": claim["match_id"],
                "status": next_status,
                "error": message,
                "now": now,
            },
        )
        _settle_usage(conn, usage_actor, claim, False, usage, message)
    return next_status


def finalize_success(
    claim: dict,
    result: dict,
    db=None,
    *,
    usage_actor: str = "",
    usage=None,
) -> str:
    db = _resolve_db(db)
    now = _now()
    judge_name = official_judge_name(claim["model_label"])
    pro, con = result["pro"], result["con"]
    with db.transaction() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"judge_submit:{claim['match_id']}"},
        )
        run = conn.execute(
            text(f"""SELECT status,current_claim_token,attempt_count
                FROM {TABLE_OFFICIAL_AI_JUDGE_RUNS}
                WHERE match_id=:match_id FOR UPDATE"""),
            {"match_id": claim["match_id"]},
        ).fetchone()
        if (
            run is None
            or str(run._mapping.get("status") or "") != "processing"
            or str(run._mapping.get("current_claim_token") or "") != claim["claim_token"]
            or int(run._mapping.get("attempt_count") or 0) != int(claim["attempt_no"])
        ):
            raise ValueError("AI 第三評判處理權已失效。")
        score_state = conn.execute(
            text(f"""SELECT
                COUNT(*) FILTER (WHERE judge_kind='human') AS humans,
                COUNT(*) FILTER (WHERE judge_kind='ai') AS ais
                FROM {TABLE_SCORES} WHERE match_id=:match_id"""),
            {"match_id": claim["match_id"]},
        ).fetchone()._mapping
        expected_row = conn.execute(
            text(f"""SELECT expected_human_judge_count FROM {TABLE_MATCHES}
                WHERE match_id=:match_id FOR UPDATE"""),
            {"match_id": claim["match_id"]},
        ).fetchone()
        expected_count = (
            expected_row._mapping.get("expected_human_judge_count")
            if expected_row is not None else None
        )
        human_count = int(score_state["humans"] or 0)
        if (
            human_count != int(claim["human_judge_count"])
            or expected_count is None
            or int(expected_count) != human_count
            or not eligible_human_judge_count(human_count)
            or int(score_state["ais"] or 0) != 0
        ):
            raise ValueError("真人評判數目已改變，AI 分紙未有寫入。")
        conn.execute(
            text(f"""INSERT INTO {TABLE_SCORE_DRAFTS}
                (match_id,judge_name,side,score_payload,is_final,updated_at)
                VALUES(:match_id,:judge_name,:side,CAST(:payload AS JSONB),TRUE,:updated_at)"""),
            [
                {
                    "match_id": claim["match_id"],
                    "judge_name": judge_name,
                    "side": side,
                    "payload": _serialize(data),
                    "updated_at": now,
                }
                for side, data in (("正方", pro), ("反方", con))
            ],
        )
        conn.execute(
            text(f"""INSERT INTO {TABLE_SCORES}
                (match_id,judge_name,pro_total_score,con_total_score,submitted_time,
                 pro_free_debate_score,con_free_debate_score,pro_deduction_points,
                 con_deduction_points,pro_coherence_score,con_coherence_score,judge_kind)
                VALUES(:match_id,:judge_name,:pro_total,:con_total,:submitted_time,
                       :pro_free,:con_free,:pro_deduction,:con_deduction,
                       :pro_coherence,:con_coherence,'ai')"""),
            {
                "match_id": claim["match_id"],
                "judge_name": judge_name,
                "pro_total": pro["final_total"],
                "con_total": con["final_total"],
                "submitted_time": now.time(),
                "pro_free": pro["total_b"],
                "con_free": con["total_b"],
                "pro_deduction": pro["deduction"],
                "con_deduction": con["deduction"],
                "pro_coherence": pro["coherence"],
                "con_coherence": con["coherence"],
            },
        )
        conn.execute(
            text(f"""INSERT INTO {TABLE_DEBATER_SCORES}
                (match_id,judge_name,side,position,debater_score)
                VALUES(:match_id,:judge_name,:side,:position,:score)"""),
            [
                {
                    "match_id": claim["match_id"],
                    "judge_name": judge_name,
                    "side": side,
                    "position": position,
                    "score": int(score),
                }
                for side, data in (("pro", pro), ("con", con))
                for position, score in enumerate(data["ind_scores"], 1)
            ],
        )
        conn.execute(
            text(f"""INSERT INTO {TABLE_BEST_DEBATER_RANKINGS}
                (match_id,judge_name,side,position,rank)
                VALUES(:match_id,:judge_name,:side,:position,:rank)"""),
            [
                {"match_id": claim["match_id"], "judge_name": judge_name, **item}
                for item in result["rankings"]
            ],
        )
        audit = {
            "judge_name": judge_name,
            "model_label": claim["model_label"],
            "pro_total": pro["final_total"],
            "con_total": con["final_total"],
            "pro_deduction": pro["deduction"],
            "con_deduction": con["deduction"],
            "rankings": result["rankings"],
            "decision_reason": result["decision_reason"],
        }
        changed = conn.execute(
            text(f"""UPDATE {TABLE_OFFICIAL_AI_JUDGE_ATTEMPTS}
                SET status='succeeded',result_payload=CAST(:result AS JSONB),completed_at=:now
                WHERE match_id=:match_id AND attempt_no=:attempt_no AND status='running'"""),
            {
                "match_id": claim["match_id"],
                "attempt_no": claim["attempt_no"],
                "result": json.dumps(audit, ensure_ascii=False, separators=(",", ":")),
                "now": now,
            },
        ).rowcount
        if changed != 1:
            raise ValueError("AI 第三評判 attempt 狀態已改變。")
        conn.execute(
            text(f"""UPDATE {TABLE_OFFICIAL_AI_JUDGE_RUNS}
                SET status='succeeded',final_model_label=:model,final_judge_name=:judge,
                    current_claim_token=NULL,claim_expires_at=NULL,last_error='',
                    updated_at=:now,completed_at=:now
                WHERE match_id=:match_id"""),
            {
                "match_id": claim["match_id"],
                "model": claim["model_label"],
                "judge": judge_name,
                "now": now,
            },
        )
        _settle_usage(conn, usage_actor, claim, True, usage)
    return judge_name
