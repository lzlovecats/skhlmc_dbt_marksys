"""Streamlit-free competition registration rules and persistence."""

import datetime as dt
import re
from zoneinfo import ZoneInfo

import pandas as pd

from core.auth_logic import verify_password
from core.vote_logic import _resolve_db
from schema import (
    CREATE_COMPETITION_REGISTRATIONS,
    CREATE_COMPETITION_REGISTRATION_SETTINGS,
    TABLE_COMPETITION_REGISTRATIONS,
    TABLE_COMPETITION_REGISTRATION_SETTINGS,
    TABLE_LOGIN_RECORDS,
)


HKT = ZoneInfo("Asia/Hong_Kong")
STATUS_LABELS = {
    "submitted": "已提交",
    "contacted": "已聯絡",
    "confirmed": "已確認",
    "withdrawn": "已退出",
}
REQUIRED_FIELDS = {
    "隊名": "team_name",
    "主辯姓名": "main_debater_name",
    "一副姓名": "first_deputy_name",
    "二副姓名": "second_deputy_name",
    "結辯姓名": "closing_debater_name",
    "聯絡人姓名": "contact_name",
    "聯絡人班別": "contact_class",
    "聯絡電話號碼": "contact_phone",
}


def _now():
    return dt.datetime.now(HKT).replace(tzinfo=None)


