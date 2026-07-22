"""Private Cloudflare R2 storage helpers.

The application keeps R2 credentials server-side and gives authenticated
browsers short-lived, operation-specific presigned URLs.  Large media bytes can
therefore travel directly between the browser and R2 instead of passing through
Render or PostgreSQL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import datetime as dt
from functools import lru_cache

from sqlalchemy import text

from core.config_store import (
    get_config,
    get_configs_from_connection,
    set_config,
)
from core.runtime_secrets import get_secret
from system_limits import (
    LOCAL_PRACTICE_FAILED_INPUT_RETRY_TTL_SECONDS,
    LOCAL_PRACTICE_MEDIA_PRUNE_BATCH,
    LOCAL_PRACTICE_TTS_OUTPUT_TTL_SECONDS,
    R2_CLAIM_MAX_TTL_SECONDS, R2_CLIENT_MAX_ATTEMPTS,
    R2_DOWNLOAD_URL_MAX_TTL_SECONDS, R2_DOWNLOAD_URL_TTL_SECONDS,
    R2_INTENT_RETENTION_DAYS, R2_OBJECT_CACHE_MAX_AGE_SECONDS,
    R2_STORAGE_STOP_BYTES, R2_STORAGE_WARN_BYTES,
    R2_UPLOAD_CLAIM_TTL_SECONDS,
    R2_UPLOAD_URL_MAX_TTL_SECONDS, R2_UPLOAD_URL_TTL_SECONDS,
    R2_URL_MIN_TTL_SECONDS, R2_USAGE_SNAPSHOT_MAX_AGE_SECONDS,
    WORKSTATION_R2_HEALTH_ORPHAN_TTL_SECONDS,
    WORKSTATION_R2_HEALTH_PRUNE_BATCH,
)


_usage_refresh_lock = threading.Lock()


def _nonnegative_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _secret(name: str, default: str = "") -> str:
    return get_secret(name, default)


def settings() -> dict:
    account_id = _secret("R2_ACCOUNT_ID")
    return {
        "account_id": account_id,
        "access_key_id": _secret("R2_ACCESS_KEY_ID"),
        "secret_access_key": _secret("R2_SECRET_ACCESS_KEY"),
        "bucket": _secret("R2_BUCKET"),
        "endpoint": _secret(
            "R2_ENDPOINT",
            f"https://{account_id}.r2.cloudflarestorage.com" if account_id else "",
        ).rstrip("/"),
    }


def configured() -> bool:
    cfg = settings()
    return all(cfg[key] for key in (
        "account_id", "access_key_id", "secret_access_key", "bucket", "endpoint"
    ))


def connection_ready() -> bool:
    """Return whether the configured private bucket answers one minimal read.

    This deliberately performs no write and never lets SDK, endpoint or
    credential details escape to callers.  It is suitable for an authenticated
    readiness screen, not as a substitute for normal operation error handling.
    """
    try:
        if not configured():
            return False
        client().list_objects_v2(Bucket=settings()["bucket"], MaxKeys=1)
    except Exception:
        return False
    return True


@lru_cache(maxsize=1)
def client():
    if not configured():
        raise RuntimeError("Cloudflare R2 is not configured")
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError("boto3 is required for Cloudflare R2") from exc
    cfg = settings()
    return boto3.client(
        "s3",
        endpoint_url=cfg["endpoint"],
        aws_access_key_id=cfg["access_key_id"],
        aws_secret_access_key=cfg["secret_access_key"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": R2_CLIENT_MAX_ATTEMPTS}),
    )


def _params(key: str) -> dict:
    return {"Bucket": settings()["bucket"], "Key": key}


def presign_put(
    key: str, mime_type: str, sha256: str, byte_size: int,
    expires: int = R2_UPLOAD_URL_TTL_SECONDS,
) -> str:
    size = int(byte_size)
    if size <= 0:
        raise ValueError("byte_size must be positive")
    params = {
        **_params(key),
        "ContentLength": size,
        "ContentType": mime_type,
        "CacheControl": f"private, max-age={R2_OBJECT_CACHE_MAX_AGE_SECONDS}",
        "Metadata": {"sha256": sha256},
    }
    return client().generate_presigned_url(
        "put_object", Params=params,
        ExpiresIn=max(R2_URL_MIN_TTL_SECONDS, min(int(expires), R2_UPLOAD_URL_MAX_TTL_SECONDS)),
    )


def presign_get(
    key: str,
    *,
    mime_type: str = "application/octet-stream",
    file_name: str = "",
    download: bool = False,
    expires: int = R2_DOWNLOAD_URL_TTL_SECONDS,
) -> str:
    params = {**_params(key), "ResponseContentType": mime_type}
    if download:
        safe_name = str(file_name or "download").replace('"', "").replace("\r", "").replace("\n", "")
        params["ResponseContentDisposition"] = f'attachment; filename="{safe_name}"'
    return client().generate_presigned_url(
        "get_object", Params=params,
        ExpiresIn=max(R2_URL_MIN_TTL_SECONDS, min(int(expires), R2_DOWNLOAD_URL_MAX_TTL_SECONDS)),
    )


def head(key: str) -> dict:
    return client().head_object(**_params(key))


def upload_bytes(key: str, data: bytes, mime_type: str, sha256: str = "") -> None:
    digest = sha256 or hashlib.sha256(data).hexdigest()
    client().put_object(
        **_params(key),
        Body=data,
        ContentType=mime_type,
        CacheControl=f"private, max-age={R2_OBJECT_CACHE_MAX_AGE_SECONDS}",
        Metadata={"sha256": digest},
    )


def download_bytes(key: str, max_bytes: int | None = None) -> bytes:
    """Read one object, optionally enforcing a hard decoded byte ceiling.

    Callers that already verified ``head_object`` still pass a limit to close
    the small time-of-check/time-of-use window while a presigned PUT remains
    valid. The extra byte detects a lying or missing Content-Length safely.
    """
    response = client().get_object(**_params(key))
    body = response["Body"]
    try:
        if max_bytes is None:
            return body.read()
        limit = int(max_bytes)
        if limit <= 0:
            raise ValueError("max_bytes must be positive")
        declared = response.get("ContentLength")
        if declared is not None and int(declared) > limit:
            raise ValueError("R2 object exceeds download limit")
        data = body.read(limit + 1)
        if len(data) > limit:
            raise ValueError("R2 object exceeds download limit")
        return data
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()


def delete(key: str) -> None:
    client().delete_object(**_params(key))


def promote(pending_key: str, final_key: str) -> dict:
    """Copy a verified pending object to its durable key without using Render bytes."""
    if not pending_key.startswith("pending/") or final_key.startswith("pending/"):
        raise ValueError("invalid R2 promotion path")
    cfg = settings()
    pending = head(pending_key)
    try:
        client().copy_object(
            **_params(final_key),
            CopySource={"Bucket": cfg["bucket"], "Key": pending_key},
            MetadataDirective="COPY",
        )
        durable = head(final_key)
        if (
            int(pending.get("ContentLength") or 0) != int(durable.get("ContentLength") or 0)
            or (pending.get("Metadata") or {}).get("sha256")
            != (durable.get("Metadata") or {}).get("sha256")
        ):
            raise RuntimeError("R2 promotion verification failed")
    except Exception:
        try:
            delete(final_key)
        except Exception:
            pass
        raise
    delete(pending_key)
    return durable


def bucket_usage_bytes() -> int:
    total = 0
    paginator = client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings()["bucket"]):
        total += sum(int(item.get("Size") or 0) for item in page.get("Contents") or [])
    return total


def _intent_declared_bytes(db) -> int:
    from schema import TABLE_R2_UPLOAD_INTENTS
    rows = db.query(f"""SELECT COALESCE(SUM(declared_bytes),0) AS total
        FROM {TABLE_R2_UPLOAD_INTENTS}
        WHERE status NOT IN ('orphan_deleted','consumed')""")
    return int(rows.iloc[0]["total"] or 0) if not rows.empty else 0


def storage_budget_status(db, *, refresh: bool = False) -> dict:
    """Return an exact-R2 snapshot plus conservative post-snapshot intents."""
    intent_bytes = _intent_declared_bytes(db)
    snapshot = get_config(db, "r2_storage_usage_snapshot", {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    now = dt.datetime.now(dt.timezone.utc)
    try:
        as_of = dt.datetime.fromisoformat(str(snapshot.get("as_of") or ""))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=dt.timezone.utc)
    except Exception:
        as_of = None
    stale = not as_of or (now - as_of).total_seconds() >= R2_USAGE_SNAPSHOT_MAX_AGE_SECONDS
    if refresh and stale:
        with _usage_refresh_lock:
            # Another request may have refreshed while this thread waited.
            latest = get_config(db, "r2_storage_usage_snapshot", {})
            try:
                latest = latest if isinstance(latest, dict) else {}
                latest_as_of = dt.datetime.fromisoformat(str(latest.get("as_of") or ""))
                if latest_as_of.tzinfo is None:
                    latest_as_of = latest_as_of.replace(tzinfo=dt.timezone.utc)
            except Exception:
                latest, latest_as_of = {}, None
            if latest_as_of and (now - latest_as_of).total_seconds() < R2_USAGE_SNAPSHOT_MAX_AGE_SECONDS:
                snapshot = latest
            else:
                exact = bucket_usage_bytes()
                snapshot = {
                    "bytes": exact, "as_of": now.isoformat(),
                    "intent_bytes_snapshot": intent_bytes,
                }
                set_config(db, "r2_storage_usage_snapshot", snapshot, updated_at=now)
            stale = False
    base = _nonnegative_int(snapshot.get("bytes"))
    intent_snapshot = _nonnegative_int(snapshot.get("intent_bytes_snapshot"))
    total = base + max(0, intent_bytes - intent_snapshot)
    try:
        from core.resource_limits import get_monthly_limit
        limits = get_monthly_limit(db, "r2_storage")
        warn_bytes = int(limits.get("warning_value") or R2_STORAGE_WARN_BYTES)
        stop_bytes = int(limits.get("stop_value") or R2_STORAGE_STOP_BYTES)
        hard_bytes = int(limits.get("hard_value") or stop_bytes)
    except Exception:
        warn_bytes = R2_STORAGE_WARN_BYTES
        stop_bytes = R2_STORAGE_STOP_BYTES
        hard_bytes = stop_bytes
    return {
        "total_bytes": total, "snapshot_bytes": base,
        "snapshot_as_of": str(snapshot.get("as_of") or ""),
        "intent_bytes": intent_bytes,
        "intent_bytes_after_snapshot": max(0, intent_bytes - intent_snapshot),
        "snapshot_stale": stale, "warning": total >= warn_bytes,
        "blocked": total >= stop_bytes,
        "warn_bytes": warn_bytes, "stop_bytes": stop_bytes,
        "hard_bytes": hard_bytes,
    }


def reserve_upload_intent(
    db, *, intent_id: str, user_id: str, media_kind: str, object_keys: list[str],
    declared_bytes: int, storage_stop_bytes: int | None = None,
    metadata: dict | None = None,
) -> tuple[bool, str]:
    """Reserve an owned upload intent under the system-wide storage gate."""
    from schema import TABLE_R2_UPLOAD_INTENTS

    # Resolve the DB-backed monthly threshold before taking a transaction-level
    # lock. RuntimeDb would otherwise borrow a second pooled connection while
    # this reservation held the first one, which can exhaust the size-3 pool.
    if storage_stop_bytes is None:
        try:
            from core.resource_limits import get_monthly_limit
            storage_stop_bytes = int(
                get_monthly_limit(db, "r2_storage").get("stop_value")
                or R2_STORAGE_STOP_BYTES
            )
        except Exception:
            storage_stop_bytes = R2_STORAGE_STOP_BYTES
    now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    with db.transaction() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('r2_upload_intent_storage'))"))
        session.execute(text(f"""DELETE FROM {TABLE_R2_UPLOAD_INTENTS}
            WHERE (status IN ('completed','orphan_deleted','consumed')
                   AND completed_at<:cutoff)
               OR (status='provider_processing' AND created_at<:cutoff)"""),
            {"cutoff": now_utc - dt.timedelta(days=R2_INTENT_RETENTION_DAYS)})
        storage_snapshot = get_configs_from_connection(
            session, ("r2_storage_usage_snapshot",)
        ).get("r2_storage_usage_snapshot", {})
        if not isinstance(storage_snapshot, dict):
            storage_snapshot = {}
        current_declared = int(session.execute(text(f"""SELECT COALESCE(SUM(declared_bytes),0)
            FROM {TABLE_R2_UPLOAD_INTENTS}
            WHERE status NOT IN ('orphan_deleted','consumed')""")).scalar() or 0)
        base_bytes = _nonnegative_int(storage_snapshot.get("bytes"))
        intent_snapshot = _nonnegative_int(storage_snapshot.get("intent_bytes_snapshot"))
        projected = base_bytes + max(0, current_declared - intent_snapshot) + int(declared_bytes)
        if projected >= int(storage_stop_bytes):
            return False, "storage_global"
        session.execute(text(f"""INSERT INTO {TABLE_R2_UPLOAD_INTENTS}
            (intent_id,user_id,media_kind,object_keys,declared_bytes,
             intent_metadata,status,created_at)
            VALUES(:id,:user,:kind,:keys,:bytes,CAST(:metadata AS jsonb),'issued',:now)"""), {
            "id": intent_id, "user": user_id, "kind": media_kind,
            "keys": json.dumps(object_keys, separators=(",", ":")),
            "bytes": int(declared_bytes),
            "metadata": json.dumps(metadata or {}, separators=(",", ":")),
            "now": now_utc,
        })
    return True, ""


def complete_upload_intent(
    db, intent_id: str, *, user_id: str, media_kind: str,
) -> bool:
    from schema import TABLE_R2_UPLOAD_INTENTS
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    changed = db.execute_count(f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
        SET status='completed',completed_at=:now
        WHERE intent_id=:id AND user_id=:user AND media_kind=:kind
          AND status='issued'""", {
        "id": intent_id, "user": user_id, "kind": media_kind, "now": now,
    })
    return int(changed or 0) == 1


