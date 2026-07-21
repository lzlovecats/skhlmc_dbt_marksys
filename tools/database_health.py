#!/usr/bin/env python3
"""One read-only operational view of database health and feature lifecycle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config_store import CONFIG_SPECS
from core.db_runtime import get_db_engine
from core.schema_features import FEATURE_CATALOG, feature_catalog_report
from tools.audit_db_access import audit_access
from tools.audit_db_schema import IDENTIFIER, audit, snapshot_summary
from tools.manage_db_migrations import load_catalog, status_report
from tools.reconcile_db_schema import build_report, declared_inventory
from version import APP_VERSION, REQUIRED_SCHEMA_MIGRATION


ACTIVITY_QUERY = """
    SELECT relname AS table_name,n_live_tup,n_dead_tup,seq_scan,idx_scan,
           n_tup_ins,n_tup_upd,n_tup_del,last_analyze,last_autoanalyze
    FROM pg_stat_user_tables
    WHERE schemaname=:schema
    ORDER BY (COALESCE(seq_scan,0)+COALESCE(idx_scan,0)) DESC,relname
"""

R2_COVERAGE_QUERY = """
    SELECT 'tts_voice_recordings' source,COUNT(*) rows,
           COUNT(*) FILTER (WHERE r2_key IS NOT NULL) primary_keys,
           NULL::bigint secondary_keys,
           COUNT(*) FILTER (WHERE r2_key IS NULL) missing_primary,
           NULL::bigint missing_secondary
    FROM public.tts_voice_recordings
    UNION ALL
    SELECT 'match_photos',COUNT(*),
           COUNT(*) FILTER (WHERE r2_key IS NOT NULL),
           COUNT(*) FILTER (WHERE thumbnail_r2_key IS NOT NULL),
           COUNT(*) FILTER (WHERE r2_key IS NULL),
           COUNT(*) FILTER (WHERE thumbnail_r2_key IS NULL)
    FROM public.match_photos
