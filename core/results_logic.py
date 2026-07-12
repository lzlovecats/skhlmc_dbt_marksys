"""Streamlit-free read model for the organiser results page."""

import pandas as pd

from core.vote_logic import _resolve_db
from schema import TABLE_BEST_DEBATER_RANKINGS, TABLE_DEBATERS, TABLE_DEBATER_SCORES, TABLE_MATCHES, TABLE_SCORES

RANK_COLUMNS = ("pro1_m", "pro2_m", "pro3_m", "pro4_m", "con1_m", "con2_m", "con3_m", "con4_m")
ROLE_KEYS = {
    "pro1_m": ("正方主辯", "pro", 1), "pro2_m": ("正方一副", "pro", 2),
    "pro3_m": ("正方二副", "pro", 3), "pro4_m": ("正方結辯", "pro", 4),
    "con1_m": ("反方主辯", "con", 1), "con2_m": ("反方一副", "con", 2),
    "con3_m": ("反方二副", "con", 3), "con4_m": ("反方結辯", "con", 4),
}


def _clean(value):
    return "" if value is None else str(value).strip()


def _scores(match_id=None, db=None):
    db = _resolve_db(db)
    params = {}
    sql = f"""
        SELECT s.match_id, s.judge_name, s.pro_total_score, s.con_total_score,
               s.submitted_time, s.pro_free_debate_score, s.con_free_debate_score,
               s.pro_deduction_points, s.con_deduction_points,
               s.pro_coherence_score, s.con_coherence_score, m.pro_team, m.con_team
        FROM {TABLE_SCORES} s LEFT JOIN {TABLE_MATCHES} m ON s.match_id = m.match_id
    """
    if match_id is not None:
        sql += " WHERE s.match_id = :match_id"
        params["match_id"] = match_id
    scores = db.query(sql, params)
    if scores.empty:
        return scores
    detail_sql = f"SELECT match_id, judge_name, side, position, debater_score FROM {TABLE_DEBATER_SCORES}"
    if match_id is not None:
        detail_sql += " WHERE match_id = :match_id"
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
    if numeric_scores.isna().any().any():
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
    explicit = not rank_df.empty and all(judge in rank_df["judge_name"].values for judge in judges)
    if explicit:
        rows = []
        for judge in judges:
            judge_rows = rank_df[rank_df["judge_name"] == judge]
            rows.append({
                column: int(found["rank"].iloc[0]) if not (found := judge_rows[(judge_rows["side"] == side) & (judge_rows["position"] == position)]).empty else 0
                for column, (_, side, position) in ROLE_KEYS.items()
            })
        ranks = pd.DataFrame(rows)
    else:
        ranks = pd.DataFrame([row.rank(ascending=False, method="min") for _, row in numeric_scores.iterrows()])

    rank_sum = ranks.sum()
    results = [
        {"role": labels[column], "rank_sum": int(rank_sum[column]), "average_score": round(float(numeric_scores[column].mean()), 2)}
        for column in RANK_COLUMNS
    ]
    results.sort(key=lambda item: (item["rank_sum"], -item["average_score"]))
    return results, results[0]


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


def results_data(selected_match_id=None, db=None):
    db = _resolve_db(db)
    scores = _scores(db=db)
    if scores is None or scores.empty:
        return {"matches": [], "selected_match_id": None, "has_scores": False}
    scores["match_id"] = scores["match_id"].astype(str)
    matches = scores["match_id"].drop_duplicates().tolist()
    selected = str(selected_match_id) if selected_match_id in matches else matches[0]
    match_scores = scores[scores["match_id"] == selected].copy()
    pro_votes = int((match_scores["pro_total_score"] > match_scores["con_total_score"]).sum())
    con_votes = int((match_scores["con_total_score"] > match_scores["pro_total_score"]).sum())
    draws = int((match_scores["pro_total_score"] == match_scores["con_total_score"]).sum())
    topic_rows = db.query(f"SELECT topic_text FROM {TABLE_MATCHES} WHERE match_id = :match_id", {"match_id": selected})
    topic = _clean(topic_rows.iloc[0]["topic_text"]) if not topic_rows.empty else "（未有辯題資料）"
    best_rows, best = _best_debaters(selected, match_scores, db)
    pro_team, con_team = _clean(match_scores["pro_team"].iloc[0]), _clean(match_scores["con_team"].iloc[0])
    winner = "pro" if pro_votes > con_votes else "con" if con_votes > pro_votes else "draw"
    return {
        "matches": matches, "selected_match_id": selected, "has_scores": True,
        "topic": topic, "judge_count": len(match_scores), "pro_team": pro_team, "con_team": con_team,
        "pro_votes": pro_votes, "con_votes": con_votes, "draws": draws, "winner": winner,
        "best_debaters": best_rows, "best_debater": best, "anomalies": _anomalies(match_scores),
    }
