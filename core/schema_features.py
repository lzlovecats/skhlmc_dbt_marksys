"""Read-only readiness checks for optional, versioned database features."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from schema import (
    TABLE_AI_DATASET_SNAPSHOT_ITEMS, TABLE_AI_DATASET_SNAPSHOTS,
    TABLE_AI_FACTORY_ATTEMPTS, TABLE_AI_FACTORY_ITEMS,
    TABLE_AI_FACTORY_ITEM_TAGS, TABLE_AI_FACTORY_JOBS,
    TABLE_AI_FACTORY_RELEASE_ITEMS, TABLE_AI_FACTORY_RELEASES,
    TABLE_AI_FACTORY_SOURCES, TABLE_AI_FACTORY_TOPIC_TAGS,
    TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS, TABLE_AI_FACTORY_TRANSCRIPT_RUNS,
    TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS, TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS,
    TABLE_AI_FACTORY_TRANSCRIPTS, TABLE_AI_MODEL_VERSIONS, TABLE_LMC_AI_NODES,
    TABLE_WORKSTATION_R2_HEALTH_PROBES,
    TABLE_RAG_CHUNKS, TABLE_RAG_DOCUMENTS,
)


ABSENT = "absent"
PARTIAL = "partial"
READY = "ready"
DISABLED = "disabled"
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")

@dataclass(frozen=True)
class FeatureSchema:
    migration_version: str | None
    lifecycle: str
    tables: tuple[str, ...]
    retention: str


# One operational catalog owns optional feature bundles, readiness markers and
# lifecycle notes. Merely finding legacy/lazy-created relations never enables
# a feature whose reviewed migration version is absent.
FEATURE_CATALOG: dict[str, FeatureSchema] = {
    "data_factory": FeatureSchema(
        "20260720_0009", "active",
        (
            TABLE_AI_FACTORY_SOURCES, TABLE_AI_FACTORY_JOBS,
            TABLE_AI_FACTORY_ATTEMPTS, TABLE_AI_FACTORY_ITEMS,
            TABLE_AI_FACTORY_TOPIC_TAGS, TABLE_AI_FACTORY_ITEM_TAGS,
            TABLE_AI_FACTORY_RELEASES, TABLE_AI_FACTORY_RELEASE_ITEMS,
            TABLE_AI_FACTORY_TRANSCRIPTS, TABLE_AI_FACTORY_TRANSCRIPT_RUNS,
            TABLE_AI_FACTORY_TRANSCRIPT_WINDOWS,
            TABLE_AI_FACTORY_TRANSCRIPT_ATTEMPTS,
            TABLE_AI_FACTORY_TRANSCRIPT_SEGMENTS,
        ),
        "bounded records with explicit review, withdrawal and audit workflows",
    ),
    "lmc_ai": FeatureSchema(
        "20260722_0003", "active",
        (TABLE_LMC_AI_NODES, TABLE_WORKSTATION_R2_HEALTH_PROBES),
        "bounded node registry and 15-minute R2 health probes; conversation content remains browser-local",
    ),
    "dataset_model": FeatureSchema(
        None, "disabled",
        (
            TABLE_AI_DATASET_SNAPSHOTS, TABLE_AI_DATASET_SNAPSHOT_ITEMS,
            TABLE_AI_MODEL_VERSIONS,
        ),
        "not provisioned",
    ),
    "rag": FeatureSchema(
        None, "disabled", (TABLE_RAG_DOCUMENTS, TABLE_RAG_CHUNKS),
        "not provisioned",
    ),
}

FEATURE_MIGRATION_VERSIONS: dict[str, str | None] = {
    name: definition.migration_version
    for name, definition in FEATURE_CATALOG.items()
}


def feature_catalog_report() -> dict[str, dict]:
    return {
        name: {
            "migration_version": definition.migration_version,
            "lifecycle": definition.lifecycle,
            "tables": list(definition.tables),
            "retention": definition.retention,
        }
        for name, definition in FEATURE_CATALOG.items()
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


def feature_bundle_state(
    db, feature: str, table_names: Sequence[str] | None = None,
) -> str:
    """Require an explicit migration marker before accepting a feature bundle."""
    definition = FEATURE_CATALOG.get(feature)
    migration_version = definition.migration_version if definition else None
    if not migration_version:
        return DISABLED
    selected_tables = table_names if table_names is not None else definition.tables
    names = tuple(dict.fromkeys(str(name) for name in selected_tables))
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
