#!/usr/bin/env python3
"""Audit the typed-config rollout without reading or printing secret values."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config_store import config_spec
from deploy.proxy import _get_db_engine


def build_report(legacy_keys: list[str], typed_rows: list[dict]) -> dict:
    legacy = set(legacy_keys)
    typed = {str(row["key"]): row for row in typed_rows}
    unknown = []
    mismatches = []
    for key, row in typed.items():
        try:
            spec = config_spec(key)
        except KeyError:
            unknown.append(key)
            continue
        actual = (
            str(row.get("namespace") or ""),
            str(row.get("value_type") or ""),
            bool(row.get("is_secret")),
            str(row.get("json_type") or ""),
        )
        expected = (spec.namespace, spec.value_type, spec.secret, spec.value_type)
        if actual != expected:
            mismatches.append({"key": key, "expected": expected, "actual": actual})

    password_keys = ("admin_password", "developer_password", "sql_password")
    password_bcrypt = {
        key: bool(typed.get(key, {}).get("credential_is_bcrypt"))
        for key in password_keys
    }
    cookie_secret_strong = bool(
        typed.get("cookie_secret", {}).get("cookie_secret_strong")
    )
    missing_in_typed = sorted(legacy - set(typed))
    report = {
        "legacy_key_count": len(legacy),
        "typed_key_count": len(typed),
        "missing_in_typed": missing_in_typed,
        "typed_only": sorted(set(typed) - legacy),
        "unknown_typed_keys": sorted(unknown),
        "metadata_mismatches": mismatches,
        "password_bcrypt": password_bcrypt,
        "cookie_secret_strong": cookie_secret_strong,
    }
    report["migration_complete"] = not (
        missing_in_typed or unknown or mismatches
    )
    report["rotation_complete"] = (
        all(password_bcrypt.values()) and cookie_secret_strong
    )
    report["bridge_removal_ready"] = (
        report["migration_complete"] and report["rotation_complete"]
    )
    return report


def audit(engine) -> dict:
    with engine.connect() as conn:
        tables = conn.execute(text("""
            SELECT
              to_regclass('public.app_config') IS NOT NULL AS typed_exists,
              to_regclass('public.system_config') IS NOT NULL AS legacy_exists
        """)).mappings().one()
        if not bool(tables["typed_exists"]):
            return {
                "typed_table_exists": False,
                "legacy_table_exists": bool(tables["legacy_exists"]),
                "migration_complete": False,
                "rotation_complete": False,
                "bridge_removal_ready": False,
            }

        legacy_keys = []
        if bool(tables["legacy_exists"]):
            legacy_keys = [
                str(row[0])
                for row in conn.execute(text(
                    "SELECT key FROM system_config ORDER BY key"
                )).fetchall()
            ]
        typed_rows = [dict(row) for row in conn.execute(text("""
            SELECT key,namespace,value_type,is_secret,jsonb_typeof(value) AS json_type,
              CASE WHEN key IN ('admin_password','developer_password','sql_password')
                    AND jsonb_typeof(value)='string'
                   THEN LEFT(value #>> '{}',4) IN ('$2a$','$2b$','$2y$')
                        AND LENGTH(value #>> '{}')=60
                   ELSE NULL END AS credential_is_bcrypt,
              CASE WHEN key='cookie_secret' AND jsonb_typeof(value)='string'
                   THEN LENGTH(value #>> '{}')>=32
                   ELSE NULL END AS cookie_secret_strong
            FROM app_config ORDER BY key
        """)).mappings().all()]
    return {
        "typed_table_exists": True,
        "legacy_table_exists": bool(tables["legacy_exists"]),
        **build_report(legacy_keys, typed_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strict", action="store_true",
        help="return non-zero until migration and credential rotation are complete",
    )
    args = parser.parse_args()
    engine = _get_db_engine()
    if engine is None:
        print("Database is not configured.", file=sys.stderr)
        return 2
    report = audit(engine)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return int(args.strict and not report.get("bridge_removal_ready", False))


if __name__ == "__main__":
    raise SystemExit(main())
