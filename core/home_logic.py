"""Streamlit-free data and content logic for the HTML home page."""

import datetime as dt
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from schema import (
    CREATE_COMPETITION_REGISTRATIONS,
    CREATE_COMPETITION_REGISTRATION_SETTINGS,
    TABLE_ACCOUNTS,
    TABLE_LOGIN_RECORDS,
    TABLE_MATCHES,
    TABLE_SCORES,
    TABLE_TOPICS,
    TABLE_TOPIC_VOTES,
)
from system_limits import HOME_ACTIVE_MEMBER_WINDOW_HOURS
from core.vote_logic import _resolve_db
from version import APP_VERSION


HKT = ZoneInfo("Asia/Hong_Kong")
ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"

MANUAL_ROLE_SECTIONS = {
    "評判": "一、評判",
    "賽會人員": "二、賽會人員",
    "參賽隊伍": "三、參賽隊伍",
    "一般人員": "四、一般人員",
    "內部委員會成員": "五、內部委員會成員",
}
RULES_ROLE_SECTIONS = {
    "評判": "一、評判",
    "賽會人員": "二、賽會人員",
    "參賽隊伍": "三、參賽隊伍",
}


def _get_config(db, key):
    result = db.query("SELECT value FROM system_config WHERE key = :key", {"key": key})
    return None if result.empty else result.iloc[0]["value"]


def is_maintenance_mode(db=None):
    try:
        value = _get_config(_resolve_db(db), "maintenance_mode")
    except Exception:
        return False
    return value is not None and str(value).strip().lower() in ("true", "1", "yes", "on")


def format_maintenance_deadline(value):
    """Format the developer-configured naive Hong Kong time for public display."""
    if not value:
        return ""
    try:
        deadline = dt.datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return ""
    if deadline.tzinfo is not None:
        deadline = deadline.astimezone(HKT).replace(tzinfo=None)
    return f"{deadline.year}年{deadline.month}月{deadline.day}日 {deadline:%H:%M}（香港時間）"


def get_registration_status(db=None):
    """Match the home page's registration-window decision and HKT time handling."""
    db = _resolve_db(db)
    now = dt.datetime.now(HKT).replace(tzinfo=None)
    try:
        # The Streamlit path creates these two tables lazily before reading them.
        db.execute(CREATE_COMPETITION_REGISTRATION_SETTINGS)
        db.execute(CREATE_COMPETITION_REGISTRATIONS)
        result = db.query(
            """
            SELECT competition_edition, registration_start, registration_end, updated_at
            FROM competition_registration_settings WHERE id = 1
            """
        )
    except Exception:
        return {"settings": None, "is_open": False, "message": "尚未設定報名時間。"}
    if result.empty:
        return {"settings": None, "is_open": False, "message": "尚未設定報名時間。"}

    row = result.iloc[0]
    start, end = row["registration_start"], row["registration_end"]
    if hasattr(start, "to_pydatetime"):
        start = start.to_pydatetime()
    if hasattr(end, "to_pydatetime"):
        end = end.to_pydatetime()
    settings = {
        "competition_edition": int(row["competition_edition"]),
        "registration_start": start,
        "registration_end": end,
    }
    if start is None or end is None:
        return {"settings": settings, "is_open": False, "message": "報名時間設定不完整。"}
    if now < start:
        return {"settings": settings, "is_open": False, "message": "報名尚未開始。"}
    if now > end:
        return {"settings": settings, "is_open": False, "message": "報名已截止。"}
    return {"settings": settings, "is_open": True, "message": "報名現正開放。"}


def home_data(db=None):
    db = _resolve_db(db)
    return {
        "version": APP_VERSION,
        "maintenance_mode": is_maintenance_mode(db),
        "maintenance_deadline": format_maintenance_deadline(
            _get_config(db, "maintenance_deadline")
        ),
        "registration": get_registration_status(db),
    }


def run_status_checks(db=None):
    """The five checks and messages previously rendered in ``home.py``."""
    db = _resolve_db(db)
    results = {
        "db_ok": False,
        "db_error": None,
        "table_counts": None,
        "config_admin_ok": False,
        "config_developer_ok": False,
        "pending_votes": None,
        "logins_24h": None,
        "errors": [],
    }
    try:
        db.query("SELECT 1")
        results["db_ok"] = True
    except Exception as exc:
        results["db_error"] = str(exc)
        return results

    try:
        counts = {}
        for table in (TABLE_ACCOUNTS, TABLE_MATCHES, TABLE_SCORES, TABLE_TOPICS):
            data = db.query(f"SELECT COUNT(*) AS cnt FROM {table}")
            counts[table] = int(data.iloc[0]["cnt"]) if not data.empty else 0
        results["table_counts"] = counts
    except Exception as exc:
        results["errors"].append(f"表格計數失敗: {exc}")

    try:
        data = db.query(
            "SELECT key FROM system_config WHERE key IN ('admin_password', 'developer_password')"
        )
        found = set(data["key"].tolist()) if not data.empty else set()
        results["config_admin_ok"] = "admin_password" in found
        results["config_developer_ok"] = "developer_password" in found
    except Exception as exc:
        results["errors"].append(f"系統設定查詢失敗: {exc}")

    try:
        data = db.query(f"SELECT COUNT(*) AS cnt FROM {TABLE_TOPIC_VOTES} WHERE status = 'pending'")
        results["pending_votes"] = int(data.iloc[0]["cnt"]) if not data.empty else 0
    except Exception as exc:
        results["errors"].append(f"辯題投票查詢失敗: {exc}")

    try:
        cutoff = dt.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None) - dt.timedelta(
            hours=HOME_ACTIVE_MEMBER_WINDOW_HOURS
        )
        data = db.query(
            f"SELECT COUNT(*) AS cnt FROM {TABLE_LOGIN_RECORDS} "
            "WHERE logged_in_at >= :cutoff", {"cutoff": cutoff}
        )
        results["logins_24h"] = int(data.iloc[0]["cnt"]) if not data.empty else 0
    except Exception as exc:
        results["errors"].append(f"登入紀錄查詢失敗: {exc}")
    return results


def _read_asset(name):
    try:
        return (ASSETS_DIR / name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"出現錯誤：{name}無法存取。"


def extract_markdown_section(content, heading_level, target_heading):
    prefix = "#" * heading_level
    match = re.search(
        rf"^{re.escape(prefix)}\s+{re.escape(target_heading)}\s*$.*?(?=^{re.escape(prefix)}\s|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(0).strip() if match else None


def manual_for_role(role):
    role = role if role in MANUAL_ROLE_SECTIONS else "評判"
    content = _read_asset("user_manual.md")
    return extract_markdown_section(content, 3, MANUAL_ROLE_SECTIONS[role]) or content


def rules_for_role(role):
    role = role if role in RULES_ROLE_SECTIONS else "評判"
    content = _read_asset("rules.md")
    divider = content.find("---")
    prefix, body = (content[:divider + 3], content[divider + 3:]) if divider != -1 else ("", content)
    section = extract_markdown_section(body, 2, RULES_ROLE_SECTIONS[role]) or body
    return f"{prefix}\n\n{section}".strip()
