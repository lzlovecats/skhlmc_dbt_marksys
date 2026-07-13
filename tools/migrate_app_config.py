#!/usr/bin/env python3
"""Copy legacy system_config rows into typed app_config without deleting legacy.

Dry-run is the default and reads key names only. Apply mode is one PostgreSQL
transaction, never overwrites an existing typed value, and rolls back unless
every legacy key has valid registry metadata in ``app_config``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config_store import config_spec, migrate_legacy_config
from core.db_runtime import get_db_engine
from tools.audit_app_config import audit_connection
from version import APP_VERSION


CONFIRMATION = f"{APP_VERSION}-MIGRATE-APP-CONFIG"


def classify_keys(keys: list[str]) -> dict:
    registered = []
    unknown = []
    for key in sorted(set(str(item) for item in keys)):
        try:
            config_spec(key)
            registered.append(key)
        except KeyError:
            unknown.append(key)
    return {"registered_keys": registered, "unknown_keys": unknown}


def preflight(engine) -> dict:
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        tables = conn.execute(text("""
            SELECT
              to_regclass('public.app_config') IS NOT NULL AS typed_exists,
              to_regclass('public.system_config') IS NOT NULL AS legacy_exists
        """)).mappings().one()
        legacy_keys = []
        if bool(tables["legacy_exists"]):
            legacy_keys = [
                str(row[0])
                for row in conn.execute(text(
                    "SELECT key FROM system_config ORDER BY key"
                )).fetchall()
            ]
        typed_keys = []
        if bool(tables["typed_exists"]):
            typed_keys = [
                str(row[0])
                for row in conn.execute(text(
                    "SELECT key FROM app_config ORDER BY key"
                )).fetchall()
            ]
    classification = classify_keys(legacy_keys)
    missing = sorted(set(legacy_keys) - set(typed_keys))
    return {
        "mode": "dry-run",
        "legacy_table_exists": bool(tables["legacy_exists"]),
        "typed_table_exists": bool(tables["typed_exists"]),
        "legacy_key_count": len(set(legacy_keys)),
        "typed_key_count": len(set(typed_keys)),
        "keys_missing_in_typed": missing,
        "unknown_legacy_keys": classification["unknown_keys"],
        "ready_to_apply": bool(tables["legacy_exists"])
        and not classification["unknown_keys"],
        "legacy_table_will_be_preserved": True,
        "confirmation": CONFIRMATION,
    }


def apply_migration(engine) -> dict:
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text("SET LOCAL statement_timeout = '60s'"))
        conn.execute(text(
            "SELECT pg_advisory_xact_lock(hashtext('skhlmc_app_config_migration'))"
        ))
        result = migrate_legacy_config(conn)
        health = audit_connection(conn)
        if not health.get("legacy_table_exists"):
            raise RuntimeError("legacy system_config table is missing")
        if not health.get("migration_complete"):
            raise RuntimeError("typed config metadata verification failed")
    return {
        "mode": "apply",
        "legacy_table_preserved": True,
        "legacy_keys_seen": int(result["seen"]),
        "typed_keys_inserted": int(result["inserted"]),
        "unknown_keys": int(result["unknown"]),
        "health": health,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and args.confirm != CONFIRMATION:
        print(
            "confirmation phrase does not match; no database access attempted",
            file=sys.stderr,
        )
        return 2
    engine = get_db_engine()
    if engine is None:
        print("Database is not configured.", file=sys.stderr)
        return 2
    try:
        report = preflight(engine)
        if args.apply:
            if not report["ready_to_apply"]:
                print(
                    "preflight failed; no database changes made",
                    file=sys.stderr,
                )
                return 1
            report = apply_migration(engine)
    except Exception as exc:
        print(
            f"app_config migration failed and was rolled back: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
