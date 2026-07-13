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
from zoneinfo import ZoneInfo

from sqlalchemy import text

from core.config_store import (
    get_config,
    get_configs_from_connection,
    set_config,
)
from core.runtime_secrets import get_secret
from system_limits import (
    R2_CLAIM_MAX_TTL_SECONDS, R2_CLIENT_MAX_ATTEMPTS,
    R2_DOWNLOAD_URL_MAX_TTL_SECONDS, R2_DOWNLOAD_URL_TTL_SECONDS,
    R2_INTENT_RETENTION_DAYS, R2_OBJECT_CACHE_MAX_AGE_SECONDS,
    R2_STORAGE_STOP_BYTES, R2_STORAGE_WARN_BYTES,
    R2_UPLOAD_CLAIM_TTL_SECONDS,
    R2_UPLOAD_URL_MAX_TTL_SECONDS, R2_UPLOAD_URL_TTL_SECONDS,
    R2_URL_MIN_TTL_SECONDS, R2_USAGE_SNAPSHOT_MAX_AGE_SECONDS,
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
    from schema import CREATE_R2_UPLOAD_INTENTS, TABLE_R2_UPLOAD_INTENTS
    db.execute(CREATE_R2_UPLOAD_INTENTS)
    rows = db.query(f"""SELECT COALESCE(SUM(declared_bytes),0) AS total
        FROM {TABLE_R2_UPLOAD_INTENTS} WHERE status!='orphan_deleted'""")
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
    return {
        "total_bytes": total, "snapshot_bytes": base,
        "snapshot_as_of": str(snapshot.get("as_of") or ""),
        "intent_bytes": intent_bytes,
        "intent_bytes_after_snapshot": max(0, intent_bytes - intent_snapshot),
        "snapshot_stale": stale, "warning": total >= R2_STORAGE_WARN_BYTES,
        "blocked": total >= R2_STORAGE_STOP_BYTES,
        "warn_bytes": R2_STORAGE_WARN_BYTES, "stop_bytes": R2_STORAGE_STOP_BYTES,
    }


def reserve_upload_intent(
    db, *, intent_id: str, user_id: str, media_kind: str, object_keys: list[str],
    declared_bytes: int, user_daily_limit: int, global_monthly_limit: int,
    storage_stop_bytes: int = R2_STORAGE_STOP_BYTES,
) -> tuple[bool, str]:
    """Persistently cap issued PUT URLs, including uploads never finalized."""
    from schema import CREATE_R2_UPLOAD_INTENTS, TABLE_R2_UPLOAD_INTENTS

    now_hk = dt.datetime.now(ZoneInfo("Asia/Hong_Kong"))
    day_hk = now_hk.replace(hour=0, minute=0, second=0, microsecond=0)
    month_hk = day_hk.replace(day=1)
    now_utc = now_hk.astimezone(dt.timezone.utc).replace(tzinfo=None)
    day_utc = day_hk.astimezone(dt.timezone.utc).replace(tzinfo=None)
    month_utc = month_hk.astimezone(dt.timezone.utc).replace(tzinfo=None)
    with db.transaction() as session:
        session.execute(text(CREATE_R2_UPLOAD_INTENTS))
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('r2_upload_intent_quota'))"))
        session.execute(text(f"""DELETE FROM {TABLE_R2_UPLOAD_INTENTS}
            WHERE status IN ('completed','orphan_deleted') AND completed_at<:cutoff"""),
            {"cutoff": now_utc - dt.timedelta(days=R2_INTENT_RETENTION_DAYS)})
        storage_snapshot = get_configs_from_connection(
            session, ("r2_storage_usage_snapshot",)
        ).get("r2_storage_usage_snapshot", {})
        if not isinstance(storage_snapshot, dict):
            storage_snapshot = {}
        current_declared = int(session.execute(text(f"""SELECT COALESCE(SUM(declared_bytes),0)
            FROM {TABLE_R2_UPLOAD_INTENTS} WHERE status!='orphan_deleted'""")).scalar() or 0)
        base_bytes = _nonnegative_int(storage_snapshot.get("bytes"))
        intent_snapshot = _nonnegative_int(storage_snapshot.get("intent_bytes_snapshot"))
        projected = base_bytes + max(0, current_declared - intent_snapshot) + int(declared_bytes)
        if projected >= int(storage_stop_bytes):
            return False, "storage_global"
        user_count = int(session.execute(text(f"""SELECT COUNT(*)
            FROM {TABLE_R2_UPLOAD_INTENTS}
            WHERE user_id=:user AND media_kind=:kind AND created_at>=:start"""), {
            "user": user_id, "kind": media_kind, "start": day_utc,
        }).scalar() or 0)
        if user_count >= int(user_daily_limit):
            return False, "user_daily"
        global_count = int(session.execute(text(f"""SELECT COUNT(*)
            FROM {TABLE_R2_UPLOAD_INTENTS}
            WHERE media_kind=:kind AND created_at>=:start"""), {
            "kind": media_kind, "start": month_utc,
        }).scalar() or 0)
        if global_count >= int(global_monthly_limit):
            return False, "global_monthly"
        session.execute(text(f"""INSERT INTO {TABLE_R2_UPLOAD_INTENTS}
            (intent_id,user_id,media_kind,object_keys,declared_bytes,status,created_at)
            VALUES(:id,:user,:kind,:keys,:bytes,'issued',:now)"""), {
            "id": intent_id, "user": user_id, "kind": media_kind,
            "keys": json.dumps(object_keys, separators=(",", ":")),
            "bytes": int(declared_bytes), "now": now_utc,
        })
    return True, ""


def complete_upload_intent(db, intent_id: str) -> None:
    from schema import TABLE_R2_UPLOAD_INTENTS
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    db.execute(f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
        SET status='completed',completed_at=:now
        WHERE intent_id=:id AND status='issued'""", {"id": intent_id, "now": now})


def mark_upload_intent_deleted(db, intent_id: str) -> None:
    from schema import TABLE_R2_UPLOAD_INTENTS
    db.execute(f"""UPDATE {TABLE_R2_UPLOAD_INTENTS}
        SET status='orphan_deleted',completed_at=:now WHERE intent_id=:id""", {
        "id": intent_id, "now": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
    })


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
