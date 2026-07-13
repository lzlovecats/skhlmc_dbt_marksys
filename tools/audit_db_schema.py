#!/usr/bin/env python3
"""Create a deterministic, read-only PostgreSQL schema baseline.

The default snapshot reads catalogs and estimated row counts only. Exact
``COUNT(*)`` scans are opt-in for a staging restore and never run implicitly.
No application table values are selected.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deploy.proxy import _get_db_engine


FORMAT_VERSION = 1
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")

CATALOG_QUERIES = {
    "tables": """
        SELECT c.relname AS table_name,
               pg_get_userbyid(c.relowner) AS owner,
               c.relkind='p' AS partitioned,
               c.relrowsecurity AS rls_enabled,
               c.relforcerowsecurity AS rls_forced,
               obj_description(c.oid,'pg_class') AS comment
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema AND c.relkind IN ('r','p')
        ORDER BY c.relname
    """,
    "columns": """
        SELECT c.relname AS table_name,
               a.attnum AS ordinal_position,
               a.attname AS column_name,
               format_type(a.atttypid,a.atttypmod) AS data_type,
               NOT a.attnotnull AS nullable,
               pg_get_expr(d.adbin,d.adrelid) AS default_expression,
               NULLIF(a.attidentity,'') AS identity_kind,
               NULLIF(a.attgenerated,'') AS generated_kind,
               col_description(c.oid,a.attnum) AS comment
        FROM pg_attribute a
        JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE n.nspname=:schema AND c.relkind IN ('r','p')
          AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname,a.attnum
    """,
    "constraints": """
        SELECT c.relname AS table_name,
               con.conname AS constraint_name,
               con.contype AS constraint_type,
               pg_get_constraintdef(con.oid,true) AS definition,
               con.convalidated AS validated,
               con.condeferrable AS deferrable,
               con.condeferred AS initially_deferred
        FROM pg_constraint con
        JOIN pg_class c ON c.oid=con.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema
        ORDER BY c.relname,con.conname
    """,
    "indexes": """
        SELECT t.relname AS table_name,
               i.relname AS index_name,
               ix.indisprimary AS primary_index,
               ix.indisunique AS unique_index,
               ix.indisvalid AS valid,
               ix.indisready AS ready,
               pg_get_indexdef(i.oid) AS definition
        FROM pg_index ix
        JOIN pg_class t ON t.oid=ix.indrelid
        JOIN pg_class i ON i.oid=ix.indexrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=:schema
        ORDER BY t.relname,i.relname
    """,
    "views": """
        SELECT c.relname AS view_name,
               CASE c.relkind WHEN 'm' THEN 'materialized' ELSE 'view' END AS view_kind,
               pg_get_userbyid(c.relowner) AS owner,
               pg_get_viewdef(c.oid,true) AS definition
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema AND c.relkind IN ('v','m')
        ORDER BY c.relname
    """,
    "functions": """
        SELECT p.proname AS function_name,
               pg_get_function_identity_arguments(p.oid) AS identity_arguments,
               pg_get_userbyid(p.proowner) AS owner,
               pg_get_function_result(p.oid) AS result_type,
               l.lanname AS language,
               p.provolatile AS volatility,
               p.proparallel AS parallel_safety,
               p.prosecdef AS security_definer,
               pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname=:schema
        ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)
    """,
    "triggers": """
        SELECT c.relname AS table_name,
               t.tgname AS trigger_name,
               t.tgenabled AS enabled_mode,
               pg_get_triggerdef(t.oid,true) AS definition
        FROM pg_trigger t
        JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema AND NOT t.tgisinternal
        ORDER BY c.relname,t.tgname
    """,
    "policies": """
        SELECT c.relname AS table_name,
               p.polname AS policy_name,
               p.polpermissive AS permissive,
               CASE p.polcmd
                   WHEN 'r' THEN 'SELECT' WHEN 'a' THEN 'INSERT'
                   WHEN 'w' THEN 'UPDATE' WHEN 'd' THEN 'DELETE'
                   ELSE 'ALL'
               END AS command,
               ARRAY(
                   SELECT CASE role_oid
                       WHEN 0 THEN 'PUBLIC'
                       ELSE pg_get_userbyid(role_oid)
                   END
                   FROM unnest(p.polroles) AS role_oid
                   ORDER BY 1
               ) AS roles,
               pg_get_expr(p.polqual,p.polrelid) AS using_expression,
               pg_get_expr(p.polwithcheck,p.polrelid) AS check_expression
        FROM pg_policy p
        JOIN pg_class c ON c.oid=p.polrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema
        ORDER BY c.relname,p.polname
    """,
    "grants": """
        SELECT c.relname AS relation_name,
               CASE c.relkind
                   WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized_view'
                   ELSE 'table'
               END AS relation_kind,
               pg_get_userbyid(acl.grantor) AS grantor,
               CASE acl.grantee
                   WHEN 0 THEN 'PUBLIC'
                   ELSE pg_get_userbyid(acl.grantee)
               END AS grantee,
               acl.privilege_type,
               acl.is_grantable
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(
            COALESCE(c.relacl,acldefault('r',c.relowner))
        ) AS acl
        WHERE n.nspname=:schema AND c.relkind IN ('r','p','v','m')
        ORDER BY c.relname,grantee,acl.privilege_type
    """,
    "function_grants": """
        SELECT p.proname AS function_name,
               pg_get_function_identity_arguments(p.oid) AS identity_arguments,
               pg_get_userbyid(acl.grantor) AS grantor,
               CASE acl.grantee
                   WHEN 0 THEN 'PUBLIC'
                   ELSE pg_get_userbyid(acl.grantee)
               END AS grantee,
               acl.privilege_type,
               acl.is_grantable
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace
        CROSS JOIN LATERAL aclexplode(
            COALESCE(p.proacl,acldefault('f',p.proowner))
        ) AS acl
        WHERE n.nspname=:schema
        ORDER BY p.proname,identity_arguments,grantee,acl.privilege_type
    """,
    "schema_grants": """
        SELECT n.nspname AS schema_name,
               pg_get_userbyid(n.nspowner) AS owner,
               pg_get_userbyid(acl.grantor) AS grantor,
               CASE acl.grantee
                   WHEN 0 THEN 'PUBLIC'
                   ELSE pg_get_userbyid(acl.grantee)
               END AS grantee,
               acl.privilege_type,
               acl.is_grantable
        FROM pg_namespace n
        CROSS JOIN LATERAL aclexplode(
            COALESCE(n.nspacl,acldefault('n',n.nspowner))
        ) AS acl
        WHERE n.nspname=:schema
        ORDER BY grantee,acl.privilege_type
    """,
    "default_grants": """
        SELECT pg_get_userbyid(d.defaclrole) AS owner,
               COALESCE(n.nspname,'*') AS schema_name,
               d.defaclobjtype AS object_type,
               pg_get_userbyid(acl.grantor) AS grantor,
               CASE acl.grantee
                   WHEN 0 THEN 'PUBLIC'
                   ELSE pg_get_userbyid(acl.grantee)
               END AS grantee,
               acl.privilege_type,
               acl.is_grantable
        FROM pg_default_acl d
        LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
        CROSS JOIN LATERAL aclexplode(d.defaclacl) AS acl
        WHERE d.defaclnamespace=0 OR n.nspname=:schema
        ORDER BY owner,schema_name,d.defaclobjtype,grantee,acl.privilege_type
    """,
    "types": """
        SELECT t.typname AS type_name,
               t.typtype AS type_kind,
               pg_get_userbyid(t.typowner) AS owner,
               NULLIF(format_type(t.typbasetype,t.typtypmod),'-') AS base_type,
               t.typnotnull AS not_null,
               t.typdefault AS default_expression,
               ARRAY(
                   SELECT e.enumlabel
                   FROM pg_enum e
                   WHERE e.enumtypid=t.oid
                   ORDER BY e.enumsortorder
               ) AS enum_labels
        FROM pg_type t
        JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname=:schema AND t.typtype IN ('d','e')
        ORDER BY t.typname
    """,
    "sequences": """
        SELECT c.relname AS sequence_name,
               pg_get_userbyid(c.relowner) AS owner,
               format_type(s.seqtypid,NULL) AS data_type,
               s.seqstart AS start_value,
               s.seqincrement AS increment_by,
               s.seqmin AS min_value,
               s.seqmax AS max_value,
               s.seqcycle AS cycles
        FROM pg_sequence s
        JOIN pg_class c ON c.oid=s.seqrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema
        ORDER BY c.relname
    """,
    "sequence_grants": """
        SELECT c.relname AS sequence_name,
               pg_get_userbyid(acl.grantor) AS grantor,
               CASE acl.grantee
                   WHEN 0 THEN 'PUBLIC'
                   ELSE pg_get_userbyid(acl.grantee)
               END AS grantee,
               acl.privilege_type,
               acl.is_grantable
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(
            COALESCE(c.relacl,acldefault('S',c.relowner))
        ) AS acl
        WHERE n.nspname=:schema AND c.relkind='S'
        ORDER BY c.relname,grantee,acl.privilege_type
    """,
    "extensions": """
        SELECT e.extname AS extension_name,
               e.extversion AS version,
               pg_get_userbyid(e.extowner) AS owner,
               n.nspname AS installed_schema
        FROM pg_extension e
        JOIN pg_namespace n ON n.oid=e.extnamespace
        ORDER BY e.extname
    """,
}

METRICS_QUERY = """
    SELECT c.relname AS table_name,
           c.reltuples::bigint AS estimated_rows,
           pg_relation_size(c.oid) AS table_bytes,
           pg_indexes_size(c.oid) AS index_bytes,
           pg_total_relation_size(c.oid) AS total_bytes
    FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname=:schema AND c.relkind IN ('r','p')
    ORDER BY c.relname
