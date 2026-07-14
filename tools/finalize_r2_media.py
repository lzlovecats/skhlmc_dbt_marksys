#!/usr/bin/env python3
"""Verify durable R2 media before a versioned migration drops legacy BYTEA.

This tool is verification-only. It uses bounded keyset batches and releases
the database connection before issuing R2 HEAD requests. Production DDL must
go through the immutable migration ledger; the legacy ``--apply`` flag is
accepted only to fail closed before any R2 or database access.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import r2_storage
from core.db_runtime import get_db_engine as _get_db_engine
from system_limits import (
    R2_FINALIZER_BATCH_SIZE,
    R2_OBJECT_CACHE_MAX_AGE_SECONDS,
)
from version import APP_VERSION


EXPECTED_CACHE_CONTROL = f"private, max-age={R2_OBJECT_CACHE_MAX_AGE_SECONDS}"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
PHOTO_SQL = """SELECT id,r2_key,thumbnail_r2_key,
       COALESCE(byte_size,0) AS byte_size,
       COALESCE(sha256,'') AS sha256,
       COALESCE(mime_type,'') AS mime_type
FROM match_photos
WHERE id>:after_id
ORDER BY id
LIMIT :limit"""
AUDIO_SQL = """SELECT id,r2_key,
       COALESCE(size_bytes,0) AS size_bytes,
       COALESCE(audio_sha256,'') AS audio_sha256,
       COALESCE(mime_type,'') AS mime_type
