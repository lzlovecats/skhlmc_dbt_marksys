"""Read model for the organiser results page."""

import math
from statistics import median

import pandas as pd

from core.judging_logic import _deserialize, normalize_judge_name
from core.vote_logic import _resolve_db
from scoring import (
    FREE_DEBATE_CRITERIA,
    GRAND_TOTAL,
    SPEECH_CRITERIA,
    derive_debater_ranks,
    free_debate_col,
    is_valid_competition_ranking,
    speech_col,
)
from schema import TABLE_BEST_DEBATER_RANKINGS, TABLE_DEBATERS, TABLE_DEBATER_SCORES, TABLE_MATCHES, TABLE_SCORE_DRAFTS, TABLE_SCORES
from system_limits import JUDGE_MAX_PER_MATCH, MATCH_INVENTORY_LIMIT

RANK_COLUMNS = ("pro1_m", "pro2_m", "pro3_m", "pro4_m", "con1_m", "con2_m", "con3_m", "con4_m")
ROLE_KEYS = {
    "pro1_m": ("正方主辯", "pro", 1), "pro2_m": ("正方一副", "pro", 2),
    "pro3_m": ("正方二副", "pro", 3), "pro4_m": ("正方結辯", "pro", 4),
    "con1_m": ("反方主辯", "con", 1), "con2_m": ("反方一副", "con", 2),
    "con3_m": ("反方二副", "con", 3), "con4_m": ("反方結辯", "con", 4),
}
ANOMALY_DIRECTION_GAP = GRAND_TOTAL * 0.10
ANOMALY_MARGIN_GAP = GRAND_TOTAL * 0.15
ANOMALY_SCALE_GAP = GRAND_TOTAL * 0.10


def _clean(value):
    return "" if value is None else str(value).strip()


def _complete_ranking(rank_df, judge):
    if rank_df.empty:
        return False
    judge_rows = rank_df[rank_df["judge_name"] == judge]
    if len(judge_rows) != 8:
        return False
    try:
        slots = {
            (str(row["side"]).strip(), int(row["position"]))
            for _, row in judge_rows.iterrows()
        }
        ranks = [int(value) for value in judge_rows["rank"].tolist()]
    except (KeyError, TypeError, ValueError):
        return False
    expected_slots = {
        (side, position)
        for side in ("pro", "con")
        for position in range(1, 5)
    }
    return slots == expected_slots and is_valid_competition_ranking(ranks)


def _ranking_row(judge, numeric_scores, rank_df):
    """Resolve one judge independently: explicit ranking or score fallback."""
    if _complete_ranking(rank_df, judge):
        judge_rows = rank_df[rank_df["judge_name"] == judge]
        submitted = {
            (str(row["side"]).strip(), int(row["position"])): int(row["rank"])
            for _, row in judge_rows.iterrows()
        }
        return ({
            column: submitted[(side, position)]
            for column, (_, side, position) in ROLE_KEYS.items()
        }, "submitted")

    derived = derive_debater_ranks(
        [numeric_scores[column] for column in RANK_COLUMNS[:4]],
        [numeric_scores[column] for column in RANK_COLUMNS[4:]],
    )
    return ({
        column: derived[(side, position)]
        for column, (_, side, position) in ROLE_KEYS.items()
    }, "derived")


def judge_ranking(match_id, judge_name, score_row, db):
    """Return the selected judge's PDF-ready ranks and their source."""
    numeric = pd.to_numeric(
        pd.Series({column: score_row.get(column) for column in RANK_COLUMNS}),
        errors="coerce",
    )
    if numeric.isna().any() or not numeric.map(math.isfinite).all():
        return None
    rank_df = db.query(
        f"""SELECT judge_name, side, position, rank
            FROM {TABLE_BEST_DEBATER_RANKINGS}
            WHERE match_id = :match_id AND judge_name = :judge_name""",
        {"match_id": match_id, "judge_name": judge_name},
    )
    row, source = _ranking_row(judge_name, numeric, rank_df)
    return {
        "正方": [row[column] for column in RANK_COLUMNS[:4]],
        "反方": [row[column] for column in RANK_COLUMNS[4:]],
        "source": source,
    }


