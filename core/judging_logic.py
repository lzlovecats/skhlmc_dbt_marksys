"""Scoring, draft and final-submission domain logic.

All final-score writes remain in one transaction: final drafts, score totals, and
the eight individual debater scores either all persist or none do.
"""

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from core.auth_logic import verify_password
from core.vote_logic import _resolve_db
from scoring import COHERENCE_MAX, FREE_DEBATE_CRITERIA, SPEECH_CRITERIA, free_debate_col, speech_col
from schema import TABLE_BEST_DEBATER_RANKINGS, TABLE_DEBATERS, TABLE_DEBATER_SCORES, TABLE_MATCHES, TABLE_SCORE_DRAFTS, TABLE_SCORES
from system_limits import JUDGE_MAX_PER_MATCH, MATCH_INVENTORY_LIMIT

HKT = ZoneInfo("Asia/Hong_Kong")
SIDES = ("正方", "反方")
ROLES = ("主辯", "一副", "二副", "結辯")


def normalize_judge_name(name):
    raw = str(name or "").replace("\u3000", " ").strip()
    raw = " ".join(raw.split())
    return "".join(char.lower() if "A" <= char <= "Z" else char for char in raw)[:100]


def _json_ready(value):
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    return value


def _serialize(data):
    payload = {key: _json_ready(value) for key, value in dict(data).items()}
    return json.dumps(payload, ensure_ascii=False)


def _deserialize(raw):
    return raw if isinstance(raw, dict) else json.loads(raw)


def _now():
    return dt.datetime.now(HKT)


def _has(value):
    return str(value or "").strip().lower() not in {"", "nan", "nat", "none", "<na>"}


def matches_for_judging(db=None, match_id=None, summaries=False):
    db = _resolve_db(db)
    if summaries:
        matches = db.query(
            f"""SELECT match_id, access_code_hash FROM {TABLE_MATCHES}
                ORDER BY match_id LIMIT :limit""",
            {"limit": MATCH_INVENTORY_LIMIT},
        )
        return [
            {
                "match_id": str(row["match_id"]),
                "is_open": _has(row.get("access_code_hash")),
            }
            for _, row in matches.iterrows()
        ]
    if match_id is None:
        matches = db.query(
            f"""SELECT match_id, topic_text, pro_team, con_team, access_code_hash
                FROM {TABLE_MATCHES} ORDER BY match_id LIMIT :limit""",
            {"limit": MATCH_INVENTORY_LIMIT},
        )
        debaters = db.query(
            f"""SELECT match_id,side,position,debater_name FROM {TABLE_DEBATERS}
                WHERE match_id IN (
                    SELECT match_id FROM {TABLE_MATCHES} ORDER BY match_id LIMIT :limit
                )""",
            {"limit": MATCH_INVENTORY_LIMIT},
        )
    else:
        matches = db.query(
            f"""SELECT match_id, topic_text, pro_team, con_team, access_code_hash
                FROM {TABLE_MATCHES} WHERE match_id=:match_id LIMIT 1""",
            {"match_id": match_id},
        )
        debaters = db.query(
            f"""SELECT match_id,side,position,debater_name FROM {TABLE_DEBATERS}
                WHERE match_id=:match_id ORDER BY side,position""",
            {"match_id": match_id},
        )
    debater_lookup = {
        (str(row["match_id"]), str(row["side"]).strip(), int(row["position"])): str(row["debater_name"] or "")
        for _, row in debaters.iterrows()
    }
    data = []
    for _, row in matches.iterrows():
        record = {
            "match_id": str(row["match_id"]), "topic_text": str(row.get("topic_text") or ""),
            "pro_team": str(row.get("pro_team") or ""), "con_team": str(row.get("con_team") or ""),
            "is_open": _has(row.get("access_code_hash")),
        }
        for side in ("pro", "con"):
            for position in range(1, 5):
                record[f"{side}_{position}"] = debater_lookup.get(
                    (record["match_id"], side, position), ""
                )
        data.append(record)
    return data


def verify_match_access(match_id, password, db=None):
    db = _resolve_db(db)
    rows = db.query(f"SELECT access_code_hash FROM {TABLE_MATCHES} WHERE match_id = :match_id", {"match_id": match_id})
    if rows.empty or not rows.iloc[0]["access_code_hash"]:
        return {"ok": False, "message": "該場次未開放評分，請向賽會人員查詢。"}
    if not verify_password(password or "", str(rows.iloc[0]["access_code_hash"])):
        return {"ok": False, "message": "密碼錯誤！"}
    return {"ok": True}


def has_final_submission(match_id, judge_name, db=None):
    db = _resolve_db(db)
    rows = db.query(
        f"SELECT 1 FROM {TABLE_SCORES} WHERE match_id = :match_id AND judge_name = :judge_name LIMIT 1",
        {"match_id": match_id, "judge_name": normalize_judge_name(judge_name)},
    )
    return not rows.empty