"""


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    return [dict(row) for row in conn.execute(text(sql), params or {}).mappings().all()]


def _config_report(conn) -> dict:
    rows = _rows(conn, """SELECT key,namespace,value_type,is_secret
        FROM public.app_config ORDER BY key""")
    actual = {str(row["key"]): row for row in rows}
    mismatches = []
    for key in sorted(set(actual) & set(CONFIG_SPECS)):
        row = actual[key]
        spec = CONFIG_SPECS[key]
        observed = (str(row["namespace"]), str(row["value_type"]), bool(row["is_secret"]))
        expected = (spec.namespace, spec.value_type, spec.secret)
        if observed != expected:
            mismatches.append({"key": key, "observed": observed, "expected": expected})
    return {
        "row_count": len(rows),
        "unknown_keys": sorted(set(actual) - set(CONFIG_SPECS)),
        "missing_optional_keys": sorted(set(CONFIG_SPECS) - set(actual)),
        "classification_mismatches": mismatches,
        "values_read": False,
    }


def _feature_report(conn) -> dict:
    report = feature_catalog_report()
    for name, definition in FEATURE_CATALOG.items():
        marker = None
        tables = []
        for table_name in definition.tables:
            row = conn.execute(text("""SELECT
                to_regclass(:relation) IS NOT NULL present,
                COALESCE(obj_description(to_regclass(:relation),'pg_class'),'') marker"""), {
                "relation": f"public.{table_name}",
            }).mappings().one()
            present = bool(row["present"])
            tables.append({"name": table_name, "present": present})
            if table_name == definition.tables[0] and present:
                marker = str(row["marker"] or "")
        expected_marker = (
            f"skhlmc-feature:{name}:{definition.migration_version}"
            if definition.migration_version else None
        )
        present_count = sum(item["present"] for item in tables)
        if not definition.migration_version:
            state = "disabled"
        elif marker != expected_marker:
            state = "disabled"
        elif present_count == len(tables):
            state = "ready"
        elif present_count:
            state = "partial"
        else:
            state = "absent"
        report[name].update({
            "state": state, "expected_marker": expected_marker,
            "observed_marker": marker, "table_presence": tables,
        })
    return report


def _activity_report(conn, schema_name: str) -> dict:
    rows = _rows(conn, ACTIVITY_QUERY, {"schema": schema_name})
    return {
        "tables": rows,
        "totals": {
            "live_rows": sum(int(row["n_live_tup"] or 0) for row in rows),
            "dead_rows": sum(int(row["n_dead_tup"] or 0) for row in rows),
            "inserts": sum(int(row["n_tup_ins"] or 0) for row in rows),
            "updates": sum(int(row["n_tup_upd"] or 0) for row in rows),
            "deletes": sum(int(row["n_tup_del"] or 0) for row in rows),
        },
    }


def build_health_report(engine, schema_name: str = "public") -> dict:
    baseline, migrations = load_catalog()
    migration = status_report(engine, baseline, migrations)
    snapshot = audit(engine, schema_name)
    schema_summary = snapshot_summary(snapshot)
    table_sizes = sorted(
        snapshot["metrics"]["tables"],
        key=lambda row: int(row.get("total_bytes") or 0), reverse=True,
    )
    reconciliation = build_report(snapshot, declared_inventory())
    access = audit_access(engine)
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL lock_timeout='2s'"))
        conn.execute(text("SET LOCAL statement_timeout='30s'"))
        config = _config_report(conn)
        features = _feature_report(conn)
        activity = _activity_report(conn, schema_name)
        r2_coverage = _rows(conn, R2_COVERAGE_QUERY)
    applied = set(migration.get("applied_versions") or [])
    unprovisioned = {
        table
        for definition in FEATURE_CATALOG.values()
        if definition.migration_version is None
        for table in definition.tables
    }
    drift = bool(
        reconciliation["production_only_tables"]
        or set(reconciliation["code_only_tables"]) - unprovisioned
        or reconciliation["column_name_drift"]
        or reconciliation["production_only_views"]
        or reconciliation["code_only_views"]
        or any(reconciliation["runtime_ddl_policy_violations"].values())
    )
    checks = {
        "migration_history_valid": bool(migration.get("history_valid")),
        "repository_migration_head_applied": bool(migration.get("at_head")),
        "release_minimum_schema_applied": REQUIRED_SCHEMA_MIGRATION in applied,
        "schema_inventory_aligned": not drift,
        "browser_access_closed": bool(access["browser_access_closed"]),
        "runtime_role_ready": bool(access["runtime_role_ready"]),
        "config_classifications_valid": not config["unknown_keys"] and not config["classification_mismatches"],
        "r2_keys_complete": all(
            int(row["missing_primary"] or 0) == 0
            and (row["missing_secondary"] is None or int(row["missing_secondary"] or 0) == 0)
            for row in r2_coverage
        ),
    }
    return {
        "operation": "database-health", "mode": "read-only",
        "release": {
            "app_version": APP_VERSION,
            "minimum_schema_migration": REQUIRED_SCHEMA_MIGRATION,
            "repository_migration_head": migrations[-1].version if migrations else baseline.version,
            "database_ahead_is_allowed": True,
        },
        "healthy": all(checks.values()), "checks": checks,
        "migrations": migration, "schema": schema_summary,
        "table_sizes": table_sizes,
        "schema_reconciliation": reconciliation, "access": access,
        "config": config, "features": features, "activity": activity,
        "r2_coverage": r2_coverage,
    }


def compact_health_report(report: dict) -> dict:
    return {
        "operation": report["operation"], "mode": report["mode"],
        "healthy": report["healthy"], "checks": report["checks"],
        "release": report["release"],
        "migrations": {
            key: report["migrations"].get(key)
            for key in (
                "history_valid", "at_head", "migration_head",
                "pending_versions", "unknown_applied_versions",
                "checksum_mismatches",
            )
        },
        "schema": report["schema"],
        "largest_tables": report["table_sizes"][:10],
        "activity_totals": report["activity"]["totals"],
        "config": report["config"],
        "features": {
            name: {
                "state": feature["state"],
                "lifecycle": feature["lifecycle"],
                "migration_version": feature["migration_version"],
                "retention": feature["retention"],
                "missing_tables": [
                    item["name"] for item in feature["table_presence"]
                    if not item["present"]
                ],
            }
            for name, feature in report["features"].items()
        },
        "r2_coverage": report["r2_coverage"],
        "details_hint": "rerun with --details for full schema, access and table activity",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", default="public")
    parser.add_argument("--fail-on-issues", action="store_true")
    parser.add_argument("--details", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not IDENTIFIER.fullmatch(args.schema):
        print("Invalid schema identifier.", file=sys.stderr)
        return 2
    engine = get_db_engine()
    if engine is None:
        print("Database is not configured.", file=sys.stderr)
        return 2
    try:
        report = build_health_report(engine, args.schema)
    except Exception as exc:
        print(f"database health audit failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    output = report if args.details else compact_health_report(report)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return int(args.fail_on_issues and not report["healthy"])


if __name__ == "__main__":
    raise SystemExit(main())
