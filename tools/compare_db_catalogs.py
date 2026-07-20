#!/usr/bin/env python3
"""Compare two full audit_db_schema snapshots by application semantics.

Physical column order, relation owners and PostgreSQL 18's synthetic NOT NULL
constraints are intentionally ignored.  Types, nullability, defaults,
constraints, indexes, views and sequence contracts remain exact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SECTION_SPECS = {
    "tables": (
        ("table_name",),
        ("partitioned", "rls_enabled", "rls_forced"),
    ),
    "columns": (
        ("table_name", "column_name"),
        (
            "data_type", "nullable", "default_expression",
            "identity_kind", "generated_kind",
        ),
    ),
    "constraints": (
        ("table_name", "constraint_name"),
        (
            "constraint_type", "definition", "validated",
            "deferrable", "initially_deferred",
        ),
    ),
    "indexes": (
        ("table_name", "index_name"),
        (
            "primary_index", "unique_index", "valid", "ready",
            "definition",
        ),
    ),
    "views": (
        ("view_name",),
        ("view_kind", "definition"),
    ),
    "sequences": (
        ("sequence_name",),
        (
            "data_type", "start_value", "increment_by", "min_value",
            "max_value", "cycles",
        ),
    ),
}


def _included(section: str, row: dict) -> bool:
    if row.get("table_name") == "schema_migrations":
        return False
    if (
        section == "constraints"
        and str(row.get("constraint_name") or "").endswith("_not_null")
    ):
        return False
    return True


def semantic_catalog(snapshot: dict) -> dict[str, dict[tuple[str, ...], dict]]:
    schema = snapshot.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("full audit snapshot with schema definitions required")
    catalog = {}
    for section, (key_fields, value_fields) in SECTION_SPECS.items():
        rows = schema.get(section)
        if not isinstance(rows, list):
            raise ValueError(f"audit snapshot is missing {section}")
        section_catalog = {}
        for row in rows:
            if not isinstance(row, dict) or not _included(section, row):
                continue
            key = tuple(str(row.get(field)) for field in key_fields)
            section_catalog[key] = {
                field: row.get(field) for field in value_fields
            }
        catalog[section] = section_catalog
    return catalog


def compare_snapshots(expected: dict, actual: dict) -> dict:
    expected_catalog = semantic_catalog(expected)
    actual_catalog = semantic_catalog(actual)
    sections = {}
    difference_count = 0
    for section in SECTION_SPECS:
        expected_rows = expected_catalog[section]
        actual_rows = actual_catalog[section]
        missing = sorted(expected_rows.keys() - actual_rows.keys())
        unexpected = sorted(actual_rows.keys() - expected_rows.keys())
        changed = []
        for key in sorted(expected_rows.keys() & actual_rows.keys()):
            if expected_rows[key] != actual_rows[key]:
                changed.append({
                    "key": list(key),
                    "expected": expected_rows[key],
                    "actual": actual_rows[key],
                })
        section_count = len(missing) + len(unexpected) + len(changed)
        difference_count += section_count
        sections[section] = {
            "missing": [list(key) for key in missing],
            "unexpected": [list(key) for key in unexpected],
            "changed": changed,
            "difference_count": section_count,
        }
    return {
        "semantic_catalog_match": difference_count == 0,
        "difference_count": difference_count,
        "sections": sections,
    }


def _load(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("audit snapshot must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("expected")
    parser.add_argument("actual")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = compare_snapshots(_load(args.expected), _load(args.actual))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"catalog comparison failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return int(not report["semantic_catalog_match"])


if __name__ == "__main__":
    raise SystemExit(main())
