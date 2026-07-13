"""Public, read-only data endpoint for the HTML topic bank."""

import hashlib
import json
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response
from system_limits import OPEN_DB_CACHE_TTL_SECONDS, OPEN_DB_STALE_REVALIDATE_SECONDS

router = APIRouter(prefix="/api/open-db", tags=["open-db"])

_CACHE_TTL_SECONDS = OPEN_DB_CACHE_TTL_SECONDS
_cache = {"expires_at": 0.0, "payload": None, "etag": ""}
_cache_lock = threading.Lock()


def _cache_headers(response: Response, etag: str):
    response.headers["Cache-Control"] = (
        f"public, max-age={OPEN_DB_CACHE_TTL_SECONDS}, "
        f"stale-while-revalidate={OPEN_DB_STALE_REVALIDATE_SECONDS}"
    )
    response.headers["ETag"] = etag


@router.get("/data")
def open_db_data(request: Request, response: Response):
    """Return only the fields and aggregates rendered by the public page."""
    now = time.monotonic()
    if _cache["payload"] is not None and now < _cache["expires_at"]:
        if request.headers.get("if-none-match") == _cache["etag"]:
            return Response(status_code=304, headers={
                "Cache-Control": (
                    f"public, max-age={OPEN_DB_CACHE_TTL_SECONDS}, "
                    f"stale-while-revalidate={OPEN_DB_STALE_REVALIDATE_SECONDS}"
                ),
                "ETag": _cache["etag"],
            })
        _cache_headers(response, _cache["etag"])
        return _cache["payload"]
    # A cold worker or an expired TTL can receive several concurrent public
    # requests. Only one of them should rebuild the large topic payload.
    with _cache_lock:
        now = time.monotonic()
        if _cache["payload"] is not None and now < _cache["expires_at"]:
            etag = _cache["etag"]
            payload = _cache["payload"]
        else:
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
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            etag = '"' + hashlib.sha256(encoded).hexdigest() + '"'
            _cache.update({"expires_at": now + _CACHE_TTL_SECONDS, "payload": payload, "etag": etag})
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={
            "Cache-Control": (
                f"public, max-age={OPEN_DB_CACHE_TTL_SECONDS}, "
                f"stale-while-revalidate={OPEN_DB_STALE_REVALIDATE_SECONDS}"
            ),
            "ETag": etag,
        })
    _cache_headers(response, etag)
    return payload
