"""
rag/retriever.py  —  LePMITS Retriever

Embeds the query via Ollama then searches DocumentChunk by cosine similarity.
All retrieval goes through chunks — document-level embedding fields are not used.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import models
from pgvector.django import CosineDistance

from documents.models import Document, DocumentChunk, LegacyDocument, NationalLaw, NationalLawChunk
from documents.rag.embedder import embed_text

logger = logging.getLogger(__name__)

TOP_K       = 5
SNIPPET_LEN = 300


def _make_snippet(text: str) -> str:
    text = text.strip()
    if len(text) <= SNIPPET_LEN:
        return text
    trimmed = text[:SNIPPET_LEN]
    last_space = trimmed.rfind(" ")
    return (trimmed[:last_space] if last_space != -1 else trimmed) + "…"


# Chunk types that are pure boilerplate — present in virtually every ordinance.
# Matching against these produces false positives so they are excluded from search.
_BOILERPLATE_CHUNK_TYPES = {
    "SEPARABILITY", "EFFECTIVITY", "SEPARABILITY CLAUSE", "EFFECTIVITY CLAUSE",
}


def _query_chunks(query_vector: list[float], k: int) -> list[dict[str, Any]]:
    qs = (
        DocumentChunk.objects
        .filter(
            models.Q(document__status="APPROVED") |
            models.Q(legacy_document__isnull=False)
        )
        .exclude(chunk_type__in=_BOILERPLATE_CHUNK_TYPES)
        .select_related("document", "legacy_document")
        .annotate(distance=CosineDistance("embedding", query_vector))
        .order_by("distance")[:k]
    )

    results = []
    for chunk in qs:
        parent_doc    = chunk.document
        parent_legacy = chunk.legacy_document

        if parent_doc is not None:
            title        = parent_doc.title or ""
            reference_no = parent_doc.reference_no or None
        elif parent_legacy is not None:
            title        = parent_legacy.title or ""
            reference_no = parent_legacy.reference_no or None
        else:
            logger.warning("DocumentChunk pk=%s has no parent — skipping.", chunk.pk)
            continue

        results.append({
            "title":        title,
            "reference_no": reference_no,
            "chunk_type":   chunk.chunk_type or None,
            "snippet":      _make_snippet(chunk.chunk_text or ""),
            "score":        round(1.0 - float(chunk.distance) / 2.0, 6),
            "source":       "legacy" if parent_legacy else "document",
            "doc_id":       parent_legacy.pk if parent_legacy else parent_doc.pk,
        })
    return results


def retrieve_national_laws(query: str, top_k: int = 5, timeout: int = 60) -> list[dict[str, Any]]:
    """Search NationalLawChunk by cosine similarity and return matching snippets."""
    if not query or not query.strip():
        return []

    if not NationalLawChunk.objects.exists():
        return []

    query_vector = embed_text(query, timeout=timeout)

    qs = (
        NationalLawChunk.objects
        .exclude(embedding=None)
        .select_related("law")
        .annotate(distance=CosineDistance("embedding", query_vector))
        .order_by("distance")[:top_k]
    )

    results = []
    for chunk in qs:
        score = round(1.0 - float(chunk.distance) / 2.0, 6)
        if score < 0.50:
            continue
        results.append({
            "law_number": chunk.law.law_number,
            "title":      chunk.law.title,
            "chunk_type": chunk.chunk_type,
            "snippet":    _make_snippet(chunk.chunk_text or ""),
            "score":      score,
        })

    logger.info("National law retrieval — %d result(s) for: %r", len(results), query[:60])
    return results


def retrieve(query: str, top_k: int = TOP_K, doc_type: str = None, timeout: int = 60) -> list[dict[str, Any]]:
    """Embed query and return top_k most similar chunks by cosine similarity."""
    if not query or not query.strip():
        raise ValueError("retrieve() received an empty query string.")

    logger.info("RAG retrieval — query: %r", query[:80])

    query_vector = embed_text(query, timeout=timeout)
    results = _query_chunks(query_vector, k=top_k)

    logger.info("Retrieval complete — %d result(s).", len(results))
    return results
