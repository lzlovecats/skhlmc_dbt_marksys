"""Streamlit-free match management and team roster logic."""

import datetime as dt
import random
import secrets
from zoneinfo import ZoneInfo

from sqlalchemy import text

from core.auth_logic import hash_password
from core.vote_logic import _resolve_db
from schema import (
    CREATE_MATCH_ROSTER_LINKS, TABLE_DEBATERS, TABLE_MATCHES,
    TABLE_MATCH_ROSTER_LINKS, TABLE_TOPICS,
)

HKT = ZoneInfo("Asia/Hong_Kong")
TIME_SLOTS = [f"{hour:02d}:{minute:02d}" for hour in range(15, 19) for minute in range(0, 60, 10) if hour < 18 or minute == 0]
DIFFICULTY_OPTIONS = {1: "Lv1 — 概念日常", 2: "Lv2 — 一般議題", 3: "Lv3 — 進階專業"}


def _now(): return dt.datetime.now(HKT).replace(tzinfo=None)
def _clean(value): return str(value or "").strip()
def _has(value): return _clean(value).lower() not in ("", "nan", "nat", "none", "<na>")
def _date(value): return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else _clean(value)[:10]
def _time(value): return value.strftime("%H:%M") if hasattr(value, "strftime") else _clean(value)[:5]


def ensure_roster_links(db=None):
    db = _resolve_db(db)
    db.execute(CREATE_MATCH_ROSTER_LINKS)
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_match_roster_links_token ON {TABLE_MATCH_ROSTER_LINKS}(roster_token)")


def _match_records(db):
    matches = db.query(f"SELECT match_id, match_date, match_time, topic_text, pro_team, con_team, access_code_hash, review_password_hash FROM {TABLE_MATCHES} ORDER BY match_id")
    debaters = db.query(f"SELECT match_id, side, position, debater_name FROM {TABLE_DEBATERS}")
    out = []
    for _, row in matches.iterrows():
        record = {"match_id": _clean(row["match_id"]), "match_date": _date(row.get("match_date")), "match_time": _time(row.get("match_time")), "topic_text": _clean(row.get("topic_text")), "pro_team": _clean(row.get("pro_team")), "con_team": _clean(row.get("con_team")), "has_access_code": _has(row.get("access_code_hash")), "has_review_password": _has(row.get("review_password_hash"))}
        for side in ("pro", "con"):
            for pos in range(1, 5): record[f"{side}_{pos}"] = ""
        subset = debaters[debaters["match_id"].astype(str) == record["match_id"]]
        for _, debater in subset.iterrows():
            record[f"{_clean(debater['side'])}_{int(debater['position'])}"] = _clean(debater["debater_name"])
        out.append(record)
    return out


def ensure_match_links(match_id, db=None):
    db = _resolve_db(db); ensure_roster_links(db)
    rows = db.query(f"SELECT side, roster_token, submitted_at, created_at FROM {TABLE_MATCH_ROSTER_LINKS} WHERE match_id = :id", {"id": match_id})
    existing = {_clean(row["side"]): row for _, row in rows.iterrows()}
    for side in ("pro", "con"):
        if side not in existing:
            db.execute(f"INSERT INTO {TABLE_MATCH_ROSTER_LINKS} (match_id, side, roster_token, created_at) VALUES (:id, :side, :token, :now)", {"id": match_id, "side": side, "token": secrets.token_urlsafe(32), "now": _now()})
    rows = db.query(f"SELECT side, roster_token, submitted_at FROM {TABLE_MATCH_ROSTER_LINKS} WHERE match_id = :id", {"id": match_id})
    return { _clean(row["side"]): {"roster_token": _clean(row["roster_token"]), "submitted": _has(row.get("submitted_at"))} for _, row in rows.iterrows() }


def match_admin_data(selected_match_id=None, db=None):
    db = _resolve_db(db); ensure_roster_links(db)
    matches = _match_records(db)
    selected = _clean(selected_match_id) or (matches[0]["match_id"] if matches else "")
    if selected and selected not in {m["match_id"] for m in matches}: selected = matches[0]["match_id"] if matches else ""
    links = ensure_match_links(selected, db) if selected else {}
    return {"matches": matches, "selected_match_id": selected or None, "roster_links": links,
            "default_date": _now().date().isoformat(), "default_time": "16:00",
            "time_slots": TIME_SLOTS, "difficulties": [{"value": key, "label": value} for key, value in DIFFICULTY_OPTIONS.items()]}