def _scores(match_id=None, db=None):
    db = _resolve_db(db)
    params = {}
    sql = f"""
        SELECT s.match_id, s.judge_name, s.pro_total_score, s.con_total_score,
               s.submitted_time, s.pro_free_debate_score, s.con_free_debate_score,
               s.pro_deduction_points, s.con_deduction_points,
               s.pro_coherence_score, s.con_coherence_score,
               m.pro_team, m.con_team, m.topic_text, m.match_date, m.match_time
        FROM {TABLE_SCORES} s LEFT JOIN {TABLE_MATCHES} m ON s.match_id = m.match_id
    """
    if match_id is not None:
        sql += " WHERE s.match_id = :match_id"
        params["match_id"] = match_id
        sql += " ORDER BY s.judge_name LIMIT :score_limit"
        params["score_limit"] = JUDGE_MAX_PER_MATCH
    else:
        sql += " ORDER BY s.match_id,s.judge_name LIMIT :score_limit"
        params["score_limit"] = MATCH_INVENTORY_LIMIT * JUDGE_MAX_PER_MATCH
    scores = db.query(sql, params)
    if scores.empty:
        return scores
    detail_sql = f"SELECT match_id, judge_name, side, position, debater_score FROM {TABLE_DEBATER_SCORES}"
    if match_id is not None:
        detail_sql += " WHERE match_id = :match_id"
        detail_sql += " ORDER BY judge_name,side,position LIMIT :detail_limit"
        params["detail_limit"] = JUDGE_MAX_PER_MATCH * 8
    else:
        detail_sql += " ORDER BY match_id,judge_name,side,position LIMIT :detail_limit"
        params["detail_limit"] = MATCH_INVENTORY_LIMIT * JUDGE_MAX_PER_MATCH * 8
    details = db.query(detail_sql, params)
    if not details.empty:
        details["column"] = details["side"].astype(str) + details["position"].astype(str) + "_m"
        pivot = details.pivot_table(index=["match_id", "judge_name"], columns="column", values="debater_score", aggfunc="first").reset_index()
        scores = scores.merge(pivot, on=["match_id", "judge_name"], how="left")
    for column in RANK_COLUMNS:
        if column not in scores.columns:
            scores[column] = None
    return scores


def _best_debaters(match_id, scores, db):
    numeric_scores = scores[list(RANK_COLUMNS)].apply(pd.to_numeric, errors="coerce")
    if (
        numeric_scores.isna().any().any()
        or not numeric_scores.apply(lambda column: column.map(math.isfinite)).all().all()
    ):
        return None, None

    names_df = db.query(
        f"SELECT side, position, debater_name FROM {TABLE_DEBATERS} WHERE match_id = :match_id",
        {"match_id": match_id},
    )
    names = {
        (_clean(row["side"]), int(row["position"])): _clean(row["debater_name"])
        for _, row in names_df.iterrows()
    }
    labels = {}
    for column, (role, side, position) in ROLE_KEYS.items():
        name = names.get((side, position), "")
        labels[column] = f"{role}（{name}）" if name else role

    rank_df = db.query(
        f"SELECT judge_name, side, position, rank FROM {TABLE_BEST_DEBATER_RANKINGS} WHERE match_id = :match_id",
        {"match_id": match_id},
    )
    judges = scores["judge_name"].tolist()
    ranks = pd.DataFrame([
        _ranking_row(judge, numeric_scores.iloc[index], rank_df)[0]
        for index, judge in enumerate(judges)
    ], columns=RANK_COLUMNS)

    rank_sum = ranks.sum()
    results = [
        {"role": labels[column], "rank_sum": int(rank_sum[column]), "average_score": round(float(numeric_scores[column].mean()), 2)}
        for column in RANK_COLUMNS
    ]
    results.sort(key=lambda item: (item["rank_sum"], -item["average_score"]))
    leaders = [
        item
        for item in results
        if item["rank_sum"] == results[0]["rank_sum"]
        and item["average_score"] == results[0]["average_score"]
    ]
    if len(leaders) == 1:
        return results, results[0]
    return results, {
        "role": None,
        "rank_sum": results[0]["rank_sum"],
        "average_score": results[0]["average_score"],
        "is_tie": True,
        "tied_roles": [item["role"] for item in leaders],
    }


