#!/usr/bin/env python3
"""Find or delete old R2 objects that have no database metadata reference."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import r2_storage
from deploy.proxy import get_vote_db


CONFIRMATION = "DELETE-R2-ORPHANS"
PREFIXES = ("photos/original/", "photos/thumb/", "audio/tts/")


def _referenced_keys() -> set[str]:
    db = get_vote_db()
    photos = db.query("SELECT r2_key,thumbnail_r2_key FROM match_photos")
    audio = db.query("SELECT r2_key FROM tts_voice_recordings")
    keys: set[str] = set()
    for frame in (photos, audio):
        for column in frame.columns:
            keys.update(str(value) for value in frame[column].dropna().tolist() if str(value).strip())
    return keys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--older-than-hours", type=int, default=48)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if not r2_storage.configured():
        print("R2 is not configured.", file=sys.stderr)
        return 2
    referenced = _referenced_keys()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max(24, args.older_than_hours))
    orphans = []
    s3 = r2_storage.client()
    bucket = r2_storage.settings()["bucket"]
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in PREFIXES:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                modified = item.get("LastModified")
                if key and key not in referenced and modified and modified <= cutoff:
                    orphans.append((key, int(item.get("Size") or 0)))
    total = sum(size for _, size in orphans)
    print(f"orphans={len(orphans)} bytes={total} cutoff={cutoff.isoformat()}")
    if not args.apply:
        for key, size in orphans[:100]:
            print(f"dry-run {size:>10} {key}")
        return 0
    if args.confirm != CONFIRMATION:
        print("confirmation phrase does not match; no R2 objects deleted", file=sys.stderr)
        return 2
    for key, _size in orphans:
        r2_storage.delete(key)
        print(f"deleted {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
