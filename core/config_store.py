"""Typed application configuration store.

All runtime settings live in ``app_config`` as native JSON values with an
explicit namespace/type/secret classification.  The legacy untyped
``system_config`` bucket was retired by migration ``20260714_0002`` after the
inventory confirmed every key existed in the typed store; rolling that
migration back rebuilds the legacy table from ``app_config``.

This module is intentionally database-only: it does not import the FastAPI
runtime and callers continue to inject the repository DB executor.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import json
from typing import Any, Iterable

from sqlalchemy import text

from schema import TABLE_APP_CONFIG


@dataclass(frozen=True)
class ConfigSpec:
    namespace: str
    value_type: str
    secret: bool = False


_STRING = "string"
_BOOLEAN = "boolean"
_NUMBER = "number"
_ARRAY = "array"
_OBJECT = "object"


CONFIG_SPECS: dict[str, ConfigSpec] = {
    # Authentication material.  Values are never returned by public APIs.
    "admin_password": ConfigSpec("auth", _STRING, True),
    "developer_password": ConfigSpec("auth", _STRING, True),
    "sql_password": ConfigSpec("auth", _STRING, True),
    "cookie_secret": ConfigSpec("auth", _STRING, True),
    # Runtime settings.
    "maintenance_mode": ConfigSpec("runtime", _BOOLEAN),
    "maintenance_deadline": ConfigSpec("runtime", _STRING),
    "ai_enabled_providers": ConfigSpec("ai", _ARRAY),
    "ai_default_model": ConfigSpec("ai", _STRING),
    # Account access and delegated capabilities.
    "login_disabled_accounts": ConfigSpec("access", _ARRAY),
    "bypass_active_check_until": ConfigSpec("access", _OBJECT),
    "solo_quota_exemptions": ConfigSpec("access", _OBJECT),
    "tts_recording_allowed_users": ConfigSpec("access", _ARRAY),
    "tts_recording_reviewers": ConfigSpec("access", _ARRAY),
    "ai_fund_treasurers": ConfigSpec("access", _ARRAY),
    "lateness_fund_managers": ConfigSpec("access", _ARRAY),
    # AI fund settings and provider balance snapshot.
    "ai_fund_target_hkd": ConfigSpec("finance", _NUMBER),
    "ai_fund_low_balance_hkd": ConfigSpec("finance", _NUMBER),
    "ai_fund_payment_instruction": ConfigSpec("finance", _STRING),
    "google_ai_studio_balance_usd": ConfigSpec("finance", _NUMBER),
    "google_ai_studio_balance_updated_at": ConfigSpec("finance", _STRING),
    "google_ai_studio_balance_updated_by": ConfigSpec("finance", _STRING),
    # Small, replaceable analysis cache records.
    "vote_bank_analysis": ConfigSpec("analysis", _STRING),
    "vote_bank_analysis_at": ConfigSpec("analysis", _STRING),
    "vote_bank_analysis_by": ConfigSpec("analysis", _STRING),
    "vote_bank_analysis_source_signature": ConfigSpec("analysis", _STRING),
    "vote_history_analysis": ConfigSpec("analysis", _STRING),
    "vote_history_analysis_at": ConfigSpec("analysis", _STRING),
    "vote_history_analysis_by": ConfigSpec("analysis", _STRING),
    "vote_history_analysis_source_signature": ConfigSpec("analysis", _STRING),
    # Resource accounting snapshots/alerts.
    "r2_storage_usage_snapshot": ConfigSpec("resource", _OBJECT),
    "bandwidth_developer_warning": ConfigSpec("resource", _OBJECT),
    # Idempotent historical migration marker.
    "ai_fund_transaction_type_v2": ConfigSpec("migration", _STRING),
}


_PREFIX_SPECS = (
    ("bandwidth_3gb_push_", ConfigSpec("resource", _NUMBER)),
)


def config_spec(key: str, *, allow_legacy: bool = False) -> ConfigSpec:
    """Return the registered classification for ``key``.

    Unknown keys are rejected for normal writes.  During the one-off legacy
    import they are retained as classified legacy strings so migration never
    silently discards production state.
    """

    normalized = str(key or "").strip()
    if normalized in CONFIG_SPECS:
        return CONFIG_SPECS[normalized]
    for prefix, spec in _PREFIX_SPECS:
        if normalized.startswith(prefix):
            return spec
    if allow_legacy:
        return ConfigSpec("legacy", _STRING)
    raise KeyError(f"Unregistered app config key: {normalized}")


def _coerce(value: Any, spec: ConfigSpec) -> Any:
    if spec.value_type == _STRING:
        return "" if value is None else str(value)
    if spec.value_type == _BOOLEAN:
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
        raise ValueError(f"Invalid boolean config value: {value!r}")
    if spec.value_type == _NUMBER:
        if isinstance(value, bool):
            raise ValueError("Boolean is not a numeric config value")
        number = float(value)
        return int(number) if number.is_integer() else number
    if spec.value_type in {_ARRAY, _OBJECT}:
        parsed = value
        if isinstance(value, str):
            parsed = json.loads(value or ("[]" if spec.value_type == _ARRAY else "{}"))
        expected = list if spec.value_type == _ARRAY else dict
        if not isinstance(parsed, expected):
            raise ValueError(f"Expected {spec.value_type} config value")
        return parsed
    raise ValueError(f"Unsupported config type: {spec.value_type}")


def _decoded(value: Any, spec: ConfigSpec) -> Any:
    # psycopg returns JSONB as native objects.  Lightweight test executors may
    # return its textual form, so normalise both without changing strings.
    if spec.value_type in {_ARRAY, _OBJECT} and isinstance(value, str):
        return _coerce(value, spec)
    return _coerce(value, spec)


def _row_value(frame, spec: ConfigSpec):
    if frame is None or frame.empty:
        return None
    return _decoded(frame.iloc[0]["value"], spec)


def get_config(db, key: str, default: Any = None) -> Any:
    """Read one typed value from the ``app_config`` store."""

    spec = config_spec(key, allow_legacy=True)
    try:
        value = _row_value(
            db.query(
                f"SELECT value FROM {TABLE_APP_CONFIG} WHERE key=:key",
                {"key": key},
            ),
            spec,
        )
        return default if value is None else value
    except Exception:
        # Bootstrap-order tolerance: a brand-new database may not have
        # app_config yet when early startup reads run.
        return default


def get_configs(db, keys: Iterable[str]) -> dict[str, Any]:
    """Batch-read keys from the typed store.

    Normal executors use one ``IN`` query.  The per-key fallback exists only
    for deliberately tiny test doubles that do not implement ``IN`` queries.
    """

    ordered = list(dict.fromkeys(str(key) for key in keys))
    if not ordered:
        return {}
    params = {f"key_{index}": key for index, key in enumerate(ordered)}
    placeholders = ",".join(f":key_{index}" for index in range(len(ordered)))
    values: dict[str, Any] = {}
    try:
        rows = db.query(
            f"SELECT key,value FROM {TABLE_APP_CONFIG} "
            f"WHERE key IN ({placeholders})",
            params,
        )
        for _, row in rows.iterrows():
            key = str(row["key"])
            values[key] = _decoded(
                row["value"], config_spec(key, allow_legacy=True)
            )
    except Exception:
        # Compatibility path for minimal unit-test executors.
        return {key: get_config(db, key) for key in ordered}
    return values


_UPSERT_SQL = f"""
INSERT INTO {TABLE_APP_CONFIG}
    (key, namespace, value, value_type, is_secret, updated_at)
