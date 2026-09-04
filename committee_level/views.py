import os

from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.utils import ProgrammingError
from .models import CommitteeReport, HearingLog
from .docx_report import build_committee_report_docx, build_subject_line, has_remarks_content
from documents.models import Document
from documents.views import _committee_report_docx_path
from documents.urls import urlpatterns
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from audit.utils import log_action


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sync_report_status_from_hearing(report, outcome, *, returned_by=None, hearing=None):
    """
    Keep CommitteeReport.status in sync with the latest hearing outcome,
    and advance (or revert) the parent Document's status accordingly.

    A 'FAILED' hearing outcome sends the measure back to its author to
    revise and resubmit — done the same way every other "return to
    author" path in this app already does it (see documents/views.py's
    return_filed_doc/return_from_first_reading): status back to DRAFT
    (so it reappears in Draft Measures) plus a ReturnReason explaining
    why. This used to set a bespoke 'RETURNED' status instead — confirmed
    directly that nothing anywhere in the app ever queries or displays
    status='RETURNED' (not Draft Measures, not any tracking page, not
    Django admin — Document isn't even registered there), so a
    committee-failed measure became permanently unreachable the moment
    this fired. `returned_by`/`hearing` are optional purely so this can
    still be called without them (e.g. a future automated path) — the
    ReturnReason just carries less detail in that case.
    """
    # CommitteeReport.STATUS_CHOICES only has PENDING/APPROVED/FAILED —
    # HearingLog.OUTCOME_CHOICES also has RESET, which isn't a valid
    # report-level status (a reset hearing hasn't concluded anything, it's
    # still pending). Storing 'RESET' directly here would silently save a
    # status value get_status_display() has no label for.
    report.status = 'PENDING' if outcome == 'RESET' else outcome
    report.save()

    draft = report.draft

    if outcome == 'RESET':
        # The hearing that just happened didn't conclude anything — the
        # committee is reconvening on a different date, which hasn't been
        # picked yet. Clearing hearing_date reuses the exact same guard
        # report_workbench/committee_amendments already enforce (redirect
        # with "set a hearing date first") instead of needing a second,
        # separate check — and it's what drives the registry showing
        # "Pending Date" instead of a stale, already-passed date.
        draft.hearing_date = None
        draft.save()

    if outcome == 'APPROVED' and draft.source_barangay_doc_id:
        # Barangay-originated documents skip First Reading entirely (they
        # go straight to REFERRED — see barangay/views.py's
        # barangay_to_referral), so they never hit the numbering trigger
        # councilor-authored documents get at move_to_first. This is
        # their equivalent moment: the first point that formally confirms
        # the measure as legislative business rather than raw supervision.
        # Councilor-authored documents already have a number by now (from
        # First Reading), so this is a no-op for them either way — see
        # assign_reference_number's own docstring.
        from documents.views import assign_reference_number
        assign_reference_number(draft)

    if outcome == 'FAILED' and draft.status != 'DRAFT':
        from documents.models import ReturnReason
        previous_status = draft.status
        draft.status = 'DRAFT'
        draft.save()

        reason = 'Did not pass the committee hearing'
        if hearing is not None:
            # hearing.hearing_date is whatever the caller's
            # HearingLog.objects.create() call put there — report_workbench
            # passes request.POST's raw 'YYYY-MM-DD' string straight
            # through without going through a ModelForm's to_python(), so
            # the in-memory attribute is a plain str at this point, not a
            # date object, until the row is reloaded from the DB
            # (confirmed directly — strftime formatting on it raised
            # ValueError: Invalid format specifier). Handle both.
            hearing_date = hearing.hearing_date
            if isinstance(hearing_date, str):
                from django.utils.dateparse import parse_date
                hearing_date = parse_date(hearing_date) or hearing_date
            reason += (
                f' on {hearing_date:%B %d, %Y}' if hasattr(hearing_date, 'strftime')
                else f' on {hearing_date}'
            )
            if hearing.attendance_notes:
                reason += f'. {hearing.attendance_notes}'
        reason += '.'

        ReturnReason.objects.create(
            document=draft,
            reason=reason,
            returned_by=returned_by,
            previous_status=previous_status,
        )

    # RESET / PENDING — document status stays where it is (still in committee)


