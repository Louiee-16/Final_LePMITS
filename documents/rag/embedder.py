"""
rag/embedder.py  —  LePMITS Embedding Pipeline

Expects clean, pre-extracted text (stored on the model as extracted_text /
content).  This module only:
  1. Chunks the text by legislative markers  (chunk_legal_text)
  2. Embeds each chunk via settings.OLLAMA_EMBED_MODEL on Ollama  (embed_text)
  3. Persists vectors back to the model  (embed_document_chunks)
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def _ollama_base_url() -> str:
    endpoint = getattr(settings, 'OLLAMA_ENDPOINT', 'http://100.74.22.65:11434/api/generate')
    return endpoint.rsplit('/api/', 1)[0]


def embed_text(text: str, timeout: int = 60) -> list[float]:
    """Return a vector from settings.OLLAMA_EMBED_MODEL via Ollama, sized to
    documents.models.EMBEDDING_DIMENSIONS.

    Every VectorField this app has (Document/LegacyDocument/NationalLawChunk/
    DocumentChunk) is a fixed width — pgvector rejects an insert of the wrong
    size, but only at that point, deep inside whatever view or management
    command called this. Checking here instead means a misconfigured
    OLLAMA_EMBED_MODEL (wrong model, or a model that changed its own output
    size) fails immediately with a clear message, not as an opaque DB error
    several calls later. See documents/models.py's EMBEDDING_DIMENSIONS
    docstring — this has already bitten once (migration 0016 had to silently
    wipe every stored embedding after a dimension change).
    """
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

    from documents.models import EMBEDDING_DIMENSIONS
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"embed_text: model '{model}' returned a {len(vector)}-dim vector, "
            f"but documents.models.EMBEDDING_DIMENSIONS is {EMBEDDING_DIMENSIONS}. "
            f"OLLAMA_EMBED_MODEL is misconfigured for this database's vector columns."
        )
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

    def _embed_one(chunk: Dict) -> Dict:
        chunk_input = f"{doc.title}. {chunk['chunk_text']}".strip()
        return {
            "source_type":     source_type,
            "document_pk":     doc.pk,
            "document_title":  doc.title,
            "chunk_index":     chunk["chunk_index"],
            "chunk_type":      chunk["chunk_type"],
            "chunk_text":      chunk["chunk_text"],
            "embedding":       embed_text(chunk_input),
        }

    # Each embed_text call is a blocking HTTP request to Ollama — running
    # them concurrently instead of one at a time is what actually cuts the
    # wall-clock time here, since the wait is on network/inference, not CPU.
    results: List[Dict] = []
    with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as executor:
        future_to_chunk = {executor.submit(_embed_one, chunk): chunk for chunk in chunks}
        for future in as_completed(future_to_chunk):
            chunk = future_to_chunk[future]
            try:
                results.append(future.result())
            except Exception as exc:
                logger.warning(
                    "embed_document_chunks: embed failed for %s pk=%s chunk %d: %s",
                    source_type, doc.pk, chunk["chunk_index"], exc,
                )

    # Concurrent completion order isn't chunk order — restore it so
    # downstream bulk_create/logging behaves the same as the old
    # sequential loop, even though chunk_index is stored explicitly either way.
    results.sort(key=lambda r: r["chunk_index"])

    logger.info(
        "embed_document_chunks: %d/%d chunks embedded for %s pk=%s.",
        len(results), len(chunks), source_type, doc.pk,
    )
    return results


def _chunk_fields(chunk_record: Dict) -> Dict:
    return {
        "chunk_type":  chunk_record["chunk_type"],
        "chunk_text":  chunk_record["chunk_text"],
        "embedding":   chunk_record["embedding"],
        "chunk_index": chunk_record["chunk_index"],
    }


def embed_document(doc, source_type: str = "document") -> bool:
    """
    Chunk, embed, and persist DocumentChunk rows for a single Document or
    LegacyDocument instance, replacing any chunks it already has.

    Returns True if at least one chunk was saved, False if the document had
    no extractable text (or embedding failed for every chunk).
    """
    from documents.models import DocumentChunk

    chunk_records = embed_document_chunks(doc, source_type=source_type)
    if not chunk_records:
        return False

    filter_kwargs = (
        {"legacy_document": doc} if source_type == "legacy_document" else {"document": doc}
    )
    DocumentChunk.objects.filter(**filter_kwargs).delete()
    DocumentChunk.objects.bulk_create([
        DocumentChunk(**filter_kwargs, **_chunk_fields(cr)) for cr in chunk_records
    ])
    return True