def claim_completed_upload_intent(
    db, intent_id: str, *, user_id: str, media_kind: str,
) -> bool:
    """Atomically make one completed temporary upload single-use."""
    from schema import TABLE_R2_UPLOAD_INTENTS
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    changed = db.execute_count(f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
        SET status='processing',completed_at=NULL,
            intent_metadata=jsonb_set(
                COALESCE(intent_metadata,'{{}}'::jsonb),
                '{{processing_started_at}}',to_jsonb(CAST(:started AS TEXT)),TRUE
            )
        WHERE intent_id=:id AND user_id=:user AND media_kind=:kind
          AND status='completed'""", {
        "id": intent_id, "user": user_id, "kind": media_kind,
        "started": now.isoformat(),
    })
    return int(changed or 0) == 1


def release_processing_upload_intent(
    db, intent_id: str, *, user_id: str, media_kind: str,
) -> bool:
    """Return a failed, retryable provider claim to the completed state.

    The media object remains private and bounded by the caller-specific retry
    lifecycle. ``retry_started_at`` is write-once so repeated retries cannot
    slide the privacy deadline indefinitely.
    """
    from schema import TABLE_R2_UPLOAD_INTENTS
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    changed = db.execute_count(f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
        SET status='completed',completed_at=:now,
            intent_metadata=CASE
                WHEN COALESCE(intent_metadata,'{{}}'::jsonb) ? 'retry_started_at'
                THEN COALESCE(intent_metadata,'{{}}'::jsonb)
                ELSE jsonb_set(
                    COALESCE(intent_metadata,'{{}}'::jsonb),
                    '{{retry_started_at}}',to_jsonb(CAST(:started AS TEXT)),TRUE
                )
            END
        WHERE intent_id=:id AND user_id=:user AND media_kind=:kind
          AND status='processing'""", {
        "id": intent_id, "user": user_id, "kind": media_kind,
        "now": now, "started": now.isoformat(),
    })
    return int(changed or 0) == 1


