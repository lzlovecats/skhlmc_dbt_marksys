"""Shared response/export limits for the small Render worker.

The limits are intentionally enforced before a response starts.  Silently
truncating a CSV/JSONL download would look like a valid backup, so oversized
exports fail clearly and can be split by a narrower filter instead.
"""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from fastapi import HTTPException, Response
from system_limits import EXPORT_MAX_BYTES, EXPORT_MAX_ROWS


def _account_export_bytes(byte_count: int) -> None:
    """Put bounded downloads on the same monthly Render egress budget."""
    try:
        from deploy.proxy import (
            _bandwidth_essential_gate_error,
            record_bandwidth_usage,
        )
        budget_error = _bandwidth_essential_gate_error()
        if budget_error:
            raise HTTPException(429, budget_error)
        record_bandwidth_usage(
            "bounded_export", byte_count, aggregate_key="all_csv_jsonl_exports"
        )
    except HTTPException:
        raise
    except Exception:
        # The endpoint's own DB query has already succeeded. A transient
        # accounting failure must not turn a valid small backup into data loss.
        pass


def require_row_limit(rows, *, limit: int = EXPORT_MAX_ROWS, label: str = "匯出"):
    """Reject a ``LIMIT limit+1`` result instead of returning a partial backup."""
    if len(rows) > limit:
        raise HTTPException(413, f"{label}超過每次 {limit} 行保護上限，請縮窄篩選範圍後分批下載。")
    return rows


def csv_response(filename: str, headers, rows, *, max_bytes: int = EXPORT_MAX_BYTES):
    """Build a bounded UTF-8 CSV and reject it before sending if it is too large."""
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    encoded = ("\ufeff" + stream.getvalue()).encode("utf-8")
    if len(encoded) > max_bytes:
        raise HTTPException(413, f"匯出檔案超過每次 {max_bytes // (1024 * 1024)}MB 保護上限，請分批下載。")
    _account_export_bytes(len(encoded))
    return Response(encoded, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "X-Export-Row-Count": str(len(rows)),
    })


def jsonl_response(filename: str, rows, *, max_bytes: int = EXPORT_MAX_BYTES):
    lines = [json.dumps(row, ensure_ascii=False, default=str) for row in rows]
    encoded = "\n".join(lines).encode("utf-8")
    if len(encoded) > max_bytes:
        raise HTTPException(413, f"匯出檔案超過每次 {max_bytes // (1024 * 1024)}MB 保護上限，請分批下載。")
    _account_export_bytes(len(encoded))
    return Response(encoded, media_type="application/x-ndjson; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename={filename}",
        "X-Export-Row-Count": str(len(rows)),
    })
