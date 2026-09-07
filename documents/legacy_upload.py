"""documents/legacy_upload.py

Legacy (pre-system) ordinance/resolution ingestion: scan a PDF, extract
text via pdfplumber + Tesseract OCR, let staff review/edit before saving,
then chunk and embed the result for RAG retrieval.

Split out of documents/views.py (previously one contiguous ~500-line
section under a "FOR UPLOADING OF LEGACY DOCUMENTS" banner comment) —
functionally self-contained, touches no live document-workflow status
transitions, and had grown large enough on its own to be its own module.
documents/views.py re-exports these names (`from .legacy_upload import
...`) so existing URL routing (documents/urls.py's `views.Upload_legacy`
etc.) and other import sites (documents/tests.py imports
`_page_ocr_confidence` directly from documents.views) keep working
unchanged.
"""
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pdfplumber
import pytesseract
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import DocumentChunk, LegacyDocument
from .permissions import _user_has_role
from documents.rag.embedder import embed_document_chunks

logger = logging.getLogger(__name__)

# Was hardcoded to the Windows installer path unconditionally — silently
# unusable (TesseractNotFoundError on every OCR call, not at import time)
# on any non-Windows deployment, with no way to fix it short of editing
# source. Now sourced from settings.TESSERACT_PATH (see config/settings.py
# for the same per-platform-default pattern already used for
# LIBREOFFICE_PATH) so it's overridable via .env like every other external
# binary path in this app.
pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_PATH


@login_required
def Upload_legacy(request):
    if not _user_has_role(request.user, 'SECRETARIAT', 'STAFF'):
        messages.error(request, "You don't have permission to upload legacy documents.")
        return redirect('dashboard')
    return render(request,'documents/upload_legacy.html')


