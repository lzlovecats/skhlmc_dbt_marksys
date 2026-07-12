"""Small, consistent pagination helpers for JSON collection endpoints."""

import math

PAGE_SIZE = 20


def bounds(page: int):
    page = max(1, int(page or 1))
    return page, PAGE_SIZE, (page - 1) * PAGE_SIZE


def json_safe(value):
    """Normalise pandas/numpy database values before FastAPI serialisation."""
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            return None
    try:
        if value != value:
            return None
    except (TypeError, ValueError):
        pass
    return value


def payload(items, page: int, total: int):
    page = max(1, int(page or 1)); total = max(0, int(total or 0))
    return {"items": json_safe(items), "page": page, "page_size": PAGE_SIZE, "total": total,
            "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)}


def scalar_count(db, sql: str, params=None):
    frame = db.query(sql, params or {})
    return 0 if frame.empty else int(frame.iloc[0]["total"] or 0)