def load_drafts(match_id, judge_name, db=None):
    db = _resolve_db(db)
    rows = db.query(
        f"""SELECT side, score_payload FROM {TABLE_SCORE_DRAFTS}
            WHERE match_id = :match_id AND judge_name = :judge_name
              AND COALESCE(is_final, FALSE) = FALSE ORDER BY updated_at DESC""",
        {"match_id": match_id, "judge_name": normalize_judge_name(judge_name)},
    )
    drafts = {side: None for side in SIDES}
    for _, row in rows.iterrows():
        side = str(row["side"]).strip()
        if side in drafts and drafts[side] is None:
            try:
                drafts[side] = _deserialize(row["score_payload"])
            except Exception:
                pass
    return drafts


def _number(value, field, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必須是整數。") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{field} 必須介乎 {minimum} 至 {maximum}。")
    return number


def normalise_side_data(side, score_data):
    if side not in SIDES:
        raise ValueError("無效評分方。")
    data = dict(score_data or {})
    speech_rows = data.get("raw_df_a")
    free_rows = data.get("raw_df_b")
    if isinstance(speech_rows, pd.DataFrame):
        speech_rows = speech_rows.to_dict(orient="records")
    elif speech_rows is None:
        speech_rows = []
    if isinstance(free_rows, pd.DataFrame):
        free_rows = free_rows.to_dict(orient="records")
    elif free_rows is None:
        free_rows = []
    if len(speech_rows) != 4 or len(free_rows) != 1:
        raise ValueError("評分表資料不完整。")
    clean_speech, individual_scores = [], []
    for index, row in enumerate(speech_rows):
        clean = {
            "辯位": str(row.get("辯位") or ROLES[index])[:10],
            "姓名": str(row.get("姓名") or "")[:80],
        }
        score = 0
        for criterion in SPEECH_CRITERIA:
            column = speech_col(criterion)
            value = _number(row.get(column, 0), column, 0, criterion["max"])
            clean[column] = value
            score += value * criterion["weight"]
        clean_speech.append(clean)
        individual_scores.append(score)
    clean_free, free_total = {}, 0
    for criterion in FREE_DEBATE_CRITERIA:
        column = free_debate_col(criterion)
        value = _number(free_rows[0].get(column, 0), column, 0, criterion["max"])
        clean_free[column] = value
        free_total += value
    deduction = _number(data.get("deduction", 0), "扣分總和", 0, 10000)
    coherence = _number(data.get("coherence", 0), "內容連貫", 0, COHERENCE_MAX)
    speech_total = sum(individual_scores)
    return {
        "team_name": str(data.get("team_name") or side)[:100], "total_a": speech_total, "total_b": free_total,
        "deduction": deduction, "coherence": coherence,
        "final_total": speech_total + free_total - deduction + coherence,
        "ind_scores": individual_scores, "raw_df_a": clean_speech, "raw_df_b": [clean_free],
        "last_saved": str(data.get("last_saved") or _now().isoformat())[:40],
    }


def save_draft(match_id, judge_name, side, score_data, db=None):
    db = _resolve_db(db)
    judge = normalize_judge_name(judge_name)
    if not judge:
        raise ValueError("請輸入評判姓名！")
    data = normalise_side_data(side, score_data)
    with db.transaction() as session:
        # Keep the lock in its own statement: SQL expression evaluation order is
        # not a safe place to rely on the advisory lock happening before the
        # capacity/final-state subqueries.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"judge_submit:{match_id}"},
        )
        capacity = session.execute(
            text(f"""SELECT
                EXISTS(SELECT 1 FROM {TABLE_SCORES}
                       WHERE match_id=:match_id AND judge_name=:judge) AS submitted,
                (SELECT COUNT(DISTINCT judge_name) FROM {TABLE_SCORE_DRAFTS}
                 WHERE match_id=:match_id) AS n,
                EXISTS(SELECT 1 FROM {TABLE_SCORE_DRAFTS}
                       WHERE match_id=:match_id AND judge_name=:judge) AS current"""),
            {
                "match_id": match_id,
                "judge": judge,
            },
        ).fetchone()
        values = capacity._mapping
        if bool(values["submitted"]):
            raise ValueError("你已提交過評分！無法修改評分！")
        if int(values["n"] or 0) >= JUDGE_MAX_PER_MATCH and not bool(values["current"]):
            raise ValueError("本場評判人數已達保護上限，請聯絡賽會人員。")
        session.execute(
            text(f"""INSERT INTO {TABLE_SCORE_DRAFTS}
                (match_id, judge_name, side, score_payload, is_final, updated_at)
                VALUES (:match_id, :judge_name, :side, :payload, FALSE, :updated_at)
                ON CONFLICT (match_id, judge_name, side) DO UPDATE SET
                    score_payload=EXCLUDED.score_payload,
                    is_final=FALSE,
                    updated_at=EXCLUDED.updated_at"""),
            {
                "match_id": match_id,
                "judge_name": judge,
                "side": side,
                "payload": _serialize(data),
                "updated_at": _now().replace(tzinfo=None),
            },
        )
    return data


