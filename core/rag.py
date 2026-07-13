"""Versioned debate-document retrieval for AI Coach.

RAG is deliberately fail-open: retrieval errors never make the existing cloud
coach unavailable. Only accepted, active documents and one embedding space are
eligible for a result.
"""

from __future__ import annotations

import json
import math
import os

import httpx
from system_limits import RAG_FALLBACK_CANDIDATE_LIMIT, RAG_PROVIDER_TIMEOUT_SECONDS


EMBEDDING_MODEL = "gemini-embedding-2"
EMBEDDING_VERSION = "gemini-embedding-2@2026-04"
EMBEDDING_DIMENSION = 768


async def _embed(text_value: str, api_key: str) -> list[float]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent"
    payload = {"content": {"parts": [{"text": text_value[:8000]}]},
               "outputDimensionality": EMBEDDING_DIMENSION}
    async with httpx.AsyncClient(timeout=RAG_PROVIDER_TIMEOUT_SECONDS) as client:
        response = await client.post(url, params={"key": api_key}, json=payload)
        response.raise_for_status()
    values = ((response.json().get("embedding") or {}).get("values") or [])
    if len(values) != EMBEDDING_DIMENSION:
        raise ValueError("Embedding dimension mismatch")
    return [float(value) for value in values]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    denom = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / denom if denom else -1.0


async def retrieve_rag_context(db, api_key: str, query: str, *, top_k: int = 6,
                               min_similarity: float = 0.35) -> str:
    if not api_key or not str(query or "").strip():
        return ""
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
        rows = db.query("""SELECT c.chunk_id,c.content_text,c.embedding_json,d.document_id,d.title,
            d.data_type,d.topic_text FROM rag_chunks c
          JOIN rag_documents d ON d.document_id=c.document_id
          JOIN llm_training_submissions s ON s.id=d.submission_id
          WHERE d.status='active' AND s.status='accepted' AND s.anonymized=TRUE
            AND s.permission_confirmed=TRUE AND c.embedding_model=:model
            AND c.embedding_version=:version
          ORDER BY c.created_at DESC LIMIT :candidate_limit""",
          {"model": EMBEDDING_MODEL, "version": EMBEDDING_VERSION,
           "candidate_limit": RAG_FALLBACK_CANDIDATE_LIMIT})
        candidates = []
        for row in rows.to_dict("records"):
            raw = row.get("embedding_json")
            values = json.loads(raw) if isinstance(raw, str) else raw
            item = dict(row); item["similarity"] = _cosine(vector, values or [])
            candidates.append(item)
        candidates.sort(key=lambda item: float(item.get("similarity") or -1), reverse=True)
        candidates = candidates[:top_k]
    accepted = [item for item in candidates if float(item.get("similarity") or -1) >= min_similarity]
    if not accepted:
        return ""
    lines = ["## 聖呂中辯內部知識庫（只作校隊資料參考，唔代表最新網上事實）"]
    for item in accepted:
        title = item.get("title") or item.get("document_id")
        meta = "｜".join(x for x in (item.get("data_type"), item.get("topic_text")) if x)
        lines.append(f"\n[RAG:{item['chunk_id']}] {title}{'｜' + meta if meta else ''}\n{item['content_text']}")
    lines.append("\n回答如使用以上內容，請引用相應[RAG:…]標記；資料不足時要明確講明。")
    return "\n".join(lines)
