"""Read model for the chairperson console, with no Streamlit dependency."""

from datetime import datetime
from pathlib import Path

from core.match_logic import _match_records
from core.results_logic import results_data
from core.vote_logic import _resolve_db
from schema import TABLE_SCORE_DRAFTS, TABLE_SCORES

ASSETS = Path(__file__).resolve().parents[1] / "assets"


def _clean(value, fallback=""):
    value = str(value or "").strip()
    return fallback if not value or value.lower() in {"nan", "nat", "none"} else value


def _template(name):
    try:
        return (ASSETS / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def _near_now(match):
    try:
        return abs((datetime.strptime(f"{match['match_date']} {match['match_time']}", "%Y-%m-%d %H:%M") - datetime.now()).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def _judge_names(match_id, db):
    names = []
    for table in (TABLE_SCORES, TABLE_SCORE_DRAFTS):
        try:
            rows = db.query(f"SELECT DISTINCT judge_name FROM {table} WHERE match_id=:match_id ORDER BY judge_name", {"match_id": match_id})
            names.extend(_clean(value) for value in rows.get("judge_name", []) if _clean(value))
        except Exception:
            pass
    return list(dict.fromkeys(names))


def chairperson_data(selected_match_id=None, db=None):
    db = _resolve_db(db)
    matches = sorted(_match_records(db), key=_near_now)
    selected = str(selected_match_id or "")
    if selected not in {item["match_id"] for item in matches}:
        selected = matches[0]["match_id"] if matches else ""
    match = next((item for item in matches if item["match_id"] == selected), None)
    if not match:
        return {"matches": [], "selected_match_id": None, "match": None}
    result = results_data(selected, db=db)
    closing = {"has_scores": bool(result.get("has_scores")), "pro_votes": 0, "con_votes": 0, "draw_votes": 0, "best_debater": "（資料不足，暫時未能判定）"}
    if result.get("has_scores"):
        closing.update({"pro_votes": result["pro_votes"], "con_votes": result["con_votes"], "draw_votes": result["draws"], "best_debater": (result.get("best_debater") or {}).get("role") or closing["best_debater"]})
    return {
        "matches": [{"match_id": item["match_id"], "label": f"{item['match_id']} — {item['pro_team']} vs {item['con_team']} ({item['match_date']} {item['match_time']})"} for item in matches],
        "selected_match_id": selected, "match": match, "judge_names": _judge_names(selected, db),
        "closing": closing, "welcome_template": _template("chairperson_welcome.md"),
        "closing_template": _template("chairperson_closing.md"),
    }