def _recompute_report_status(report):
    """Called right after a HearingLog is soft-deleted or restored (see
    delete_hearing_log/restore_hearing_log below) — without this,
    report.status keeps reflecting whatever outcome the deleted hearing
    set (e.g. APPROVED) even after the entry that justified it is gone
    (or was restored back), so the Committee Referral Registry shows a
    status nothing currently backs. Recomputes from whichever non-deleted
    hearing is now the most recent (HearingLog.Meta.ordering:
    -hearing_date, -id) — PENDING if none are left at all, same "RESET
    isn't a real report-level status" mapping _sync_report_status_from_
    hearing itself uses.

    Deliberately doesn't try to reverse the draft.status/reference-number/
    ReturnReason side effects a deleted hearing's outcome may have
    triggered (e.g. a FAILED hearing already having sent the draft back to
    the councilor as DRAFT) — those are separate, already-idempotent or
    already-visible-elsewhere effects, not something safe to silently
    unwind here without more context than a single hearing row gives.
    """
    latest = report.hearings.filter(deleted_at__isnull=True).first()
    report.status = 'PENDING' if (latest is None or latest.outcome == 'RESET') else latest.outcome
    report.save()


def _purge_expired_deleted_hearings(report):
    """Hard-deletes any HearingLog under this report whose same-day undo
    window (see delete_hearing_log) has actually lapsed — called on every
    report_workbench load, since this app has no background job runner to
    do it on a schedule. A soft-deleted entry from today is left alone
    (Restore still needs it); anything older is gone for good, same as a
    plain hard delete would have been before Restore existed."""
    today = timezone.now().date()
    report.hearings.filter(deleted_at__isnull=False, deleted_at__date__lt=today).delete()


# ---------------------------------------------------------------------------
# Workbench view
# ---------------------------------------------------------------------------
@login_required
def move_to_second_reading(request, doc_id):
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to advance this document.")
        return redirect('view-committee')

    if request.method != 'POST':
        return redirect('view-committee')

    # Was `get_object_or_404(Document, id=doc_id)` with no status filter at
    # all — unlike every sibling transition view here (committee_amendments/
    # save_committee_amendments/move_to_unfinished all filter the same way)
    # — meaning this could be POSTed against a document in *any* status,
    # including an already-APPROVED, enacted ordinance, silently regressing
    # it back to SECOND_READING. Same status scope those sibling views use.
    referred_doc = get_object_or_404(
        Document, Q(id=doc_id) & (Q(status='REFERRED') | Q(status__icontains='COMMITTEE'))
    )
    from documents.views import _onlyoffice_snapshot_pdf, _onlyoffice_checkpoint_sync, _onlyoffice_archive_docx

    # Amendments save live to the docx via Casual Docs (see
    # amending_table.html) — editing no longer syncs Document.content on
    # every autosave, only at checkpoints like this one, right before the
    # status change below. Must run first — see
    # _onlyoffice_checkpoint_sync's docstring.
    _onlyoffice_checkpoint_sync(referred_doc)
    referred_doc.amended_content = None
    referred_doc.amendment_status = None
    referred_doc.status = 'SECOND_READING'
    referred_doc.save()

    log_action(request, action='MOVE', target=f'{referred_doc.doc_type} — {referred_doc.reference_no}',
               detail='Moved to Second Reading.')

    _onlyoffice_snapshot_pdf(request, referred_doc)
    _onlyoffice_archive_docx(referred_doc)

    return redirect('view-committee')