def mark_processing_upload_cleanup_pending(
    db, intent_id: str, *, user_id: str, media_kind: str,
) -> bool:
    """Mark successful processing for prompt delete retry without closing it."""
    from schema import TABLE_R2_UPLOAD_INTENTS
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat()
    changed = db.execute_count(f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
        SET intent_metadata=jsonb_set(
            COALESCE(intent_metadata,'{{}}'::jsonb),
            '{{cleanup_pending_at}}',to_jsonb(CAST(:started AS TEXT)),TRUE
        )
        WHERE intent_id=:id AND user_id=:user AND media_kind=:kind
          AND status='processing'""", {
        "id": intent_id, "user": user_id, "kind": media_kind,
        "started": now,
    })
    return int(changed or 0) == 1


def get_upload_intent(db, intent_id: str, user_id: str, media_kind: str) -> dict | None:
    from schema import TABLE_R2_UPLOAD_INTENTS
    frame = db.query(f"""SELECT intent_id,user_id,media_kind,object_keys,
            declared_bytes,intent_metadata,status,created_at,completed_at
        FROM {TABLE_R2_UPLOAD_INTENTS}
        WHERE intent_id=:id AND user_id=:user AND media_kind=:kind""", {
        "id": str(intent_id), "user": str(user_id), "kind": str(media_kind),
    })
    if frame.empty:
        return None
    row = dict(frame.iloc[0])
    for field, fallback in (("object_keys", []), ("intent_metadata", {})):
        value = row.get(field)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                value = fallback
        row[field] = value if isinstance(value, type(fallback)) else fallback
    return row


def mark_upload_intent_deleted(db, intent_id: str) -> None:
    from schema import TABLE_R2_UPLOAD_INTENTS
    db.execute(f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
        SET status='orphan_deleted',completed_at=:now
        WHERE intent_id=:id
          AND status IN ('issued','completed','processing','orphan_deleted')""", {
        "id": intent_id, "now": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
    })


