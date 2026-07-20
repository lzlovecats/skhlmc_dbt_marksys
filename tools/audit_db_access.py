#!/usr/bin/env python3
"""Read-only database access-contract audit for browser and runtime roles."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.db_runtime import get_db_engine


COUNT_QUERIES = {
    "browser_relation_privileges": """
        SELECT COUNT(*)
        FROM pg_class relation
        JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
        CROSS JOIN LATERAL aclexplode(
            COALESCE(relation.relacl, acldefault('r', relation.relowner))
        ) privilege
        LEFT JOIN pg_roles grantee ON grantee.oid=privilege.grantee
        WHERE namespace.nspname='public'
          AND relation.relkind IN ('r','p','v','m','f')
          AND (
              privilege.grantee=0
              OR grantee.rolname IN ('anon','authenticated')
          )
    """,
    "browser_sequence_privileges": """
        SELECT COUNT(*)
        FROM pg_class sequence
        JOIN pg_namespace namespace ON namespace.oid=sequence.relnamespace
        CROSS JOIN LATERAL aclexplode(
            COALESCE(sequence.relacl, acldefault('S', sequence.relowner))
        ) privilege
        LEFT JOIN pg_roles grantee ON grantee.oid=privilege.grantee
        WHERE namespace.nspname='public' AND sequence.relkind='S'
          AND (
              privilege.grantee=0
              OR grantee.rolname IN ('anon','authenticated')
          )
    """,
    "browser_schema_privileges": """
        SELECT COUNT(*)
        FROM pg_namespace namespace
        CROSS JOIN LATERAL aclexplode(
            COALESCE(namespace.nspacl, acldefault('n', namespace.nspowner))
        ) privilege
        LEFT JOIN pg_roles grantee ON grantee.oid=privilege.grantee
        WHERE namespace.nspname='public'
          AND (
              privilege.grantee=0
              OR grantee.rolname IN ('anon','authenticated')
          )
    """,
    "current_owner_browser_default_privileges": """
        SELECT COUNT(*)
        FROM pg_default_acl defaults
        CROSS JOIN LATERAL aclexplode(defaults.defaclacl) privilege
        LEFT JOIN pg_roles grantee ON grantee.oid=privilege.grantee
        WHERE defaults.defaclrole=(SELECT oid FROM pg_roles WHERE rolname=CURRENT_USER)
          AND defaults.defaclnamespace=(
              SELECT oid FROM pg_namespace WHERE nspname='public'
          )
          AND (
              privilege.grantee=0
              OR grantee.rolname IN ('anon','authenticated')
          )
    """,
    "managed_owner_browser_default_privileges": """
        SELECT COUNT(*)
        FROM pg_default_acl defaults
        CROSS JOIN LATERAL aclexplode(defaults.defaclacl) privilege
        LEFT JOIN pg_roles grantee ON grantee.oid=privilege.grantee
        WHERE defaults.defaclrole<>(SELECT oid FROM pg_roles WHERE rolname=CURRENT_USER)
          AND defaults.defaclnamespace=(
              SELECT oid FROM pg_namespace WHERE nspname='public'
          )
          AND (
              privilege.grantee=0
              OR grantee.rolname IN ('anon','authenticated')
          )
    """,
}


ROLE_QUERY = """
    SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
           rolinherit, rolbypassrls
    FROM pg_roles
    WHERE rolname='app_backend'
"""


RUNTIME_QUERY = """
    SELECT
        COUNT(*) FILTER (
            WHERE relation.relkind IN ('r','p','f')
              AND relation.relname<>'schema_migrations'
              AND NOT (
                  has_table_privilege('app_backend', relation.oid, 'SELECT')
                  AND has_table_privilege('app_backend', relation.oid, 'INSERT')
                  AND has_table_privilege('app_backend', relation.oid, 'UPDATE')
                  AND has_table_privilege('app_backend', relation.oid, 'DELETE')
              )
        ) AS missing_table_privileges,
        COUNT(*) FILTER (
            WHERE relation.relkind IN ('v','m')
              AND NOT has_table_privilege(
                  'app_backend', relation.oid, 'SELECT'
              )
        ) AS missing_view_privileges,
        COUNT(*) FILTER (
            WHERE pg_get_userbyid(relation.relowner)='app_backend'
        ) AS owned_relations,
        COALESCE(
            has_table_privilege(
                'app_backend', to_regclass('public.schema_migrations'),
                'SELECT,INSERT,UPDATE,DELETE'
            ),
            FALSE
        ) AS ledger_privileges,
        has_schema_privilege('app_backend', 'public', 'USAGE')
            AS schema_usage,
        has_schema_privilege('app_backend', 'public', 'CREATE')
            AS schema_create
    FROM pg_class relation
    JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
    WHERE namespace.nspname='public'
      AND relation.relkind IN ('r','p','v','m','f')
"""


SEQUENCE_QUERY = """
    SELECT COUNT(*) FILTER (
        WHERE NOT has_sequence_privilege(
            'app_backend', sequence.oid, 'USAGE'
        )
    ) AS missing_sequence_privileges
    FROM pg_class sequence
    JOIN pg_namespace namespace ON namespace.oid=sequence.relnamespace
    WHERE namespace.nspname='public' AND sequence.relkind='S'
"""


def audit_access(engine) -> dict:
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL lock_timeout='2s'"))
        conn.execute(text("SET LOCAL statement_timeout='30s'"))
        counts = {
            name: int(conn.execute(text(sql)).scalar_one())
            for name, sql in COUNT_QUERIES.items()
        }
        role = conn.execute(text(ROLE_QUERY)).mappings().one_or_none()
        runtime = None
        sequences = None
        if role is not None:
            runtime = dict(conn.execute(text(RUNTIME_QUERY)).mappings().one())
            sequences = dict(
                conn.execute(text(SEQUENCE_QUERY)).mappings().one()
            )

    role_secure = bool(
        role is not None
        and not role["rolcanlogin"]
        and not role["rolsuper"]
        and not role["rolcreatedb"]
        and not role["rolcreaterole"]
        and not role["rolbypassrls"]
    )
    runtime_ready = bool(
        runtime is not None
        and int(runtime["missing_table_privileges"] or 0) == 0
        and int(runtime["missing_view_privileges"] or 0) == 0
        and int(runtime["owned_relations"] or 0) == 0
        and not runtime["ledger_privileges"]
        and runtime["schema_usage"]
        and not runtime["schema_create"]
        and int(sequences["missing_sequence_privileges"] or 0) == 0
    )
    browser_closed = all(
        counts[key] == 0 for key in (
            "browser_relation_privileges",
            "browser_sequence_privileges",
            "browser_schema_privileges",
            "current_owner_browser_default_privileges",
        )
    )
    return {
        "mode": "read-only",
        "browser_access_closed": browser_closed,
        "runtime_role_secure": role_secure,
        "runtime_role_ready": runtime_ready,
        "ready_for_runtime_cutover": (
            browser_closed and role_secure and runtime_ready
        ),
        "counts": counts,
        "app_backend": dict(role) if role is not None else None,
        "runtime_privileges": runtime,
        "sequence_privileges": sequences,
        "managed_default_privileges_are_advisory": True,
    }


def main() -> int:
    engine = get_db_engine()
    if engine is None:
        print("Database is not configured.", file=sys.stderr)
        return 2
    try:
        report = audit_access(engine)
    except Exception as exc:
        print(f"database access audit failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return int(not report["ready_for_runtime_cutover"])


if __name__ == "__main__":
    raise SystemExit(main())
