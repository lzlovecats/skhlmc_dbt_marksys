"""Monthly system-wide resource and provider limit helpers.

Database rows are authoritative.  A process-local last-known-good cache keeps
the safety gates available during a short database outage; hard-coded defaults
are used only before the first successful read.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text

from schema import TABLE_MONTHLY_RESOURCE_LIMITS


HONG_KONG = ZoneInfo("Asia/Hong_Kong")
GB = 1_000_000_000
DEFAULT_LIMITS = {
    "render_bandwidth": {
        "unit": "bytes", "warning_value": 3 * GB,
        "stop_value": 3_500_000_000, "hard_value": 4 * GB,
    },
    "r2_storage": {
        "unit": "bytes", "warning_value": 7 * GB,
        "stop_value": 8 * GB, "hard_value": 8 * GB,
    },
}

_cache: dict[tuple[str, str], dict] = {}
_cache_lock = threading.Lock()


def current_period_month(now: dt.datetime | None = None) -> dt.date:
    value = now or dt.datetime.now(HONG_KONG)
    if value.tzinfo is None:
        value = value.replace(tzinfo=HONG_KONG)
    else:
        value = value.astimezone(HONG_KONG)
    return value.date().replace(day=1)


def _number(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = float(value)
    number = float(value)
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _json_value(value):
    """Convert database/pandas values into standard JSON-compatible values."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        exponent = value.as_tuple().exponent
        return int(value) if isinstance(exponent, int) and exponent >= 0 else float(value)
    value_type = type(value)
    if (
        value_type.__module__.startswith("pandas.")
        and value_type.__name__ in {"NAType", "NaTType"}
    ):
        return None
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _normalise(row: dict, *, fallback: dict | None = None) -> dict:
    result = dict(fallback or {})
    for key, value in row.items():
        result[key] = _number(value) if key.endswith("_value") or key in {
            "allocated_hkd", "fx_hkd_per_usd",
        } else _json_value(value)
    return result


def _fallback(period: dt.date, key: str) -> dict:
    values = DEFAULT_LIMITS.get(key, {"unit": "hkd"})
    return {
        "period_month": period.isoformat(),
        "limit_key": key,
        **values,
        "allocated_hkd": None,
        "fx_hkd_per_usd": None,
        "external_cap_confirmed": False,
        "source": "safe_default",
    }


def ensure_default_rows(db, period: dt.date | None = None) -> None:
    period = period or current_period_month()
    with db.transaction() as session:
        for key, values in DEFAULT_LIMITS.items():
            session.execute(text(f"""INSERT INTO {TABLE_MONTHLY_RESOURCE_LIMITS}
                (period_month,limit_key,unit,warning_value,stop_value,hard_value)
                VALUES(:month,:key,:unit,:warning,:stop,:hard)
                ON CONFLICT(period_month,limit_key) DO NOTHING"""), {
                "month": period, "key": key, "unit": values["unit"],
                "warning": values["warning_value"],
                "stop": values["stop_value"], "hard": values["hard_value"],
            })
        session.execute(text(f"""DELETE FROM {TABLE_MONTHLY_RESOURCE_LIMITS}
            WHERE period_month < :current_month
              AND (period_month + INTERVAL '1 month') < :cutoff"""), {
            "current_month": period,
            "cutoff": dt.datetime.now(HONG_KONG).replace(tzinfo=None)
            - dt.timedelta(days=62),
        })


def get_monthly_limit(db, key: str, period: dt.date | None = None) -> dict:
    period = period or current_period_month()
    cache_key = (period.isoformat(), str(key))
    try:
        ensure_default_rows(db, period)
        frame = db.query(f"""SELECT * FROM {TABLE_MONTHLY_RESOURCE_LIMITS}
            WHERE period_month=:month AND limit_key=:key""", {
            "month": period, "key": str(key),
        })
        if not frame.empty:
            value = _normalise(dict(frame.iloc[0]))
            value["period_month"] = str(value["period_month"])
            value["source"] = "database"
            with _cache_lock:
                _cache[cache_key] = dict(value)
            return value
    except Exception:
        pass
    with _cache_lock:
        cached = _cache.get(cache_key)
    if cached:
        return {**cached, "source": "last_known_cache"}
    return _fallback(period, str(key))


def system_limits_payload(db) -> dict:
    render = get_monthly_limit(db, "render_bandwidth")
    r2 = get_monthly_limit(db, "r2_storage")
    providers = []
    try:
        period = current_period_month()
        frame = db.query(f"""SELECT * FROM {TABLE_MONTHLY_RESOURCE_LIMITS}
            WHERE period_month=:month AND limit_key LIKE 'provider:%'
            ORDER BY limit_key""", {"month": period})
        providers = [_normalise(dict(row)) for row in frame.to_dict("records")]
    except Exception:
        providers = []
    return {"render_bandwidth": render, "r2_storage": r2, "providers": providers}
