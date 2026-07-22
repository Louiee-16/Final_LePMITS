"""
rag/embedder.py  —  LePMITS Embedding Pipeline

Expects clean, pre-extracted text (stored on the model as extracted_text /
content).  This module only:
  1. Chunks the text by legislative markers  (chunk_legal_text)
  2. Embeds each chunk via nomic-embed-text on Ollama  (embed_text)
  3. Persists vectors back to the model  (embed_document_chunks)
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def _ollama_base_url() -> str:
    endpoint = getattr(settings, 'OLLAMA_ENDPOINT', 'http://localhost:11434/api/generate')
    return endpoint.rsplit('/api/', 1)[0]


def embed_text(text: str, timeout: int = 60) -> list[float]:
    """Return a 768-dim vector from nomic-embed-text via Ollama."""
    if not text or not text.strip():
        raise ValueError("embed_text: empty text.")

    model = getattr(settings, 'OLLAMA_EMBED_MODEL', 'nomic-embed-text')
    resp = requests.post(
        f"{_ollama_base_url()}/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=timeout,
    )
    resp.raise_for_status()
    vector = resp.json().get("embedding")
    if not vector:
        raise ValueError(f"Ollama returned no embedding for model '{model}'.")
    return vector


# ---------------------------------------------------------------------------
# Legal text chunker
# ---------------------------------------------------------------------------

_BOILERPLATE_RE = re.compile(
    r"""
    (?:I\s+HEREBY\s+CERTIFY[\s\S]*?$)
    |
    (?:\(participated\s+thru\s+video\s+conferencing\)\s*)+
    |
    (?:^[A-Z][A-Z\s\.\-]+\n(?:City Councilor|Mayor|Vice Mayor|Sergeant-at-Arms|
        Majority Floor Leader|Minority Floor Leader|President Pro-Tempore|
        Assistant\s+\w+\s+Floor\s+Leader)[^\n]*\n?)
    |
    (?:\(page\s+\d+\s+of\s+[^\)]+\))
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

_LEGAL_MARKERS: List[str] = [
    "NOW THEREFORE", "BE IT ENACTED", "BE IT RESOLVED",
    "SEPARABILITY CLAUSE", "SEPARABILITY", "DEFINITIONS",
    "EFFECTIVITY CLAUSE", "EFFECTIVITY", "PENALTIES",
    "PROVIDED", "WHEREAS", "SECTION", "ARTICLE",
]

_MARKER_RE = re.compile(
    r"^(" + "|".join(re.escape(m) for m in _LEGAL_MARKERS) + r")"
    r"(?:\s+(\d+[\.\-]?\d*))?\b",
    re.IGNORECASE | re.MULTILINE,
)

_MIN_CHUNK_LENGTH = 30


def chunk_legal_text(text: str) -> List[Dict]:
    """Split legislative plain text into clause-level chunks."""
    clean = _BOILERPLATE_RE.sub("", text).strip()

    segments: List[Dict] = []
    last_end   = 0
    last_marker  = "BODY"
    last_number  = None
    current_article = None

    for match in _MARKER_RE.finditer(clean):
        segment_text = clean[last_end : match.start()].strip()
        if segment_text:
            segments.append({
                "chunk_type":   last_marker,
                "chunk_number": last_number,
                "chunk_text":   segment_text,
                "parent": current_article if last_marker == "SECTION" else None,
            })

        last_marker = match.group(1).upper()
        last_number = match.group(2).rstrip(".") if match.group(2) else None
        if last_marker == "ARTICLE":
            current_article = f"ARTICLE {last_number}" if last_number else "ARTICLE"
        last_end = match.start()

    tail = clean[last_end:].strip()
    if tail:
        segments.append({
            "chunk_type":   last_marker,
            "chunk_number": last_number,
            "chunk_text":   tail,
            "parent": current_article if last_marker == "SECTION" else None,
        })

    result: List[Dict] = []
    for seg in segments:
        if len(seg["chunk_text"]) >= _MIN_CHUNK_LENGTH:
            seg["chunk_index"] = len(result)
            result.append(seg)

    return result


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def embed_document_chunks(doc, source_type: str = "document") -> List[Dict]:
    """
    Chunk and embed a Document or LegacyDocument instance.

    Expects clean text to already be present:
      - Document      → doc.content  (plain text or Quill-stripped text)
      - LegacyDocument→ doc.extracted_text

    Returns a list of dicts ready for bulk-insert into DocumentChunk.
    """
    if source_type == "legacy_document":
        plain_text = getattr(doc, "extracted_text", "") or ""
    else:
        plain_text = getattr(doc, "content", "") or ""

    plain_text = plain_text.strip()

    if not plain_text:
        logger.warning("embed_document_chunks: no text for %s pk=%s — skipping.", source_type, doc.pk)
        return []

    chunks = chunk_legal_text(plain_text)

    if not chunks:
        logger.warning("embed_document_chunks: no chunks for %s pk=%s.", source_type, doc.pk)
        return []

    results: List[Dict] = []
    for chunk in chunks:
        chunk_input = f"{doc.title}. {chunk['chunk_text']}".strip()
        try:
            embedding = embed_text(chunk_input)
        except Exception as exc:
            logger.warning(
                "embed_document_chunks: embed failed for %s pk=%s chunk %d: %s",
                source_type, doc.pk, chunk["chunk_index"], exc,
            )
            continue

        results.append({
            "source_type":     source_type,
            "document_pk":     doc.pk,
            "document_title":  doc.title,
            "chunk_index":     chunk["chunk_index"],
            "chunk_type":      chunk["chunk_type"],
            "chunk_text":      chunk["chunk_text"],
            "embedding":       embedding,
        })

    logger.info(
        "embed_document_chunks: %d/%d chunks embedded for %s pk=%s.",
        len(results), len(chunks), source_type, doc.pk,
    )
    return results
