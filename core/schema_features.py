"""Read-only readiness checks for optional, versioned database features."""

from __future__ import annotations

import re
from collections.abc import Sequence


ABSENT = "absent"
PARTIAL = "partial"
READY = "ready"
DISABLED = "disabled"
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")

# These features stay disabled until a reviewed migration exists. Enabling one
# is an explicit code change to the exact applied migration version; merely
# finding legacy/lazy-created relations can never turn a feature on.
FEATURE_MIGRATION_VERSIONS: dict[str, str | None] = {
    "data_factory": "20260720_0001",
    "dataset_model": None,
    "eval": None,
    "rag": None,
}


def table_bundle_state(db, table_names: Sequence[str]) -> str:
    """Return whether a controlled bundle is absent, partial or real tables."""
    names = tuple(dict.fromkeys(str(name) for name in table_names))
    if not names or any(not _IDENTIFIER.fullmatch(name) for name in names):
        raise ValueError("invalid schema feature table bundle")
    expressions = []
    params = {}
    for index, name in enumerate(names):
        parameter = f"table_{index}"
        expressions.append(
            f"to_regclass(:{parameter}) IS NOT NULL AS relation_{index}"
        )
        expressions.append(
            "EXISTS (SELECT 1 FROM pg_class c "
            "WHERE c.oid=to_regclass(:{parameter}) AND c.relkind IN ('r','p')) "
            "AS {parameter}".format(parameter=parameter)
        )
        params[parameter] = f"public.{name}"
    result = db.query("SELECT " + ", ".join(expressions), params)
    if result.empty:
        raise RuntimeError("schema feature readiness query returned no row")
    table_flags = [bool(result.iloc[0][f"table_{index}"]) for index in range(len(names))]
    relation_flags = [bool(result.iloc[0][f"relation_{index}"]) for index in range(len(names))]
    if all(table_flags):
        return READY
    if any(relation_flags):
        return PARTIAL
    return ABSENT


def feature_bundle_state(db, feature: str, table_names: Sequence[str]) -> str:
    """Require an explicit migration marker before accepting a feature bundle."""
    migration_version = FEATURE_MIGRATION_VERSIONS.get(feature)
    if not migration_version:
        return DISABLED
    names = tuple(dict.fromkeys(str(name) for name in table_names))
    if not names or any(not _IDENTIFIER.fullmatch(name) for name in names):
        raise ValueError("invalid schema feature table bundle")
    # The future feature migration must stamp its anchor table with this exact
    # COMMENT. PostgreSQL catalog comments are readable by a restricted runtime
    # role, unlike the private migration ledger, while legacy runtime DDL
    # cannot accidentally satisfy the marker.
    expected_marker = f"skhlmc-feature:{feature}:{migration_version}"
    marker = db.query(
        """SELECT COALESCE(
            obj_description(to_regclass(:anchor), 'pg_class')=:marker,
            FALSE
        ) AS applied""",
        {"anchor": f"public.{names[0]}", "marker": expected_marker},
    )
    if marker.empty or not bool(marker.iloc[0]["applied"]):
        return DISABLED
    return table_bundle_state(db, names)
