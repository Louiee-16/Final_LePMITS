"""
rag/retriever.py
LePMITS — Step 4: Retriever

Accepts a query string, embeds it locally, and returns the top-5 most
similar documents (merged from Document + LegacyDocument) ranked by
cosine similarity descending.

Return shape per result:
    {
        "title":        str,
        "reference_no": str | None,
        "snippet":      str,          # ~300-char plain-text excerpt
        "score":        float,        # 0.0–1.0, higher = more similar
        "source":       "document" | "legacy",
    }
"""

from __future__ import annotations

import logging
from typing import Any

from pgvector.django import CosineDistance

from documents.models import Document, LegacyDocument
from documents.rag.embedder import embed_text, strip_quill_html, extract_pdf_text

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

TOP_K = 5          # total results returned (merged across both tables)
SNIPPET_LEN = 300  # max plain-text snippet characters sent to LLM

# ── Internal helpers ─────────────────────────────────────────────────────────

def _make_snippet(plain_text: str) -> str:
    """Return the first SNIPPET_LEN characters of plain_text, word-boundary trimmed."""
    text = plain_text.strip()
    if len(text) <= SNIPPET_LEN:
        return text
    trimmed = text[:SNIPPET_LEN]
    # Trim to last complete word so the snippet doesn't cut mid-word.
    last_space = trimmed.rfind(" ")
    return (trimmed[:last_space] if last_space != -1 else trimmed) + "…"


def _query_documents(query_vector: list[float], k: int) -> list[dict[str, Any]]:
    """Cosine-similarity search over Document (status=APPROVED only)."""
    qs = (
        Document.objects.filter(status="APPROVED")
        .exclude(embedding=None)
        .annotate(distance=CosineDistance("embedding", query_vector))
        .order_by("distance")[:k]
    )

    results: list[dict[str, Any]] = []
    for doc in qs:
        plain_text = strip_quill_html(doc.content)
        results.append(
            {
                "title": doc.title or "",
                "reference_no": getattr(doc, "reference_no", None) or None,
                "snippet": _make_snippet(plain_text),
                # CosineDistance is in [0, 2]; convert to similarity in [0, 1].
                "score": round(1.0 - float(doc.distance) / 2.0, 6),
                "source": "document",
            }
        )
    return results


def _query_legacy_documents(query_vector: list[float], k: int) -> list[dict[str, Any]]:
    """Cosine-similarity search over LegacyDocument."""
    qs = (
        LegacyDocument.objects.exclude(embedding=None)
        .annotate(distance=CosineDistance("embedding", query_vector))
        .order_by("distance")[:k]
    )

    results: list[dict[str, Any]] = []
    for legacy_doc in qs:
        # Re-extract plain text from the PDF for the snippet.
        # This is a small, bounded read (only top-k docs are touched).
        try:
            pdf_path = legacy_doc.pdf_file
            plain_text = extract_pdf_text(pdf_path)
        except Exception as exc:
            logger.warning(
                "Could not extract text for LegacyDocument pk=%s: %s — using empty snippet.",
                legacy_doc.pk,
                exc,
            )
            plain_text = ""

        results.append(
            {
                "title": legacy_doc.title or "",
                "reference_no": getattr(legacy_doc, "reference_no", None) or None,
                "snippet": _make_snippet(plain_text),
                "score": round(1.0 - float(legacy_doc.distance) / 2.0, 6),
                "source": "legacy",
            }
        )
    return results

# ── Public API ────────────────────────────────────────────────────────────────

def retrieve(query: str, top_k: int = TOP_K) -> list[dict[str, Any]]:
    """
    Embed *query* and return the *top_k* most relevant approved/legacy
    documents, sorted by cosine similarity descending.

    Parameters
    ----------
    query   : str   — draft document title, or title + keywords
    top_k   : int   — number of final results to return (default: 5)

    Returns
    -------
    List of result dicts (see module docstring for shape).
    Raises ValueError if query is empty.
    """
    if not query or not query.strip():
        raise ValueError("retrieve() received an empty query string.")

    logger.info("RAG retrieval — query: %r", query)

    # 1. Embed the query locally (no API call).
    query_vector = embed_text(query)

    # 2. Fetch top_k candidates from each table independently so that even if
    #    one table dominates by score, the other gets a fair shot before the
    #    final merge cut.
    doc_results = _query_documents(query_vector, k=top_k)
    legacy_results = _query_legacy_documents(query_vector, k=top_k)

    # 3. Merge and re-rank by score descending, then take the overall top_k.
    merged = sorted(doc_results + legacy_results, key=lambda r: r["score"], reverse=True)
    final = merged[:top_k]

    logger.info(
        "Retrieval complete — %d result(s) returned (from %d doc + %d legacy candidates).",
        len(final),
        len(doc_results),
        len(legacy_results),
    )
    return final