def _coerce_datetime(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if getattr(value, "tzinfo", None) is not None:
        value = value.astimezone(HKT).replace(tzinfo=None)
    return value


def format_datetime(value):
    value = _coerce_datetime(value)
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def json_datetime(value):
    value = _coerce_datetime(value)
    return value.isoformat() if value else None


def ensure_registration_tables(db=None):
    db = _resolve_db(db)
    db.execute(CREATE_COMPETITION_REGISTRATION_SETTINGS)
    db.execute(CREATE_COMPETITION_REGISTRATIONS)


def get_registration_settings(db=None):
    db = _resolve_db(db)
    ensure_registration_tables(db)
    result = db.query(
        f"""
        SELECT competition_edition, registration_start, registration_end, updated_at
        FROM {TABLE_COMPETITION_REGISTRATION_SETTINGS}
        WHERE id = 1
        """
    )
    if result.empty:
        return None
    row = result.iloc[0]
    return {
        "competition_edition": int(row["competition_edition"]),
        "registration_start": _coerce_datetime(row["registration_start"]),
        "registration_end": _coerce_datetime(row["registration_end"]),
        "updated_at": _coerce_datetime(row.get("updated_at")),
    }


def get_registration_status(db=None):
    settings = get_registration_settings(db)
    now = _now()
    if not settings:
        return {"settings": None, "is_open": False, "now": now, "message": "尚未設定報名時間。"}
    start, end = settings.get("registration_start"), settings.get("registration_end")
    if start is None or end is None:
        return {"settings": settings, "is_open": False, "now": now, "message": "報名時間設定不完整。"}
    if now < start:
        return {"settings": settings, "is_open": False, "now": now, "message": "報名尚未開始。"}
    if now > end:
        return {"settings": settings, "is_open": False, "now": now, "message": "報名已截止。"}
    return {"settings": settings, "is_open": True, "now": now, "message": "報名現正開放。"}


def registration_status_payload(db=None):
    status = get_registration_status(db)
    settings = status["settings"]
    return {
        "is_open": status["is_open"],
        "message": status["message"],
        "settings": None if not settings else {
            "competition_edition": settings["competition_edition"],
            "registration_start": json_datetime(settings["registration_start"]),
            "registration_end": json_datetime(settings["registration_end"]),
        },
    }


def clean_registration(data):
    return {key: str(data.get(key) or "").strip() for key in REQUIRED_FIELDS.values()}


def validate_registration(data):
    missing = [label for label, key in REQUIRED_FIELDS.items() if not data.get(key)]
    if missing:
        return f"請填寫所有必填資料：{'、'.join(missing)}"
    if not re.fullmatch(r"\d{8}", data["contact_phone"]):
        return "請輸入有效的8位電話號碼。"
    return None


def submit_registration(data, competition_edition, db=None):
    """Apply the public form's validation, time-window and duplicate checks."""
    db = _resolve_db(db)
    form_data = clean_registration(data)
    error = validate_registration(form_data)
    if error:
        return {"ok": False, "message": error}

    latest_status = get_registration_status(db)
    if not latest_status["is_open"]:
        return {"ok": False, "message": "報名時間已關閉，未能提交報名。"}
    current_edition = int(latest_status["settings"]["competition_edition"])
    try:
        submitted_edition = int(competition_edition)
    except (TypeError, ValueError):
        submitted_edition = -1
    if submitted_edition != current_edition:
        return {"ok": False, "message": "報名屆數已更新，請重新載入頁面後再提交。"}

    duplicate = db.query(
        f"""
        SELECT 1 FROM {TABLE_COMPETITION_REGISTRATIONS}
        WHERE competition_edition = :competition_edition AND team_name = :team_name
        """,
        {"competition_edition": current_edition, "team_name": form_data["team_name"]},
    )
    if not duplicate.empty:
        return {"ok": False, "message": "此隊名已於本屆提交報名，請勿重覆提交。"}

    now = _now()
    try:
        db.execute(
            f"""
            INSERT INTO {TABLE_COMPETITION_REGISTRATIONS} (
                competition_edition, team_name, main_debater_name, first_deputy_name,
                second_deputy_name, closing_debater_name, contact_name, contact_class,
                contact_phone, status, submitted_at, updated_at
            ) VALUES (
                :competition_edition, :team_name, :main_debater_name, :first_deputy_name,
                :second_deputy_name, :closing_debater_name, :contact_name, :contact_class,
                :contact_phone, 'submitted', :submitted_at, :updated_at
            )
            """,
            {"competition_edition": current_edition, **form_data,
             "submitted_at": now, "updated_at": now},
        )
    except Exception as exc:
        error_text = str(exc).lower()
        if "duplicate key" in error_text or "unique" in error_text:
            return {"ok": False, "message": "此隊名已於本屆提交報名，請勿重覆提交。"}
        return {"ok": False, "message": f"提交報名失敗：{exc}"}
    return {"ok": True, "team_name": form_data["team_name"]}


def _settings_payload(settings):
    if not settings:
        return None
    return {
        "competition_edition": settings["competition_edition"],
        "registration_start": json_datetime(settings["registration_start"]),
        "registration_end": json_datetime(settings["registration_end"]),
        "registration_start_display": format_datetime(settings["registration_start"]),
        "registration_end_display": format_datetime(settings["registration_end"]),
    }


def _record_payload(row):
    status = str(row.get("status") or "submitted")
    return {
        "id": int(row["id"]), "competition_edition": int(row["competition_edition"]),
        "team_name": str(row.get("team_name") or ""),
        "main_debater_name": str(row.get("main_debater_name") or ""),
        "first_deputy_name": str(row.get("first_deputy_name") or ""),
        "second_deputy_name": str(row.get("second_deputy_name") or ""),
        "closing_debater_name": str(row.get("closing_debater_name") or ""),
        "contact_name": str(row.get("contact_name") or ""),
        "contact_class": str(row.get("contact_class") or ""),
        "contact_phone": str(row.get("contact_phone") or ""),
        "status": status, "status_label": STATUS_LABELS.get(status, status),
        "submitted_at": format_datetime(row.get("submitted_at")),
        "updated_at": format_datetime(row.get("updated_at")),
    }


def registration_admin_data(competition_edition=None, status="全部", db=None):
    db = _resolve_db(db)
    settings = get_registration_settings(db)
    editions_data = db.query(
        f"SELECT DISTINCT competition_edition FROM {TABLE_COMPETITION_REGISTRATIONS} ORDER BY competition_edition DESC"
    )
    editions = [int(value) for value in editions_data["competition_edition"].tolist()] if not editions_data.empty else []
    if settings and settings["competition_edition"] not in editions:
        editions.insert(0, settings["competition_edition"])
    if not editions:
        editions = [1]
    selected_edition = int(competition_edition) if competition_edition in editions else editions[0]
    params = {"competition_edition": selected_edition}
    where_sql = "WHERE competition_edition = :competition_edition"
    if status in STATUS_LABELS:
        where_sql += " AND status = :status"
        params["status"] = status
    items = []
    count_rows = db.query(f"SELECT status,COUNT(*) AS count FROM {TABLE_COMPETITION_REGISTRATIONS} WHERE competition_edition=:competition_edition GROUP BY status", {"competition_edition":selected_edition})
    counts = {key: 0 for key in STATUS_LABELS}
    for _, row in count_rows.iterrows():
        if row["status"] in counts: counts[row["status"]] = int(row["count"])
    return {
        "settings": _settings_payload(settings), "now": json_datetime(_now()),
        "default_end": json_datetime(_now() + dt.timedelta(days=14)),
        "editions": editions, "selected_edition": selected_edition,
        "selected_status": status if status in STATUS_LABELS else "全部",
        "status_labels": STATUS_LABELS, "status_counts": counts, "registrations": items,
    }


def save_registration_settings(competition_edition, registration_start, registration_end, db=None):
    db = _resolve_db(db)
    try:
        competition_edition = int(competition_edition)
    except (TypeError, ValueError):
        return {"ok": False, "message": "比賽屆數必須為正整數。"}
    if competition_edition < 1:
        return {"ok": False, "message": "比賽屆數必須為正整數。"}
    try:
        start = dt.datetime.fromisoformat(str(registration_start))
        end = dt.datetime.fromisoformat(str(registration_end))
    except ValueError:
        return {"ok": False, "message": "請輸入有效的報名開始及截止時間。"}
    if end <= start:
        return {"ok": False, "message": "報名截止時間必須遲於開始時間。"}
    ensure_registration_tables(db)
    db.execute(
        f"""
        INSERT INTO {TABLE_COMPETITION_REGISTRATION_SETTINGS}
            (id, competition_edition, registration_start, registration_end, updated_at)
        VALUES (1, :competition_edition, :registration_start, :registration_end, :updated_at)
        ON CONFLICT (id) DO UPDATE SET
            competition_edition = EXCLUDED.competition_edition,
            registration_start = EXCLUDED.registration_start,
            registration_end = EXCLUDED.registration_end,
            updated_at = EXCLUDED.updated_at
        """,
        {"competition_edition": competition_edition, "registration_start": start,
         "registration_end": end, "updated_at": _now()},
    )
    return {"ok": True, "message": "報名設定已更新。"}


def update_registration_status(registration_id, status, db=None):
    if status not in STATUS_LABELS:
        return {"ok": False, "message": "請選擇有效的新狀態。"}
    db = _resolve_db(db)
    changed = db.execute_count(
        f"UPDATE {TABLE_COMPETITION_REGISTRATIONS} SET status = :status, updated_at = :updated_at WHERE id = :id",
        {"status": status, "updated_at": _now(), "id": int(registration_id)},
    )
    if not changed:
        return {"ok": False, "message": "找不到指定的報名紀錄。"}
    return {"ok": True, "message": "報名狀態已更新。"}


def check_admin_password(password, db=None):
    db = _resolve_db(db)
    result = db.query("SELECT value FROM system_config WHERE key = 'admin_password'")
    if result.empty:
        return {"ok": False, "message": "系統錯誤：未能讀取密碼，請聯絡開發人員"}
    if not verify_password((password or "").strip(), str(result.iloc[0]["value"])):
        return {"ok": False, "message": "密碼錯誤"}
    db.execute(
        f"INSERT INTO {TABLE_LOGIN_RECORDS} (user_id, login_type, logged_in_at) VALUES ('admin', 'admin', :logged_in_at)",
        {"logged_in_at": _now().strftime("%Y-%m-%d %H:%M:%S")},
    )
    return {"ok": True}