@login_required
def upload_legacy_document(request):
    if not _user_has_role(request.user, 'SECRETARIAT', 'STAFF'):
        messages.error(request, "You don't have permission to upload legacy documents.")
        return redirect('dashboard')

    if request.method == 'POST':
        title        = request.POST.get('title', '').strip()
        reference_no = request.POST.get('reference_no', '').strip()
        doc_type     = request.POST.get('doc_type', '').strip()
        year_raw     = request.POST.get('year', '').strip()
        pdf_file     = request.FILES.get('pdf_file')

        # ── Validation ────────────────────────────────────────────
        errors = []

        if not title:
            errors.append("Document title is required.")

        if not reference_no:
            errors.append("Reference number is required.")
        elif LegacyDocument.objects.filter(reference_no__iexact=reference_no).exists():
            errors.append(f'Reference number "{reference_no}" is already in use by another legacy document.')

        if doc_type not in ('ORDINANCE', 'RESOLUTION'):
            errors.append("Please select a valid document type.")

        year = None
        if not year_raw:
            errors.append("Year is required.")
        else:
            try:
                year = int(year_raw)
                if not (1900 <= year <= 2100):
                    errors.append("Year must be between 1900 and 2100.")
            except ValueError:
                errors.append("Year must be a valid number.")

        if not pdf_file:
            errors.append("A PDF file is required.")
        else:
            ext = os.path.splitext(pdf_file.name)[1].lower()
            if ext != '.pdf':
                errors.append("Only PDF files are accepted.")
            if pdf_file.size > 20 * 1024 * 1024:   # 20 MB
                errors.append("File size must not exceed 20 MB.")

        if errors:
            for error in errors:
                messages.error(request, error)
            context = {
                'form': {
                    'title':        type('f', (), {'value': lambda self: title})(),
                    'reference_no': type('f', (), {'value': lambda self: reference_no})(),
                    'doc_type':     type('f', (), {'value': lambda self: doc_type})(),
                    'year':         type('f', (), {'value': lambda self: year_raw})(),
                    'pdf_file':     type('f', (), {'errors': [], 'value': lambda self: None})(),
                }
            }
            return render(request, 'documents/upload_legacy.html', context)

        # ── Save ──────────────────────────────────────────────────
        validated_text = request.POST.get('validated_text', '').strip()

        try:
            # atomic(): a bare except IntegrityError here without a
            # savepoint would leave the whole surrounding transaction
            # unusable after a collision (Postgres aborts it until
            # rollback) — this scopes the rollback to just this insert.
            with transaction.atomic():
                legacy_doc = LegacyDocument.objects.create(
                    title          = title,
                    reference_no   = reference_no,
                    doc_type       = doc_type,
                    year           = year,
                    pdf_file       = pdf_file,
                    extracted_text = validated_text,
                    ocr_processed  = bool(validated_text),
                )
        except IntegrityError:
            # Narrow race window between the .exists() check above and this
            # insert — the unique constraint on reference_no is the actual
            # guarantee, this just keeps a collision from surfacing as a
            # raw 500.
            messages.error(request, f'Reference number "{reference_no}" is already in use by another legacy document.')
            return render(request, 'documents/upload_legacy.html', {
                'form': {
                    'title':        type('f', (), {'value': lambda self: title})(),
                    'reference_no': type('f', (), {'value': lambda self: reference_no})(),
                    'doc_type':     type('f', (), {'value': lambda self: doc_type})(),
                    'year':         type('f', (), {'value': lambda self: year_raw})(),
                    'pdf_file':     type('f', (), {'errors': [], 'value': lambda self: None})(),
                }
            })

        # ── Chunk + embed immediately ──────────────────────────────
        if validated_text:
            try:
                chunk_records = embed_document_chunks(legacy_doc, source_type="legacy_document")
                if chunk_records:
                    # atomic(): a DB-level failure here (e.g. a misconfigured
                    # OLLAMA_EMBED_MODEL producing the wrong vector width —
                    # pgvector enforces the column's fixed dimensions at
                    # insert time, not just embed_text's own check) would
                    # otherwise leave the surrounding transaction unusable
                    # for the rest of this request, same class of bug fixed
                    # for the reference_no uniqueness check above.
                    with transaction.atomic():
                        DocumentChunk.objects.bulk_create([
                            DocumentChunk(
                                legacy_document = legacy_doc,
                                chunk_type      = cr["chunk_type"],
                                chunk_text      = cr["chunk_text"],
                                embedding       = cr["embedding"],
                                chunk_index     = cr["chunk_index"],
                            )
                            for cr in chunk_records
                        ])
                    messages.success(
                        request,
                        f'{doc_type.capitalize()} "{reference_no}" uploaded and indexed '
                        f'({len(chunk_records)} chunks).'
                    )
                else:
                    messages.warning(
                        request,
                        f'{doc_type.capitalize()} "{reference_no}" uploaded but no chunks were produced.'
                    )
            except Exception as e:
                logger.warning("Embedding failed for LegacyDocument pk=%s: %s", legacy_doc.pk, e)
                messages.warning(
                    request,
                    f'{doc_type.capitalize()} "{reference_no}" uploaded, but indexing failed — '
                    f'Ollama may be unavailable. Run embed_documents manually to index it.'
                )
        else:
            messages.success(
                request,
                f'{doc_type.capitalize()} "{reference_no}" uploaded (no text to index).'
            )

        return redirect('UPLOAD-LEGACY-DOCUMENT')

    # ── GET ───────────────────────────────────────────────────────
    return render(request, 'documents/upload_legacy.html', {})


# Experimental — tune based on real usage. Below this average Tesseract
# word-confidence (0-100), the AI cleanup pass is recommended by default;
# at or above it, the pass is skipped automatically (the "AI Review"
# button stays available to run it manually regardless — this is meant to
# be an observable, reversible default, not a hard cutoff).
AI_REVIEW_CONFIDENCE_THRESHOLD = 85.0