def _ordinal(n):
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 4 -> '4th', 11-13 -> 'th' (the
    usual English exception), etc."""
    if 10 <= n % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def view_committee(request):
    committee_docs = Document.objects.filter(
        Q(status__icontains='REFERRED') | Q(status__icontains='COMMITTEE')
    ).select_related('committee_report')

    # Sort toggles for the registry table — "Referred On" (updated_at, since
    # there's no dedicated referred_at timestamp) and "Draft No." (reference_no,
    # a plain string sort — safe because every reference_no is zero-padded to
    # the same width within a prefix/year, e.g. DO-003-2026).
    sort = request.GET.get('sort', '-updated_at')
    valid_sorts = {'updated_at', '-updated_at', 'reference_no', '-reference_no'}
    if sort not in valid_sorts:
        sort = '-updated_at'
    committee_docs = committee_docs.order_by(sort)

    # A measure can go through more than one hearing (a Reset outcome
    # clears hearing_date and sends it back for another) — the registry
    # otherwise looks identical to a first-time referral, giving no hint
    # this is actually a repeat. hearing_number counts the hearing about
    # to happen (or already scheduled): existing logged hearings + 1.
    for doc in committee_docs:
        report = getattr(doc, 'committee_report', None)
        doc.hearing_number = (report.hearings.filter(deleted_at__isnull=True).count() + 1) if report else 1
        doc.hearing_ordinal = _ordinal(doc.hearing_number)

    return render(request, 'documents/tracking/committee_page.html', {
        'committee_docs': committee_docs,
        'current_sort': sort,
    })





@login_required
def report_workbench(request, draft_id):
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to manage committee reports.")
        return redirect('view-committee')

    draft  = get_object_or_404(Document, id=draft_id)

    # Was previously only enforced by hiding the link in committee_page.html
    # when doc.hearing_date is empty — nothing stopped a direct GET/POST to
    # this URL. A committee report reflects what was decided at an actual
    # hearing, so it shouldn't be startable before one is even scheduled.
    # Only applies to *starting* a report that's never been opened —
    # logging a Reset outcome deliberately clears hearing_date (see
    # _sync_report_status_from_hearing), and this same view is exactly
    # where that POST redirects back to; without the report_exists
    # check, that redirect would immediately re-trigger this guard and
    # bounce the user straight back out to the registry before they ever
    # saw the hearing they just logged (confirmed live).
    report_exists = CommitteeReport.objects.filter(draft=draft).exists()
    if not draft.hearing_date and not report_exists:
        messages.error(request, "Set a hearing date for this measure before starting its committee report.")
        return redirect('view-committee')

    report, _ = CommitteeReport.objects.get_or_create(draft=draft)
    _purge_expired_deleted_hearings(report)

    # Seed the report's own working docx once, the first time this page is
    # opened — everything after this is a real live document edited through
    # Casual Docs (see committee_report_document_bytes/_save below), same
    # as any other document in this app. Single committee only for now —
    # see docx_report.py's module docstring.
    docx_path = _committee_report_docx_path(report.id)
    if not os.path.exists(docx_path):
        os.makedirs(os.path.dirname(docx_path), exist_ok=True)
        with open(docx_path, "wb") as f:
            f.write(build_committee_report_docx(draft, report))

    if request.method == 'POST':
        action = request.POST.get('action')

        # ---- Add a new hearing entry ----
        if action == 'add_hearing':
            h_date  = request.POST.get('hearing_date')
            outcome = request.POST.get('hearing_outcome', 'PENDING')

            if h_date:
                hearing = HearingLog.objects.create(
                    report           = report,
                    hearing_date     = h_date,
                    outcome          = outcome,
                    attendance_notes = request.POST.get('attendance_notes', ''),
                )
                log_action(request, action='UPDATE',
                           target=f'{draft.doc_type} — {draft.reference_no or draft.id}',
                           detail=f'Hearing logged ({outcome}) for committee report.')
                _sync_report_status_from_hearing(report, outcome, returned_by=request.user, hearing=hearing)

        return redirect('report_workbench', draft_id=draft.id)

    return render(request, 'documents/committee_level/committee_workbench.html', {
        'report': report,
        'draft':  draft,
        'subject_line': build_subject_line(draft),
        # report.hearings.count would still count a same-day soft-deleted
        # entry (see HearingLog.deleted_at) — the "must have logged at
        # least one hearing" Save gate and the Hearing Timeline header
        # count both mean *active* hearings, not rows that happen to
        # still be in the table pending purge.
        'active_hearing_count': report.hearings.filter(deleted_at__isnull=True).count(),
    })


@login_required
@require_POST
def delete_hearing_log(request, hearing_id):
    """Undo an accidental "Log a Hearing" click — only for the rest of the
    day it was logged. Soft-delete (see HearingLog.deleted_at): Restore
    can bring it back for the remainder of that same day (see
    restore_hearing_log below); once the day has actually passed,
    _purge_expired_deleted_hearings hard-deletes it for good next time
    report_workbench loads — at that point it's part of the record and
    correcting it should be a new hearing entry, not silently rewriting
    history."""
    hearing = get_object_or_404(HearingLog, pk=hearing_id)
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to delete hearing logs.")
        return redirect('report_workbench', draft_id=hearing.report.draft_id)

    if hearing.created_at.date() != timezone.now().date():
        messages.error(request, "This hearing log can no longer be deleted — it wasn't logged today.")
        return redirect('report_workbench', draft_id=hearing.report.draft_id)

    draft_id = hearing.report.draft_id
    report = hearing.report
    hearing.deleted_at = timezone.now()
    hearing.save(update_fields=['deleted_at'])
    _recompute_report_status(report)
    log_action(request, action='DELETE', target=f'Hearing log #{hearing_id}',
               detail='Removed a same-day hearing log entry (restorable for the rest of today).')
    return redirect('report_workbench', draft_id=draft_id)


@login_required
@require_POST
def restore_hearing_log(request, hearing_id):
    """Undo a delete_hearing_log call — only while still within the same
    day the hearing was deleted (matching the same window deleting it in
    the first place requires). Once that window closes,
    _purge_expired_deleted_hearings will have already hard-deleted the
    row, so there's nothing left here to restore."""
    hearing = get_object_or_404(HearingLog, pk=hearing_id)
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to restore hearing logs.")
        return redirect('report_workbench', draft_id=hearing.report.draft_id)

    if not hearing.deleted_at or hearing.deleted_at.date() != timezone.now().date():
        messages.error(request, "This hearing log can no longer be restored.")
        return redirect('report_workbench', draft_id=hearing.report.draft_id)

    draft_id = hearing.report.draft_id
    report = hearing.report
    hearing.deleted_at = None
    hearing.save(update_fields=['deleted_at'])
    _recompute_report_status(report)
    log_action(request, action='UPDATE', target=f'Hearing log #{hearing_id}',
               detail='Restored a same-day-deleted hearing log entry.')
    return redirect('report_workbench', draft_id=draft_id)


_DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@login_required
def committee_report_document_bytes(request, report_id):
    """GET -> raw docx bytes for Casual Docs, same contract as
    documents/wopi.py::casualdocs_document_bytes but for a CommitteeReport
    instead of a Document — report_workbench always seeds this file before
    the page renders, so "not found yet" isn't a real case here."""
    report = get_object_or_404(CommitteeReport, pk=report_id)
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        return JsonResponse({"error": "forbidden"}, status=403)
    path = _committee_report_docx_path(report.id)
    if not os.path.exists(path):
        return JsonResponse({"error": "no_docx"}, status=404)
    with open(path, "rb") as f:
        return HttpResponse(f.read(), content_type=_DOCX_CONTENT_TYPE)


@login_required
def committee_report_has_remarks(request, report_id):
    """Polled from report_workbench to gate hearing-outcome selection —
    checks the actual saved docx (same source of truth as the file/save
    endpoints above), not live in-editor state, since REMARKS lives in a
    table cell and Casual Docs' own document-model accessor doesn't
    reliably reflect unsaved keystrokes for that (confirmed live)."""
    report = get_object_or_404(CommitteeReport, pk=report_id)
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        return JsonResponse({"error": "forbidden"}, status=403)
    path = _committee_report_docx_path(report.id)
    return JsonResponse({"has_remarks": has_remarks_content(path)})