def submit_final_scores(match_id, judge_name, pro_data, con_data, db=None):
    db = _resolve_db(db)
    judge = normalize_judge_name(judge_name)
    if not judge:
        raise ValueError("請輸入評判姓名！")
    pro, con = normalise_side_data("正方", pro_data), normalise_side_data("反方", con_data)
    now = _now()
    with db.transaction() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                        {"key": f"judge_submit:{match_id}"})
        state = session.execute(
            text(f"""SELECT
                EXISTS(SELECT 1 FROM {TABLE_SCORES}
                       WHERE match_id=:match_id AND judge_name=:judge_name) AS submitted,
                (SELECT COUNT(*) FROM {TABLE_SCORES} WHERE match_id=:match_id) AS n"""),
            {"match_id": match_id, "judge_name": judge},
        ).fetchone()
        if bool(state._mapping["submitted"]):
            return False
        if int(state._mapping["n"] or 0) >= JUDGE_MAX_PER_MATCH:
            raise ValueError("本場評判人數已達保護上限，請聯絡賽會人員。")
        session.execute(
            text(f"""INSERT INTO {TABLE_SCORE_DRAFTS} (match_id, judge_name, side, score_payload, is_final, updated_at)
                VALUES (:match_id,:judge_name,:side,:payload,TRUE,:updated_at)
                ON CONFLICT (match_id,judge_name,side) DO UPDATE SET
                    score_payload=EXCLUDED.score_payload,
                    is_final=TRUE,
                    updated_at=EXCLUDED.updated_at"""),
            [
                {"match_id":match_id,"judge_name":judge,"side":side,"payload":_serialize(data),"updated_at":now.replace(tzinfo=None)}
                for side, data in (("正方", pro), ("反方", con))
            ],
        )
        session.execute(text(f"""INSERT INTO {TABLE_SCORES} (match_id,judge_name,pro_total_score,con_total_score,submitted_time,pro_free_debate_score,con_free_debate_score,pro_deduction_points,con_deduction_points,pro_coherence_score,con_coherence_score)
            VALUES (:match_id,:judge_name,:pro_total,:con_total,:submitted_time,:pro_free,:con_free,:pro_deduction,:con_deduction,:pro_coherence,:con_coherence)"""), {"match_id":match_id,"judge_name":judge,"pro_total":pro["final_total"],"con_total":con["final_total"],"submitted_time":now.strftime("%H:%M:%S"),"pro_free":pro["total_b"],"con_free":con["total_b"],"pro_deduction":pro["deduction"],"con_deduction":con["deduction"],"pro_coherence":pro["coherence"],"con_coherence":con["coherence"]})
        params = [{"match_id":match_id,"judge_name":judge,"side":db_side,"position":index + 1,"score":int(score)} for db_side, data in (("pro",pro),("con",con)) for index, score in enumerate(data["ind_scores"])]
        session.execute(text(f"""INSERT INTO {TABLE_DEBATER_SCORES} (match_id,judge_name,side,position,debater_score)
            VALUES (:match_id,:judge_name,:side,:position,:score)
            ON CONFLICT (match_id,judge_name,side,position) DO UPDATE SET debater_score=EXCLUDED.debater_score"""), params)
    return {"ok": True, "pro": pro, "con": con}


def auto_derive_ranking_order(pro_scores, con_scores):
    rows = [{"side": side, "position": position, "score": int(score)} for side, scores in (("pro", pro_scores), ("con", con_scores)) for position, score in enumerate(scores, 1)]
    rows.sort(key=lambda row: row["score"], reverse=True)
    return {(row["side"], row["position"]): rank for rank, row in enumerate(rows, 1)}


def submit_best_debater_rankings(match_id, judge_name, rankings, db=None):
    db = _resolve_db(db)
    judge = normalize_judge_name(judge_name)
    if not judge:
        raise ValueError("請輸入評判姓名！")
    if not has_final_submission(match_id, judge, db=db):
        raise ValueError("請先正式提交本場評分，才可提交最佳辯論員排名。")
    if len(rankings) != 8:
        raise ValueError("排名資料必須包含本場 8 位辯員。")
    try:
        slots = [(str(item.get("side", "")), int(item.get("position", 0))) for item in rankings]
        assigned = sorted(int(item.get("rank", 0)) for item in rankings)
    except (TypeError, ValueError) as exc:
        raise ValueError("排名資料格式不正確。") from exc
    expected_slots = {(side, position) for side in ("pro", "con") for position in range(1, 5)}
    if set(slots) != expected_slots or len(set(slots)) != 8:
        raise ValueError("排名資料必須完整包含正反方各四個辯位。")
    if assigned != list(range(1, 9)):
        raise ValueError("每個名次（1–8）必須恰好使用一次，請檢查是否有重複或遺漏。")
    params = [
        {"match_id": match_id, "judge_name": judge, "side": side, "position": position, "rank": int(item["rank"])}
        for item, (side, position) in zip(rankings, slots)
    ]
    with db.transaction() as session:
        session.execute(text(f"""INSERT INTO {TABLE_BEST_DEBATER_RANKINGS} (match_id,judge_name,side,position,rank)
            VALUES (:match_id,:judge_name,:side,:position,:rank)
            ON CONFLICT (match_id,judge_name,side,position) DO UPDATE SET rank=EXCLUDED.rank"""), params)
    return True