FROM tts_voice_recordings
WHERE id>:after_id
ORDER BY id
LIMIT :limit"""


class VerificationError(RuntimeError):
    """A database row and its durable R2 object do not agree."""


def _mime(value: object) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _verify_object(
    key: str,
    *,
    label: str,
    expected_prefix: str,
    expected_mime: str,
    expected_size: int | None = None,
    expected_sha: str | None = None,
) -> int:
    """Validate one HEAD response without returning object-identifying data."""
    clean_key = str(key or "").strip()
    if not clean_key.startswith(expected_prefix):
        raise VerificationError(f"{label}: missing or invalid durable R2 key")
    if expected_size is not None and int(expected_size or 0) <= 0:
        raise VerificationError(f"{label}: database byte size is missing or invalid")
    clean_expected_sha = str(expected_sha or "").strip().lower()
    if expected_sha is not None and not SHA256_PATTERN.fullmatch(clean_expected_sha):
        raise VerificationError(f"{label}: database SHA-256 is missing or invalid")

    try:
        remote = r2_storage.head(clean_key)
    except Exception as exc:
        raise VerificationError(f"{label}: R2 HEAD failed") from exc
    size = int(remote.get("ContentLength") or 0)
    remote_sha = str((remote.get("Metadata") or {}).get("sha256") or "").lower()
    content_type = _mime(remote.get("ContentType"))
    cache_control = str(remote.get("CacheControl") or "").strip().lower()

    if size <= 0:
        raise VerificationError(f"{label}: R2 content length is missing or invalid")
    if expected_size is not None and size != int(expected_size):
        raise VerificationError(f"{label}: R2 content length differs from database")
    if not SHA256_PATTERN.fullmatch(remote_sha):
        raise VerificationError(f"{label}: R2 SHA-256 metadata is missing or invalid")
    if expected_sha is not None and remote_sha != clean_expected_sha:
        raise VerificationError(f"{label}: R2 SHA-256 differs from database")
    if content_type != _mime(expected_mime):
        raise VerificationError(f"{label}: R2 content type differs from database")
    if cache_control != EXPECTED_CACHE_CONTROL.lower():
        raise VerificationError(f"{label}: R2 cache-control metadata is invalid")
    return size


def _photo_verifier(row: dict) -> tuple[int, int]:
    row_id = int(row["id"])
    original_size = _verify_object(
        str(row["r2_key"] or ""),
        label=f"photo {row_id} original",
        expected_prefix="photos/original/",
        expected_mime=str(row["mime_type"] or ""),
        expected_size=int(row["byte_size"] or 0),
        expected_sha=str(row["sha256"] or ""),
    )
    thumbnail_size = _verify_object(
        str(row["thumbnail_r2_key"] or ""),
        label=f"photo {row_id} thumbnail",
        expected_prefix="photos/thumb/",
        expected_mime="image/webp",
    )
    return 2, original_size + thumbnail_size


def _audio_verifier(row: dict) -> tuple[int, int]:
    row_id = int(row["id"])
    size = _verify_object(
        str(row["r2_key"] or ""),
        label=f"audio {row_id}",
        expected_prefix="audio/tts/",
        expected_mime=str(row["mime_type"] or ""),
        expected_size=int(row["size_bytes"] or 0),
        expected_sha=str(row["audio_sha256"] or ""),
    )
    return 1, size


def _verify_rows(
    engine,
    sql: str,
    verifier,
    batch_size: int = R2_FINALIZER_BATCH_SIZE,
) -> dict[str, int]:
    """Read a bounded metadata page, close DB, then perform its R2 HEADs."""
    limit = int(batch_size)
    if limit <= 0:
        raise ValueError("batch_size must be positive")
    after_id = 0
    row_count = object_count = byte_count = 0
    while True:
        with engine.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    text(sql), {"after_id": after_id, "limit": limit}
                ).mappings().all()
            ]
        if not rows:
            break
        for row in rows:
            objects, verified_bytes = verifier(row)
            row_count += 1
            object_count += int(objects)
            byte_count += int(verified_bytes)
        next_id = int(rows[-1]["id"])
        if next_id <= after_id:
            raise VerificationError("database keyset pagination did not advance")
        after_id = next_id
    return {
        "rows": row_count,
        "objects": object_count,
        "bytes": byte_count,
    }


def verify_media(engine) -> dict[str, dict[str, int]]:
    return {
        "photos": _verify_rows(engine, PHOTO_SQL, _photo_verifier),
        "audio": _verify_rows(engine, AUDIO_SQL, _audio_verifier),
    }


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            text(
                """SELECT EXISTS(
                SELECT 1 FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name=:table AND column_name=:column
            )"""
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def legacy_columns(engine) -> dict[str, bool]:
    with engine.connect() as conn:
        return {
            "match_photos.image_data": _column_exists(
                conn, "match_photos", "image_data"
            ),
            "tts_voice_recordings.audio_data": _column_exists(
                conn, "tts_voice_recordings", "audio_data"
            ),
        }


def drop_legacy_columns(_engine) -> list[str]:
    """Fail closed: destructive schema changes belong to versioned migrations."""
    raise RuntimeError(
        "direct BYTEA drop is disabled; create and apply a versioned migration"
    )


def build_report(
    verified: dict[str, dict[str, int]],
    columns: dict[str, bool],
) -> dict:
    totals = {
        key: sum(int(section[key]) for section in verified.values())
        for key in ("rows", "objects", "bytes")
    }
    return {
        "app_version": APP_VERSION,
        "mode": "verification",
        "verified": verified,
        "totals": totals,
        "legacy_columns_present": columns,
        "drop_required": any(columns.values()),
        "object_verification_passed": True,
        "requires_browser_backup_gates": True,
    }


def _print_report(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    totals = report["totals"]
    print(
        "verified "
        f"rows={totals['rows']} objects={totals['objects']} bytes={totals['bytes']}"
    )
    print(
        "verification only; after browser and backup gates, drop legacy columns "
        "with a versioned migration"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="disabled legacy flag; exits before external access",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help="accepted only for compatibility with the disabled legacy flag",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print an aggregate machine-readable report without object keys",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply:
        print(
            "--apply is disabled; use a versioned migration after verification. "
            "No R2 or database access attempted.",
            file=sys.stderr,
        )
        return 2
    if not r2_storage.configured():
        print("R2 is not configured.", file=sys.stderr)
        return 2
    engine = _get_db_engine()
    if engine is None:
        print("Database is not configured.", file=sys.stderr)
        return 2

    try:
        verified = verify_media(engine)
        columns = legacy_columns(engine)
    except VerificationError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"verification unavailable: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1

    _print_report(build_report(verified, columns), args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
