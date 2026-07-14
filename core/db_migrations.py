"""Small, deterministic PostgreSQL migration ledger and SQL-file runner.

The production baseline is metadata only: it records the catalog checksum that
already exists and never tries to recreate existing tables. Later migrations
must be paired ``*.up.sql``/``*.down.sql`` files. The runner owns transaction
boundaries so a failed migration and its ledger insert roll back together.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from sqlalchemy import text


LEDGER_TABLE = "schema_migrations"
MIGRATION_LOCK = "skhlmc_schema_migrations"
RESTRICTED_ROLES = ("anon", "authenticated", "app_backend")

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_VERSION = re.compile(r"\d{8}_\d{4}")
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_CHECKSUM = re.compile(r"[0-9a-f]{64}")
_MIGRATION_FILE = re.compile(
    r"(?P<version>\d{8}_\d{4})_(?P<name>[a-z][a-z0-9_]*)"
    r"\.(?P<direction>up|down)\.sql"
)
_CREATE_TABLE_STATEMENT = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(?:[A-Za-z_][A-Za-z0-9_$]*)\.)?"
    r"(?P<table>[A-Za-z_][A-Za-z0-9_$]*)\b",
    re.IGNORECASE,
)
_REVOKE_TABLE_STATEMENT = re.compile(
    r"\bREVOKE\s+ALL\s+PRIVILEGES\s+ON\s+TABLE\s+"
    r"(?P<tables>.*?)\s+FROM\s+(?P<roles>[^;]+);",
    re.IGNORECASE | re.DOTALL,
)
_BROWSER_ROLES = {"public", "anon", "authenticated"}
_LEGACY_PRIVILEGE_COMPANIONS = {
    "20260713_0001": "20260713_0002",
}
_BASELINE_FIELDS = {
    "version",
    "name",
    "schema_name",
    "source_schema_checksum",
    "source_table_count",
    "captured_on",
}
_TRANSACTION_CONTROLS = {"BEGIN", "COMMIT", "ROLLBACK", "START"}


@dataclass(frozen=True)
class BaselineManifest:
    version: str
    name: str
    schema_name: str
    source_schema_checksum: str
    source_table_count: int
    captured_on: str
    checksum: str


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up_sql: str
    down_sql: str
    checksum: str


def _canonical_checksum(value: Mapping) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_sql(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def migration_checksum(
    version: str,
    name: str,
    up_sql: str,
    down_sql: str,
) -> str:
    return _canonical_checksum({
        "version": version,
        "name": name,
        "up_sql": _normalized_sql(up_sql),
        "down_sql": _normalized_sql(down_sql),
    })


def _baseline_checksum(payload: Mapping) -> str:
    canonical = {key: payload[key] for key in sorted(_BASELINE_FIELDS)}
    return _canonical_checksum(canonical)


def load_baseline_manifest(path: Path) -> BaselineManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid baseline manifest: {path.name}") from exc
    if not isinstance(payload, dict) or set(payload) != _BASELINE_FIELDS:
        raise ValueError("baseline manifest fields do not match the required format")

    version = str(payload["version"])
    name = str(payload["name"])
    schema_name = str(payload["schema_name"])
    source_checksum = str(payload["source_schema_checksum"]).lower()
    captured_on = str(payload["captured_on"])
    table_count = payload["source_table_count"]
    if not _VERSION.fullmatch(version):
        raise ValueError("invalid baseline version")
    if not _NAME.fullmatch(name):
        raise ValueError("invalid baseline name")
    if not _IDENTIFIER.fullmatch(schema_name):
        raise ValueError("invalid baseline schema name")
    if not _CHECKSUM.fullmatch(source_checksum):
        raise ValueError("invalid baseline source checksum")
    if (
        isinstance(table_count, bool)
        or not isinstance(table_count, int)
        or table_count < 1
    ):
        raise ValueError("invalid baseline table count")
    try:
        dt.date.fromisoformat(captured_on)
    except ValueError as exc:
        raise ValueError("invalid baseline capture date") from exc

    normalized = dict(payload)
    normalized["source_schema_checksum"] = source_checksum
    return BaselineManifest(
        version=version,
        name=name,
        schema_name=schema_name,
        source_schema_checksum=source_checksum,
        source_table_count=table_count,
        captured_on=captured_on,
        checksum=_baseline_checksum(normalized),
    )


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    index = start + 1
    while index < len(sql):
        if sql[index] == quote:
            if index + 1 < len(sql) and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return len(sql)


def _skip_block_comment(sql: str, start: int) -> int:
    index = start + 2
    depth = 1
    while index < len(sql) and depth:
        if sql.startswith("/*", index):
            depth += 1
            index += 2
        elif sql.startswith("*/", index):
            depth -= 1
            index += 2
        else:
            index += 1
    return index


def _dollar_quote_at(sql: str, start: int) -> str | None:
    match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql[start:])
    return match.group(0) if match else None


def explicit_transaction_control(sql: str) -> str | None:
    """Return a top-level transaction command, ignoring comments/quoted bodies."""
    index = 0
    at_statement_start = True
    while index < len(sql):
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            index = _skip_block_comment(sql, index)
            continue
        if char in {"'", '"'}:
            at_statement_start = False
            index = _skip_quoted(sql, index, char)
            continue
        if char == "$":
            delimiter = _dollar_quote_at(sql, index)
            if delimiter:
                at_statement_start = False
                end = sql.find(delimiter, index + len(delimiter))
                index = len(sql) if end < 0 else end + len(delimiter)
                continue
        if char == ";":
            at_statement_start = True
            index += 1
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(sql) and (
                sql[end].isalnum() or sql[end] in {"_", "$"}
            ):
                end += 1
            word = sql[index:end].upper()
            if at_statement_start and word in _TRANSACTION_CONTROLS:
                return word
            at_statement_start = False
            index = end
            continue
        at_statement_start = False
        index += 1
    return None


def stray_files(directory: Path, allowed_names: set[str]) -> list[str]:
    """Return entries that are neither allowed manifests nor valid pair members.

    ``discover_migrations`` already rejects malformed ``*.sql`` names; this
    catches everything else (stale notes, editor droppings, nested folders) so
    an offline hygiene gate can keep ``migrations/`` byte-exact.
    """
    strays = []
    for path in sorted(directory.iterdir()):
        if path.name in allowed_names:
            continue
        if path.is_dir() or not _MIGRATION_FILE.fullmatch(path.name):
            strays.append(path.name)
    return strays


def discover_migrations(directory: Path) -> list[Migration]:
    if not directory.is_dir():
        raise ValueError(f"migration directory does not exist: {directory}")

    pairs: dict[tuple[str, str], dict[str, Path]] = {}
    versions: dict[str, str] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILE.fullmatch(path.name)
        if not match:
            raise ValueError(f"invalid migration filename: {path.name}")
        version = match.group("version")
        name = match.group("name")
        direction = match.group("direction")
        previous_name = versions.setdefault(version, name)
        if previous_name != name:
            raise ValueError(f"duplicate migration version: {version}")
        pair = pairs.setdefault((version, name), {})
        if direction in pair:
            raise ValueError(f"duplicate {direction} migration: {version}")
        pair[direction] = path

    result = []
    for (version, name), files in sorted(pairs.items()):
        missing = {"up", "down"} - set(files)
        if missing:
            raise ValueError(
                f"migration {version}_{name} is missing {sorted(missing)[0]}.sql"
            )
        try:
            up_sql = _normalized_sql(files["up"].read_text(encoding="utf-8"))
            down_sql = _normalized_sql(files["down"].read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"cannot read migration {version}_{name}") from exc
        if not up_sql.strip() or not down_sql.strip():
            raise ValueError(f"migration {version}_{name} has an empty direction")
        for direction, sql in (("up", up_sql), ("down", down_sql)):
            control = explicit_transaction_control(sql)
            if control:
                raise ValueError(
                    f"migration {version}_{name}.{direction}.sql contains {control}; "
                    "the runner owns transaction boundaries"
                )
            if "%" in sql:
                # The runner executes migration files through exec_driver_sql,
                # and psycopg2 applies printf-style interpolation to the raw
                # statement text — a literal percent anywhere in the file
                # (RAISE placeholders, LIKE patterns, even comments) fails at
                # apply time with a TypeError. Reject it at lint time instead.
                raise ValueError(
                    f"migration {version}_{name}.{direction}.sql contains a "
                    "literal percent character, which exec_driver_sql cannot "
                    "execute safely"
                )
        result.append(
            Migration(
                version=version,
                name=name,
                up_sql=up_sql,
                down_sql=down_sql,
                checksum=migration_checksum(version, name, up_sql, down_sql),
            )
        )
    return result


def validate_catalog(
    baseline: BaselineManifest,
    migrations: Sequence[Migration],
) -> None:
    seen = {baseline.version}
    for migration in migrations:
        if migration.version in seen:
            raise ValueError(f"duplicate migration version: {migration.version}")
        if migration.version <= baseline.version:
            raise ValueError(
                f"migration {migration.version} must be newer than baseline {baseline.version}"
            )
        seen.add(migration.version)
    by_version = {migration.version: migration for migration in migrations}
    for migration in migrations:
        created = created_table_names(migration.up_sql)
        if not created:
            continue
        policy_sql = migration.up_sql
        companion_version = _LEGACY_PRIVILEGE_COMPANIONS.get(migration.version)
        if companion_version:
            companion = by_version.get(companion_version)
            if companion is None:
                raise ValueError(
                    f"migration {migration.version} is missing browser-privilege "
                    f"companion {companion_version}"
                )
            policy_sql += "\n" + companion.up_sql
        revoked = browser_privilege_revokes(policy_sql)
        missing = sorted(created - revoked)
        if missing:
            raise ValueError(
                f"migration {migration.version} creates tables without explicit "
                f"PUBLIC/anon/authenticated revoke: {', '.join(missing)}"
            )


def created_table_names(sql: str) -> set[str]:
    """Return unquoted table names created by a migration."""
    return {
        match.group("table")
        for match in _CREATE_TABLE_STATEMENT.finditer(sql)
    }


def browser_privilege_revokes(sql: str) -> set[str]:
    """Return tables whose Supabase browser-role grants are fully revoked."""
    revoked = set()
    for match in _REVOKE_TABLE_STATEMENT.finditer(sql):
        roles = {
            role.strip().strip('"').lower()
            for role in match.group("roles").split(",")
        }
        if not _BROWSER_ROLES.issubset(roles):
            continue
        for raw_table in match.group("tables").split(","):
            table = raw_table.strip().strip('"').rsplit(".", 1)[-1].strip('"')
            if _IDENTIFIER.fullmatch(table):
                revoked.add(table)
    return revoked


def expected_records(
    baseline: BaselineManifest,
    migrations: Sequence[Migration],
) -> list[dict[str, str]]:
    validate_catalog(baseline, migrations)
    return [{
        "version": baseline.version,
        "name": baseline.name,
        "migration_checksum": baseline.checksum,
        "kind": "baseline",
    }] + [{
        "version": migration.version,
        "name": migration.name,
        "migration_checksum": migration.checksum,
        "kind": "migration",
    } for migration in migrations]


def plan_history(
    baseline: BaselineManifest,
    migrations: Sequence[Migration],
    applied_rows: Iterable[Mapping],
) -> dict:
    expected = expected_records(baseline, migrations)
    expected_by_version = {row["version"]: row for row in expected}
    applied = [dict(row) for row in applied_rows]
    applied_by_version = {str(row["version"]): row for row in applied}
    duplicate_applied = sorted(
        version
        for version in set(str(row["version"]) for row in applied)
        if sum(str(row["version"]) == version for row in applied) > 1
    )
    unknown = sorted(set(applied_by_version) - set(expected_by_version))
    checksum_mismatches = sorted(
        version
        for version in set(applied_by_version) & set(expected_by_version)
        if str(applied_by_version[version].get("migration_checksum", ""))
        != expected_by_version[version]["migration_checksum"]
    )
    name_mismatches = sorted(
        version
        for version in set(applied_by_version) & set(expected_by_version)
        if str(applied_by_version[version].get("name", ""))
        != expected_by_version[version]["name"]
    )
    pending = [
        row["version"] for row in expected if row["version"] not in applied_by_version
    ]
    applied_expected = [
        row["version"] for row in expected if row["version"] in applied_by_version
    ]
    seen_missing = False
    history_gaps = []
    for row in expected:
        if row["version"] not in applied_by_version:
            seen_missing = True
        elif seen_missing:
            history_gaps.append(row["version"])
    drift = bool(
        duplicate_applied
        or unknown
        or checksum_mismatches
        or name_mismatches
        or history_gaps
    )
    return {
        "baseline_version": baseline.version,
        "baseline_applied": baseline.version in applied_by_version,
        "applied_versions": applied_expected,
        "pending_versions": pending,
        "unknown_applied_versions": unknown,
        "duplicate_applied_versions": duplicate_applied,
        "checksum_mismatches": checksum_mismatches,
        "name_mismatches": name_mismatches,
        "history_gaps": history_gaps,
        "history_valid": not drift,
        "migration_head": expected[-1]["version"],
        "at_head": not drift and not pending,
    }


def _quote_identifier(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"invalid SQL identifier: {name}")
    return f'"{name}"'


def _qualified_ledger(schema_name: str) -> str:
    return f"{_quote_identifier(schema_name)}.{_quote_identifier(LEDGER_TABLE)}"


def ledger_exists(conn, schema_name: str) -> bool:
    qualified = f"{schema_name}.{LEDGER_TABLE}"
    return bool(conn.execute(
        text("SELECT to_regclass(:qualified_name) IS NOT NULL"),
        {"qualified_name": qualified},
    ).scalar_one())


def fetch_applied(conn, schema_name: str) -> list[dict]:
    if not ledger_exists(conn, schema_name):
        return []
    table = _qualified_ledger(schema_name)
    return [dict(row) for row in conn.execute(text(f"""
        SELECT version, name, migration_checksum, source_schema_checksum,
               applied_at
        FROM {table}
        ORDER BY version
    """)).mappings().all()]


def ledger_security(conn, schema_name: str) -> dict:
    """Return non-sensitive privilege health for the internal ledger."""
    if not ledger_exists(conn, schema_name):
        return {
            "owner_is_current_user": None,
            "rls_enabled": None,
            "rls_forced": None,
            "public_has_privileges": None,
            "anon_has_privileges": None,
            "authenticated_has_privileges": None,
            "app_backend_has_privileges": None,
            "restricted_roles_have_privileges": None,
        }
    row = conn.execute(text("""
        SELECT
            pg_get_userbyid(c.relowner) = CURRENT_USER AS owner_is_current_user,
            c.relrowsecurity AS rls_enabled,
            c.relforcerowsecurity AS rls_forced,
            COALESCE(bool_or(acl.grantee = 0), FALSE)
                AS public_has_privileges,
            COALESCE(bool_or(role.rolname = 'anon'), FALSE)
                AS anon_has_privileges,
            COALESCE(bool_or(role.rolname = 'authenticated'), FALSE)
                AS authenticated_has_privileges,
            COALESCE(bool_or(role.rolname = 'app_backend'), FALSE)
                AS app_backend_has_privileges
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(
            COALESCE(c.relacl, acldefault('r', c.relowner))
        ) AS acl
        LEFT JOIN pg_roles role ON role.oid = acl.grantee
        WHERE n.nspname = :schema_name
          AND c.relname = :table_name
          AND c.relkind = 'r'
        GROUP BY c.relowner, c.relrowsecurity, c.relforcerowsecurity
    """), {
        "schema_name": schema_name,
        "table_name": LEDGER_TABLE,
    }).mappings().one()
    report = {key: bool(value) for key, value in row.items()}
    report["restricted_roles_have_privileges"] = any(
        report[f"{role}_has_privileges"]
        for role in ("public", "anon", "authenticated", "app_backend")
    )
    return report


def ensure_ledger(conn, schema_name: str) -> None:
    table = _qualified_ledger(schema_name)
    conn.execute(text(f"""
        CREATE TABLE {table} (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            migration_checksum TEXT NOT NULL,
            source_schema_checksum TEXT,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT schema_migrations_version_format
                CHECK (version ~ '^[0-9]{{8}}_[0-9]{{4}}$'),
            CONSTRAINT schema_migrations_name_format
                CHECK (name ~ '^[a-z][a-z0-9_]*$'),
            CONSTRAINT schema_migrations_checksum_format
                CHECK (migration_checksum ~ '^[0-9a-f]{{64}}$'),
            CONSTRAINT schema_migrations_source_checksum_format
                CHECK (
                    source_schema_checksum IS NULL
                    OR source_schema_checksum ~ '^[0-9a-f]{{64}}$'
                )
        )
    """))
    conn.execute(text(f"REVOKE ALL ON TABLE {table} FROM PUBLIC"))
    roles = {
        str(row[0])
        for row in conn.execute(text("""
            SELECT rolname
            FROM pg_roles
            WHERE rolname IN ('anon', 'authenticated', 'app_backend')
        """)).fetchall()
    }
    for role in RESTRICTED_ROLES:
        if role in roles:
            conn.execute(text(
                f"REVOKE ALL ON TABLE {table} FROM {_quote_identifier(role)}"
            ))


def lock_transaction(conn) -> None:
    """Serialize repository migration operations for the current transaction."""
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(text("SET LOCAL statement_timeout = '60s'"))
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"),
        {"lock_name": MIGRATION_LOCK},
    )


def _insert_record(
    conn,
    schema_name: str,
    *,
    version: str,
    name: str,
    checksum: str,
    source_schema_checksum: str | None,
) -> None:
    table = _qualified_ledger(schema_name)
    conn.execute(text(f"""
        INSERT INTO {table}
            (version, name, migration_checksum, source_schema_checksum)
        VALUES (:version, :name, :checksum, :source_schema_checksum)
    """), {
        "version": version,
        "name": name,
        "checksum": checksum,
        "source_schema_checksum": source_schema_checksum,
    })


def verify_existing_history(
    conn,
    baseline: BaselineManifest,
    migrations: Sequence[Migration],
) -> dict:
    report = plan_history(
        baseline,
        migrations,
        fetch_applied(conn, baseline.schema_name),
    )
    if not report["history_valid"]:
        raise RuntimeError("migration ledger differs from repository history")
    return report


def record_baseline(
    conn,
    baseline: BaselineManifest,
    migrations: Sequence[Migration],
    *,
    acquire_lock: bool = True,
) -> bool:
    """Create the ledger and baseline row, or verify an existing baseline."""
    if acquire_lock:
        lock_transaction(conn)
    if ledger_exists(conn, baseline.schema_name):
        report = verify_existing_history(conn, baseline, migrations)
        if not report["baseline_applied"]:
            raise RuntimeError(
                "migration ledger exists without the repository baseline"
            )
        return False
    ensure_ledger(conn, baseline.schema_name)
    _insert_record(
        conn,
        baseline.schema_name,
        version=baseline.version,
        name=baseline.name,
        checksum=baseline.checksum,
        source_schema_checksum=baseline.source_schema_checksum,
    )
    return True


def apply_pending(
    engine,
    baseline: BaselineManifest,
    migrations: Sequence[Migration],
) -> list[str]:
    """Apply each pending migration in its own atomic transaction."""
    applied_now = []
    migration_by_version = {item.version: item for item in migrations}
    while True:
        with engine.begin() as conn:
            lock_transaction(conn)
            if not ledger_exists(conn, baseline.schema_name):
                raise RuntimeError("migration baseline has not been recorded")
            report = verify_existing_history(conn, baseline, migrations)
            if not report["baseline_applied"]:
                raise RuntimeError("migration baseline has not been recorded")
            pending = [
                version for version in report["pending_versions"]
                if version != baseline.version
            ]
            if not pending:
                return applied_now
            migration = migration_by_version[pending[0]]
            conn.exec_driver_sql(migration.up_sql)
            _insert_record(
                conn,
                baseline.schema_name,
                version=migration.version,
                name=migration.name,
                checksum=migration.checksum,
                source_schema_checksum=None,
            )
            applied_now.append(migration.version)


def rollback_latest(
    engine,
    baseline: BaselineManifest,
    migrations: Sequence[Migration],
) -> str:
    """Roll back the newest applied SQL migration; the baseline is immutable."""
    migration_by_version = {item.version: item for item in migrations}
    with engine.begin() as conn:
        lock_transaction(conn)
        if not ledger_exists(conn, baseline.schema_name):
            raise RuntimeError("migration baseline has not been recorded")
        report = verify_existing_history(conn, baseline, migrations)
        applied = [
            version for version in report["applied_versions"]
            if version != baseline.version
        ]
        if not applied:
            raise RuntimeError("no SQL migration is available to roll back")
        version = applied[-1]
        migration = migration_by_version[version]
        conn.exec_driver_sql(migration.down_sql)
        table = _qualified_ledger(baseline.schema_name)
        result = conn.execute(text(f"""
            DELETE FROM {table}
            WHERE version=:version AND migration_checksum=:checksum
        """), {"version": version, "checksum": migration.checksum})
        if result.rowcount != 1:
            raise RuntimeError("migration ledger changed during rollback")
        return version