@login_required
@require_POST
def committee_report_save(request, report_id):
    """POST -> the raw bytes Casual Docs' onSave produced, same contract as
    documents/wopi.py::casualdocs_save but for a CommitteeReport."""
    report = get_object_or_404(CommitteeReport, pk=report_id)
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        return JsonResponse({"error": "forbidden"}, status=403)
    if not request.body:
        return JsonResponse({"error": "empty_body"}, status=400)
    path = _committee_report_docx_path(report.id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(request.body)
    # Previously never called — report.updated_at (auto_now) silently
    # never moved on a live Casual Docs save, only on the unrelated
    # add_hearing/outcome writes in report_workbench. That's the field a
    # "saved" indicator in committee_workbench.html would actually read.
    report.save()
    return JsonResponse({"error": 0, "saved_at": report.updated_at.isoformat()})


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

def generate_committee_pdf(request, draft_id):
    """Converts the report's own working docx to PDF — same LibreOffice
    conversion every other document type in this app uses, replacing the
    old hand-built ReportLab layout now that the report is a real docx a
    user edits directly rather than a fixed template plus a remarks
    field."""
    draft  = get_object_or_404(Document, id=draft_id)
    report = get_object_or_404(CommitteeReport, draft=draft)

    docx_path = _committee_report_docx_path(report.id)
    if not os.path.exists(docx_path):
        messages.error(request, "This committee report has no content yet.")
        return redirect('report_workbench', draft_id=draft.id)

    from documents import libreoffice
    with open(docx_path, "rb") as f:
        docx_bytes = f.read()
    try:
        pdf_bytes = libreoffice.convert(docx_bytes, ".docx", "pdf", label=f"committee_report_{report.id}")
    except Exception as e:
        messages.error(request, f"PDF conversion failed: {e}")
        return redirect('report_workbench', draft_id=draft.id)

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    filename = f"Committee_Report_{draft.doc_type}_{draft.id}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
@login_required
def set_hearing_date(request):
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to set hearing dates.")
        return redirect('view-committee')

    if request.method == "POST":
        doc_id = request.POST.get('doc_id')
        h_date = request.POST.get('hearing_date')

        parsed = parse_date(h_date) if h_date else None
        if not parsed:
            messages.error(request, "Invalid hearing date.")
            return redirect(request.META.get('HTTP_REFERER', 'view-committee'))
        if parsed < timezone.localdate():
            messages.error(request, "Hearing date can't be in the past.")
            return redirect(request.META.get('HTTP_REFERER', 'view-committee'))

        doc = get_object_or_404(Document, id = doc_id)
        doc.hearing_date = parsed
        doc.save()
        log_action(request, action='UPDATE', target=f'{doc.doc_type} — {doc.reference_no or doc.id}',
                   detail=f'Hearing date set to {parsed}.')

    return redirect(request.META.get('HTTP_REFERER','view-committee'))


@login_required
def committee_amendments(request, doc_id):
    """Render the floor amendments workbench for a second-reading document."""
    from documents.models import PublicComment
    from councilors.models import Councilor
    doc = get_object_or_404(Document, Q(id=doc_id) & (Q(status='REFERRED') | Q(status__icontains='COMMITTEE')))
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to make amendments.")
        # 'second_reading' isn't a real URL name (this doc is REFERRED/
        # COMMITTEE, not SECOND_READING at all — copy-paste from the
        # actual second-reading flow) — was a guaranteed 500 on every
        # permission-denied hit. Redirect where the hearing-date guard
        # right below already sends people instead.
        return redirect('view-committee')

    # A committee can't meaningfully amend a measure before it knows when
    # it's actually hearing it — was previously unguarded on both this page
    # and its save endpoint below, unlike report_workbench (which at least
    # had a UI-only hide in committee_page.html; this had none at all).
    if not doc.hearing_date:
        messages.error(request, "Set a hearing date for this measure before making amendments.")
        return redirect('view-committee')

    amendments = doc.amendment_notes.select_related('author').all()
    try:
        # atomic() wraps just this query in its own savepoint — a bare
        # try/except here isn't enough: Postgres poisons the *whole*
        # surrounding transaction after a failed statement regardless of
        # whether Python catches it, so without this savepoint, every
        # query after this one in the same transaction (any later view in
        # the same request-scoped atomic block, or in the same test under
        # TestCase's wrapping) would also start failing with "current
        # transaction is aborted" — confirmed directly: this crashed the
        # very next query in the same request (a context processor) once
        # gazette_publiccomment was actually missing, e.g. in the test DB.
        with transaction.atomic():
            comments = list(PublicComment.objects.filter(
                document=doc,
                replyTo__isnull=True,       # top-level only, no replies
            ).order_by('created_at'))
    except ProgrammingError:
        # The Gazette's public-comment table isn't provisioned in this environment.
        comments = []
    return render(request, 'documents/committee_level/amending_table.html', {
        'doc': doc,
        'amendments': amendments,
        'comments':comments,
        'all_councilors': Councilor.objects.filter(is_active=True).order_by('name'),
    })


@login_required
def move_to_unfinished(request, doc_id):
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to move this document.")
        return redirect('view-committee')

    doc = get_object_or_404(Document,id=doc_id, status__icontains='COMMITTEE' )
    if request.method != 'POST':
        return redirect('report_workbench',draft_id = doc_id)
    # Amendments now save live to the docx via Casual Docs (see
    # amending_table.html) — doc.content is already current, no
    # amended_content to promote.
    doc.status = 'UNFINISHED_BUSINESS'
    doc.amended_content = None
    doc.amendment_status = None
    doc.save()
    log_action(request, action='MOVE', target=f'{doc.doc_type} — {doc.reference_no or doc.id}',
               detail='Moved to Unfinished Business.')

    from documents.views import _onlyoffice_snapshot_pdf
    _onlyoffice_snapshot_pdf(request, doc)

    return redirect('view-committee')


@login_required
def save_committee_amendments(request, doc_id):
    """Handle save and add_note actions from the amendments form."""
    doc = get_object_or_404(Document, Q(id=doc_id) & (Q(status='REFERRED') | Q(status__icontains='COMMITTEE')))

    # Role guard
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to make amendments.")
        # See the same fix/reasoning in committee_amendments above.
        return redirect('view-committee')

    # Same hearing-date gate as committee_amendments above, enforced here
    # too since this is a separate POST endpoint reachable on its own.
    if not doc.hearing_date:
        messages.error(request, "Set a hearing date for this measure before making amendments.")
        return redirect('view-committee')

    if request.method != 'POST':
        return redirect('committee-amendments', doc_id=doc_id)

    # Amendments themselves now save live to the docx via Casual Docs (see
    # amending_table.html) — this handler only tracks the informational
    # "In Progress / Finalized / No Amendments" label for the session.
    doc.amendment_status = request.POST.get('amendment_status', 'IN_PROGRESS')
    doc.save()
    return redirect('committee-amendments',doc_id=doc_id)