"""


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _rows(conn, sql: str, schema_name: str) -> list[dict]:
    return [
        _json_safe(dict(row))
        for row in conn.execute(
            text(sql), {"schema": schema_name}
        ).mappings().all()
    ]


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _exact_counts(conn, schema_name: str, tables: list[dict]) -> dict[str, int]:
    result = {}
    schema_sql = _quote_identifier(schema_name)
    for table in tables:
        name = str(table["table_name"])
        count = conn.execute(
            text(f"SELECT COUNT(*) FROM {schema_sql}.{_quote_identifier(name)}")
        ).scalar_one()
        result[name] = int(count)
    return result


def schema_checksum(schema: dict) -> str:
    encoded = json.dumps(
        _json_safe(schema),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_snapshot(
    schema_name: str,
    server_version: str,
    schema_sections: dict[str, list[dict]],
    metrics: list[dict],
    exact_counts: dict[str, int] | None = None,
) -> dict:
    schema = _json_safe(schema_sections)
    return {
        "format_version": FORMAT_VERSION,
        "schema_name": schema_name,
        "server_version": str(server_version),
        "schema_checksum": schema_checksum(schema),
        "schema": schema,
        "metrics": {
            "row_count_mode": "exact" if exact_counts is not None else "estimated",
            "tables": _json_safe(metrics),
            "exact_row_counts": exact_counts,
        },
    }


def audit(engine, schema_name: str, *, exact_row_counts: bool = False) -> dict:
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL lock_timeout = '2s'"))
        conn.execute(text("SET LOCAL statement_timeout = '30s'"))
        server_version = conn.execute(text("SHOW server_version")).scalar_one()
        sections = {
            name: _rows(conn, sql, schema_name)
            for name, sql in CATALOG_QUERIES.items()
        }
        metrics = _rows(conn, METRICS_QUERY, schema_name)
        counts = (
            _exact_counts(conn, schema_name, sections["tables"])
            if exact_row_counts
            else None
        )
    return build_snapshot(
        schema_name,
        str(server_version),
        sections,
        metrics,
        counts,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", default="public")
    parser.add_argument(
        "--exact-row-counts",
        action="store_true",
        help="scan every table with COUNT(*); use only on a staging restore",
    )
    parser.add_argument(
        "--expect-checksum",
        default="",
        help="return exit code 1 when the deterministic schema checksum differs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not IDENTIFIER.fullmatch(args.schema):
        print("Invalid schema identifier.", file=sys.stderr)
        return 2
    engine = _get_db_engine()
    if engine is None:
        print("Database is not configured.", file=sys.stderr)
        return 2
    try:
        snapshot = audit(
            engine,
            args.schema,
            exact_row_counts=args.exact_row_counts,
        )
    except Exception as exc:
        print(f"Schema audit failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
    expected = args.expect_checksum.strip().lower()
    return int(bool(expected) and snapshot["schema_checksum"].lower() != expected)


if __name__ == "__main__":
    raise SystemExit(main())
