#!/usr/bin/env python3
"""Migrate PostgreSQL BYTEA media to private Cloudflare R2 one row at a time.

The default is deliberately non-destructive: R2 keys are written back but the
legacy BYTEA remains available for application fallback.  Run a second verified
pass with ``--delete-db-binary`` only after the R2-enabled application is live.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image
from sqlalchemy import text

from core import r2_storage
from deploy.proxy import _get_db_engine


PHOTO_DDL = (
    "ALTER TABLE match_photos ALTER COLUMN image_data DROP NOT NULL",
    "ALTER TABLE match_photos ADD COLUMN IF NOT EXISTS r2_key TEXT",
    "ALTER TABLE match_photos ADD COLUMN IF NOT EXISTS thumbnail_r2_key TEXT",
    "ALTER TABLE match_photos ADD COLUMN IF NOT EXISTS byte_size INTEGER",
    "ALTER TABLE match_photos ADD COLUMN IF NOT EXISTS sha256 TEXT",
    "ALTER TABLE match_photos ADD COLUMN IF NOT EXISTS width INTEGER",
    "ALTER TABLE match_photos ADD COLUMN IF NOT EXISTS height INTEGER",
)
AUDIO_DDL = (
    "ALTER TABLE tts_voice_recordings ALTER COLUMN audio_data DROP NOT NULL",
    "ALTER TABLE tts_voice_recordings ADD COLUMN IF NOT EXISTS r2_key TEXT",
    "ALTER TABLE tts_voice_recordings ADD COLUMN IF NOT EXISTS audio_sha256 TEXT",
    "ALTER TABLE tts_voice_recordings ADD COLUMN IF NOT EXISTS size_bytes INTEGER",
)


def _bytes(value) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    return bytes(value or b"")


def _ext(file_name: str, mime_type: str, default: str) -> str:
    suffix = Path(str(file_name or "")).suffix.lower().lstrip(".")
    if suffix and suffix.isalnum():
        return suffix
    return {
        "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
        "audio/webm": "webm", "audio/mp4": "m4a", "audio/mpeg": "mp3",
        "audio/wav": "wav", "audio/ogg": "ogg",
    }.get(str(mime_type or "").split(";", 1)[0], default)


def _verify(key: str, size: int, sha256: str) -> None:
    remote = r2_storage.head(key)
    remote_sha = str((remote.get("Metadata") or {}).get("sha256") or "")
    if int(remote.get("ContentLength") or 0) != int(size) or remote_sha != sha256:
        raise RuntimeError(f"R2 verification failed for {key}")


def _photo_thumbnail(data: bytes) -> tuple[bytes, int, int]:
    with Image.open(io.BytesIO(data)) as source:
        width, height = source.size
        image = source.convert("RGB")
        image.thumbnail((480, 480), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="WEBP", quality=72, method=6)
        return output.getvalue(), width, height


def migrate_photos(engine, limit: int, delete_binary: bool) -> tuple[int, int]:
    with engine.begin() as conn:
        for ddl in PHOTO_DDL:
            conn.execute(text(ddl))
        ids = [row[0] for row in conn.execute(text(
            "SELECT id FROM match_photos WHERE image_data IS NOT NULL ORDER BY id LIMIT :limit"
        ), {"limit": limit})]
    migrated = cleared = 0
    for photo_id in ids:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT file_name,mime_type,image_data,r2_key,thumbnail_r2_key,sha256 "
                "FROM match_photos WHERE id=:id"
            ), {"id": photo_id}).mappings().first()
        if not row:
            continue
        data = _bytes(row["image_data"])
        digest = str(row["sha256"] or hashlib.sha256(data).hexdigest())
        mime = str(row["mime_type"] or "image/jpeg").split(";", 1)[0]
        original_key = str(row["r2_key"] or "")
        thumbnail_key = str(row["thumbnail_r2_key"] or "")
        thumbnail, width, height = _photo_thumbnail(data)
        thumb_sha = hashlib.sha256(thumbnail).hexdigest()
        if not original_key:
            ext = _ext(row["file_name"], mime, "jpg")
            original_key = f"photos/original/legacy/{photo_id}-{digest[:12]}.{ext}"
            thumbnail_key = f"photos/thumb/legacy/{photo_id}-{digest[:12]}.webp"
            r2_storage.upload_bytes(original_key, data, mime, digest)
            r2_storage.upload_bytes(thumbnail_key, thumbnail, "image/webp", thumb_sha)
            _verify(original_key, len(data), digest)
            _verify(thumbnail_key, len(thumbnail), thumb_sha)
            with engine.begin() as conn:
                conn.execute(text("""UPDATE match_photos SET r2_key=:original,
                    thumbnail_r2_key=:thumbnail,byte_size=:size,sha256=:sha,
                    width=:width,height=:height WHERE id=:id"""), {
                    "original": original_key, "thumbnail": thumbnail_key,
                    "size": len(data), "sha": digest, "width": width,
                    "height": height, "id": photo_id,
                })
            migrated += 1
        else:
            _verify(original_key, len(data), digest)
            if thumbnail_key:
                _verify(thumbnail_key, len(thumbnail), thumb_sha)
        if delete_binary:
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE match_photos SET image_data=NULL WHERE id=:id AND r2_key IS NOT NULL"
                ), {"id": photo_id})
            cleared += 1
        print(f"photo {photo_id}: verified{', BYTEA cleared' if delete_binary else ''}")
    return migrated, cleared


def migrate_audio(engine, limit: int, delete_binary: bool) -> tuple[int, int]:
    with engine.begin() as conn:
        for ddl in AUDIO_DDL:
            conn.execute(text(ddl))
        ids = [row[0] for row in conn.execute(text(
            "SELECT id FROM tts_voice_recordings WHERE audio_data IS NOT NULL ORDER BY id LIMIT :limit"
        ), {"limit": limit})]
    migrated = cleared = 0
    for record_id in ids:
        with engine.connect() as conn:
            row = conn.execute(text("""SELECT speaker_user_id,file_ext,mime_type,
                audio_data,r2_key,audio_sha256 FROM tts_voice_recordings WHERE id=:id"""),
                {"id": record_id}).mappings().first()
        if not row:
            continue
        data = _bytes(row["audio_data"])
        digest = str(row["audio_sha256"] or hashlib.sha256(data).hexdigest())
        mime = str(row["mime_type"] or "audio/webm").split(";", 1)[0]
        key = str(row["r2_key"] or "")
        if not key:
            speaker = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(row["speaker_user_id"] or "member"))[:48]
            ext = _ext(row["file_ext"], mime, "webm")
            key = f"audio/tts/{speaker}/{record_id}-{digest[:12]}.{ext}"
            r2_storage.upload_bytes(key, data, mime, digest)
            _verify(key, len(data), digest)
            with engine.begin() as conn:
                conn.execute(text("""UPDATE tts_voice_recordings SET r2_key=:key,
                    audio_sha256=:sha,size_bytes=:size WHERE id=:id"""),
                    {"key": key, "sha": digest, "size": len(data), "id": record_id})
            migrated += 1
        else:
            _verify(key, len(data), digest)
        if delete_binary:
            with engine.begin() as conn:
                conn.execute(text("""UPDATE tts_voice_recordings SET audio_data=NULL
                    WHERE id=:id AND r2_key IS NOT NULL"""), {"id": record_id})
            cleared += 1
        print(f"audio {record_id}: verified{', BYTEA cleared' if delete_binary else ''}")
    return migrated, cleared


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", choices=("photos", "audio", "all"), default="all")
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--delete-db-binary", action="store_true")
    args = parser.parse_args()
    if not r2_storage.configured():
        print("R2 is not configured. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
              "R2_SECRET_ACCESS_KEY and R2_BUCKET.", file=sys.stderr)
        return 2
    engine = _get_db_engine()
    if engine is None:
        print("Database is not configured.", file=sys.stderr)
        return 2
    if args.media in ("audio", "all"):
        print("audio", migrate_audio(engine, args.limit, args.delete_db_binary))
    if args.media in ("photos", "all"):
        print("photos", migrate_photos(engine, args.limit, args.delete_db_binary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