def _page_ocr_confidence(pil_image):
    """Average Tesseract word-confidence (0-100) for one rendered page, or
    None if Tesseract couldn't produce any confidence data. -1 entries in
    Tesseract's own output are structural (page/block/paragraph/line
    boundaries with no associated word) and excluded, not averaged in as 0."""
    try:
        data = pytesseract.image_to_data(pil_image, lang="eng", output_type=pytesseract.Output.DICT)
    except Exception as e:
        logger.warning("Tesseract confidence check failed: %s", e)
        return None

    confidences = []
    for raw_conf in data.get('conf', []):
        try:
            conf = float(raw_conf)
        except (TypeError, ValueError):
            continue
        if conf >= 0:
            confidences.append(conf)

    return sum(confidences) / len(confidences) if confidences else None


@login_required
@require_POST
def extract_legacy_metadata(request):
    """
    POST /documents/extract-legacy-metadata/
    Accepts a PDF file, reads first 2 pages, extracts
    title, reference_no, year, doc_type using regex.
    Returns JSON with extracted fields.
    """
    if not _user_has_role(request.user, 'SECRETARIAT', 'STAFF'):
        return JsonResponse({'error': "You don't have permission to upload legacy documents."}, status=403)

    pdf_file = request.FILES.get('pdf_file')
    if not pdf_file:
        return JsonResponse({'error': 'No file provided.'}, status=400)

    try:
        with pdfplumber.open(pdf_file) as pdf:
            total_pages = len(pdf.pages)
            # Indexed by page number so order survives OCR running out of
            # order below — None means "not yet extracted" (image-only page
            # whose OCR either hasn't run yet or produced nothing).
            page_texts = [None] * total_pages
            ocr_targets = []  # (page_index, PIL image) for pages needing OCR

            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                if text.strip():
                    page_texts[i] = text
                else:
                    # Image-only page — render it now, while the pdf is
                    # still open; the actual OCR pass (below) only needs
                    # the already-rendered image, not the open file.
                    try:
                        pil_image = page.to_image(resolution=200).original.convert("RGB")
                        ocr_targets.append((i, pil_image))
                    except Exception as render_err:
                        logger.warning("Failed to render page %d for OCR: %s", i + 1, render_err)

        # Each Tesseract call shells out to its own tesseract process, so
        # these are safe — and much faster — to run concurrently instead
        # of one page at a time, which is what made multi-page scanned
        # documents slow to upload.
        page_confidences = {}  # page index -> confidence, only for OCR'd pages

        def _ocr_page(pil_image):
            text = pytesseract.image_to_string(pil_image, lang="eng")
            confidence = _page_ocr_confidence(pil_image)
            return text, confidence

        if ocr_targets:
            with ThreadPoolExecutor(max_workers=min(4, len(ocr_targets))) as executor:
                future_to_index = {
                    executor.submit(_ocr_page, img): idx
                    for idx, img in ocr_targets
                }
                for future in as_completed(future_to_index):
                    idx = future_to_index[future]
                    try:
                        ocr_text, confidence = future.result()
                        if ocr_text.strip():
                            page_texts[idx] = ocr_text
                        page_confidences[idx] = confidence
                    except Exception as ocr_err:
                        logger.warning("Tesseract OCR failed for page %d: %s", idx + 1, ocr_err)

        all_pages_text  = [t for t in page_texts if t]
        meta_pages_text = [t for t in page_texts[:2] if t]

        # No OCR needed at all (fully born-digital text) counts as full
        # confidence — Tesseract was never even involved. Otherwise, only
        # average across pages that actually went through OCR; a page
        # whose confidence check itself failed doesn't count against or
        # for the average (excluded, not scored as 0).
        if not ocr_targets:
            ocr_confidence = 100.0
        else:
            valid_confidences = [c for c in page_confidences.values() if c is not None]
            ocr_confidence = (sum(valid_confidences) / len(valid_confidences)) if valid_confidences else None

        # Fail-safe: an unknown confidence (Tesseract's confidence check
        # itself failed on every OCR'd page) defaults to recommending
        # review rather than silently skipping it.
        ai_review_recommended = ocr_confidence is None or ocr_confidence < AI_REVIEW_CONFIDENCE_THRESHOLD

        # Full text with page separators — for user display
        display_text = "\n\n--- Page Break ---\n\n".join(all_pages_text).strip()

        # Metadata extraction uses only first 2 pages
        full_text = " ".join(meta_pages_text)
        # normalize whitespace
        full_text = re.sub(r'\s+', ' ', full_text).strip()

        if not full_text:
            return JsonResponse({
                'title': '',
                'reference_no': '',
                'year': str(timezone.now().year),
                'doc_type': 'ORDINANCE',
                'full_text': display_text,
                'total_pages': total_pages,
                'word_count': len(display_text.split()),
                'ocr_confidence': ocr_confidence,
                'ai_review_recommended': True,  # nothing usable came out — always worth a look
                'warning': 'Could not extract text from PDF. Check that OCR service is reachable.',
            })

        year_pattern = re.search(
            r'(?:Series\s+of|s\.?)\s+(\d{4})',
            full_text,
            re.IGNORECASE
        )
        # ── Extract reference number ──────────────────────────────
        # Matches: "City Ordinance No. 1", "Resolution No. 45-2021"
        ref_pattern = re.search(
            r'((?:CITY\s+)?(?:ORDINANCE|RESOLUTION)\s+NO[\.\s]+[\w\-]+)',
            full_text,
            re.IGNORECASE
        )
        reference_no = ref_pattern.group(1).strip() if ref_pattern else ""

        refnumber_match = re.search(
            r'NO\.\s*(\d+)',
            reference_no,
            re.IGNORECASE | re.DOTALL
        )

        # No zero-padding — CO-1-2025, not CO-001-2025 — matching the
        # same convention already used for live Document reference
        # numbers (see DocumentReferenceCounter/assign_reference_number).
        refnumber = str(int(refnumber_match.group(1))) if refnumber_match else "0"

        # ── Extract year ──────────────────────────────────────────
        # fallback: plain 4-digit year if "Series of YYYY" not found
        if not year_pattern:
            year_pattern = re.search(r'\b((?:19|20)\d{2})\b', full_text)
        year = year_pattern.group(1) if year_pattern else str(timezone.now().year)

        if "ORDINANCE" in reference_no.upper():
            first = "CO"
        else:
            first = "CR"
        reference_no = f"{first}-{refnumber}-{year}"

        # ── Extract doc_type ──────────────────────────────────────
        doc_type = "ORDINANCE"
        if re.search(r'\bRESOLUTION\b', full_text, re.IGNORECASE):
            doc_type = "RESOLUTION"

        # ── Extract title ─────────────────────────────────────────
        # Title is "AN ORDINANCE/RESOLUTION..." between "Series of YYYY"
        # and whichever terminator actually follows it. Previously required
        # "Sponsored" specifically and nothing else — real scanned
        # documents don't all have a "Sponsored by" line immediately after
        # the title (or OCR drops/garbles it), which silently left the
        # title blank with no indication anything went wrong. Falls back
        # through other common terminators, then to anchoring on the
        # ordinance/resolution number line if "Series of YYYY" itself
        # wasn't found, before giving up.
        title_terminators = r'(?:Sponsored|WHEREAS|NOW,?\s+THEREFORE)'
        title = ""
        title_pattern = re.search(
            rf'Series\s+of\s+\d{{4}}\s*(.*?)\s*{title_terminators}',
            full_text,
            re.IGNORECASE | re.DOTALL
        )
        if not title_pattern and reference_no:
            title_pattern = re.search(
                rf'(?:ORDINANCE|RESOLUTION)\s+NO[\.\s]+[\w\-]+\s*(.*?)\s*{title_terminators}',
                full_text,
                re.IGNORECASE | re.DOTALL
            )
        if title_pattern:
            candidate = re.sub(r'\s+', ' ', title_pattern.group(1)).strip().rstrip('.,;')
            # An implausibly long match means the non-greedy capture
            # skipped past a nearer, OCR-garbled terminator and grabbed
            # real body text instead — worse to accept than to leave
            # blank for manual entry.
            if 0 < len(candidate) <= 500:
                title = candidate

        return JsonResponse({
            'title': title,
            'reference_no': reference_no,
            'year': year,
            'doc_type': doc_type,
            'full_text': display_text,
            'total_pages': total_pages,
            'word_count': len(display_text.split()),
            'ocr_confidence': ocr_confidence,
            'ai_review_recommended': ai_review_recommended,
        })

    except Exception as e:
        logger.error("extract_legacy_metadata error: %s", e, exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def validate_ocr_with_ai(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    if not _user_has_role(request.user, 'SECRETARIAT', 'STAFF'):
        return JsonResponse({'error': "You don't have permission to upload legacy documents."}, status=403)

    # Accept pre-extracted text directly — no PDF scan needed
    raw_text = request.POST.get('raw_text', '').strip()

    if not raw_text:
        return JsonResponse({'error': 'No text provided.'}, status=400)

    import requests as req

    endpoint  = getattr(settings, 'OLLAMA_ENDPOINT',  'http://100.74.22.65:11434/api/generate')
    ocr_model = getattr(settings, 'OLLAMA_OCR_MODEL', 'siegemt/legislama:latest')
    logger.info("validate_ocr_with_ai: %d chars → AI", len(raw_text))
    logger.info("Using endpoint: %s, model: %s", endpoint, ocr_model)

    prompt = (
        "/set nothink \n"
        "You are a text correction engine for Philippine legislative documents.\n"
        "The text below was extracted by Tesseract OCR from a scanned document.\n"
        "Do the following:\n"
        "1. Fix garbled characters, broken words, and OCR typos.\n"
        "2. Remove all signature blocks, councilor names (HON. prefix), "
        "position/title lines, certification lines, and everything from "
        "'ENACTED BY THE COUNCIL' onward.\n"
        "3. Keep all legislative content: headers, document type and number, "
        "title, WHEREAS clauses, NOW THEREFORE, SECTION text, "
        "penalty and effectivity clauses.\n"
        "4. Keep '--- Page Break ---' markers exactly as-is.\n"
        "Return only the cleaned text, nothing else.\n\n"
        f"{raw_text}"
    )

    # True token streaming was tried and reverted here: manage.py runserver
    # (the only server this app runs under — no gunicorn/uwsgi in this
    # repo) always sets Connection: close and buffers the entire response
    # until the generator is exhausted, for any response without a
    # Content-Length (StreamingHttpResponse never has one). Verified
    # directly against Django 6.0.3's own ServerHandler.cleanup_headers —
    # a real HTTP client sees nothing until the whole thing is done
    # regardless of how the view yields internally, so streaming bought
    # nothing here and only added failure surface. If this app is ever
    # deployed behind a real WSGI/ASGI server, revisit — this limitation
    # is dev-server-specific, not inherent to Django or to Ollama's API.
    try:
        resp = req.post(
            endpoint,
            json={
                'model': ocr_model,
                'prompt': prompt,
                'stream': False,
                'options': {
                    'temperature': 0.1,
                    'num_predict': 4096,
                    'num_ctx': 8192,
                    'think': False,
                    'thinking': False,
                },
            },
            timeout=300
        )
        resp.raise_for_status()
        result = resp.json()
        cleaned_text = (
            result.get('response') or
            result.get('message', {}).get('content') or
            raw_text
        ).strip()
        cleaned_text = re.sub(r'<think>.*?</think>', '', cleaned_text, flags=re.DOTALL).strip()
        logger.info("AI cleanup done: %d chars, model=%s", len(cleaned_text), ocr_model)

    except Exception as e:
        logger.warning("AI cleanup failed (%s) — returning raw text.", e)
        cleaned_text = raw_text

    return JsonResponse({'cleaned_text': cleaned_text})