def _number_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _zero_score_fields(payload, side):
    try:
        data = _deserialize(payload)
    except (AttributeError, TypeError, ValueError, KeyError):
        return []
    if not isinstance(data, dict):
        return []
    fields = []
    speech_rows = data.get("raw_df_a")
    if isinstance(speech_rows, list):
        for index, source in enumerate(speech_rows):
            if not isinstance(source, dict):
                continue
            role = _clean(source.get("辯位")) or f"第{index + 1}位"
            for criterion in SPEECH_CRITERIA:
                value = _number_or_none(source.get(speech_col(criterion)))
                if value == 0:
                    fields.append(f"{side}{role}－{criterion['key']}")
    free_rows = data.get("raw_df_b")
    if isinstance(free_rows, list):
        for source in free_rows:
            if not isinstance(source, dict):
                continue
            for criterion in FREE_DEBATE_CRITERIA:
                value = _number_or_none(source.get(free_debate_col(criterion)))
                if value == 0:
                    fields.append(f"{side}自由辯論－{criterion['key']}")
    if _number_or_none(data.get("coherence")) == 0:
        fields.append(f"{side}內容連貫")
    return fields


def _zero_score_anomalies(scores, drafts):
    if drafts is None or drafts.empty:
        return []
    score_judges = {
        normalize_judge_name(value): _clean(value) for value in scores["judge_name"]
    }
    fields_by_judge = {}
    for _, row in drafts.iterrows():
        judge_key = normalize_judge_name(row.get("judge_name"))
        if judge_key not in score_judges:
            continue
        judge = score_judges[judge_key]
        side = {"pro": "正方", "con": "反方"}.get(
            _clean(row.get("side")).lower(), _clean(row.get("side")),
        )
        if side not in {"正方", "反方"}:
            continue
        fields_by_judge.setdefault(judge, []).extend(
            _zero_score_fields(row.get("score_payload"), side),
        )
    anomalies = []
    for judge, fields in fields_by_judge.items():
        unique_fields = list(dict.fromkeys(fields))
        if unique_fields:
            anomalies.append({
                "type": "zero_score",
                "judge_name": judge,
                "fields": unique_fields,
                "message": (
                    f"有 {len(unique_fields)} 個評分欄位為 0："
                    f"{'、'.join(unique_fields)}。"
                ),
            })
    return anomalies


def _margin_label(value):
    if value > 0:
        return f"正方 +{round(value, 1):g}"
    if value < 0:
        return f"反方 +{round(abs(value), 1):g}"
    return "平分"


