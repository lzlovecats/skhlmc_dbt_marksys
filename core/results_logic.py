"""Read model for the organiser results page."""

import math

import pandas as pd

from core.vote_logic import _resolve_db
from scoring import derive_debater_ranks
from schema import TABLE_BEST_DEBATER_RANKINGS, TABLE_DEBATERS, TABLE_DEBATER_SCORES, TABLE_MATCHES, TABLE_SCORES
from system_limits import JUDGE_MAX_PER_MATCH, MATCH_INVENTORY_LIMIT

RANK_COLUMNS = ("pro1_m", "pro2_m", "pro3_m", "pro4_m", "con1_m", "con2_m", "con3_m", "con4_m")
ROLE_KEYS = {
    "pro1_m": ("正方主辯", "pro", 1), "pro2_m": ("正方一副", "pro", 2),
    "pro3_m": ("正方二副", "pro", 3), "pro4_m": ("正方結辯", "pro", 4),
    "con1_m": ("反方主辯", "con", 1), "con2_m": ("反方一副", "con", 2),
    "con3_m": ("反方二副", "con", 3), "con4_m": ("反方結辯", "con", 4),
}


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
        ranks = {int(value) for value in judge_rows["rank"].tolist()}
    except (KeyError, TypeError, ValueError):
        return False
    expected_slots = {
        (side, position)
        for side in ("pro", "con")
        for position in range(1, 5)
    }
    return slots == expected_slots and ranks == set(range(1, 9))


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


def _anomalies(scores):
    if len(scores) < 3:
        return []
    anomalies = []
    for column, side in (("pro_total_score", "正方"), ("con_total_score", "反方")):
        mean = scores[column].mean()
        std = scores[column].std()
        if std and not pd.isna(std):
            for _, row in scores.iterrows():
                value = row[column]
                if abs(value - mean) > 2 * std:
                    anomalies.append({
                        "judge_name": _clean(row["judge_name"]), "side": side, "score": int(value),
                        "mean": round(float(mean), 1), "std": round(float(std), 1),
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
    best_rows, best = _best_debaters(selected, match_scores, db)
    pro_team, con_team = _clean(match_scores["pro_team"].iloc[0]), _clean(match_scores["con_team"].iloc[0])
    winner = "pro" if pro_votes > con_votes else "con" if con_votes > pro_votes else "draw"
    return {
        "matches": matches, "selected_match_id": selected, "has_scores": True,
        "topic": topic, "judge_count": len(match_scores), "pro_team": pro_team, "con_team": con_team,
        "pro_votes": pro_votes, "con_votes": con_votes, "draws": draws, "winner": winner,
        "best_debaters": best_rows, "best_debater": best, "anomalies": _anomalies(match_scores),
    }
