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
import os
import time
from functools import lru_cache
from pathlib import Path

import tomllib


BASE_DIR = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _file_secrets() -> dict:
    path = BASE_DIR / ".streamlit" / "secrets.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except Exception:
        return {}


def _secret(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        value = _file_secrets().get(name, default)
    return str(value or default).strip()


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
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def _params(key: str) -> dict:
    return {"Bucket": settings()["bucket"], "Key": key}


def presign_put(key: str, mime_type: str, sha256: str, expires: int = 300) -> str:
    params = {
        **_params(key),
        "ContentType": mime_type,
        "CacheControl": "private, max-age=86400",
        "Metadata": {"sha256": sha256},
    }
    return client().generate_presigned_url(
        "put_object", Params=params, ExpiresIn=max(60, min(int(expires), 900))
    )


def presign_get(
    key: str,
    *,
    mime_type: str = "application/octet-stream",
    file_name: str = "",
    download: bool = False,
    expires: int = 600,
) -> str:
    params = {**_params(key), "ResponseContentType": mime_type}
    if download:
        safe_name = str(file_name or "download").replace('"', "").replace("\r", "").replace("\n", "")
        params["ResponseContentDisposition"] = f'attachment; filename="{safe_name}"'
    return client().generate_presigned_url(
        "get_object", Params=params, ExpiresIn=max(60, min(int(expires), 3600))
    )


def head(key: str) -> dict:
    return client().head_object(**_params(key))


def upload_bytes(key: str, data: bytes, mime_type: str, sha256: str = "") -> None:
    digest = sha256 or hashlib.sha256(data).hexdigest()
    client().put_object(
        **_params(key),
        Body=data,
        ContentType=mime_type,
        CacheControl="private, max-age=86400",
        Metadata={"sha256": digest},
    )


def download_bytes(key: str) -> bytes:
    response = client().get_object(**_params(key))
    return response["Body"].read()


def delete(key: str) -> None:
    client().delete_object(**_params(key))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    raw = value.encode("ascii")
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


def sign_upload_claim(claim: dict, secret: str, expires: int = 600) -> str:
    payload = dict(claim)
    payload["exp"] = int(time.time()) + max(60, min(int(expires), 1800))
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
