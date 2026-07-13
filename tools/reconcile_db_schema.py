#!/usr/bin/env python3
"""Compare live PostgreSQL inventory with the repository bootstrap owner.

The report is read-only and compares table/column names only. Exact type,
default, constraint and index reconciliation still belongs on a staging
restore, where forward and rollback migrations can be exercised safely.
Application row values are never selected.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import schema as bootstrap_schema
from core.db_migrations import LEDGER_TABLE
from core.db_runtime import get_db_engine
from tools.audit_db_schema import IDENTIFIER, audit


_CREATE_TABLE = re.compile(
    r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
    r"(?P<table>[A-Za-z_][A-Za-z0-9_$]*)\s*\(",
    re.IGNORECASE,
)
_ALTER_COLUMN = re.compile(
    r"\bALTER\s+TABLE\s+(?P<table>[A-Za-z_][A-Za-z0-9_$]*)\s+"
    r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
    r"(?P<column>[A-Za-z_][A-Za-z0-9_$]*)\b",
    re.IGNORECASE,
)
_RUNTIME_DDL = re.compile(
    r"\b(?P<verb>CREATE|ALTER|DROP|TRUNCATE)\s+"
    r"(?:OR\s+REPLACE\s+)?"
    r"(?P<object>TABLE|INDEX|VIEW|EXTENSION|POLICY|TYPE|SEQUENCE|FUNCTION|"
    r"PROCEDURE|TRIGGER|SCHEMA)\b",
    re.IGNORECASE,
)
_CREATE_INDEX = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<index>[A-Za-z_][A-Za-z0-9_$]*)\b",
    re.IGNORECASE,
)
_RUNTIME_DDL_FILE_ALLOWLIST = {
    "api/ai_training_api.py",
    "core/config_store.py",
    "deploy/proxy.py",
}
_RUNTIME_DDL_REFERENCE_ALLOWLIST = {
    "CREATE_AI_DATASET_SNAPSHOTS",
    "CREATE_AI_DATASET_SNAPSHOT_ITEMS",
    "CREATE_AI_EVAL_CASES",
    "CREATE_AI_EVAL_RUNS",
    "CREATE_AI_MODEL_VERSIONS",
    "CREATE_AI_TRAINING_AUDIT",
    "CREATE_APP_CONFIG",
    "CREATE_RAG_CHUNKS",
    "CREATE_RAG_DOCUMENTS",
}
_RUNTIME_DDL_DIRECT_ALLOWLIST: set[str] = set()
_RUNTIME_DDL_INDIRECT_ALLOWLIST: set[str] = set()
_RUNTIME_INDEX_ALLOWLIST: set[str] = set()
_RUNTIME_DDL_SITE_BUDGET = 4
_NON_COLUMN_PREFIXES = {
    "CHECK",
    "CONSTRAINT",
    "EXCLUDE",
    "FOREIGN",
    "PRIMARY",
    "UNIQUE",
}


def _matching_parenthesis(sql: str, opening: int) -> int:
    depth = 0
    quote = ""
    index = opening
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError("unbalanced CREATE TABLE definition")


def _split_top_level(body: str) -> list[str]:
    chunks = []
    start = 0
    depth = 0
    quote = ""
    index = 0
    while index < len(body):
        char = body[index]
        if quote:
            if char == quote:
                if index + 1 < len(body) and body[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            chunks.append(body[start:index])
            start = index + 1
        index += 1
    chunks.append(body[start:])
    return chunks


def _table_columns(sql: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for match in _CREATE_TABLE.finditer(sql):
        opening = match.end() - 1
        closing = _matching_parenthesis(sql, opening)
        body = re.sub(r"--[^\n]*", "", sql[opening + 1:closing])
        columns = result.setdefault(match.group("table"), set())
        for chunk in _split_top_level(body):
            token = re.match(r"\s*\"?([A-Za-z_][A-Za-z0-9_$]*)", chunk)
            if token and token.group(1).upper() not in _NON_COLUMN_PREFIXES:
                columns.add(token.group(1))
    for match in _ALTER_COLUMN.finditer(sql):
        result.setdefault(match.group("table"), set()).add(match.group("column"))
    return result


def declared_inventory() -> dict:
    tables = {
        value
        for name, value in vars(bootstrap_schema).items()
        if name.startswith("TABLE_") and isinstance(value, str)
    }
    columns: dict[str, set[str]] = {}
    ddl_values = [
        value
        for name, value in vars(bootstrap_schema).items()
        if name.startswith("CREATE_") and isinstance(value, str)
    ]
    for ddl in ddl_values:
        for table, names in _table_columns(ddl).items():
            tables.add(table)
            columns.setdefault(table, set()).update(names)
    views = {
        value
        for name, value in vars(bootstrap_schema).items()
        if name.startswith("VIEW_") and isinstance(value, str)
    }
    return {"tables": tables, "columns": columns, "views": views}


def runtime_ddl_inventory() -> dict:
    sites = set()
    indexes: dict[str, set[str]] = {}
    references: dict[str, set[str]] = {}
    direct_statements: dict[str, set[str]] = {}
    indirect_statements: dict[str, set[str]] = {}
    for directory in (ROOT / "api", ROOT / "core", ROOT / "deploy"):
        for path in sorted(directory.rglob("*.py")):
            if path == ROOT / "core" / "db_migrations.py":
                continue
            source = path.read_text(encoding="utf-8")
            relative = path.relative_to(ROOT).as_posix()
            for number, line in enumerate(source.splitlines(), start=1):
                for match in _RUNTIME_DDL.finditer(line):
                    site = f"{relative}:{number}"
                    sites.add(site)
                    signature = (
                        f"{match.group('verb').upper()} "
                        f"{match.group('object').upper()}"
                    )
                    direct_statements.setdefault(signature, set()).add(site)
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Name):
                    continue
                value = getattr(bootstrap_schema, node.id, None)
                is_create_reference = node.id.startswith("CREATE_")
                is_ddl_collection = (
                    isinstance(value, (list, tuple))
                    and any(
                        isinstance(item, str) and _RUNTIME_DDL.search(item)
                        for item in value
                    )
                )
                if not (is_create_reference or is_ddl_collection):
                    continue
                site = f"{relative}:{node.lineno}"
                sites.add(site)
                references.setdefault(node.id, set()).add(site)
                if is_ddl_collection:
                    for statement in value:
                        if not isinstance(statement, str):
                            continue
                        for match in _RUNTIME_DDL.finditer(statement):
                            signature = (
                                f"{match.group('verb').upper()} "
                                f"{match.group('object').upper()}"
                            )
                            indirect_statements.setdefault(
                                signature, set()
                            ).add(site)
                        for match in _CREATE_INDEX.finditer(statement):
                            indexes.setdefault(match.group("index"), set()).add(site)
            for match in _CREATE_INDEX.finditer(source):
                number = source.count("\n", 0, match.start()) + 1
                site = f"{relative}:{number}"
                indexes.setdefault(match.group("index"), set()).add(site)
    files = {site.split(":", 1)[0] for site in sites}
    reference_names = set(references)
    policy_violations = {
        "unexpected_files": sorted(files - _RUNTIME_DDL_FILE_ALLOWLIST),
        "unexpected_references": sorted(
            reference_names - _RUNTIME_DDL_REFERENCE_ALLOWLIST
        ),
        "unexpected_direct_statements": sorted(
            set(direct_statements) - _RUNTIME_DDL_DIRECT_ALLOWLIST
        ),
        "unexpected_indirect_statements": sorted(
            set(indirect_statements) - _RUNTIME_DDL_INDIRECT_ALLOWLIST
        ),
        "unexpected_indexes": sorted(set(indexes) - _RUNTIME_INDEX_ALLOWLIST),
        "site_budget_exceeded_by": max(0, len(sites) - _RUNTIME_DDL_SITE_BUDGET),
    }
    return {
        "sites": sorted(sites),
        "references": {
            name: sorted(reference_sites)
            for name, reference_sites in sorted(references.items())
        },
        "direct_statements": {
            name: sorted(statement_sites)
            for name, statement_sites in sorted(direct_statements.items())
        },
        "indirect_statements": {
            name: sorted(statement_sites)
            for name, statement_sites in sorted(indirect_statements.items())
        },
        "indexes": {
            name: sorted(index_sites)
            for name, index_sites in sorted(indexes.items())
        },
        "policy_violations": policy_violations,
    }


def build_report(snapshot: dict, declared: dict) -> dict:
    schema = snapshot["schema"]
    production_tables = {
        str(row["table_name"]) for row in schema.get("tables", [])
    }
    production_views = {
        str(row["view_name"]) for row in schema.get("views", [])
    }
    live_columns: dict[str, set[str]] = {}
    for row in schema.get("columns", []):
        live_columns.setdefault(str(row["table_name"]), set()).add(
            str(row["column_name"])
        )

    declared_tables = set(declared["tables"])
    internal_tables = {LEDGER_TABLE} & production_tables
    production_application_tables = production_tables - internal_tables
    shared = sorted(production_application_tables & declared_tables)
    column_drift = []
    for table in shared:
        expected = declared["columns"].get(table, set())
        live = live_columns.get(table, set())
        missing_live = sorted(expected - live)
        missing_code = sorted(live - expected)
        if missing_live or missing_code:
            column_drift.append({
                "table_name": table,
                "code_only_columns": missing_live,
                "production_only_columns": missing_code,
            })

    ddl_inventory = runtime_ddl_inventory()
    ddl_sites = ddl_inventory["sites"]
    production_indexes = {
        str(row["index_name"]) for row in schema.get("indexes", [])
    }
    runtime_index_names = set(ddl_inventory["indexes"])
    metrics = {
        str(row["table_name"]): {
            "estimated_rows": max(0, int(row.get("estimated_rows") or 0)),
            "total_bytes": max(0, int(row.get("total_bytes") or 0)),
        }
        for row in snapshot.get("metrics", {}).get("tables", [])
    }
    production_only = sorted(production_application_tables - declared_tables)
    return {
        "mode": "read-only",
        "schema_name": snapshot["schema_name"],
        "production_schema_checksum": snapshot["schema_checksum"],
        "production_table_count": len(production_tables),
        "production_application_table_count": len(production_application_tables),
        "internal_tables": sorted(internal_tables),
        "declared_bootstrap_table_count": len(declared_tables),
        "shared_table_count": len(shared),
        "production_only_tables": production_only,
        "production_only_table_metrics": {
            table: metrics.get(table, {"estimated_rows": 0, "total_bytes": 0})
            for table in production_only
        },
        "code_only_tables": sorted(declared_tables - production_application_tables),
        "column_name_drift": column_drift,
        "production_only_views": sorted(
            production_views - set(declared["views"])
        ),
        "code_only_views": sorted(
            set(declared["views"]) - production_views
        ),
        "runtime_ddl_site_count": len(ddl_sites),
        "runtime_ddl_sites": ddl_sites,
        "runtime_ddl_references": ddl_inventory["references"],
        "runtime_ddl_direct_statements": ddl_inventory["direct_statements"],
        "runtime_ddl_indirect_statements": ddl_inventory["indirect_statements"],
        "runtime_ddl_policy_violations": ddl_inventory["policy_violations"],
        "runtime_index_sites": ddl_inventory["indexes"],
        "runtime_indexes_present_in_production": sorted(
            runtime_index_names & production_indexes
        ),
        "runtime_indexes_missing_in_production": sorted(
            runtime_index_names - production_indexes
        ),
        "definition_drift_requires_staging": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", default="public")
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="return exit code 1 when inventory or column-name drift exists",
    )
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
        snapshot = audit(engine, args.schema)
        report = build_report(snapshot, declared_inventory())
    except Exception as exc:
        print(
            f"schema reconciliation failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    drift = bool(
        report["production_only_tables"]
        or report["code_only_tables"]
        or report["column_name_drift"]
        or report["production_only_views"]
        or report["code_only_views"]
        or any(report["runtime_ddl_policy_violations"].values())
    )
    return int(args.fail_on_drift and drift)


if __name__ == "__main__":
    raise SystemExit(main())
