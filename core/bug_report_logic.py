"""Validation and persistence for committee bug reports."""

import re
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.vote_logic import _resolve_db
from schema import CREATE_BUG_REPORTS, TABLE_BUG_REPORTS
from system_limits import (
    BUG_REPORT_MAX_PER_USER_DAY, BUG_REPORT_MAX_TOTAL,
    BUG_REPORT_RATE_WINDOW_HOURS, BUG_REPORT_RECENT_LIMIT,
)


STATUS_LABELS = {
    "open": "待處理",
    "investigating": "跟進中",
    "fixed": "已修正",
    "not_reproducible": "未能重現",
    "duplicate": "重複回報",
    "closed": "已關閉",
}
PAGE_OPTIONS = [
    "主頁",
    "辯題徵集、投票及罷免",
    "AI 辯論易",
    "聖呂中辯AI訓練",
    "比賽片段重溫",
    "比賽圖片回顧",
    "遲到罰款基金",
    "其他",
]
VAGUE_PATTERNS = [
    r"有\s*bug", r"用\s*唔\s*到", r"壞\s*咗", r"出\s*錯",
    r"唔\s*得", r"有\s*問題", r"唔\s*work", r"唔\s*正常",
]
MIN_STEPS_LEN = 15
_schema_lock = threading.Lock()
_schema_ready = False


def ensure_bug_reports_table(db=None):
    """Create the legacy-compatible table once per worker."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        db = _resolve_db(db)
        db.execute(CREATE_BUG_REPORTS)
        _schema_ready = True


def plain_len(text):
    return len(re.sub(r"\s+", "", text or ""))


def is_too_vague(text):
    cleaned = re.sub(r"\s+", "", text or "")
    if len(cleaned) < MIN_STEPS_LEN:
        return True
    concrete = cleaned
    for pattern in VAGUE_PATTERNS:
        concrete = re.sub(pattern, "", concrete)
    return len(concrete) < 8


def validate_report(affected_page, steps, expected, actual):
    errors = []
    if not (affected_page or "").strip():
        errors.append("請選擇或填寫受影響頁面。")
    if is_too_vague(steps):
        errors.append("請用具體步驟寫明點樣重現，例如：先去邊頁、撳邊個掣、輸入咩內容、之後發生咩事。")
    if plain_len(actual) < 15:
        errors.append("請具體描述實際出現的錯誤或異常畫面。")
    if plain_len(expected) < 8:
        errors.append("請寫明正常情況下你預期系統應該點樣運作。")
    return errors


def submit_report(user_id, affected_page, device_info, reproduction_steps, expected_result, actual_result, extra_notes, db=None):
    db = _resolve_db(db)
    ensure_bug_reports_table(db)
    affected_page = (affected_page or "").strip()
    reproduction_steps = (reproduction_steps or "").strip()
    expected_result = (expected_result or "").strip()
    actual_result = (actual_result or "").strip()
    errors = validate_report(affected_page, reproduction_steps, expected_result, actual_result)
    if errors:
        return {"ok": False, "errors": errors}
    cutoff = datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None) - timedelta(
        hours=BUG_REPORT_RATE_WINDOW_HOURS
    )
    counts = db.query(f"""SELECT COUNT(*) AS total,
        COUNT(*) FILTER (WHERE reporter_user_id=:uid AND created_at>=:cutoff) AS user_day
        FROM {TABLE_BUG_REPORTS}""", {"uid": user_id, "cutoff": cutoff})
    if not counts.empty:
        if int(counts.iloc[0]["total"] or 0) >= BUG_REPORT_MAX_TOTAL:
            return {"ok": False, "errors": ["問題回報已達保護上限，請聯絡開發者整理舊紀錄。"]}
        if int(counts.iloc[0]["user_day"] or 0) >= BUG_REPORT_MAX_PER_USER_DAY:
            return {"ok": False, "errors": ["你今日提交的問題回報已達上限，請翌日再試。"]}
    now = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        f"""
        INSERT INTO {TABLE_BUG_REPORTS}
            (reporter_user_id, affected_page, device_info, reproduction_steps,
             expected_result, actual_result, extra_notes, status, created_at, updated_at)
        VALUES
            (:uid, :page, :device, :steps, :expected, :actual, :notes, 'open', :now, :now)
        """,
        {
            "uid": user_id, "page": affected_page[:120], "device": (device_info or "").strip()[:1000],
            "steps": reproduction_steps[:5000], "expected": expected_result[:3000], "actual": actual_result[:5000],
            "notes": (extra_notes or "").strip()[:3000], "now": now,
        },
    )
    return {"ok": True, "errors": []}


def _format_time(value):
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value or "")[:16]


def reports_for_user(user_id, db=None):
    db = _resolve_db(db)
    ensure_bug_reports_table(db)
    reports = db.query(
        f"""
        SELECT id, affected_page, device_info, reproduction_steps, expected_result,
               actual_result, extra_notes, status, developer_reply, fixed_version,
               created_at, updated_at, resolved_at
        FROM {TABLE_BUG_REPORTS}
        WHERE reporter_user_id = :uid
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        {"uid": user_id, "limit": BUG_REPORT_RECENT_LIMIT},
    )
    items = []
    for _, row in reports.iterrows():
        status = str(row.get("status") or "open")
        items.append({
            "id": int(row["id"]), "affected_page": str(row.get("affected_page") or ""),
            "device_info": str(row.get("device_info") or ""),
            "reproduction_steps": str(row.get("reproduction_steps") or ""),
            "expected_result": str(row.get("expected_result") or ""),
            "actual_result": str(row.get("actual_result") or ""),
            "extra_notes": str(row.get("extra_notes") or ""), "status": status,
            "developer_reply": str(row.get("developer_reply") or ""),
            "fixed_version": str(row.get("fixed_version") or ""),
            "created_at": _format_time(row.get("created_at")),
            "updated_at": _format_time(row.get("updated_at")),
            "resolved_at": _format_time(row.get("resolved_at")),
        })
    return items