def delete_intent_objects(db, intent_id: str, object_keys) -> bool:
    """Delete every possible object before closing its conservative intent."""
    keys = tuple(dict.fromkeys(str(key) for key in object_keys if str(key or "").strip()))
    deleted = True
    for key in keys:
        try:
            delete(key)
        except Exception:
            deleted = False
    if not deleted:
        # Keep the current open status (issued/processing) so storage accounting
        # remains conservative and the orphan sweeper can retry every key.
        return False
    try:
        mark_upload_intent_deleted(db, str(intent_id or ""))
    except Exception:
        return False
    return True


def _intent_timestamp(value) -> dt.datetime | None:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, dt.datetime):
        try:
            value = dt.datetime.fromisoformat(str(value or ""))
        except (TypeError, ValueError):
            return None
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def prune_local_practice_media(
    db,
    *,
    now: dt.datetime | None = None,
    limit: int = LOCAL_PRACTICE_MEDIA_PRUNE_BATCH,
) -> dict:
    """Delete temporary practice media at the confirmed privacy deadlines.

    Completed rows are conditionally claimed before any R2 mutation, so an ASR
    retry cannot race the retention worker. Expired issued uploads, crashed ASR
    claims, and successful-ASR cleanup retries are also included. Delete failures
    keep a conservative open intent so storage accounting can retry safely.
    """
    from schema import TABLE_R2_UPLOAD_INTENTS

    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is not None:
        current = current.astimezone(dt.timezone.utc).replace(tzinfo=None)
    bounded_limit = max(1, min(int(limit), LOCAL_PRACTICE_MEDIA_PRUNE_BATCH))
    frame = db.query(f"""SELECT intent_id,media_kind,object_keys,intent_metadata,
            status,created_at,completed_at
        FROM {TABLE_R2_UPLOAD_INTENTS}
        WHERE media_kind IN (
            'local_practice_input','local_practice_tts_output'
        ) AND status IN ('issued','completed','processing')
        ORDER BY created_at ASC LIMIT :limit""", {"limit": bounded_limit * 4})
    deleted = 0
    failed = 0
    examined = 0
    for row in frame.to_dict("records"):
        if examined >= bounded_limit:
            break
        kind = str(row.get("media_kind") or "")
        ttl = (
            LOCAL_PRACTICE_FAILED_INPUT_RETRY_TTL_SECONDS
            if kind == "local_practice_input"
            else LOCAL_PRACTICE_TTS_OUTPUT_TTL_SECONDS
        )
        metadata = row.get("intent_metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                metadata = {}
        status = str(row.get("status") or "")
        cleanup_pending = bool(metadata.get("cleanup_pending_at"))
        if status == "completed":
            age_value = (
                metadata.get("retry_started_at")
                if kind == "local_practice_input"
                else None
            ) or row.get("completed_at")
        elif status == "processing":
            age_value = metadata.get("processing_started_at") or row.get("created_at")
        else:
            age_value = row.get("created_at")
        timestamp = _intent_timestamp(age_value)
        if (
            timestamp is None
            or (
                not cleanup_pending
                and timestamp > current - dt.timedelta(seconds=ttl)
            )
        ):
            continue
        intent_id = str(row.get("intent_id") or "")
        if not intent_id:
            continue
        if status == "completed":
            changed = db.execute_count(f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
                SET status='processing',completed_at=NULL,
                    intent_metadata=jsonb_set(
                        COALESCE(intent_metadata,'{{}}'::jsonb),
                        '{{processing_started_at}}',
                        to_jsonb(CAST(:started AS TEXT)),TRUE
                    )
                WHERE intent_id=:id AND status='completed'
                  AND completed_at=:completed""", {
                "id": intent_id,
                "completed": row.get("completed_at"),
                "started": current.isoformat(),
            })
            if int(changed or 0) != 1:
                continue
        keys = row.get("object_keys") or []
        if isinstance(keys, str):
            try:
                keys = json.loads(keys)
            except (TypeError, ValueError):
                keys = []
        examined += 1
        if delete_intent_objects(db, intent_id, keys if isinstance(keys, list) else []):
            deleted += 1
        else:
            failed += 1
    return {"examined": examined, "deleted": deleted, "failed": failed}


def reserve_workstation_r2_health_probe(
    db,
    *,
    intent_id: str,
    node_id: str,
    object_key: str,
    sha256: str,
    byte_size: int,
) -> bool:
    """Create one durable, outstanding functional R2 probe for a node."""
    from schema import TABLE_WORKSTATION_R2_HEALTH_PROBES

    changed = db.execute_count(f"""INSERT INTO {TABLE_WORKSTATION_R2_HEALTH_PROBES}
        (intent_id,node_id,object_key,sha256,byte_size,created_at)
        VALUES(:id,:node,:key,:sha,:bytes,NOW())
        ON CONFLICT (node_id) DO NOTHING""", {
        "id": str(intent_id),
        "node": str(node_id),
        "key": str(object_key),
        "sha": str(sha256),
        "bytes": int(byte_size),
    })
    return int(changed or 0) == 1


def get_workstation_r2_health_probe(
    db, *, intent_id: str, node_id: str,
) -> dict | None:
    from schema import TABLE_WORKSTATION_R2_HEALTH_PROBES

    frame = db.query(f"""SELECT intent_id,node_id,object_key,sha256,
            byte_size,created_at
        FROM {TABLE_WORKSTATION_R2_HEALTH_PROBES}
        WHERE intent_id=:id AND node_id=:node""", {
        "id": str(intent_id), "node": str(node_id),
    })
    if frame.empty:
        return None
    return dict(frame.iloc[0])


def delete_workstation_r2_health_probe(
    db, *, intent_id: str, node_id: str, object_key: str,
) -> bool:
    """Delete the private object before releasing its durable cleanup row."""
    from schema import TABLE_WORKSTATION_R2_HEALTH_PROBES

    try:
        delete(str(object_key))
        db.execute_count(f"""DELETE FROM {TABLE_WORKSTATION_R2_HEALTH_PROBES}
            WHERE intent_id=:id AND node_id=:node AND object_key=:key""", {
            "id": str(intent_id),
            "node": str(node_id),
            "key": str(object_key),
        })
    except Exception:
        # Keep the row when either R2 or PostgreSQL is unavailable. The
        # retention worker can safely retry the idempotent object deletion.
        return False
    return True


def prune_workstation_r2_health_probes(
    db,
    *,
    now: dt.datetime | None = None,
    limit: int = WORKSTATION_R2_HEALTH_PRUNE_BATCH,
) -> dict:
    """Remove abandoned Workstation functional probes after their fixed TTL."""
    from schema import TABLE_WORKSTATION_R2_HEALTH_PROBES

    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is not None:
        current = current.astimezone(dt.timezone.utc).replace(tzinfo=None)
    bounded_limit = max(1, min(int(limit), WORKSTATION_R2_HEALTH_PRUNE_BATCH))
    frame = db.query(f"""SELECT intent_id,node_id,object_key
        FROM {TABLE_WORKSTATION_R2_HEALTH_PROBES}
        WHERE created_at<=:cutoff
        ORDER BY created_at ASC LIMIT :limit""", {
        "cutoff": current - dt.timedelta(
            seconds=WORKSTATION_R2_HEALTH_ORPHAN_TTL_SECONDS
        ),
        "limit": bounded_limit,
    })
    deleted = 0
    failed = 0
    rows = frame.to_dict("records")
    for row in rows:
        if delete_workstation_r2_health_probe(
            db,
            intent_id=str(row.get("intent_id") or ""),
            node_id=str(row.get("node_id") or ""),
            object_key=str(row.get("object_key") or ""),
        ):
            deleted += 1
        else:
            failed += 1
    return {"examined": len(rows), "deleted": deleted, "failed": failed}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    raw = value.encode("ascii")
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


def sign_upload_claim(
    claim: dict, secret: str, expires: int = R2_UPLOAD_CLAIM_TTL_SECONDS,
) -> str:
    payload = dict(claim)
    payload["exp"] = int(time.time()) + max(
        R2_URL_MIN_TTL_SECONDS, min(int(expires), R2_CLAIM_MAX_TTL_SECONDS)
    )
    encoded = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64url(signature)}"


def verify_upload_claim(token: str, secret: str) -> dict | None:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expected), signature):
            return None
        payload = json.loads(_b64url_decode(encoded))
        if int(payload.get("exp") or 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None
