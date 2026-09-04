"""Document-editing backend for this system (drafting, committee/floor
amendments with Track Changes, and read-only viewing), fronting the Casual
Docs (@casualoffice/docs) React editor — see frontend/casualdocs-entry.jsx.

Casual Docs is a plain browser-side React component, not a WOPI client —
it optionally takes raw docx bytes in via a `documentBuffer` prop (omitted
entirely, it creates its own blank document — confirmed directly against
its type definitions, not assumed) and hands edited bytes back out via an
`onSave` callback, so this module is just two plain session-authenticated
endpoints (casualdocs_document_bytes/casualdocs_save) rather than a WOPI
host implementation. `mode: 'editing' | 'suggesting' | 'viewing'` is
passed straight through to the `<DocxEditor>` component on the frontend;
the backend doesn't need to know which mode a given page uses since both
endpoints here are mode-agnostic (viewing pages simply never call the
save endpoint).

This module previously implemented a full WOPI host for Collabora Online,
which briefly ran as this system's document editor, then (after that was
replaced by Casual Docs) kept its Docker container running purely as a
headless document-conversion service. Both are gone now — see
documents/libreoffice.py for why (running Collabora's container safely
needs container permissions/seccomp changes that were a real tradeoff for
what's fundamentally just a file-format conversion) and for the local,
dependency-free replacement _ensure_docx_exists below now uses. Kept as
its own module rather than folded into views.py (already 2000+ lines) for
the same reason as before: shares its similarity-check logic with the
rest of the app via _run_similarity_check_on_docx in views.py, rather
than duplicating it. (The docx->HTML content sync used to live here too,
on every save — it doesn't anymore; see casualdocs_save's docstring for
why, and documents/views.py's _onlyoffice_checkpoint_sync for where that
now happens instead.)
"""
import logging
import os

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from . import libreoffice
from .docx_comments import restore_missing_comments
from .models import Document
from .views import (
    _can_edit_document,
    _can_view_document,
    _onlyoffice_saved_docx_path,
    _run_similarity_check_on_docx,
)

logger = logging.getLogger(__name__)


class NoDocxYet(Exception):
    """Raised by _ensure_docx_exists when doc has neither a saved docx nor
    any real Document.content to preserve — there's nothing to bootstrap.
    Distinct from a bootstrap-conversion failure: the caller should let
    Casual Docs create its own blank document client-side rather than
    treating this as an error."""


def _ensure_docx_exists(doc):
    """Returns the on-disk path to doc's saved docx, bootstrapping it if
    this document has never been saved as a docx before.

    Two different "never saved" cases, handled differently:
      - No real content either (a brand-new draft, Document.content is
        still ''): raises NoDocxYet — there's nothing to convert, and
        Casual Docs can start blank on its own with zero server
        involvement (confirmed via its own type definitions:
        documentBuffer/document are both optional — the editor doesn't
        require an existing file at all). This is the common case, since
        drafting always starts empty.
      - Real Document.content exists but was never saved as docx (e.g.
        it predates the docx-based editor entirely): genuinely needs a
        local LibreOffice conversion to avoid silently discarding that
        content. Rare in practice — every document that's gone through
        Casual Docs at all already has a docx by the time this matters.
    """
    path = _onlyoffice_saved_docx_path(doc.id)
    if os.path.exists(path):
        return path

    if not (doc.content or "").strip():
        raise NoDocxYet(f"Document {doc.id} has no saved docx and no content to bootstrap from.")

    html = f"<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>{doc.content or ''}</body></html>"
    docx_bytes = libreoffice.convert(html.encode("utf-8"), ".html", "docx", label=f"bootstrap_{doc.id}")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(docx_bytes)
    return path


# ------------------------------------------------------------------------
# Casual Docs endpoints — session-authenticated (browser calls these
# directly). Deliberately reuses _ensure_docx_exists/
# _onlyoffice_saved_docx_path/_sync_docx_to_document_content rather than a
# separate storage path: the on-disk docx represents the document's edited
# state, not which page/mode last touched it, so every editing surface's
# save-back must be visible to every other surface's next open.
# ------------------------------------------------------------------------

_DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _normalize_docx_default_spacing(docx_path):
    """Fills in explicit zero paragraph spacing on the docx's docDefaults/
    Normal style, but only where nothing is already set there — never
    overrides an actually-configured value.

    Casual Docs' own exported docx carries NO <w:spacing> anywhere at all
    (confirmed directly against a real saved file: neither per-paragraph,
    nor on the Normal style, nor in docDefaults) — which is a real gap,
    not a "0 means 0" value. Per the OOXML spec, when spacing is genuinely
    absent at every level, a compliant reader falls back to its own
    built-in default rather than to zero — the same "no spacing recorded
    -> apply ~8pt-after/1.08-line Word-default" behavior real Word itself
    uses for a paragraph with nothing overriding Normal. Confirmed live:
    a document typed and viewed within a single Casual Docs session (never
    round-tripped through this exact ambiguity) renders tight, but the
    identical content — same word/char count — visibly gains space
    between every paragraph once reopened from the saved .docx. Since
    LibreOffice reads this exact file for every PDF download/snapshot and
    the checkpoint HTML sync too (see documents/views.py's
    _onlyoffice_convert_docx_to_pdf/_sync_docx_to_document_content), an
    ambiguous file doesn't just look different in the editor on reopen —
    it can render with different spacing in the official PDF than what
    the councilor saw while drafting it.

    Making the docDefaults/Normal-style spacing explicit (rather than
    absent) removes that ambiguity for every downstream reader — Casual
    Docs' own reimport, LibreOffice, and Word itself if the .docx is ever
    opened there directly — instead of leaving each one to guess.
    Deliberately conditional on "not already set" rather than an
    unconditional overwrite: this should never fight a real, intentional
    spacing choice, only fill in a genuine gap.
    """
    from docx import Document as DocxDocument
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = DocxDocument(docx_path)
    changed = False

    normal_pf = doc.styles["Normal"].paragraph_format
    if normal_pf.space_before is None:
        normal_pf.space_before = Pt(0)
        changed = True
    if normal_pf.space_after is None:
        normal_pf.space_after = Pt(0)
        changed = True
    if normal_pf.line_spacing is None:
        normal_pf.line_spacing = 1.0
        changed = True

    # python-docx has no high-level API for docDefaults (it's a document-
    # wide fallback, not tied to any one style) — lxml directly, same as
    # Normal style above: only added if genuinely absent.
    styles_el = doc.styles.element
    doc_defaults = styles_el.find(qn("w:docDefaults"))
    if doc_defaults is not None:
        ppr_default = doc_defaults.find(qn("w:pPrDefault"))
        if ppr_default is not None:
            ppr = ppr_default.find(qn("w:pPr"))
            if ppr is None:
                ppr = OxmlElement("w:pPr")
                ppr_default.append(ppr)
            if ppr.find(qn("w:spacing")) is None:
                spacing = OxmlElement("w:spacing")
                spacing.set(qn("w:before"), "0")
                spacing.set(qn("w:after"), "0")
                spacing.set(qn("w:line"), "240")
                spacing.set(qn("w:lineRule"), "auto")
                ppr.append(spacing)
                changed = True

    if changed:
        doc.save(docx_path)


@login_required
def casualdocs_document_bytes(request, doc_id):
    """GET → raw docx bytes for Casual Docs' `documentBuffer` prop, or a
    distinct "no_docx" 404 for a brand-new document with nothing to load
    yet — the frontend treats that as "mount blank", not an error."""
    doc = get_object_or_404(Document, pk=doc_id)
    if not _can_view_document(request.user, doc):
        return JsonResponse({"error": "not_found"}, status=404)
    try:
        path = _ensure_docx_exists(doc)
    except NoDocxYet:
        return JsonResponse({"error": "no_docx"}, status=404)
    except Exception as e:
        logger.warning("Casual Docs bytes: bootstrap conversion failed for doc %s: %s", doc_id, e)
        return JsonResponse({"error": "bootstrap_failed", "detail": str(e)}, status=502)
    with open(path, "rb") as f:
        return HttpResponse(f.read(), content_type=_DOCX_CONTENT_TYPE)


def _log_incoming_comment_state(doc_id, body_bytes):
    """TEMPORARY diagnostic — not a fix, just visibility. Logs whether
    Casual Docs' own POST body already has comment parts before anything
    on our side (restore_missing_comments, _normalize_docx_default_spacing)
    touches it, to settle whether a reported "comment vanished" case
    originates client-side (Casual Docs never sent it) or server-side
    (something here is stripping it) — see HANDOFF_NOTES.md's
    2026-09-01/02 entry for the investigation this is part of. Safe to
    remove once that's resolved. Never raises — a logging failure must
    never affect the actual save."""
    try:
        import zipfile
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(body_bytes)) as z:
            names = z.namelist()
            comment_parts = [n for n in names if "comment" in n.lower()]
            body_ids = []
            if "word/comments.xml" in names:
                import re
                body_ids = re.findall(rb'<w:comment\s[^>]*w:id="(\d+)"', z.read("word/comments.xml"))
            anchor_ids = []
            if "word/document.xml" in names:
                import re
                anchor_ids = re.findall(rb'commentReference w:id="(\d+)"', z.read("word/document.xml"))
        logger.info(
            "[comment-diagnostic] doc %s incoming POST: %d bytes, comment parts=%s, "
            "anchor ids in document.xml=%s, comment body ids in comments.xml=%s",
            doc_id, len(body_bytes), comment_parts, anchor_ids, body_ids,
        )
    except Exception as e:
        logger.info("[comment-diagnostic] doc %s: could not inspect incoming body: %s", doc_id, e)