def _anomalies(scores, drafts=None):
    anomalies = _zero_score_anomalies(scores, drafts)
    rows = []
    for _, row in scores.iterrows():
        pro = _number_or_none(row.get("pro_total_score"))
        con = _number_or_none(row.get("con_total_score"))
        if pro is not None and con is not None:
            rows.append({
                "judge_name": _clean(row.get("judge_name")),
                "margin": pro - con,
                "scale": (pro + con) / 2,
            })
    if len(rows) < 3:
        return anomalies

    for index, row in enumerate(rows):
        peers = rows[:index] + rows[index + 1:]
        peer_margins = [item["margin"] for item in peers]
        peer_margin = float(median(peer_margins))
        margin_gap = abs(row["margin"] - peer_margin)
        peer_direction_agrees = (
            all(value > 0 for value in peer_margins)
            or all(value < 0 for value in peer_margins)
        )
        opposite_direction = (
            peer_direction_agrees and row["margin"] * peer_margin < 0
        )
        if opposite_direction and margin_gap >= ANOMALY_DIRECTION_GAP:
            anomalies.append({
                "type": "direction",
                "judge_name": row["judge_name"],
                "gap": round(margin_gap, 1),
                "message": (
                    "判決方向與其餘評判一致方向相反；"
                    f"該評判為 {_margin_label(row['margin'])}，"
                    f"同儕中位數為 {_margin_label(peer_margin)}，"
                    f"相差 {round(margin_gap, 1):g} 分。"
                ),
            })
        elif margin_gap >= ANOMALY_MARGIN_GAP:
            anomalies.append({
                "type": "margin",
                "judge_name": row["judge_name"],
                "gap": round(margin_gap, 1),
                "message": (
                    "正反方分差與其餘評判差距過大；"
                    f"該評判為 {_margin_label(row['margin'])}，"
                    f"同儕中位數為 {_margin_label(peer_margin)}，"
                    f"相差 {round(margin_gap, 1):g} 分。"
                ),
            })

        peer_scale = float(median([item["scale"] for item in peers]))
        scale_gap = abs(row["scale"] - peer_scale)
        if scale_gap >= ANOMALY_SCALE_GAP:
            anomalies.append({
                "type": "scale",
                "judge_name": row["judge_name"],
                "gap": round(scale_gap, 1),
                "message": (
                    "雙方整體評分水平與其餘評判差距過大；"
                    f"該評判雙方平均為 {round(row['scale'], 1):g} 分，"
                    f"同儕中位數為 {round(peer_scale, 1):g} 分，"
                    f"相差 {round(scale_gap, 1):g} 分。"
                ),
            })
    return anomalies


def results_data(selected_match_id=None, db=None, match_ids=None):
    db = _resolve_db(db)
    if match_ids is None:
        match_rows = db.query(f"""SELECT DISTINCT match_id FROM {TABLE_SCORES}
            ORDER BY match_id LIMIT :limit""", {"limit": MATCH_INVENTORY_LIMIT})
        if match_rows.empty:
            return {"matches": [], "selected_match_id": None, "has_scores": False}
        matches = match_rows["match_id"].astype(str).tolist()
    else:
        matches = list(dict.fromkeys(str(value) for value in match_ids if str(value).strip()))[:MATCH_INVENTORY_LIMIT]
        if not matches:
            return {"matches": [], "selected_match_id": None, "has_scores": False}
    requested = str(selected_match_id or "")
    selected = requested if requested in matches else matches[0]
    match_scores = _scores(selected, db=db)
    if match_scores.empty:
        return {"matches": matches, "selected_match_id": selected, "has_scores": False}
    pro_votes = int((match_scores["pro_total_score"] > match_scores["con_total_score"]).sum())
    con_votes = int((match_scores["con_total_score"] > match_scores["pro_total_score"]).sum())
    draws = int((match_scores["pro_total_score"] == match_scores["con_total_score"]).sum())
    topic = (
        _clean(match_scores["topic_text"].iloc[0])
        if "topic_text" in match_scores.columns and _clean(match_scores["topic_text"].iloc[0])
        else "（未有辯題資料）"
    )
    final_drafts = db.query(
        f"""SELECT judge_name,side,score_payload FROM {TABLE_SCORE_DRAFTS}
            WHERE match_id=:match_id AND COALESCE(is_final,FALSE)=TRUE
            ORDER BY judge_name,side LIMIT :draft_limit""",
        {
            "match_id": selected,
            "draft_limit": JUDGE_MAX_PER_MATCH * 2,
        },
    )
    best_rows, best = _best_debaters(selected, match_scores, db)
    pro_team, con_team = _clean(match_scores["pro_team"].iloc[0]), _clean(match_scores["con_team"].iloc[0])
    winner = "pro" if pro_votes > con_votes else "con" if con_votes > pro_votes else "draw"
    return {
        "matches": matches, "selected_match_id": selected, "has_scores": True,
        "topic": topic, "judge_count": len(match_scores), "pro_team": pro_team, "con_team": con_team,
        "pro_votes": pro_votes, "con_votes": con_votes, "draws": draws, "winner": winner,
        "best_debaters": best_rows, "best_debater": best,
        "anomalies": _anomalies(match_scores, final_drafts),
    }
