"""Authoritative schedule for temporarily pausing Vote and AI Coach."""

from __future__ import annotations

import datetime as dt
import math
import re
from zoneinfo import ZoneInfo

from core.config_store import get_configs


HKT = ZoneInfo("Asia/Hong_Kong")
SUSPENSION_START_KEY = "interactive_features_suspension_start"
SUSPENSION_END_KEY = "interactive_features_suspension_end"
SUSPENSION_KEYS = (SUSPENSION_START_KEY, SUSPENSION_END_KEY)
_LOCAL_MINUTE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _now_hkt(now: dt.datetime | None = None) -> dt.datetime:
    value = now or dt.datetime.now(HKT)
    if value.tzinfo is None:
        return value.replace(tzinfo=HKT)
    return value.astimezone(HKT)


def _parse_hkt_minute(value: object) -> dt.datetime | None:
    text = str(value or "").strip()
    if not _LOCAL_MINUTE.fullmatch(text):
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return None
    return parsed.replace(tzinfo=HKT)


def validate_suspension_window(
    start: object,
    end: object,
    *,
    now: dt.datetime | None = None,
) -> tuple[str, str]:
    """Validate and normalise a developer-supplied HKT minute interval."""

    start_text = str(start or "").strip()
    end_text = str(end or "").strip()
    if not start_text and not end_text:
        return "", ""
    if not start_text or not end_text:
        raise ValueError("請同時設定停用開始及結束時間")
    parsed_start = _parse_hkt_minute(start_text)
    parsed_end = _parse_hkt_minute(end_text)
    if parsed_start is None or parsed_end is None:
        raise ValueError("停用時間須使用有效的香港本地日期及時間")
    if parsed_end <= parsed_start:
        raise ValueError("停用結束時間必須遲於開始時間")
    if parsed_end <= _now_hkt(now):
        raise ValueError("停用結束時間必須在未來")
    return (
        parsed_start.strftime("%Y-%m-%dT%H:%M"),
        parsed_end.strftime("%Y-%m-%dT%H:%M"),
    )


def _public_time(value: dt.datetime) -> str:
    return f"{value.year}年{value.month}月{value.day}日 {value:%H:%M}（香港時間）"


def suspension_status(db, now: dt.datetime | None = None) -> dict:
    """Return one fail-open, read-only view of the configured interval."""

    try:
        values = get_configs(db, SUSPENSION_KEYS)
    except Exception:
        values = {}
    start = _parse_hkt_minute(values.get(SUSPENSION_START_KEY))
    end = _parse_hkt_minute(values.get(SUSPENSION_END_KEY))
    current = _now_hkt(now)
    if start is None or end is None or end <= start:
        return {
            "configured": False,
            "scheduled": False,
            "active": False,
            "start": "",
            "end": "",
            "message": "",
            "retry_after_seconds": 0,
        }
    active = start <= current < end
    scheduled = current < start
    return {
        "configured": True,
        "scheduled": scheduled,
        "active": active,
        "start": start.strftime("%Y-%m-%dT%H:%M"),
        "end": end.strftime("%Y-%m-%dT%H:%M"),
        "message": (
            f"Vote 及 AI 辯論易暫停使用，將於{_public_time(end)}自動恢復。"
            if active
            else ""
        ),
        "retry_after_seconds": (
            max(1, math.ceil((end - current).total_seconds())) if active else 0
        ),
    }
