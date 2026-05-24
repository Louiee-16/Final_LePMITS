"""
rag/embedder.py
LePMITS — Step 2: Embedding Pipeline

Responsibilities:
  - Strip Quill HTML → plain text (BeautifulSoup)
  - Extract text from LegacyDocument PDFs via pdfplumber + Tesseract OCR fallback
  - Embed text with sentence-transformers (all-MiniLM-L6-v2, fully offline)
  - Save 384-dim vector to Document.embedding / LegacyDocument.embedding
"""

from __future__ import annotations

import logging
from typing import Optional

import pdfplumber
import pytesseract
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
from bs4 import BeautifulSoup
from PIL import Image
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model — loaded once at module import (cached for the process lifetime).
# all-MiniLM-L6-v2 produces 384-dim vectors; runs fully offline after first
# download.  First import triggers a one-time model download (~90 MB).
# ---------------------------------------------------------------------------
_model: Optional[SentenceTransformer] = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info("Loading sentence-transformers model (all-MiniLM-L6-v2)…")
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Model loaded.")
    return _model


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def strip_quill_html(html: str) -> str:
    """
    Strip Quill-generated HTML to plain text.

    Quill stores rich content as HTML (e.g. <p>, <strong>, <ol>, <li>).
    BeautifulSoup handles malformed tags gracefully.

    Returns an empty string if html is None or blank.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # get_text with separator=" " prevents words from adjacent tags merging.
    text = soup.get_text(separator=" ", strip=True)
    # Collapse multiple whitespace runs into a single space.
    return " ".join(text.split())


def extract_pdf_text(pdf_path: str) -> str:
    """
    Extract text from a PDF file.

    Strategy:
      1. Try pdfplumber (fast, text-layer PDFs).
      2. If a page yields no text, fall back to Tesseract OCR on that page's
         rasterised image (handles scanned/image-only PDFs).

    Args:
        pdf_path: Absolute filesystem path to the PDF.

    Returns:
        Concatenated plain text from all pages, or "" on failure.
    """
    if not pdf_path:
        return ""

    pages_text: list[str] = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                page_text = page.extract_text() or ""

                if page_text.strip():
                    pages_text.append(page_text)
                else:
                    # --- OCR fallback ---
                    logger.debug(
                        "Page %d has no text layer — falling back to OCR.", page_num
                    )
                    try:
                        # Render page to PIL image at 200 dpi (good balance of
                        # speed vs. accuracy for A4 legislative documents).
                        pil_image: Image.Image = page.to_image(resolution=200).original
                        pil_image = pil_image.convert("RGB")
                        ocr_text: str = pytesseract.image_to_string(
                            pil_image, lang="eng"
                        )
                        pages_text.append(ocr_text)
                    except Exception as ocr_err:
                        logger.warning(
                            "OCR failed on page %d: %s", page_num, ocr_err
                        )

    except Exception as pdf_err:
        logger.error("pdfplumber could not open '%s': %s", pdf_path, pdf_err)
        return ""

    full_text = "\n".join(pages_text)
    return " ".join(full_text.split())  # normalise whitespace


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_text(text: str) -> list[float]:
    """
    Convert plain text to a 384-dimensional float vector.

    sentence-transformers truncates inputs longer than 256 word-pieces by
    default.  For long ordinances the most legally distinctive content is
    usually in the first ~512 tokens, so no special chunking is needed at
    this stage (can be added later if retrieval quality warrants it).

    Returns a Python list[float] compatible with pgvector's VectorField.
    """
    if not text or not text.strip():
        raise ValueError("embed_text received empty text — cannot produce embedding.")

    model = _get_model()
    vector = model.encode(text, convert_to_numpy=True)
    return vector.tolist()


# ---------------------------------------------------------------------------
# High-level entry points
# ---------------------------------------------------------------------------

def embed_document(doc) -> bool:
    """
    Embed a Document instance (Quill HTML content) and persist the vector.

    Args:
        doc: A documents.models.Document instance.

    Returns:
        True if the embedding was saved, False on failure.
    """
    try:
        plain_text = strip_quill_html(doc.content)

        # Include title for better semantic representation.
        full_text = f"{doc.title}. {plain_text}".strip()

        if not full_text:
            logger.warning(
                "Document pk=%s has no extractable text — skipping.", doc.pk
            )
            return False

        doc.embedding = embed_text(full_text)
        doc.save(update_fields=["embedding"])
        logger.info("Embedded Document pk=%s ('%s').", doc.pk, doc.title)
        return True

    except Exception as exc:
        logger.error("Failed to embed Document pk=%s: %s", doc.pk, exc)
        return False


def embed_legacy_document(legacy_doc) -> bool:
    """
    Embed a LegacyDocument instance (PDF file) and persist the vector.

    Args:
        legacy_doc: A documents.models.LegacyDocument instance.
                    Expected to have a `file` FileField and a `title` field.

    Returns:
        True if the embedding was saved, False on failure.
    """
    try:
        pdf_path = legacy_doc.pdf_file # absolute filesystem path
        plain_text = extract_pdf_text(pdf_path)

        full_text = f"{legacy_doc.title}. {plain_text}".strip()

        if not full_text:
            logger.warning(
                "LegacyDocument pk=%s has no extractable text — skipping.",
                legacy_doc.pk,
            )
            return False


        legacy_doc.extracted_text = plain_text
        legacy_doc.ocr_processed = True
        legacy_doc.embedding = embed_text(full_text)
        legacy_doc.save(update_fields=["extracted_text", "ocr_processed", "embedding"])
        logger.info(
            "Embedded LegacyDocument pk=%s ('%s').", legacy_doc.pk, legacy_doc.title
        )
        return True

    except Exception as exc:
        logger.error(
            "Failed to embed LegacyDocument pk=%s: %s", legacy_doc.pk, exc
        )
        return False