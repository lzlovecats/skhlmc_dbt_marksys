"""Read-only review model for finalised judge score sheets."""

import pandas as pd

from core.auth_logic import verify_password
from core.judging_logic import _deserialize, normalize_judge_name
from core.results_logic import _best_debaters, _scores
from core.vote_logic import _resolve_db
from scoring import FREE_DEBATE_CRITERIA, FREE_DEBATE_MAX, SPEECH_CRITERIA, speech_col, free_debate_col
from schema import TABLE_MATCHES, TABLE_SCORE_DRAFTS, TABLE_SCORES


def available_matches(db=None):
    db = _resolve_db(db)
    rows = db.query(f"""SELECT DISTINCT m.match_id, m.review_password_hash, m.match_date, m.match_time, m.topic_text
        FROM {TABLE_MATCHES} m INNER JOIN {TABLE_SCORES} s ON m.match_id=s.match_id ORDER BY m.match_id""")
    return [{"match_id": str(row["match_id"]), "is_open": bool(str(row.get("review_password_hash") or "").strip() not in {"", "nan", "None"})} for _, row in rows.iterrows()]


def verify_review_access(match_id, password, db=None):
    db = _resolve_db(db)
    rows = db.query(f"SELECT review_password_hash FROM {TABLE_MATCHES} WHERE match_id=:match_id", {"match_id": match_id})
    stored = "" if rows.empty else str(rows.iloc[0]["review_password_hash"] or "")
    if not stored or stored.lower() == "nan": return {"ok":False,"message":"此場次尚未設定查閱分紙密碼，請聯絡賽會人員。"}
    return {"ok": True} if verify_password(password or "", stored) else {"ok":False,"message":"密碼錯誤"}


def review_data(match_id, judge_name=None, db=None):
    db = _resolve_db(db); scores = _scores(match_id, db)
    if scores.empty:
        return {
            "has_scores": False, "judges": [], "selected_judge": None,
            "record": None, "best_debaters": None, "best_debater": None,
            "sides": {}, "missing_sides": [],
        }
    judges = scores["judge_name"].drop_duplicates().tolist()
    judge = judge_name if judge_name in judges else judges[0]
    record = scores[scores["judge_name"] == judge].iloc[0]
    drafts = db.query(f"""SELECT side,score_payload,updated_at FROM {TABLE_SCORE_DRAFTS}
        WHERE match_id=:match_id AND lower(btrim(judge_name))=:judge AND COALESCE(is_final,FALSE)=TRUE ORDER BY updated_at DESC""", {"match_id":match_id,"judge":normalize_judge_name(judge)})
    sides = {}; missing=[]
    for side in ("正方","反方"):
        found = drafts[drafts["side"].astype(str).str.strip() == side]
        if found.empty: missing.append(side)
        else:
            try:
                data = _deserialize(found.iloc[0]["score_payload"])
                speech_rows = []
                for source in data.get("raw_df_a") or []:
                    row = dict(source)
                    row["總分（100）"] = sum(float(row.get(speech_col(item), 0) or 0) * item["weight"] for item in SPEECH_CRITERIA)
                    if row["總分（100）"].is_integer():
                        row["總分（100）"] = int(row["總分（100）"])
                    speech_rows.append(row)
                free_rows = []
                for source in data.get("raw_df_b") or []:
                    row = dict(source)
                    row[f"總分（{FREE_DEBATE_MAX}）"] = sum(float(row.get(free_debate_col(item), 0) or 0) for item in FREE_DEBATE_CRITERIA)
                    if row[f"總分（{FREE_DEBATE_MAX}）"].is_integer():
                        row[f"總分（{FREE_DEBATE_MAX}）"] = int(row[f"總分（{FREE_DEBATE_MAX}）"])
                    free_rows.append(row)
                data["raw_df_a"], data["raw_df_b"] = speech_rows, free_rows
                sides[side] = data
            except Exception: missing.append(side)
    best_rows,best=_best_debaters(match_id,scores,db)
    return {"has_scores":True,"judges":judges,"selected_judge":judge,"record":record.to_dict(),"best_debaters":best_rows,"best_debater":best,"sides":sides,"missing_sides":missing}