def create_match(match_id, db=None):
    match_id = _clean(match_id)
    if not match_id: return {"ok": False, "message": "未輸入任何文字！"}
    db = _resolve_db(db)
    found = db.query(f"SELECT 1 FROM {TABLE_MATCHES} WHERE match_id = :id", {"id": match_id})
    if not found.empty: return {"ok": False, "message": "此場次已存在。"}
    db.execute(f"INSERT INTO {TABLE_MATCHES} (match_id, match_date, match_time, topic_text, pro_team, con_team) VALUES (:id, NULL, NULL, '', '', '')", {"id": match_id})
    return {"ok": True, "message": f"已建立場次：{match_id}", "match_id": match_id}


def save_match(data, db=None):
    db = _resolve_db(db); match_id = _clean(data.get("match_id"))
    if not match_id: return {"ok": False, "message": "場次不存在。"}
    if data.get("clear_access_code") and _clean(data.get("access_code")): return {"ok": False, "message": "如需清除評判入場密碼，請將密碼欄留空。"}
    if data.get("clear_review_password") and _clean(data.get("review_password")): return {"ok": False, "message": "如需清除查閱分紙密碼，請將密碼欄留空。"}
    match_date, match_time = _clean(data.get("match_date")), _clean(data.get("match_time"))
    try:
        if match_date: dt.date.fromisoformat(match_date)
    except ValueError:
        return {"ok": False, "message": "請輸入有效的比賽日期。"}
    if match_time and match_time not in TIME_SLOTS:
        return {"ok": False, "message": "請選擇有效的比賽時間。"}
    with db.transaction() as session:
        exists = session.execute(text(f"SELECT access_code_hash, review_password_hash FROM {TABLE_MATCHES} WHERE match_id = :id"), {"id": match_id}).fetchone()
        if not exists: return {"ok": False, "message": "場次不存在。"}
        access, review = exists._mapping["access_code_hash"], exists._mapping["review_password_hash"]
        access = None if data.get("clear_access_code") else hash_password(_clean(data.get("access_code"))) if _clean(data.get("access_code")) else access
        review = None if data.get("clear_review_password") else hash_password(_clean(data.get("review_password"))) if _clean(data.get("review_password")) else review
        params = {"id": match_id, "date": match_date or None, "time": match_time or None, "topic": _clean(data.get("topic_text")), "pro": _clean(data.get("pro_team")), "con": _clean(data.get("con_team")), "access": access, "review": review}
        session.execute(text(f"UPDATE {TABLE_MATCHES} SET match_date=:date, match_time=:time, topic_text=:topic, pro_team=:pro, con_team=:con, access_code_hash=:access, review_password_hash=:review WHERE match_id=:id"), params)
        for side in ("pro", "con"):
            for pos in range(1, 5):
                session.execute(text(f"INSERT INTO {TABLE_DEBATERS} (match_id, side, position, debater_name) VALUES (:id, :side, :pos, :name) ON CONFLICT (match_id, side, position) DO UPDATE SET debater_name=EXCLUDED.debater_name"), {"id": match_id, "side": side, "pos": pos, "name": _clean(data.get(f"{side}_{pos}"))})
    return {"ok": True, "message": f"場次「{match_id}」資料已儲存至資料庫！"}


def draw_topic(difficulty=None, db=None):
    if difficulty not in (None, "", 1, 2, 3, "1", "2", "3"):
        return {"ok": False, "message": "請選擇有效的難度。"}
    db = _resolve_db(db); params = {"difficulty": int(difficulty)} if difficulty else {}
    sql = f"SELECT topic_text FROM {TABLE_TOPICS}" + (" WHERE difficulty = :difficulty" if difficulty else "")
    rows = db.query(sql, params)
    if rows.empty: return {"ok": False, "message": "抽取辯題失敗：辯題庫為空或出現錯誤。"}
    return {"ok": True, "topic": random.choice(rows["topic_text"].tolist())}


def draw_sides(team1, team2):
    if not _clean(team1) or not _clean(team2): return {"ok": False, "message": "請輸入兩隊隊伍名稱。"}
    pro, con = random.sample([_clean(team1), _clean(team2)], 2)
    return {"ok": True, "pro_team": pro, "con_team": con}


