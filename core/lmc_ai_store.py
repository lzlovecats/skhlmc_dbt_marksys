"""Durable metadata store for the outbound local-AI node registry.

Conversation text never enters this module or the database.  Live sockets,
heartbeats and queues belong to :mod:`core.lmc_ai_runtime` process memory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from sqlalchemy import text

from core.config_store import get_config, set_config
from core.schema_features import READY, feature_bundle_state
from schema import TABLE_LMC_AI_NODES
from system_limits import LMC_AI_NODE_MAX, LMC_AI_NODE_NAME_MAX_CHARS


ACTIVE_NODE_CONFIG_KEY = "lmc_ai_active_node_id"
THINKING_ENABLED_CONFIG_KEY = "lmc_ai_thinking_enabled"


def require_lmc_ai_schema(db) -> None:
    try:
        state = feature_bundle_state(db, "lmc_ai", (TABLE_LMC_AI_NODES,))
    except Exception as exc:
        raise RuntimeError("自家 AI 資料庫功能未準備好。") from exc
    if state != READY:
        raise RuntimeError("自家 AI 資料庫功能未準備好。")


def _token_digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _clean_name(value: object) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("請輸入 AI 電腦名稱。")
    if len(name) > LMC_AI_NODE_NAME_MAX_CHARS:
        raise ValueError("AI 電腦名稱太長。")
    return name


def list_node_rows(db) -> list[dict]:
    require_lmc_ai_schema(db)
    frame = db.query(
        f"""SELECT node_id,display_name,enabled,last_runtime,
                   last_runtime_version,last_model,last_capabilities,
                   created_at,updated_at,last_connected_at,last_disconnected_at
            FROM {TABLE_LMC_AI_NODES}
            ORDER BY created_at,node_id
            LIMIT :node_limit""",
        {"node_limit": LMC_AI_NODE_MAX},
    )
    rows = []
    for _, row in frame.iterrows():
        item = dict(row)
        for key, value in tuple(item.items()):
            try:
                if value is not None and value != value:
                    item[key] = None
            except (TypeError, ValueError):
                pass
        capabilities = item.get("last_capabilities")
        if isinstance(capabilities, str):
            try:
                capabilities = json.loads(capabilities)
            except (TypeError, ValueError, json.JSONDecodeError):
                capabilities = None
        item["last_capabilities"] = capabilities
        rows.append(item)
    return rows


def create_node(db, display_name: object) -> tuple[dict, str]:
    require_lmc_ai_schema(db)
    name = _clean_name(display_name)
    node_id = secrets.token_hex(16)
    raw_token = secrets.token_urlsafe(32)
    params = {
        "node_id": node_id,
        "display_name": name,
        "token_hash": _token_digest(raw_token),
    }
    insert_sql = f"""INSERT INTO {TABLE_LMC_AI_NODES}
           (node_id,display_name,token_hash,enabled,created_at,updated_at)
        VALUES(:node_id,:display_name,:token_hash,TRUE,NOW(),NOW())"""
    if hasattr(db, "transaction"):
        with db.transaction() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": 4_802_010},
            )
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM {TABLE_LMC_AI_NODES}")
            ).scalar()
            if int(count or 0) >= LMC_AI_NODE_MAX:
                raise ValueError("已達 AI 電腦登記上限。")
            conn.execute(text(insert_sql), params)
    else:
        count = db.query(f"SELECT COUNT(*) AS count FROM {TABLE_LMC_AI_NODES}")
        if count.empty or int(count.iloc[0]["count"] or 0) >= LMC_AI_NODE_MAX:
            raise ValueError("已達 AI 電腦登記上限。")
        db.execute(insert_sql, params)
    return {"node_id": node_id, "display_name": name, "enabled": True}, raw_token


def rotate_node_token(db, node_id: str) -> str:
    require_lmc_ai_schema(db)
    raw_token = secrets.token_urlsafe(32)
    changed = db.execute_count(
        f"""UPDATE {TABLE_LMC_AI_NODES}
            SET token_hash=:token_hash,enabled=TRUE,updated_at=NOW()
            WHERE node_id=:node_id""",
        {"token_hash": _token_digest(raw_token), "node_id": node_id},
    )
    if changed != 1:
        raise LookupError("找不到指定 AI 電腦。")
    return raw_token


def revoke_node(db, node_id: str) -> None:
    require_lmc_ai_schema(db)
    changed = db.execute_count(
        f"""UPDATE {TABLE_LMC_AI_NODES}
            SET enabled=FALSE,updated_at=NOW()
            WHERE node_id=:node_id""",
        {"node_id": node_id},
    )
    if changed != 1:
        raise LookupError("找不到指定 AI 電腦。")


def authenticate_node(db, raw_token: str) -> dict | None:
    """Constant-time compare against the bounded enabled-node inventory."""
    require_lmc_ai_schema(db)
    candidate = _token_digest(raw_token)
    rows = db.query(
        f"""SELECT node_id,display_name,token_hash
            FROM {TABLE_LMC_AI_NODES}
            WHERE enabled=TRUE
            ORDER BY node_id
            LIMIT :node_limit""",
        {"node_limit": LMC_AI_NODE_MAX},
    )
    match = None
    for _, row in rows.iterrows():
        valid = hmac.compare_digest(candidate, str(row["token_hash"] or ""))
        if valid:
            match = {
                "node_id": str(row["node_id"]),
                "display_name": str(row["display_name"]),
            }
    return match


def update_node_hello(db, node_id: str, raw_token: str, hello: dict) -> None:
    """Persist hello metadata only while the authenticated token is still current."""
    require_lmc_ai_schema(db)
    changed = db.execute_count(
        f"""UPDATE {TABLE_LMC_AI_NODES}
            SET display_name=:display_name,last_runtime=:runtime,
                last_runtime_version=:runtime_version,last_model=:model,
                last_capabilities=CAST(:capabilities AS JSONB),
                last_connected_at=NOW(),updated_at=NOW()
            WHERE node_id=:node_id AND enabled=TRUE
              AND token_hash=:token_hash""",
        {
            "node_id": node_id,
            "token_hash": _token_digest(raw_token),
            "display_name": _clean_name(hello.get("name")),
            "runtime": str(hello.get("runtime") or "")[:80] or None,
            "runtime_version": str(hello.get("runtime_version") or "")[:80] or None,
            "model": str(hello.get("model") or "")[:200] or None,
            "capabilities": json.dumps(
                hello.get("capabilities") or {},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    )
    if changed != 1:
        raise LookupError("AI 電腦憑證已更新或撤銷。")


def mark_node_disconnected(db, node_id: str) -> None:
    try:
        db.execute(
            f"""UPDATE {TABLE_LMC_AI_NODES}
                SET last_disconnected_at=NOW(),updated_at=NOW()
                WHERE node_id=:node_id""",
            {"node_id": node_id},
        )
    except Exception:
        # Connection cleanup is best effort; readiness remains memory-authoritative.
        pass


def get_active_node_id(db) -> str:
    require_lmc_ai_schema(db)
    return str(get_config(db, ACTIVE_NODE_CONFIG_KEY, "") or "").strip()


def set_active_node_id(db, node_id: str) -> None:
    require_lmc_ai_schema(db)
    node_id = str(node_id or "").strip()
    if node_id:
        row = db.query(
            f"SELECT enabled FROM {TABLE_LMC_AI_NODES} WHERE node_id=:node_id",
            {"node_id": node_id},
        )
        if row.empty or not bool(row.iloc[0]["enabled"]):
            raise LookupError("找不到已啟用嘅 AI 電腦。")
    set_config(db, ACTIVE_NODE_CONFIG_KEY, node_id)


def get_thinking_enabled(db) -> bool:
    require_lmc_ai_schema(db)
    return bool(get_config(db, THINKING_ENABLED_CONFIG_KEY, False))


def set_thinking_enabled(db, enabled: bool) -> None:
    require_lmc_ai_schema(db)
    set_config(db, THINKING_ENABLED_CONFIG_KEY, bool(enabled))
