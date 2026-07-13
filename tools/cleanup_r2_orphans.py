#!/usr/bin/env python3
"""Find or delete old R2 objects that have no database metadata reference."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import r2_storage
from deploy.proxy import get_vote_db
from schema import CREATE_R2_UPLOAD_INTENTS
from system_limits import R2_ORPHAN_DRY_RUN_DISPLAY_LIMIT, R2_ORPHAN_MIN_AGE_HOURS


CONFIRMATION = "DELETE-R2-ORPHANS"
PREFIXES = ("pending/", "photos/original/", "photos/thumb/", "audio/tts/")


def _referenced_keys(db) -> set[str]:
    photos = db.query("SELECT r2_key,thumbnail_r2_key FROM match_photos")
    audio = db.query("SELECT r2_key FROM tts_voice_recordings")
    keys: set[str] = set()
    for frame in (photos, audio):
        for column in frame.columns:
            keys.update(str(value) for value in frame[column].dropna().tolist() if str(value).strip())
    return keys


def _issued_intents_by_key(db) -> dict[str, str]:
    db.execute(CREATE_R2_UPLOAD_INTENTS)
    rows = db.query("SELECT intent_id,object_keys FROM r2_upload_intents WHERE status='issued'")
    result: dict[str, str] = {}
    for _, row in rows.iterrows():
        try:
            keys = json.loads(str(row["object_keys"] or "[]"))
        except Exception:
            keys = []
        for key in keys:
            if str(key).startswith("pending/"):
                result[str(key)] = str(row["intent_id"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--older-than-hours", type=int, default=R2_ORPHAN_MIN_AGE_HOURS)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRMATION:
        print("confirmation phrase does not match; no R2 objects deleted", file=sys.stderr)
        return 2
    if not r2_storage.configured():
        print("R2 is not configured.", file=sys.stderr)
        return 2
    db = get_vote_db()
    referenced = _referenced_keys(db)
    pending_intents = _issued_intents_by_key(db)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        hours=max(R2_ORPHAN_MIN_AGE_HOURS, args.older_than_hours)
    )
    orphan_count = 0
    total_bytes = 0
    dry_run_sample: list[tuple[str, int]] = []
    s3 = r2_storage.client()
    bucket = r2_storage.settings()["bucket"]
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in PREFIXES:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                modified = item.get("LastModified")
                if key and key not in referenced and modified and modified <= cutoff:
                    size = int(item.get("Size") or 0)
                    orphan_count += 1
                    total_bytes += size
                    if args.apply:
                        r2_storage.delete(key)
                        intent_id = pending_intents.get(key)
                        if intent_id:
                            r2_storage.mark_upload_intent_deleted(db, intent_id)
                        print(f"deleted {key}")
                    elif len(dry_run_sample) < R2_ORPHAN_DRY_RUN_DISPLAY_LIMIT:
                        dry_run_sample.append((key, size))
    print(f"orphans={orphan_count} bytes={total_bytes} cutoff={cutoff.isoformat()}")
    for key, size in dry_run_sample:
        print(f"dry-run {size:>10} {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
