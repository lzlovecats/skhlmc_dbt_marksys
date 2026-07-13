#!/usr/bin/env python3
"""Inspect and operate the repository-owned PostgreSQL migration ledger.

Every mutating command is a dry-run unless ``--apply`` and its versioned
confirmation phrase are both supplied. The baseline operation only creates
``schema_migrations`` and records a verified catalog checksum; it never alters
an existing application table.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.db_migrations import (
    apply_pending,
    discover_migrations,
    fetch_applied,
    ledger_exists,
    ledger_security,
    load_baseline_manifest,
    lock_transaction,
    plan_history,
    record_baseline,
    rollback_latest,
    validate_catalog,
    verify_existing_history,
)
from core.db_runtime import get_db_engine
from tools.audit_db_schema import audit_connection, snapshot_summary
from version import APP_VERSION


MIGRATIONS_DIR = ROOT / "migrations"
BASELINE_PATH = MIGRATIONS_DIR / "baseline.json"
BASELINE_CONFIRMATION = f"{APP_VERSION}-CREATE-DB-BASELINE"
APPLY_CONFIRMATION = f"{APP_VERSION}-APPLY-DB-MIGRATIONS"
ROLLBACK_CONFIRMATION = f"{APP_VERSION}-ROLLBACK-DB-MIGRATION"


def load_catalog():
    baseline = load_baseline_manifest(BASELINE_PATH)
    migrations = discover_migrations(MIGRATIONS_DIR)
    validate_catalog(baseline, migrations)
    return baseline, migrations


def _read_only_transaction(conn) -> None:
    conn.execute(text("SET TRANSACTION READ ONLY"))
    conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    conn.execute(text("SET LOCAL statement_timeout = '30s'"))


def status_report(engine, baseline, migrations) -> dict:
    with engine.connect() as conn, conn.begin():
        _read_only_transaction(conn)
        exists = ledger_exists(conn, baseline.schema_name)
        rows = fetch_applied(conn, baseline.schema_name) if exists else []
        security = ledger_security(conn, baseline.schema_name)
    plan = plan_history(baseline, migrations, rows)
    return {
        "operation": "status",
        "mode": "read-only",
        "schema_name": baseline.schema_name,
        "ledger_exists": exists,
        "registered_sql_migrations": len(migrations),
        "ledger_security": security,
        **plan,
    }


def baseline_preflight(engine, baseline, migrations) -> dict:
    with engine.connect() as conn, conn.begin():
        _read_only_transaction(conn)
        exists = ledger_exists(conn, baseline.schema_name)
        rows = fetch_applied(conn, baseline.schema_name) if exists else []
        plan = plan_history(baseline, migrations, rows)
        snapshot = audit_connection(conn, baseline.schema_name)
    summary = snapshot_summary(snapshot)
    table_count = int(summary["object_counts"].get("tables", 0))
    checksum_matches = (
        snapshot["schema_checksum"] == baseline.source_schema_checksum
    )
    table_count_matches = table_count == baseline.source_table_count
    already_applied = bool(
        exists and plan["history_valid"] and plan["baseline_applied"]
    )
    ready = already_applied or bool(
        not exists
        and plan["history_valid"]
        and checksum_matches
        and table_count_matches
    )
    return {
        "operation": "baseline",
        "mode": "dry-run",
        "schema_name": baseline.schema_name,
        "ledger_exists": exists,
        "already_applied": already_applied,
        "history_valid": plan["history_valid"],
        "current_schema_checksum": snapshot["schema_checksum"],
        "expected_source_schema_checksum": baseline.source_schema_checksum,
        "schema_checksum_matches_source": checksum_matches,
        "current_table_count": table_count,
        "expected_source_table_count": baseline.source_table_count,
        "table_count_matches_source": table_count_matches,
        "ready_to_apply": ready,
        "application_tables_will_be_altered": False,
        "ledger_table_will_be_created": not exists,
        "confirmation": BASELINE_CONFIRMATION,
    }


def apply_baseline(engine, baseline, migrations) -> dict:
    with engine.begin() as conn:
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        lock_transaction(conn)
        if ledger_exists(conn, baseline.schema_name):
            history = verify_existing_history(conn, baseline, migrations)
            if not history["baseline_applied"]:
                raise RuntimeError("migration ledger exists without baseline")
            created = False
            observed_checksum = None
            observed_table_count = None
        else:
            snapshot = audit_connection(conn, baseline.schema_name)
            summary = snapshot_summary(snapshot)
            observed_checksum = str(snapshot["schema_checksum"])
            observed_table_count = int(
                summary["object_counts"].get("tables", 0)
            )
            if observed_checksum != baseline.source_schema_checksum:
                raise RuntimeError("production schema checksum changed")
            if observed_table_count != baseline.source_table_count:
                raise RuntimeError("production table count changed")
            created = record_baseline(
                conn,
                baseline,
                migrations,
                acquire_lock=False,
            )
    return {
        "operation": "baseline",
        "mode": "apply",
        "schema_name": baseline.schema_name,
        "ledger_created": created,
        "baseline_recorded": True,
        "application_tables_altered": False,
        "verified_source_schema_checksum": observed_checksum,
        "verified_source_table_count": observed_table_count,
    }


def apply_migrations_report(engine, baseline, migrations) -> dict:
    before = status_report(engine, baseline, migrations)
    if not before["history_valid"] or not before["baseline_applied"]:
        raise RuntimeError("valid migration baseline is required")
    applied = apply_pending(engine, baseline, migrations)
    after = status_report(engine, baseline, migrations)
    return {
        "operation": "apply",
        "mode": "apply",
        "applied_versions": applied,
        "at_head": after["at_head"],
        "pending_versions": after["pending_versions"],
    }


def rollback_report(engine, baseline, migrations) -> dict:
    version = rollback_latest(engine, baseline, migrations)
    after = status_report(engine, baseline, migrations)
    return {
        "operation": "rollback",
        "mode": "apply",
        "rolled_back_version": version,
        "at_head": after["at_head"],
        "pending_versions": after["pending_versions"],
    }


def _add_mutation_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the operation; without this flag only report the plan",
    )
    parser.add_argument("--confirm", default="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status")
    _add_mutation_flags(commands.add_parser("baseline"))
    _add_mutation_flags(commands.add_parser("apply"))
    _add_mutation_flags(commands.add_parser("rollback"))
    return parser


def _required_confirmation(command: str) -> str:
    return {
        "baseline": BASELINE_CONFIRMATION,
        "apply": APPLY_CONFIRMATION,
        "rollback": ROLLBACK_CONFIRMATION,
    }[command]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "status"
    if command != "status" and args.apply:
        if args.confirm != _required_confirmation(command):
            print(
                "confirmation phrase does not match; no database access attempted",
                file=sys.stderr,
            )
            return 2
    try:
        baseline, migrations = load_catalog()
    except ValueError as exc:
        print(f"migration catalog is invalid: {exc}", file=sys.stderr)
        return 2

    engine = get_db_engine()
    if engine is None:
        print("Database is not configured.", file=sys.stderr)
        return 2
    try:
        if command == "status":
            report = status_report(engine, baseline, migrations)
        elif command == "baseline":
            report = baseline_preflight(engine, baseline, migrations)
            if args.apply:
                if not report["ready_to_apply"]:
                    print(
                        "baseline preflight failed; no database changes made",
                        file=sys.stderr,
                    )
                    return 1
                report = apply_baseline(engine, baseline, migrations)
        elif command == "apply":
            report = status_report(engine, baseline, migrations)
            report.update({
                "operation": "apply",
                "mode": "dry-run",
                "confirmation": APPLY_CONFIRMATION,
            })
            if args.apply:
                report = apply_migrations_report(engine, baseline, migrations)
        else:
            report = status_report(engine, baseline, migrations)
            candidates = [
                version for version in report["applied_versions"]
                if version != baseline.version
            ]
            report.update({
                "operation": "rollback",
                "mode": "dry-run",
                "rollback_candidate": candidates[-1] if candidates else None,
                "confirmation": ROLLBACK_CONFIRMATION,
            })
            if args.apply:
                report = rollback_report(engine, baseline, migrations)
    except Exception as exc:
        print(
            "database migration operation failed and was rolled back: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if command == "status":
        return int(
            not report["history_valid"] or not report["baseline_applied"]
        )
    if command == "baseline" and not args.apply:
        return int(not report["ready_to_apply"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
