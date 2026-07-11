"""Public, read-only data endpoint for the HTML topic bank."""

import time

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/open-db", tags=["open-db"])

_CACHE_TTL_SECONDS = 60
_cache = {"expires_at": 0.0, "payload": None}


@router.get("/data")
def open_db_data():
    """Return only the fields and aggregates rendered by ``open_db.py``."""
    now = time.monotonic()
    if _cache["payload"] is not None and now < _cache["expires_at"]:
        return _cache["payload"]
    try:
        from deploy.proxy import get_vote_db
        from core import open_db_logic as logic

        topics, vote_stats = logic.fetch_open_db_data(db=get_vote_db())
        topics = logic.with_difficulty_label(topics)
        payload = {
            "topics": logic.dataframe_records(topics),
            "filters": logic.filter_options(topics) if not topics.empty else {
                "authors": ["全部"], "categories": ["全部"], "difficulties": ["全部"]
            },
            "category_distribution": logic.dataframe_records(logic.category_distribution(topics)),
            "difficulty_distribution": logic.dataframe_records(logic.difficulty_distribution(topics)),
            "category_vote_pass_rate": logic.dataframe_records(logic.category_vote_pass_rate(vote_stats)),
        }
    except Exception as exc:
        raise HTTPException(503, f"連線錯誤: {exc}") from exc
    _cache.update({"expires_at": now + _CACHE_TTL_SECONDS, "payload": payload})
    return payload

