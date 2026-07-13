"""Versioned debate-document retrieval for AI Coach.

RAG is deliberately fail-open: retrieval errors never make the existing cloud
coach unavailable. Only accepted, active documents and one embedding space are
eligible for a result.
"""

from __future__ import annotations

import threading
import time

import httpx
from core.ai_provider import post_json_bounded
from core.schema_features import READY, feature_bundle_state
from schema import TABLE_RAG_CHUNKS, TABLE_RAG_DOCUMENTS
from system_limits import RAG_CONTEXT_MAX_CHARS, RAG_PROVIDER_TIMEOUT_SECONDS


EMBEDDING_MODEL = "gemini-embedding-2"
EMBEDDING_VERSION = "gemini-embedding-2@2026-04"
EMBEDDING_DIMENSION = 768
RAG_SCHEMA_CHECK_TTL_SECONDS = 300
_RAG_SCHEMA_CACHE = {"ready": False, "checked_at": 0.0}
_RAG_SCHEMA_LOCK = threading.Lock()


def rag_schema_ready(db, *, force: bool = False) -> bool:
    """Check the complete vector schema before any paid embedding request."""
    now = time.monotonic()
    if (
        not force
        and not _RAG_SCHEMA_CACHE["ready"]
        and _RAG_SCHEMA_CACHE["checked_at"] > 0
        and now - _RAG_SCHEMA_CACHE["checked_at"] < RAG_SCHEMA_CHECK_TTL_SECONDS
    ):
        return bool(_RAG_SCHEMA_CACHE["ready"])
    with _RAG_SCHEMA_LOCK:
        if (
            not force
            and not _RAG_SCHEMA_CACHE["ready"]
            and _RAG_SCHEMA_CACHE["checked_at"] > 0
            and now - _RAG_SCHEMA_CACHE["checked_at"] < RAG_SCHEMA_CHECK_TTL_SECONDS
        ):
            return bool(_RAG_SCHEMA_CACHE["ready"])
        ready = False
        try:
            tables_ready = feature_bundle_state(
                db, "rag", (TABLE_RAG_DOCUMENTS, TABLE_RAG_CHUNKS)
            ) == READY
            if tables_ready:
                status = db.query(
                    """SELECT
                        EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')
                            AS vector_extension_ready,
                        EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_schema='public'
                              AND table_name=:table_name
                              AND column_name='embedding'
                              AND udt_name='vector'
                        ) AS embedding_column_ready""",
                    {"table_name": TABLE_RAG_CHUNKS},
                )
                ready = bool(
                    not status.empty
                    and status.iloc[0]["vector_extension_ready"]
                    and status.iloc[0]["embedding_column_ready"]
                )
        except Exception:
            ready = False
        _RAG_SCHEMA_CACHE.update(ready=ready, checked_at=now)
        return ready


async def _embed(text_value: str, api_key: str) -> list[float]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent"
    payload = {"content": {"parts": [{"text": text_value[:8000]}]},
               "outputDimensionality": EMBEDDING_DIMENSION}
    async with httpx.AsyncClient(timeout=RAG_PROVIDER_TIMEOUT_SECONDS) as client:
        data = await post_json_bounded(client, url, params={"key": api_key}, json=payload)
    values = ((data.get("embedding") or {}).get("values") or [])
    if len(values) != EMBEDDING_DIMENSION:
        raise ValueError("Embedding dimension mismatch")
    return [float(value) for value in values]


async def retrieve_rag_context(db, api_key: str, query: str, *, top_k: int = 6,
                               min_similarity: float = 0.35) -> str:
    if not api_key or not str(query or "").strip():
        return ""
    if not rag_schema_ready(db):
        return ""
    top_k = max(1, min(int(top_k), 20))
    vector = await _embed(str(query), api_key)
    vector_text = "[" + ",".join(f"{value:.9g}" for value in vector) + "]"
    try:
        rows = db.query("""SELECT c.chunk_id,c.content_text,d.document_id,d.title,d.data_type,d.topic_text,
            1-(c.embedding <=> CAST(:embedding AS vector)) AS similarity
          FROM rag_chunks c JOIN rag_documents d ON d.document_id=c.document_id
          JOIN llm_training_submissions s ON s.id=d.submission_id
          WHERE d.status='active' AND s.status='accepted' AND s.anonymized=TRUE
            AND s.permission_confirmed=TRUE AND c.embedding IS NOT NULL
            AND c.embedding_model=:model AND c.embedding_version=:version
          ORDER BY c.embedding <=> CAST(:embedding AS vector) LIMIT :limit""",
          {"embedding": vector_text, "model": EMBEDDING_MODEL,
           "version": EMBEDDING_VERSION, "limit": int(top_k)})
        candidates = [dict(row) for row in rows.to_dict("records")]
    except Exception:
        return ""
    accepted = [item for item in candidates if float(item.get("similarity") or -1) >= min_similarity]
    if not accepted:
        return ""
    header = "## 聖呂中辯內部知識庫（只作校隊資料參考，唔代表最新網上事實）"
    footer = "\n回答如使用以上內容，請引用相應[RAG:…]標記；資料不足時要明確講明。"
    lines = [header]
    for item in accepted:
        title = item.get("title") or item.get("document_id")
        meta = "｜".join(x for x in (item.get("data_type"), item.get("topic_text")) if x)
        block = f"\n[RAG:{item['chunk_id']}] {title}{'｜' + meta if meta else ''}\n{item['content_text']}"
        # Reserve both separators that ``join`` adds around this block and the
        # footer so the configured cap remains exact.
        used = len("\n".join(lines)) + len(footer) + 2
        remaining = RAG_CONTEXT_MAX_CHARS - used
        if remaining <= 0:
            break
        lines.append(block[:remaining])
        if len(block) > remaining:
            break
    lines.append(footer)
    return "\n".join(lines)