VALUES
    (:key, :namespace, CAST(:value AS JSONB), :value_type, :is_secret, :updated_at)
ON CONFLICT (key) DO UPDATE SET
    namespace=EXCLUDED.namespace,
    value=EXCLUDED.value,
    value_type=EXCLUDED.value_type,
    is_secret=EXCLUDED.is_secret,
    updated_at=EXCLUDED.updated_at
"""


def _params(key: str, value: Any, updated_at=None, *, allow_legacy=False) -> dict:
    spec = config_spec(key, allow_legacy=allow_legacy)
    typed_value = _coerce(value, spec)
    return {
        "key": key,
        "namespace": spec.namespace,
        "value": json.dumps(typed_value, ensure_ascii=False, separators=(",", ":")),
        "value_type": spec.value_type,
        "is_secret": spec.secret,
        "updated_at": updated_at or dt.datetime.now(dt.timezone.utc),
    }


def set_config(db, key: str, value: Any, *, updated_at=None) -> None:
    """Write one registered key to the typed store."""

    db.execute(_UPSERT_SQL, _params(key, value, updated_at))


def set_configs(db, values: dict[str, Any], *, updated_at=None) -> None:
    """Atomically write registered keys when the executor supports transactions."""

    stamp = updated_at or dt.datetime.now(dt.timezone.utc)
    if hasattr(db, "transaction"):
        with db.transaction() as conn:
            for key, value in values.items():
                conn.execute(text(_UPSERT_SQL), _params(key, value, stamp))
        return
    for key, value in values.items():
        set_config(db, key, value, updated_at=stamp)


def set_configs_on_connection(conn, values: dict[str, Any], *, updated_at=None) -> None:
    """Write within an existing SQLAlchemy transaction."""

    stamp = updated_at or dt.datetime.now(dt.timezone.utc)
    for key, value in values.items():
        conn.execute(text(_UPSERT_SQL), _params(key, value, stamp))


def get_configs_from_connection(conn, keys: Iterable[str]) -> dict[str, Any]:
    """Batch read for hot proxy authentication paths using one connection."""

    ordered = list(dict.fromkeys(str(key) for key in keys))
    if not ordered:
        return {}
    params = {f"key_{index}": key for index, key in enumerate(ordered)}
    placeholders = ",".join(f":key_{index}" for index in range(len(ordered)))
    values: dict[str, Any] = {}
    try:
        rows = conn.execute(text(
            f"SELECT key,value FROM {TABLE_APP_CONFIG} WHERE key IN ({placeholders})"
        ), params).fetchall()
        for row in rows:
            key = str(row._mapping["key"])
            values[key] = _decoded(row._mapping["value"], config_spec(key, allow_legacy=True))
    except Exception:
        pass
    return values