def regenerate_link(match_id, side, db=None):
    if side not in ("pro", "con"): return {"ok": False}
    db = _resolve_db(db); ensure_match_links(match_id, db); token = secrets.token_urlsafe(32)
    db.execute(f"UPDATE {TABLE_MATCH_ROSTER_LINKS} SET roster_token=:token, submitted_at=NULL, created_at=:now WHERE match_id=:id AND side=:side", {"token": token, "now": _now(), "id": match_id, "side": side})
    return {"ok": True, "token": token, "message": f"已重新生成{'正方' if side == 'pro' else '反方'}填寫連結，舊連結將不能使用。"}


def reopen_link(match_id, side, db=None):
    if side not in ("pro", "con"): return {"ok": False}
    db = _resolve_db(db); db.execute(f"UPDATE {TABLE_MATCH_ROSTER_LINKS} SET submitted_at=NULL WHERE match_id=:id AND side=:side", {"id": match_id, "side": side})
    return {"ok": True, "message": f"已重開{'正方' if side == 'pro' else '反方'}填寫連結。"}


def delete_match(match_id, db=None):
    changed = _resolve_db(db).execute_count(f"DELETE FROM {TABLE_MATCHES} WHERE match_id = :id", {"id": _clean(match_id)})
    if not changed: return {"ok": False, "message": "場次不存在或已被刪除。"}
    return {"ok": True, "message": f"已成功刪除場次 「{match_id}」 及其所有相關評分記錄。"}


def roster_by_token(token, db=None):
    token = _clean(token); db = _resolve_db(db); ensure_roster_links(db)
    rows = db.query(f"SELECT l.match_id,l.side,l.roster_token,l.submitted_at,m.match_date,m.match_time,m.topic_text,m.pro_team,m.con_team FROM {TABLE_MATCH_ROSTER_LINKS} l JOIN {TABLE_MATCHES} m ON l.match_id=m.match_id WHERE l.roster_token=:token", {"token": token})
    if rows.empty: return None
    row = rows.iloc[0]; side = _clean(row["side"]); match_id = _clean(row["match_id"])
    record = {"match_id":match_id,"side":side,"side_label":"正方" if side=="pro" else "反方","submitted":_has(row.get("submitted_at")),"match_date":_date(row.get("match_date")),"match_time":_time(row.get("match_time")),"topic_text":_clean(row.get("topic_text")),"team_name":_clean(row.get("pro_team") if side=="pro" else row.get("con_team"))}
    debaters = db.query(f"SELECT position,debater_name FROM {TABLE_DEBATERS} WHERE match_id=:id AND side=:side", {"id":match_id,"side":side})
    for pos in range(1,5): record[f"debater_{pos}"]=""
    for _, d in debaters.iterrows(): record[f"debater_{int(d['position'])}"]=_clean(d["debater_name"])
    return record


def save_roster(token, data, db=None):
    roster = roster_by_token(token, db)
    if not roster: return {"ok":False,"reason":"invalid"}
    if roster["submitted"]: return {"ok":False,"reason":"submitted"}
    clean = {key:_clean(data.get(key)) for key in ("team_name","debater_1","debater_2","debater_3","debater_4")}
    missing=[label for label,key in (("隊名","team_name"),("主辯","debater_1"),("一副","debater_2"),("二副","debater_3"),("結辯","debater_4")) if not clean[key]]
    if missing: return {"ok":False,"reason":"validation","message":"請填寫所有必填資料："+"、".join(missing)}
    db=_resolve_db(db)
    col="pro_team" if roster["side"]=="pro" else "con_team"
    with db.transaction() as session:
        claimed=session.execute(text(f"UPDATE {TABLE_MATCH_ROSTER_LINKS} SET submitted_at=:now WHERE roster_token=:token AND submitted_at IS NULL"), {"now":_now(),"token":token}).rowcount
        if not claimed: return {"ok":False,"reason":"submitted"}
        session.execute(text(f"UPDATE {TABLE_MATCHES} SET {col}=:name WHERE match_id=:id"), {"name":clean["team_name"],"id":roster["match_id"]})
        for pos in range(1,5):
            session.execute(text(f"INSERT INTO {TABLE_DEBATERS} (match_id,side,position,debater_name) VALUES (:id,:side,:pos,:name) ON CONFLICT (match_id,side,position) DO UPDATE SET debater_name=EXCLUDED.debater_name"), {"id":roster["match_id"],"side":roster["side"],"pos":pos,"name":clean[f"debater_{pos}"]})
    return {"ok":True,"match_id":roster["match_id"],"side":roster["side"]}
