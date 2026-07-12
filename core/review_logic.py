"""Read-only review model for finalised judge score sheets."""

import pandas as pd

from core.auth_logic import verify_password
from core.judging_logic import _deserialize, normalize_judge_name
from core.results_logic import _best_debaters, _scores
from core.vote_logic import _resolve_db
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
    if scores.empty: return {"judges": [], "selected_judge": None}
    judges = scores["judge_name"].tolist(); judge = judge_name if judge_name in judges else judges[0]
    record = scores[scores["judge_name"] == judge].iloc[0]
    drafts = db.query(f"""SELECT side,score_payload,updated_at FROM {TABLE_SCORE_DRAFTS}
        WHERE match_id=:match_id AND lower(btrim(judge_name))=:judge AND COALESCE(is_final,FALSE)=TRUE ORDER BY updated_at DESC""", {"match_id":match_id,"judge":normalize_judge_name(judge)})
    sides = {}; missing=[]
    for side in ("正方","反方"):
        found = drafts[drafts["side"].astype(str).str.strip() == side]
        if found.empty: missing.append(side)
        else:
            try: sides[side]=_deserialize(found.iloc[0]["score_payload"])
            except Exception: missing.append(side)
    best_rows,best=_best_debaters(match_id,scores,db)
    return {"judges":judges,"selected_judge":judge,"record":record.to_dict(),"best_debaters":best_rows,"best_debater":best,"sides":sides,"missing_sides":missing}