@login_required
@require_POST
def casualdocs_save(request, doc_id):
    """POST → the raw bytes Casual Docs' onSave callback produced. Just
    writes them to the on-disk docx — that alone is a complete save, since
    Casual Docs produces real docx bytes client-side (no server-side
    conversion needed to persist an edit).

    Deliberately does NOT sync Document.content here anymore. That used to
    run on every save (autosave included) via a local LibreOffice
    conversion, which meant every single edit needed a conversion to
    succeed — expensive, and the thing silently failing across a whole
    editing session left Document.content stale with no visible symptom.
    Content only needs to be fresh where it's actually read: PDF
    snapshots, the view-mode HTML fallback, similarity-check, and the
    Archives trail — all of which only change at status-transition
    checkpoints (filing, committee/floor amendment finalize), not on every
    keystroke. Those checkpoints call
    documents.views._onlyoffice_checkpoint_sync themselves, right before
    the status change that's about to archive this content. See
    documents/views.py's create_draft, barangay/views.py's
    barangay_to_referral, committee_level/views.py's
    move_to_second_reading, and this app's move_to_third_reading."""
    doc = get_object_or_404(Document, pk=doc_id)
    if not _can_edit_document(request.user, doc):
        return JsonResponse({"error": "forbidden"}, status=403)
    if not request.body:
        return JsonResponse({"error": "empty_body"}, status=400)
    path = _onlyoffice_saved_docx_path(doc.id)

    _log_incoming_comment_state(doc.id, request.body)

    # Stopgap for a Casual Docs reimport gap — see
    # documents/docx_comments.py's module docstring for the full
    # diagnosis: a comment (from the live inline check or the post-save
    # similarity check) can silently vanish from the editor's live
    # session once the document is reopened, and without this, the very
    # next save — autosave included — would overwrite the stored .docx
    # with a version that's permanently missing it. Must run BEFORE the
    # incoming bytes overwrite `path` below: it reads the version still
    # on disk to know what might need restoring. Non-blocking — any
    # failure here just means this particular save doesn't get the
    # protection, not that the save itself fails.
    incoming_bytes = request.body
    try:
        incoming_bytes = restore_missing_comments(path, incoming_bytes)
    except Exception as e:
        logger.warning("Comment restoration failed for doc %s: %s", doc.id, e)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(incoming_bytes)

    # Non-blocking, same reasoning as _onlyoffice_checkpoint_sync et al.:
    # a malformed/corrupt docx here (or any other python-docx hiccup)
    # shouldn't turn a successful save into a 500 — worst case the
    # ambiguous-spacing issue this fixes just isn't fixed for this
    # particular save, exactly as if this call didn't exist.
    try:
        _normalize_docx_default_spacing(path)
    except Exception as e:
        logger.warning("Docx spacing normalization failed for doc %s: %s", doc.id, e)

    # doc.save() is deliberately not called here (see this function's
    # docstring — the whole point is skipping Document's heavier save()
    # override on every keystroke-triggered autosave). The docx file's own
    # mtime is what a "saved"/"unsaved" indicator in amending_table.html
    # should actually key off of instead.
    from datetime import datetime, timezone as dt_timezone
    saved_at = datetime.fromtimestamp(os.path.getmtime(path), tz=dt_timezone.utc)
    return JsonResponse({"error": 0, "saved_at": saved_at.isoformat()})


@login_required
@require_POST
def similarity_check(request, doc_id):
    """Thin wrapper — see _run_similarity_check_on_docx in views.py for the
    actual logic. Writes native Word comments onto matching paragraphs in
    the saved docx; visible next time the document is reopened, not
    instantly."""
    doc = get_object_or_404(Document, pk=doc_id)
    if not _can_edit_document(request.user, doc):
        return JsonResponse({"error": "forbidden"}, status=403)
    flagged_count = _run_similarity_check_on_docx(doc)
    return JsonResponse({"error": 0, "new_flags": flagged_count})
