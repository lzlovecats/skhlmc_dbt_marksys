#!/usr/bin/env python3
"""Verify all production media in R2, then permanently remove BYTEA columns.

Dry-run is the default.  The destructive action is deliberately gated behind
both ``--apply`` and an exact confirmation phrase, and must only be run after
the R2-only application has been deployed and browser playback verified.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import r2_storage
from deploy.proxy import _get_db_engine


CONFIRMATION = "4.1.2-R2-VERIFIED"


def _verify_object(key: str, expected_size: int = 0, expected_sha: str = "") -> None:
    if not key:
        raise RuntimeError("missing R2 key")
    remote = r2_storage.head(key)
    size = int(remote.get("ContentLength") or 0)
    sha = str((remote.get("Metadata") or {}).get("sha256") or "")
    if size <= 0 or (expected_size and size != expected_size) or (expected_sha and sha != expected_sha):
        raise RuntimeError(f"R2 verification failed for {key}")


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(conn.execute(text("""SELECT EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=:table AND column_name=:column
    )"""), {"table": table, "column": column}).scalar())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if not r2_storage.configured():
        print("R2 is not configured.", file=sys.stderr)
        return 2
    engine = _get_db_engine()
    if engine is None:
        print("Database is not configured.", file=sys.stderr)
        return 2

    with engine.connect() as conn:
        photos = conn.execute(text("""SELECT id,r2_key,thumbnail_r2_key,
            COALESCE(byte_size,0) byte_size,COALESCE(sha256,'') sha256
            FROM match_photos ORDER BY id""")).mappings().all()
        audio = conn.execute(text("""SELECT id,r2_key,COALESCE(size_bytes,0) size_bytes,
            COALESCE(audio_sha256,'') audio_sha256
            FROM tts_voice_recordings ORDER BY id""")).mappings().all()
    for row in photos:
        _verify_object(str(row["r2_key"] or ""), int(row["byte_size"]), str(row["sha256"] or ""))
        _verify_object(str(row["thumbnail_r2_key"] or ""))
    for row in audio:
        _verify_object(str(row["r2_key"] or ""), int(row["size_bytes"]), str(row["audio_sha256"] or ""))
    print(f"verified photos={len(photos)} audio={len(audio)}")

    if not args.apply:
        print(f"dry-run only; use --apply --confirm {CONFIRMATION} after production playback verification")
        return 0
    if args.confirm != CONFIRMATION:
        print("confirmation phrase does not match; no database changes made", file=sys.stderr)
        return 2
    with engine.begin() as conn:
        if _column_exists(conn, "match_photos", "image_data"):
            conn.execute(text("ALTER TABLE match_photos DROP COLUMN image_data"))
        if _column_exists(conn, "tts_voice_recordings", "audio_data"):
            conn.execute(text("ALTER TABLE tts_voice_recordings DROP COLUMN audio_data"))
    print("legacy BYTEA columns dropped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
