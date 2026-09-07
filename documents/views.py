from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, Http404, HttpResponse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_POST
from django.utils.html import strip_tags
import hashlib
import json
import os
from django.db import connection, transaction, IntegrityError
from django.db.models import Q
from .models import Document, AmendmentNote
from .permissions import _user_has_role
from committees.models import Committee
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from django.contrib import messages
from archives.models import Archives
from barangay.models import BarangayFiles
from secretariat.models import Session
from audit.utils import log_action
from .docx_comments import strip_ai_comments_from_file
import re
from io import BytesIO
from xhtml2pdf import pisa
from django.template.loader import get_template, render_to_string  # render_to_string used over get_template for more stability
from documents.rag.generator import generate_legal_basis
from .rag.embedder import embed_text
from .rag.retriever import retrieve


def _acquire_sequence_lock(*parts):
    """
    Serializes concurrent reference-number assignment for the same bucket
    (e.g. same doc_type + year) via a Postgres advisory lock, held for the
    rest of the current transaction and released automatically on
    commit/rollback. A plain COUNT()+1 assignment races when two requests
    read the same count before either commits — two staff filing or
    approving measures at the same moment could land on the same
    reference number. select_for_update() on the matching rows doesn't
    fully close this: the very first document of a doc_type/year has no
    existing row to lock against. Locking a stable, always-present key
    instead of the rows themselves closes that gap too. Must be called
    inside a transaction.atomic() block, and the number-assignment's
    .save() must happen before that block exits — the lock only prevents
    a second request from reading a stale count while it's held.
    """
    key = int(hashlib.md5('|'.join(str(p) for p in parts).encode()).hexdigest()[:15], 16)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def assign_reference_number(doc):
    """Assigns doc.reference_no exactly once, via a persistent
    per-(doc_type, year) counter (DocumentReferenceCounter) rather than
    counting existing rows in a hardcoded set of statuses — the old
    approach silently undercounted (and produced duplicate numbers,
    confirmed directly in production data) the moment a document moved
    into a status the list didn't include, or moved backward after
    already holding a number. This counter doesn't care about status at
    all, so neither failure mode can happen again.

    No-ops if doc.reference_no is already set. Callers decide *when* to
    call this — it's the trigger point that differs between a
    councilor-authored document (move_to_first, i.e. First Reading) and a
    barangay-originated one (committee_level's
    _sync_report_status_from_hearing, on an Approved outcome — barangay
    documents skip First Reading entirely, going straight to REFERRED, so
    they need a different numbering trigger). Self-contained: opens its
    own transaction.atomic() and saves doc itself, so callers don't need
    to know about the locking.
    """
    if doc.reference_no:
        return
    from .models import DocumentReferenceCounter
    current_year = timezone.now().year
    prefix = 'DO' if doc.doc_type == 'ORDINANCE' else 'DR'
    with transaction.atomic():
        _acquire_sequence_lock('draft_reference_no', prefix, current_year)
        counter, _ = DocumentReferenceCounter.objects.get_or_create(
            doc_type=doc.doc_type, year=current_year
        )
        counter.count += 1
        counter.save()
        doc.reference_no = f"{prefix}-{counter.count}-{current_year}"
        doc.save()


@login_required
def create_draft(request):
    if request.method == "POST":
        doc_id  = request.POST.get('doc_id')
        action  = request.POST.get('action')
 
        # Fetch existing GHOST/DRAFT or start fresh
        if doc_id and doc_id.strip():
            doc = get_object_or_404(Document, id=doc_id, author=request.user)

        else:
            doc = Document(author=request.user)
 
        doc.title    = request.POST.get('title', '').strip() or 'Untitled Draft'
        # draft.html and view_draft.html's edit branch both run on Casual Docs
        # now and post to this view without a `content` field — content is
        # saved separately, live, by the save handler (see
        # documents/wopi.py::casualdocs_save), so the `in request.POST`
        # check leaves it untouched here rather than blanking it out. Kept
        # generic (not assuming content is always absent) since this view
        # is also reachable from older/other callers that might still post
        # it directly.
        if 'content' in request.POST:
            doc.content = request.POST.get('content', '')
        # Only set if a real choice was posted — no default fallback, same
        # "don't silently default" reasoning as page_size/page_orientation
        # just below. A fresh draft starts with doc_type blank until the
        # councilor actually picks Ordinance or Resolution.
        posted_type = request.POST.get('type')
        if posted_type in dict(Document.DOC_CHOICES):
            doc.doc_type = posted_type

        # Layout tab (Page Setup) — same "only touch it if the form
        # actually sent it" reasoning as `content` above, since
        # view_draft.html's edit branch posts to this same view without
        # these fields either. Falls back to whatever's already on
        # doc (existing value, or the model's own default for a brand-new
        # Document) rather than a hardcoded default, so a malformed value
        # can't silently reset an already-configured document's layout.
        if request.POST.get('page_size') in dict(Document.PAGE_SIZE_CHOICES):
            doc.page_size = request.POST['page_size']
        if request.POST.get('page_orientation') in dict(Document.PAGE_ORIENTATION_CHOICES):
            doc.page_orientation = request.POST['page_orientation']
        for field in ('margin_top_cm', 'margin_bottom_cm', 'margin_left_cm', 'margin_right_cm'):
            raw = request.POST.get(field)
            if raw is not None:
                try:
                    value = float(raw)
                except ValueError:
                    continue
                if 0.5 <= value <= 10:
                    setattr(doc, field, value)

        # Fixed: field name now matches the template (referred_committee)
        committee_id = request.POST.get('referred_committee')
        if committee_id:
            doc.referred_committee_id = committee_id
        else:
            doc.referred_committee = None
 
        if action == 'submit':
            # Filing is the point of no return (reference number assigned,
            # goes to First Reading) — a draft that's still literally
            # "Untitled Draft" or has never had Ordinance/Resolution chosen
            # shouldn't be allowed through, even though the UI is also
            # expected to disable the File button for the same reason.
            # This is the real enforcement; that's just a convenience.
            title_ok = doc.title.strip() and doc.title.strip().lower() != 'untitled draft'
            type_ok = doc.doc_type in dict(Document.DOC_CHOICES)
            if not title_ok or not type_ok:
                if not title_ok:
                    messages.error(request, "Give this draft a real title before filing it.")
                else:
                    messages.error(request, "Choose Ordinance or Resolution before filing.")
                if not doc.pk or doc.status == 'GHOST':
                    doc.status = 'DRAFT'
                doc.save()
                return redirect('view-draft', id=doc.id)

            # Filing is the point of no return — the AI inline-check's own
            # scratch comments (Similarity Check (AI) / LePMITS AI) are a
            # drafting aid and shouldn't follow the document past this
            # point. Strip before the checkpoint sync so a stale comment
            # can't leak into Document.content or the archived docx.
            if doc.pk:
                strip_ai_comments_from_file(_onlyoffice_saved_docx_path(doc.id))
            # Must run before doc.status changes — see
            # _onlyoffice_checkpoint_sync's docstring.
            _onlyoffice_checkpoint_sync(doc)
            doc.status = 'FILED'
            # No reference number here — filing just means "secretariat
            # can now review this." A number is only assigned once it's
            # actually accepted into the legislative pipeline, at First
            # Reading (see move_to_first / assign_reference_number).
            doc.save()

        else:
            # Plain save — keep as DRAFT, never promote
            if not doc.pk or doc.status == 'GHOST':
                doc.status = 'DRAFT'
            doc.save()

        if action == 'submit':
            # Filing is the first checkpoint where a PDF snapshot exists —
            # every passive viewer downstream (incoming docs, first
            # reading, etc.) shows this instead of mounting a live editor.
            _onlyoffice_snapshot_pdf(request, doc)
            _onlyoffice_archive_docx(doc)

        log_action(
            request,
            action='FILE',
            target=f'{doc.doc_type} — {doc.reference_no}',
            detail=f'"{doc.title}" filed by {request.user.username}'
        )
        return redirect('dashboard')
 
    committees = Committee.objects.all()
    # Only ever reuse an existing GHOST — a never-formally-saved
    # placeholder, recycled so repeat visits to this page without saving
    # don't pile up empty orphan rows (nothing else deletes them
    # automatically; that's what the Discard button's discard_ghost is
    # for). Deliberately excludes DRAFT: a DRAFT is a real, explicitly
    # saved draft with its own title/content, and a councilor can have
    # several in flight at once (see Draft Measures) — "Create Draft"
    # should always hand back a blank slate (or the one still-unsaved
    # ghost), never silently reopen and overwrite an already-saved one.
    ghost = Document.objects.filter(
        author=request.user,
        status='GHOST'
    ).order_by('-updated_at').first()

    # The editor needs a real Document id to load/save against from the
    # very first keystroke — unlike Quill, there's no JS-driven
    # autosave to create that row lazily. So a brand-new visit (no existing
    # ghost) gets one created up front here instead.
    ghost_is_new_row = not ghost
    if ghost_is_new_row:
        ghost = Document.objects.create(
            author=request.user,
            title='Untitled Draft',
            content='',
            status='GHOST',
        )

    # is_new_ghost tells the template not to show the "unsaved work
    # recovered" banner unless there's genuinely something to recover.
    # Deliberately NOT just "was a new row created above" — a ghost row can
    # already exist but still be empty (created on an earlier visit that
    # the user left without ever typing/saving anything), and showing
    # "Unsaved work recovered" for a blank document with 0 words is
    # actively misleading (confirmed live). Plain truthiness on
    # doc.content isn't enough either — an empty document that's been
    # through even one docx<->HTML round trip (e.g. a periodic autosave
    # firing on a still-blank doc) comes back as
    # '<p class="western">\n<br>\n<br>\n</p>\n', which is non-empty as a
    # raw string but has no actual text (confirmed against a real row in
    # the DB). strip_tags() reduces that to whitespace, correctly read as
    # "nothing here". Also checks for a saved .docx: Document.content no
    # longer syncs on every autosave (only at status-transition
    # checkpoints — see _onlyoffice_checkpoint_sync), so a ghost that's
    # been actively edited in Casual Docs but never reached a checkpoint
    # would otherwise still show content='' here even with real,
    # unsaved-to-a-checkpoint work sitting in its .docx. Casual Docs only
    # calls onSave once something's actually been changed (not on a bare,
    # untouched mount), so a saved docx existing at all is itself a
    # reasonable signal that this isn't a pristine, never-touched ghost.
    has_saved_docx = os.path.exists(_onlyoffice_saved_docx_path(ghost.id))
    has_real_content = (
        bool(strip_tags(ghost.content or '').strip())
        or (ghost.title and ghost.title != 'Untitled Draft')
        or has_saved_docx
    )
    is_new_ghost = ghost_is_new_row or not has_real_content

    # Check if the draft was returned with a reason
    return_reason = None
    if not is_new_ghost:
        from .models import ReturnReason
        return_reason = ReturnReason.objects.filter(document=ghost).first()

    return render(request, 'documents/draft.html', {
        'committees': committees,
        'ghost': ghost,
        'is_new_ghost': is_new_ghost,
        'return_reason': return_reason,
    })


@login_required
@require_POST
def upload_draft_image(request):
    """Backs the ribbon's Insert > Image button. Re-encodes through Pillow
    rather than storing the upload byte-for-byte — this both validates it's
    a real image (not a mislabeled file) and strips anything riding along
    in the original bytes (EXIF, embedded scripts some formats allow,
    trailing junk after the image data). Returns a /media/draft_images/...
    URL; sanitize_document_html only lets <img src> through when it starts
    with that exact prefix, so an editor can't be tricked into embedding
    an external or javascript: URL as an "image"."""
    upload = request.FILES.get('image')
    if not upload:
        return JsonResponse({"error": "No file uploaded."}, status=400)

    max_bytes = 8 * 1024 * 1024
    if upload.size > max_bytes:
        return JsonResponse({"error": "Image must be under 8MB."}, status=400)

    from PIL import Image as PILImage
    try:
        img = PILImage.open(upload)
        img.verify()
        upload.seek(0)
        img = PILImage.open(upload)
        img.load()
    except Exception:
        return JsonResponse({"error": "That doesn't look like a valid image file."}, status=400)

    if img.format not in ("JPEG", "PNG", "WEBP", "GIF"):
        return JsonResponse({"error": "Only JPEG, PNG, WEBP, or GIF images are supported."}, status=400)

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        out_format, ext = "PNG", "png"
    else:
        img = img.convert("RGB")
        out_format, ext = "JPEG", "jpg"

    import uuid
    filename = f"{uuid.uuid4().hex}.{ext}"
    save_dir = os.path.join(settings.MEDIA_ROOT, "draft_images")
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)
    img.save(save_path, out_format, quality=88 if out_format == "JPEG" else None, optimize=True)

    return JsonResponse({"url": f"{settings.MEDIA_URL}draft_images/{filename}"})


############# MOVING FUNCTIONS$#########################
@login_required
def return_filed_doc(request, doc_id):
    """Return a FILED document back to DRAFT with a reason."""
    if request.method == 'POST' and request.user.role in ['SECRETARIAT', 'STAFF']:
        from .models import ReturnReason
        doc    = get_object_or_404(Document, id=doc_id, status='FILED')
        reason = request.POST.get('return_reason', '').strip()

        if not reason:
            messages.error(request, 'A reason is required when returning a document.')
            return redirect('incoming_docs')

        doc.status = 'DRAFT'
        doc.save()

        ReturnReason.objects.create(
            document=doc,
            reason=reason,
            returned_by=request.user,
            previous_status='FILED',
        )
        log_action(request, action='RETURN', target=doc.title,
                   detail=f'Returned to draft. Reason: {reason}')
        messages.success(request, f'"{doc.title}" returned to the councilor.')
    return redirect('incoming_docs')


@login_required
def return_from_first_reading(request, doc_id):
    """Return a FIRST_READING document to DRAFT with a reason."""
    if request.method != 'POST' or not _user_has_role(request.user, 'SECRETARIAT', 'STAFF'):
        return redirect('first_reading')

    from .models import ReturnReason
    doc    = get_object_or_404(Document, id=doc_id)
    reason = request.POST.get('return_reason', '').strip()

    if not reason:
        messages.error(request, 'A reason is required when returning a document.')
        return redirect('first_reading')

    previous = doc.status
    doc.status = 'DRAFT'
    doc.save()

    ReturnReason.objects.create(
        document=doc,
        reason=reason,
        returned_by=request.user,
        previous_status=previous,
    )

    log_action(
        request, action='RETURN', target=doc.title,
        detail=f'Returned from {previous} to DRAFT. Reason: {reason}'
    )
    messages.success(request, f'"{doc.title}" returned to the councilor with reason.')
    return redirect('first_reading')


@login_required
def return_barangay_file(request, doc_id):
    """Return a FILED barangay document back to DRAFT status."""
    if request.method == 'POST' and request.user.role in ['SECRETARIAT', 'STAFF']:
        doc = get_object_or_404(BarangayFiles, id=doc_id, status='FILED')
        doc.status = 'DRAFT'
        doc.save()
        log_action(request, action='RETURN', target=str(doc.title or doc.id),
                   detail=f'Barangay file #{doc.id} returned by {request.user.username}')
    return redirect('incoming_docs')


@login_required
def return_to_committee(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id)
    user = request.user
    if doc.status == 'SECOND_READING' and user.role in ['SECRETARIAT','STAFF']:
        # Must run before doc.status changes — see _onlyoffice_checkpoint_sync's docstring.
        _onlyoffice_checkpoint_sync(doc)
        committee_check = Archives.objects.filter(original_doc = doc_id, status__icontains= 'COMMITTEE')
        if committee_check:
            x = committee_check.count()
            doc.status = f'COMMITTEE [{x+1}]'
        else:
            doc.status = 'COMMITTEE'
        doc.save()
        # Same checkpoint pattern as every other status transition — was
        # missing here, which is why no PDF/docx snapshot ever existed for
        # a document sent back to committee this way.
        _onlyoffice_snapshot_pdf(request, doc)
        _onlyoffice_archive_docx(doc)
        log_action(request, action='MOVE', target=f'{doc.doc_type} — {doc.reference_no or doc.id}',
                   detail=f'Returned to committee ({doc.status}).')
    else:
        doc.save()
    return redirect('second-reading')


@login_required
def move_to_disapproved(request, doc_id):
    third_reading_doc = get_object_or_404(Document, id = doc_id)
    user = request.user
    if third_reading_doc.status == 'THIRD_READING' and user.role in ['SECRETARIAT','STAFF']:
        # Must run before status changes — see _onlyoffice_checkpoint_sync's docstring.
        _onlyoffice_checkpoint_sync(third_reading_doc)
        third_reading_doc.status = 'DISAPPROVED'
        third_reading_doc.save()
        _onlyoffice_snapshot_pdf(request, third_reading_doc)
        _onlyoffice_archive_docx(third_reading_doc)
        log_action(request, action='MOVE',
                   target=f'{third_reading_doc.doc_type} — {third_reading_doc.reference_no or third_reading_doc.id}',
                   detail='Marked Disapproved.', severity='HIGH')
    return redirect('third-reading')

@login_required
def move_to_first(request, pk):
    doc = get_object_or_404(Document, pk=pk)

    if request.method != 'POST' or doc.status != 'FILED' or not _user_has_role(request.user, 'SECRETARIAT'):
        messages.error(request, "You don't have permission to perform this action.")
        return redirect('incoming_docs')

    # Must run before doc.status changes — see _onlyoffice_checkpoint_sync's docstring.
    _onlyoffice_checkpoint_sync(doc)
    doc.status = 'FIRST_READING'
    # This is the real numbering moment for a councilor-authored document
    # — see assign_reference_number's docstring for why it moved here
    # from filing. No-ops (and doesn't save) if doc already has a number
    # (e.g. returned-then-refiled) — the explicit save below still needs
    # to run either way to persist the status change.
    assign_reference_number(doc)
    doc.save()
    _onlyoffice_snapshot_pdf(request, doc)
    _onlyoffice_archive_docx(doc)
    log_action(request, action='MOVE', target=f'{doc.doc_type} — {doc.reference_no or doc.id}',
               detail='Moved to First Reading.')
    return redirect('incoming_docs')

@login_required
def move_to_other_matters(request, doc_id):
    doc = get_object_or_404(BarangayFiles, id=doc_id)

    if request.method != 'POST' or doc.status != 'FILED' or not _user_has_role(request.user, 'SECRETARIAT'):
        messages.error(request, "You don't have permission to perform this action.")
        return redirect('incoming_docs')

    doc.status = 'OTHER_MATTERS'
    doc.save()
    log_action(request, action='MOVE', target=doc.title or f'Barangay measure #{doc.id}',
               detail='Moved to Other Matters.')
    return redirect('incoming_docs')

@login_required
def unfinished_to_third(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, status = 'UNFINISHED_BUSINESS')
    if not _user_has_role(request.user, 'SECRETARIAT', 'STAFF'):
        messages.error(request, "You don't have permission to perform this action.")
        return redirect('unfinished-business')
    if request.method != 'POST':
        return redirect('unfinished-business')
    doc.status = 'THIRD_READING'
    doc.save()
    log_action(request, action='MOVE', target=f'{doc.doc_type} — {doc.reference_no or doc.id}',
               detail='Moved from Unfinished Business to Third Reading.')
    return redirect('unfinished-business')

@login_required
def move_to_third_reading(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, status='SECOND_READING')

    if not _user_has_role(request.user, 'SECRETARIAT', 'STAFF', 'ADMIN'):
        messages.error(request, "You don't have permission to perform this action.")
        return redirect('second-reading')

    if request.method != 'POST':
        return redirect('second-reading')

    # No amendment_status guard here (deliberately removed — see
    # docs/activity-log.md, "Move to Third Reading" was permanently
    # unblockable for a while after floor_amendments.html's own controls
    # for setting it were removed in a UI simplification, unaware this
    # guard depended on them). Not a gap: this URL is only ever linked
    # from floor_amendments.html — second_reading.html's only row action
    # is Insert Amendments — so reaching this endpoint through the UI at
    # all already means the amendment step was opened, without needing a
    # second, now-unsatisfiable server-side check for it.

    # Amendments during floor deliberation are made live, directly in the
    # .docx via Casual Docs (see floor_amendments.html) — editing no longer
    # syncs Document.content on every autosave, only at checkpoints like
    # this one, right before the status change below. Must run first — see
    # _onlyoffice_checkpoint_sync's docstring. amendment_status stays purely
    # informational (the "In Progress / Finalized / No Amendments" label
    # for this floor session) and is just cleared on exit, same
    # housekeeping as before.
    _onlyoffice_checkpoint_sync(doc)
    doc.amended_content = None
    doc.amendment_status = None

    # This triggers your save() override → version bump + Archives snapshot
    doc.status = 'THIRD_READING'
    doc.save()

    # New checkpoint's PDF + archived docx, keyed to the version Archives
    # just snapshotted.
    _onlyoffice_snapshot_pdf(request, doc)
    _onlyoffice_archive_docx(doc)

    log_action(request, action='MOVE', target=f'{doc.doc_type} — {doc.reference_no or doc.id}',
               detail='Moved to Third Reading.')
    messages.success(request, f'"{doc.title}" has been moved to Third Reading.')
    return redirect('second-reading')

########## for autosaving #####################################

def _apply_layout_fields(doc, data):
    """Shared by autosave_draft's two save branches — mirrors create_draft's
    same "only touch it if present, validate before applying" handling for
    the Layout tab's Page Setup fields, so a malformed autosave payload
    can't blank out or corrupt an already-configured document's layout."""
    if data.get('page_size') in dict(Document.PAGE_SIZE_CHOICES):
        doc.page_size = data['page_size']
    if data.get('page_orientation') in dict(Document.PAGE_ORIENTATION_CHOICES):
        doc.page_orientation = data['page_orientation']
    for field in ('margin_top_cm', 'margin_bottom_cm', 'margin_left_cm', 'margin_right_cm'):
        raw = data.get(field)
        if raw is not None:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if 0.5 <= value <= 10:
                setattr(doc, field, value)


@login_required
def autosave_draft(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)
        doc_id = data.get('doc_id')

        if doc_id and str(doc_id) != 'null':
            doc = get_object_or_404(Document, id=doc_id, author=request.user)

            if doc.status not in ['GHOST', 'DRAFT']:
                return JsonResponse({'status': 'error', 'message': 'Document cannot be edited'}, status=403)

            doc.title = data.get('title', doc.title)
            doc.content = data.get('content', doc.content)
            _apply_layout_fields(doc, data)
            doc.save()
        else:
            doc = Document.objects.filter(
                author=request.user,
                status='GHOST'
            ).first()

            if doc:
                doc.title = data.get('title', doc.title)
                doc.content = data.get('content', doc.content)
                _apply_layout_fields(doc, data)
                doc.save()
            else:
                doc = Document.objects.create(
                    title=data.get('title', 'Untitled Draft'),
                    content=data.get('content', ''),
                    author=request.user,
                    status='GHOST'
                )
                _apply_layout_fields(doc, data)
                doc.save()

        return JsonResponse({'status': 'saved', 'doc_id': doc.id})

    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
    except Exception:
        return JsonResponse({'status': 'error', 'message': 'Autosave failed'}, status=500)
    
@login_required
def discard_ghost(request):
    if request.method == 'POST':
        deleted, _ = Document.objects.filter(
            author=request.user,
            status='GHOST'
        ).delete()
        if deleted:
            log_action(request, action='DELETE', target=f'{deleted} untitled draft(s)',
                       detail=f'Ghost draft(s) discarded by {request.user.username}.')
    return JsonResponse({'status': 'discarded'})



@login_required
def incoming_docs(request):
    incoming_docs = Document.objects.filter(status = 'FILED') 
    barangay_docs = BarangayFiles.objects.filter(status = 'FILED') 
    for doc in incoming_docs:
        try:
            name = doc.author.councilor_profile.name
            parts = name.replace('"','').split()
            doc.surname = parts[-1]
        except:
            doc.surname = ""
    context = {
        'incoming_docs': incoming_docs,
        'barangay_docs': barangay_docs
    }
    return render(request,'documents/incoming.html', context)






    


######################___________first reading________________######################

@login_required
def refer_to_committee(request, doc_id):
    if request.method == "POST" and request.user.role in ('SECRETARIAT', 'STAFF'):
        doc = get_object_or_404(Document, id=doc_id)
        committee_id = request.POST.get('referred_committee')
        
        if not committee_id:
            messages.error(request, "Please select a target committee.")
            return redirect('first_reading')

        committee = get_object_or_404(Committee, id=committee_id)

        # Must run before doc.status changes — see _onlyoffice_checkpoint_sync's docstring.
        _onlyoffice_checkpoint_sync(doc)
        doc.status = 'REFERRED'
        doc.referred_committee = committee
        doc.save()
        _onlyoffice_snapshot_pdf(request, doc)
        _onlyoffice_archive_docx(doc)
        log_action(request, action='MOVE', target=f'{doc.doc_type} — {doc.reference_no or doc.id}',
                   detail=f'Referred to committee: {committee.name}.')
        recipients = []

        def add_email(councilor):
            if councilor and hasattr(councilor, 'user') and councilor.user.email:
                recipients.append(councilor.user.email)
            elif councilor and hasattr(councilor, 'email') and councilor.email:
                recipients.append(councilor.email)

        add_email(committee.chairman)
        add_email(committee.vice_chairman)
        add_email(committee.member)
        recipients = list(set(recipients))
        if recipients:
            subject = f"Official Referral: {doc.reference_no} - {doc.title}"
            message = (
                f"Honorable Members of the {committee.name},\\n\\n"
                f"The legislative measure entitled '{doc.title}' ({doc.reference_no}) "
                f"has been officially referred to your committee.\\n\\n"
                f"Please log in to the system to review the content and prepare for the Committee Hearing.\\n\\n"
                f"Best regards,\\n"
                f"Office of the Secretariat"
            )
            
            try:
                send_mail(
                    subject,
                    message,
                    settings.DEFAULT_FROM_EMAIL,
                    recipients,
                    fail_silently=False,
                )
            except Exception as e:
                print(f"SMTP Error: {e}")

        messages.success(request, f"{doc.reference_no} successfully referred to {committee.name}.")
        return redirect('first_reading')
        
    return redirect('first_reading')



######################___________second reading_______________######################

@login_required
def floor_amendments(request, doc_id):
    """Render the floor amendments workbench for a second-reading document."""
    doc = get_object_or_404(Document, id=doc_id, status='SECOND_READING')


    if not _user_has_role(request.user, 'SECRETARIAT', 'STAFF', 'ADMIN'):
        messages.error(request, "You don't have permission to make amendments.")
        # 'second_reading' isn't a registered URL name (only the
        # hyphenated 'second-reading' is) — was a guaranteed 500 on every
        # permission-denied hit here.
        return redirect('second-reading')

    amendments = doc.amendment_notes.select_related('author').all()
    from councilors.models import Councilor

    return render(request, 'documents/tracking/floor_amendments.html', {
        'doc': doc,
        'amendments': amendments,
        'all_councilors': Councilor.objects.filter(is_active=True).order_by('name'),
    })



@login_required
def save_amendments(request, doc_id):
    """Handle save and add_note actions from the amendments form."""
    doc = get_object_or_404(Document, id=doc_id, status='SECOND_READING')

    # Role guard
    if not _user_has_role(request.user, 'SECRETARIAT', 'STAFF', 'ADMIN'):
        messages.error(request, "You don't have permission to make amendments.")
        # See the same fix/reasoning in floor_amendments above.
        return redirect('second-reading')

    if request.method != 'POST':
        # 'floor-amendments' isn't a registered URL name (only the
        # underscored 'floor_amendments' is, per documents/urls.py) — was
        # a guaranteed 500 on every non-POST hit here.
        return redirect('floor_amendments', doc_id=doc_id)

    action = request.POST.get('action')

    # Amendments themselves now save live to the docx via Casual Docs (see
    # floor_amendments.html) — this handler only tracks the informational
    # "In Progress / Finalized / No Amendments" label for the session.
    doc.amendment_status = request.POST.get('amendment_status', 'IN_PROGRESS')
    doc.save()

    # Handle the "Add Note" action
    if action == 'add_note':
        note_text = request.POST.get('amendment_note', '').strip()
        if note_text:
            AmendmentNote.objects.create(
                doc=doc,
                author=request.user,
                note=note_text
            )
            messages.success(request, "Amendment note added.")
        else:
            messages.warning(request, "Note cannot be empty.")

    elif action == 'save':
        messages.success(request, "Amendments saved successfully.")

    return redirect('floor_amendments', doc_id=doc_id)
######################___________third reading approval_______________######################
def _is_valid_pdf_upload(uploaded_file) -> bool:
    """Extension + magic-byte check — not foolproof, but catches a
    mislabeled or non-PDF file before it's stored as the official signed
    copy of an enacted measure."""
    if not uploaded_file.name.lower().endswith('.pdf'):
        return False
    header = uploaded_file.read(5)
    uploaded_file.seek(0)
    return header.startswith(b'%PDF-')


@login_required
def approve_measure(request, pk):

    if request.method == "POST" and request.user.role in ['SECRETARIAT', 'STAFF']:
        doc = get_object_or_404(Document, pk=pk)

        if doc.status == 'APPROVED':
            messages.warning(request, "This measure is already approved.")
            return redirect('third-reading')

        if doc.status != 'THIRD_READING':
            messages.error(request, f'"{doc.title}" has not reached Third Reading yet and cannot be approved.')
            return redirect('third-reading')

        signed_pdf = request.FILES.get('signed_pdf')
        if not signed_pdf:
            messages.error(request, "Upload the scanned, signed copy before giving Final Approval.")
            return redirect('third-reading')
        if not _is_valid_pdf_upload(signed_pdf):
            messages.error(request, "The signed copy must be a real PDF file.")
            return redirect('third-reading')

        current_year = timezone.now().year
        doc.signed_pdf = signed_pdf

        # Must run before doc.status changes — see _onlyoffice_checkpoint_sync's docstring.
        _onlyoffice_checkpoint_sync(doc)
        doc.status = 'APPROVED'

        with transaction.atomic():
            _acquire_sequence_lock('approved_reference_no', doc.doc_type, current_year)
            approved_count = Document.objects.filter(
                doc_type=doc.doc_type,
                status='APPROVED',
                updated_at__year=current_year
            ).count()
            next_number = approved_count + 1

            if doc.doc_type == 'ORDINANCE':
                doc.reference_no = f"Ordinance No. {next_number}"
            else:
                doc.reference_no = f"Resolution No. {next_number}"

            doc.save()

        # Outside the atomic block on purpose — a slow LibreOffice
        # conversion here shouldn't hold the reference-number advisory
        # lock any longer than it needs to be held.
        _onlyoffice_snapshot_pdf(request, doc)
        _onlyoffice_archive_docx(doc)

        log_action(request, action='MOVE', target=f'{doc.doc_type} — {doc.reference_no}',
                   detail=f'"{doc.title}" officially enacted.', severity='HIGH')
        messages.success(request, f"Measure officially enacted as {doc.reference_no}!")
        return redirect('third-reading')

    return redirect('third-reading')

@login_required
def fail_measure(request, pk):
    """If the body rejects the measure at Third Reading."""
    if request.method == "POST" and request.user.role in ['SECRETARIAT', 'STAFF']:
        doc = get_object_or_404(Document, pk=pk)

        if doc.status != 'THIRD_READING':
            messages.error(request, f'"{doc.title}" has not reached Third Reading yet.')
            return redirect('third_reading')

        # Must run before doc.status changes — see _onlyoffice_checkpoint_sync's docstring.
        _onlyoffice_checkpoint_sync(doc)
        doc.status = 'FAILED'
        doc.save()
        _onlyoffice_snapshot_pdf(request, doc)
        _onlyoffice_archive_docx(doc)
        log_action(request, action='MOVE', target=f'{doc.doc_type} — {doc.reference_no or doc.id}',
                   detail=f'"{doc.title}" marked Failed at Third Reading.', severity='HIGH')
        messages.error(request, f"{doc.title} was marked as Failed.")
    return redirect('third_reading')


@login_required
def update_document_councilors(request, doc_id):
    """
    Set which councilors participated in this measure — added during
    committee hearing (committee_level/amending_table.html) or
    second-reading floor deliberation (tracking/floor_amendments.html).
    Feeds Document.sponsor_councilors(): the "Sponsored by" byline and the
    signature list on the printed sheet ahead of Final Approval.
    """
    doc = get_object_or_404(Document, id=doc_id)
    if not _user_has_role(request.user, 'SECRETARIAT', 'STAFF', 'ADMIN'):
        messages.error(request, "You don't have permission to do this.")
        return redirect(request.META.get('HTTP_REFERER') or 'dashboard')

    if request.method == 'POST':
        councilor_ids = request.POST.getlist('councilor_ids')
        doc.participating_councilors.set(councilor_ids)
        log_action(request, action='UPDATE', target=f'{doc.doc_type} — {doc.reference_no or doc.id}',
                   detail='Participating councilors updated.')
        messages.success(request, "Participating councilors updated.")

    return redirect(request.META.get('HTTP_REFERER') or 'dashboard')


    #########___________for viewing stages_________############
@login_required
def view_unfinished(request):
    unfinished_docs = Document.objects.filter(status = 'UNFINISHED_BUSINESS')
    context = {
        'unfinished': unfinished_docs,
    }
    return render(request, 'documents/tracking/unfinished.html', context)

@login_required
def view_disapproved(request):
    disapproved_docs = Document.objects.filter(status = 'DISAPPROVED')
    available_years = Document.objects.filter(status='DISAPPROVED').dates('updated_at', 'year', order='DESC')
    context = {
        'disapproved': disapproved_docs,
        'available_years': available_years,
    }

    return render(request,'documents/tracking/disapproved.html',context)


@login_required
def other_matters(request):
    other_matters = BarangayFiles.objects.filter(status='OTHER_MATTERS')
    committees = Committee.objects.all()
    context ={
        'other_matters':other_matters,
        'committees':committees
    }
    return render(request, 'documents/tracking/other_matters.html', context)


@login_required
def first_reading(request):
    first_reading = Document.objects.filter(status ='FIRST_READING')
    committees = Committee.objects.all()
    for doc in first_reading:
        try:
            name = doc.author.councilor_profile.name
            parts = name.replace('"','').split()
            doc.surname = parts[-1]
        except:
            doc.surname = ""
    return render(request,'documents/tracking/first_reading.html',{'first_reading':first_reading, 'committees':committees})
@login_required
def second_reading(request):
    second_reading = Document.objects.filter(status ='SECOND_READING')
    for doc in second_reading:
        try:
            name = doc.author.councilor_profile.name
            parts = name.replace('"','').split()
            doc.surname = parts[-1]
        except:
            doc.surname = ""
    return render(request, 'documents/tracking/second_reading.html',{'second_reading':second_reading})
@login_required
def third_reading(request):
    third_reading  = Document.objects.filter(status = 'THIRD_READING')
    for doc in third_reading:
        try:
            name = doc.author.councilor_profile.name
            parts = name.replace('"','').split()
            doc.surname = parts[-1]
        except:
            doc.surname = ""
    return render(request, 'documents/tracking/third_reading.html', {'third_reading': third_reading})
from django.db.models import Q
from django.utils import timezone

@login_required
def approved_registry(request):
    from .models import LegacyDocument

    active_docs = list(Document.objects.filter(status='APPROVED').order_by('-updated_at'))
    legacy_docs = list(LegacyDocument.objects.all().order_by('-uploaded_at'))

    ordinances_total = (
        Document.objects.filter(status='APPROVED', doc_type='ORDINANCE').count() +
        LegacyDocument.objects.filter(doc_type='ORDINANCE').count()
    )
    resolutions_total = (
        Document.objects.filter(status='APPROVED', doc_type='RESOLUTION').count() +
        LegacyDocument.objects.filter(doc_type='RESOLUTION').count()
    )

    active_years  = set(Document.objects.filter(status='APPROVED').dates('updated_at', 'year', order='DESC').values_list('updated_at__year', flat=True))
    legacy_years  = set(LegacyDocument.objects.exclude(year__isnull=True).values_list('year', flat=True))
    available_years = sorted(active_years | legacy_years, reverse=True)

    return render(request, 'documents/tracking/approved.html', {
        'approved':         active_docs,
        'legacy_docs':      legacy_docs,
        'ordinances_count': ordinances_total,
        'resolutions_count': resolutions_total,
        'available_years':  available_years,
    })
    
################# HISTORY SYSTEM #############
def _can_view_document(user, doc):
    if doc.status not in ('DRAFT', 'GHOST'):
        return True
    return doc.author_id == user.id or user.role in ('SECRETARIAT', 'STAFF', 'ADMIN')


def _can_edit_document(user, doc):
    """Whether `user` may push edits into `doc` at its current status —
    i.e. whether documents/wopi.py's casualdocs_save/similarity_check
    should accept a write. Deliberately narrower than _can_view_document:
    being allowed to *see* a filed document (anyone logged in, once it's
    past DRAFT/GHOST — see _can_view_document above) doesn't mean being
    allowed to *edit* it.

    Mirrors exactly the set of pages that actually wire up a saveUrl for
    each status, so this also doubles as the status guard the editor
    endpoints never had: any status not listed here (FILED, FIRST_READING,
    THIRD_READING, APPROVED, ...) has no editing surface in the UI at all,
    and is rejected here too rather than silently trusting the frontend
    never to call save on it.
      - DRAFT/GHOST         -> draft.html / view_draft.html — author only
                                (create_draft's own POST handler already
                                scopes its `Document` lookup to
                                author=request.user; this matches that).
                                Barangay-originated documents never pass
                                through DRAFT/GHOST at all — they're
                                created directly at REFERRED (see
                                barangay/views.py's barangay_to_referral).
      - REFERRED/COMMITTEE* -> committee_level's amending_table.html
                                (status can be the dynamic 'COMMITTEE [n]'
                                label set by return_to_committee).
      - SECOND_READING      -> tracking/floor_amendments.html.
    Both of the latter two are secretariat-only amendment workbenches —
    no author-based access, since the document's original author isn't
    who's editing at those stages.
    """
    if doc.status in ('DRAFT', 'GHOST'):
        return doc.author_id == user.id
    if doc.status == 'REFERRED' or doc.status == 'SECOND_READING' or doc.status.startswith('COMMITTEE'):
        return user.role in ('SECRETARIAT', 'STAFF', 'ADMIN')
    return False


@login_required
def modal_document_viewer(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id)
    if not _can_view_document(request.user, doc):
        return HttpResponse('Not found.', status=404)

    try:
        # A GHOST/DRAFT document not yet filed has no reference_no at all
        # (None) — AttributeError from the .split() call, not IndexError/
        # ValueError, so it needs to be in this tuple too or viewing/
        # downloading a never-filed draft 500s instead of just falling
        # back to showing no reference number.
        parts = doc.reference_no.split("-")
        doc.ref_number = int(parts[1])
    except (AttributeError, IndexError, ValueError):
        doc.ref_number = doc.reference_no
    doc.has_onlyoffice_docx = os.path.exists(_onlyoffice_saved_docx_path(doc.id))
    doc.has_snapshot_pdf = os.path.exists(_onlyoffice_saved_pdf_path(doc.id, doc.current_version))
    from django.urls import reverse
    return render(request, 'documents/modal_document_viewer.html', {
        'doc': doc,
        'snapshot_pdf_url': reverse('document-snapshot-pdf', args=[doc.id]),
    })


@login_required
def modal_legacy_viewer(request, pk):
    from .models import LegacyDocument
    doc = get_object_or_404(LegacyDocument, pk=pk)
    return render(request, 'documents/modal_legacy_viewer.html', {'doc': doc})


@login_required
@xframe_options_exempt
def serve_legacy_pdf(request, pk):
    from .models import LegacyDocument
    from django.http import FileResponse, Http404
    doc = get_object_or_404(LegacyDocument, pk=pk)
    if not doc.pdf_file:
        raise Http404
    return FileResponse(doc.pdf_file.open('rb'), content_type='application/pdf')



@login_required
def document_history(request, pk):
    doc = get_object_or_404(Document, pk=pk)
    history = Archives.objects.filter(original_doc=doc).order_by('-version')
    
    return render(request, 'documents/tracking/history.html', {
        'doc': doc,
        'history': history
    })

@login_required
def view_document(request, doc_id):

    doc = get_object_or_404(Document, id=doc_id)
    if not _can_view_document(request.user, doc):
        raise Http404

    try:
        # A GHOST/DRAFT document not yet filed has no reference_no at all
        # (None) — AttributeError from the .split() call, not IndexError/
        # ValueError, so it needs to be in this tuple too or viewing/
        # downloading a never-filed draft 500s instead of just falling
        # back to showing no reference number.
        parts = doc.reference_no.split("-")
        doc.ref_number = int(parts[1])
    except (AttributeError, IndexError, ValueError):
        doc.ref_number = doc.reference_no
    doc.has_onlyoffice_docx = os.path.exists(_onlyoffice_saved_docx_path(doc.id))
    doc.has_snapshot_pdf = os.path.exists(_onlyoffice_saved_pdf_path(doc.id, doc.current_version))
    from django.urls import reverse
    return render(request, 'documents/tracking/document_view.html', {
        'doc': doc,
        'pdf_doc_id': doc.id,
        'snapshot_pdf_url': reverse('document-snapshot-pdf', args=[doc.id]),
    })


@login_required
def view_trail_version(request, doc_id):
    trail = get_object_or_404(Archives, id = doc_id)
    try:
        parts = trail.reference_no.split("-")
        trail.ref_number = int(parts[1])
    except (IndexError, ValueError):
        trail.ref_number = trail.reference_no
    # Archived trail versions are point-in-time snapshots — this version's
    # own PDF if one was taken at that checkpoint (see
    # _onlyoffice_snapshot_pdf), otherwise the stored-HTML rendering in
    # document_view.html. Never the live docx viewer — that always
    # reflects the document's *current* state, which would be wrong under
    # a historical version.
    trail.has_snapshot_pdf = os.path.exists(_onlyoffice_saved_pdf_path(trail.original_doc_id, trail.version))
    from django.urls import reverse
    return render(request, 'documents/tracking/document_view.html', {
        'doc': trail,
        'pdf_doc_id': trail.original_doc_id,
        'snapshot_pdf_url': reverse('archive-snapshot-pdf', args=[trail.id]),
    })

############## FOR DOWNLOADING #################################################
from django.http import HttpResponse

@login_required
def download_official_pdf(request, pk):
    doc = get_object_or_404(Document, pk=pk)

    if doc.status != 'APPROVED':
        messages.error(request, "Document not yet approved.")
        return redirect('approved_list')

    filename = f"{doc.reference_no.replace(' ', '_')}.pdf"

    try:
        pdf_bytes = _onlyoffice_convert_docx_to_pdf(request, doc.id)
    except Exception as e:
        logger.warning("PDF conversion failed for doc %s: %s", doc.id, e)
        pdf_bytes = None

    if pdf_bytes is not None:
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # Fallback: this document was never opened in an editor (legacy,
    # pre-migration data) — no .docx to convert, so reconstruct from the
    # stored HTML the same way this view always used to.
    html_string = render_to_string('documents/pdf/official_copy.html', {'doc': doc})
    
    result = BytesIO()
    
    # Generate PDF
    # We pass the string directly to pisa
    pisa_status = pisa.CreatePDF(html_string, dest=result)
    
    if pisa_status.err:
        return HttpResponse('We had some errors <pre>' + html_string + '</pre>')

    # Prepare Response
    response = HttpResponse(result.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    return response


def _pdf_page_layout(doc):
    """Computes the exact point-based geometry document_pdf.html's @page/
    @frame CSS needs from a document's Layout-tab settings (page_size,
    page_orientation, margin_*_cm).

    xhtml2pdf's own `@frame` at-rule with relative left/top/right/bottom
    properties resolves those against whatever `c.pageSize` happens to be
    *at CSS-parse time* (confirmed directly against the installed
    xhtml2pdf source and by rendering test PDFs) — since a `@frame` block
    has to be declared before the `@page` rule that actually triggers
    xhtml2pdf's frame-building step (see the comment in document_pdf.html),
    a landscape @page{size: ... landscape} coming *after* the frames would
    silently size them against the pre-swap portrait dimensions instead.
    Computing everything here as absolute points and emitting it via
    `-pdf-frame-box: left top width height` (xhtml2pdf's other, order-
    independent frame-geometry syntax) sidesteps that ordering trap
    entirely, and keeps a single @page rule instead of the two-rule
    workaround that was the only other way to force pageSize to update
    before the frames are parsed.

    The footer occupies a fixed 0.8cm band sitting directly above the
    user's own bottom margin (so margin_bottom_cm always stays true blank
    space beneath the footer, regardless of its size), matching the
    layout already verified working in document_pdf.html.
    """
    from reportlab.lib.pagesizes import A4, LETTER, LEGAL, landscape
    from reportlab.lib.units import cm as CM_PT

    base_size = {'A4': A4, 'LETTER': LETTER, 'LEGAL': LEGAL}.get(doc.page_size, A4)
    page_w, page_h = landscape(base_size) if doc.page_orientation == 'LANDSCAPE' else base_size

    margin_top = doc.margin_top_cm * CM_PT
    margin_bottom = doc.margin_bottom_cm * CM_PT
    margin_left = doc.margin_left_cm * CM_PT
    margin_right = doc.margin_right_cm * CM_PT
    footer_height = 0.8 * CM_PT

    content_width = page_w - margin_left - margin_right
    content_height = page_h - margin_top - margin_bottom - footer_height

    return {
        'page_width_pt': round(page_w, 2),
        'page_height_pt': round(page_h, 2),
        'content_frame_box': f"{margin_left:.2f}pt {margin_top:.2f}pt {content_width:.2f}pt {content_height:.2f}pt",
        'footer_frame_box': (
            f"{margin_left:.2f}pt {(margin_top + content_height):.2f}pt "
            f"{content_width:.2f}pt {footer_height:.2f}pt"
        ),
    }


@login_required
def download_document_pdf(request, doc_id):
    import os
    doc = get_object_or_404(Document, id=doc_id)

    # Draft reference numbers (e.g. "DO-002-2026") already end with the
    # year, since that's baked in when they're first assigned in
    # update_draft() — appending it again gave filenames like
    # "ORDINANCE-DO-002-2026-2026.pdf".
    ref = doc.reference_no or str(doc.id)
    year = str(doc.created_at.year)
    filename = f"{doc.doc_type}-{ref}.pdf" if ref.endswith(year) else f"{doc.doc_type}-{ref}-{year}.pdf"

    try:
        pdf_bytes = _onlyoffice_convert_docx_to_pdf(request, doc.id)
    except Exception as e:
        logger.warning("PDF conversion failed for doc %s: %s", doc.id, e)
        pdf_bytes = None

    if pdf_bytes is not None:
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # Fallback: this document was never opened in an editor (legacy,
    # pre-migration data) — no .docx to convert, so reconstruct from the
    # stored HTML the same way this view always used to.
    try:
        # A GHOST/DRAFT document not yet filed has no reference_no at all
        # (None) — AttributeError from the .split() call, not IndexError/
        # ValueError, so it needs to be in this tuple too or viewing/
        # downloading a never-filed draft 500s instead of just falling
        # back to showing no reference number.
        parts = doc.reference_no.split("-")
        doc.ref_number = int(parts[1])
    except (AttributeError, IndexError, ValueError):
        doc.ref_number = doc.reference_no
    # xhtml2pdf has no request/static-file context of its own, so the seal
    # is passed as an absolute filesystem path rather than a static URL.
    seal_path = os.path.join(settings.BASE_DIR, 'static', 'images', 'SanJuanCityCouncilLogo_HD.png')
    watermark_path = os.path.join(settings.BASE_DIR, 'static', 'images', 'draft_watermark.png')
    html_string = render_to_string('documents/document_pdf.html', {
        'doc': doc,
        'request': request,
        'seal_path': seal_path,
        'watermark_path': watermark_path,
        **_pdf_page_layout(doc),
    })
    html_string = re.sub(r'<p[^>]*>\s*<br\s*/?>\s*</p>', '', html_string)
    # Also collapse multiple consecutive empty lines
    html_string = re.sub(r'(\s*<br\s*/?>\s*){2,}', '<br>', html_string)
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    from xhtml2pdf import pisa
    pisa_status = pisa.CreatePDF(html_string, dest=response)

    if pisa_status.err:
        return HttpResponse('PDF generation failed', status=500)

    return response


@login_required
def download_document_docx(request, doc_id):
    """Converts the same rendered document_pdf.html output used for PDF
    download into a real .docx via headless LibreOffice, so a councilor
    gets an editable Word file rather than just a PDF. This is a local
    subprocess call our own server-side code shells out to — not a
    service anyone's browser talks to, so it doesn't carry the AGPL
    "network use" question OnlyOffice did, and LibreOffice's own license
    (MPL 2.0) places no restriction on headless/automated use or on files
    it merely outputs (verified directly against
    https://www.libreoffice.org/licenses/)."""
    from documents import libreoffice

    doc = get_object_or_404(Document, id=doc_id)

    ref = doc.reference_no or str(doc.id)
    year = str(doc.created_at.year)
    filename = f"{doc.doc_type}-{ref}.docx" if ref.endswith(year) else f"{doc.doc_type}-{ref}-{year}.docx"

    try:
        parts = doc.reference_no.split("-")
        doc.ref_number = int(parts[1])
    except (AttributeError, IndexError, ValueError):
        doc.ref_number = doc.reference_no

    seal_path = os.path.join(settings.BASE_DIR, 'static', 'images', 'SanJuanCityCouncilLogo_HD.png')
    watermark_path = os.path.join(settings.BASE_DIR, 'static', 'images', 'draft_watermark.png')
    html_string = render_to_string('documents/document_pdf.html', {
        'doc': doc,
        'request': request,
        'seal_path': seal_path,
        'watermark_path': watermark_path,
        **_pdf_page_layout(doc),
    })
    html_string = re.sub(r'<p[^>]*>\s*<br\s*/?>\s*</p>', '', html_string)
    html_string = re.sub(r'(\s*<br\s*/?>\s*){2,}', '<br>', html_string)

    try:
        docx_bytes = libreoffice.convert(
            html_string.encode("utf-8"), ".html", "docx", label=f"docx_{doc.id}"
        )
    except libreoffice.LibreOfficeUnavailable as e:
        return HttpResponse(f"DOCX export isn't available yet — {e} Try Download PDF instead for now.", status=503)
    except RuntimeError as e:
        logger.warning("LibreOffice DOCX conversion failed for doc %s: %s", doc.id, e)
        status = 504 if "timed out" in str(e) else 500
        return HttpResponse(str(e), status=status)

    response = HttpResponse(
        docx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from documents.models import Document

logger = logging.getLogger(__name__)


@login_required
@require_POST
def ai_legal_basis(request):
    """
    POST /documents/ai-legal-basis/
    Body (JSON): { "pk": <int> }

    Reads title + doc_type + content from the DB record (never trusts
    user-supplied strings), calls generate_legal_basis(), returns
    { "national_laws": [...] } or { "error": <str> }.

    On the create-draft page the ghost pk comes from the autosave response
    (docIdField.value). The JS layer ensures autosave has completed before
    this endpoint is called.
    """
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid request body."}, status=400)

    pk = body.get("pk")
    if not pk:
        return JsonResponse(
            {"error": "Draft not saved yet — please wait a moment and try again."},
            status=400,
        )

    document = get_object_or_404(Document, pk=pk)

    if document.author != request.user and not request.user.is_staff:
        return JsonResponse({"error": "Permission denied."}, status=403)

    logger.info("ai_legal_basis: pk=%s title=%r", pk, document.title)

    # document.content only reflects the live docx at status-transition
    # checkpoints now (see _onlyoffice_checkpoint_sync's docstring) — for a
    # brand-new, still-unfiled draft that's still ''/stale even after the
    # editor has real content in its saved .docx, silently degrading this
    # to a title-only search with no indication to the user. The frontend
    # already forces a docx save before calling this endpoint (see
    # draft.html's aiCheckBtn handler), so by the time we're here the
    # working .docx is current — this just needs to also land in
    # Document.content before generate_legal_basis reads it. Same
    # no-status-change call _onlyoffice_checkpoint_sync makes at every real
    # checkpoint; non-blocking on failure, same as those.
    _onlyoffice_checkpoint_sync(document)

    try:
        result = generate_legal_basis(
            title=document.title,
            doc_type=document.doc_type,
            content=document.content,
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.error("ai_legal_basis error pk=%s: %s", pk, exc)
        return JsonResponse({"error": str(exc)}, status=500)

    return JsonResponse({"national_laws": result})



####################################################################################

import json
import logging
from django.http import JsonResponse
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)


# Common legislative boilerplate words that appear in every ordinance —
# excluded from keyword overlap so they don't create false positives. Shared
# between ai_inline_check (live, per-keystroke) and
# _run_similarity_check_on_docx (post-save) so the two checks agree on what
# counts as a real match rather than silently drifting apart over time.
_SIMILARITY_STOP_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'is', 'it', 'be',
    'that', 'this', 'for', 'on', 'are', 'with', 'as', 'by', 'at', 'from',
    'not', 'but', 'its', 'may', 'shall', 'any', 'all', 'which', 'such',
    'city', 'san', 'juan', 'whereas', 'section', 'ordinance', 'resolution',
    'provided', 'hereby', 'hereof', 'thereof', 'therefore', 'now',
    'sangguniang', 'panlungsod', 'barangay', 'pursuant', 'under',
    'directly', 'indirectly', 'thereby', 'resulting', 'cause', 'effect',
    'duly', 'enacted', 'ordained', 'resolved', 'government', 'local',
}


def _similarity_keyword_overlap(query_text: str, snippet: str, min_shared: int = 3) -> bool:
    """Return True if query and snippet share at least min_shared meaningful words."""
    def keywords(text):
        return {
            w for w in re.sub(r'[^a-z\s]', '', text.lower()).split()
            if len(w) > 3 and w not in _SIMILARITY_STOP_WORDS
        }
    shared = keywords(query_text) & keywords(snippet)
    return len(shared) >= min_shared


@login_required
@require_POST
def ai_inline_check(request):
    """
    Batched inline intelligence check.

    Accepts:  POST JSON { "paragraphs": [{"id": "<uuid>", "text": "<str>"}, ...] }
    Returns:  JSON { "results": [{"id": "<uuid>", "matches": [...]}, ...] }
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from django.core.cache import cache
    import hashlib

    INLINE_TIMEOUT = 6

    def _retrieve_cached(para_id, text):
        cache_key = "rag_inline_" + hashlib.md5(text.encode()).hexdigest()
        cached = cache.get(cache_key)
        if cached is not None:
            return para_id, cached
        result = retrieve(text, top_k=3, timeout=INLINE_TIMEOUT)
        cache.set(cache_key, result, timeout=300)
        return para_id, result

    try:
        body       = json.loads(request.body)
        paragraphs = body.get("paragraphs", [])

        if not isinstance(paragraphs, list) or not paragraphs:
            return JsonResponse({"error": "No paragraphs provided."}, status=400)

        skip     = {item["id"]: [] for item in paragraphs if len((item.get("text") or "").strip()) < 20}
        to_check = [item for item in paragraphs if item["id"] not in skip]

        retrieved_map = {}
        if to_check:
            with ThreadPoolExecutor(max_workers=min(len(to_check), 6)) as pool:
                futures = {
                    pool.submit(_retrieve_cached, item["id"], item["text"].strip()): item["id"]
                    for item in to_check
                }
                for future in as_completed(futures):
                    try:
                        para_id, retrieved = future.result()
                        retrieved_map[para_id] = retrieved
                    except Exception as e:
                        # Ollama unreachable — treat as no matches, don't crash
                        logger.debug("[ai_inline_check] paragraph failed: %s", e)
                        retrieved_map[futures[future]] = []

        # Build results preserving input order
        results = []
        for item in paragraphs:
            para_id = item.get("id", "")
            if para_id in skip:
                results.append({"id": para_id, "matches": []})
                continue

            query_text = item.get("text", "")
            matches = [
                {
                    "title":        r["title"],
                    "reference_no": r["reference_no"],
                    "snippet":      r["snippet"],
                    "score":        round(r["score"], 4),
                    "source":       r["source"],
                    "chunk_type":   r.get("chunk_type", ""),
                    "doc_id":       r.get("doc_id"),
                }
                for r in retrieved_map.get(para_id, [])
                if r["score"] >= 0.78
                and _similarity_keyword_overlap(query_text, r["snippet"])
            ]
            results.append({"id": para_id, "matches": matches})

        return JsonResponse({"results": results})

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    except Exception as e:
        logger.exception("[ai_inline_check] Unexpected error: %s", e)
        return JsonResponse({"error": "Internal server error."}, status=500)

# Legacy (pre-system) document upload — moved to documents/legacy_upload.py
# (was a contiguous ~500-line section here under a "FOR UPLOADING OF
# LEGACY DOCUMENTS" banner comment; functionally self-contained, no
# overlap with the live document-workflow status transitions elsewhere in
# this file). Re-exported here so existing references (documents/urls.py's
# views.Upload_legacy etc., documents/tests.py's direct import of
# _page_ocr_confidence) keep working unchanged.
from .legacy_upload import (
    AI_REVIEW_CONFIDENCE_THRESHOLD,
    Upload_legacy,
    _page_ocr_confidence,
    extract_legacy_metadata,
    upload_legacy_document,
    validate_ocr_with_ai,
)

############################# DOCUMENT EDITOR (CASUAL DOCS) #############################
# Shared helpers used by both this file and documents/wopi.py for the
# document-editing integration — path conventions for the saved docx/PDF
# snapshot files on disk. The actual editor endpoints (casualdocs_document_
# bytes/casualdocs_save) live in documents/wopi.py; these are kept here
# because several are also used by plain download/PDF-export code
# elsewhere in this file that has nothing to do with the editor.


def _onlyoffice_saved_docx_path(doc_id):
    """Path to the real docx from this document's last editor save (see
    documents/wopi.py::casualdocs_save). Once one exists, it's what
    reopening the editor should hand back — not a reconstruction from the
    sanitized Document.content HTML, which is deliberately stripped of
    anything outside the sanitizer's tag set (no arbitrary <span>, no
    inline style attributes) and would silently lose color/highlighting a
    councilor left as a "where I stopped" marker across drafting
    sessions.

    Lives under settings.DOCUMENT_EDITOR_STORAGE_ROOT, deliberately NOT
    MEDIA_ROOT — see that setting's comment in config/settings.py for why
    (every read of this path already goes through a permission-checked
    Django view; a raw MEDIA_URL path never should)."""
    return os.path.join(settings.DOCUMENT_EDITOR_STORAGE_ROOT, f"doc_{doc_id}.docx")


def _onlyoffice_saved_pdf_path(doc_id, version):
    """Path to the PDF snapshot for one specific Archives version of a
    document (see Document.save() — every non-DRAFT/GHOST status change
    creates an Archives row stamped with Document.current_version at that
    moment). Keyed by (doc_id, version) so each archived version can carry
    its own exact-fidelity PDF, the same way it already carries its own
    HTML snapshot in Archives.content. See _onlyoffice_saved_docx_path
    above for why this lives under DOCUMENT_EDITOR_STORAGE_ROOT rather
    than MEDIA_ROOT."""
    return os.path.join(settings.DOCUMENT_EDITOR_STORAGE_ROOT, f"doc_{doc_id}_v{version}.pdf")


def _onlyoffice_saved_docx_version_path(doc_id, version):
    """Path to the archived .docx for one specific Archives version — the
    docx-file counterpart to _onlyoffice_saved_pdf_path above. Unlike
    _onlyoffice_saved_docx_path (the single, continuously-overwritten
    working copy every editing surface reads/writes), this is a frozen
    snapshot: once a stage is finalized, its docx stays retrievable as its
    own artifact instead of being silently overwritten by the next
    stage's edits. See _onlyoffice_saved_docx_path above for why this
    lives under DOCUMENT_EDITOR_STORAGE_ROOT rather than MEDIA_ROOT."""
    return os.path.join(settings.DOCUMENT_EDITOR_STORAGE_ROOT, f"doc_{doc_id}_v{version}.docx")


def _committee_report_docx_path(report_id):
    """Path to a CommitteeReport's own working .docx — separate from a
    Document's working docx (_onlyoffice_saved_docx_path) since a
    committee report is a different document entirely from the measure
    it's about, not a status of the same file. Keyed by CommitteeReport.id,
    not Document.id, since CommitteeReport isn't a Document."""
    return os.path.join(settings.DOCUMENT_EDITOR_STORAGE_ROOT, f"report_{report_id}.docx")


def _onlyoffice_checkpoint_sync(doc):
    """Refreshes Document.content from the live .docx — call this BEFORE
    mutating doc.status and calling doc.save() at a status-transition
    checkpoint (filing, committee/floor amendment finalize), so the
    Archives row that save() is about to create captures what's actually
    in the docx right now, not whatever the last autosave left behind.

    Editing itself (documents/wopi.py::casualdocs_save) no longer syncs
    content on every save — only here, at the checkpoints that actually
    get archived — so this is the only place Document.content still needs
    a docx->HTML conversion at all. Must run before doc.status changes:
    _sync_docx_to_document_content does its own partial save() first,
    and Document.save()'s version-bump/Archives-creation logic keys off
    whether doc.status differs from the DB's current value at save time —
    calling this after doc.status is already mutated in memory would make
    that intermediate save look like the real transition and archive a
    version prematurely, before doc.save() is actually called for real.

    Non-blocking, same reasoning as _onlyoffice_snapshot_pdf: a conversion
    hiccup here shouldn't stop secretariat staff from filing or advancing
    a document. On failure this just logs and leaves Document.content as
    whatever it already was."""
    docx_path = _onlyoffice_saved_docx_path(doc.id)
    if not os.path.exists(docx_path):
        return
    try:
        _sync_docx_to_document_content(doc)
    except Exception as e:
        logger.warning("Checkpoint content sync failed for doc %s: %s", doc.id, e)


def _onlyoffice_archive_docx(doc):
    """Copies the current working .docx to a version-stamped snapshot —
    the docx-file counterpart to _onlyoffice_snapshot_pdf. Call this AFTER
    doc.save() has bumped doc.current_version, so it archives under the
    version that was actually just created.

    Non-blocking, same reasoning as _onlyoffice_snapshot_pdf: on failure
    this just logs and leaves no archived docx for this version — the PDF
    snapshot still exists either way."""
    import shutil
    src = _onlyoffice_saved_docx_path(doc.id)
    if not os.path.exists(src):
        return
    dst = _onlyoffice_saved_docx_version_path(doc.id, doc.current_version)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    except Exception as e:
        logger.warning("Docx version archive failed for doc %s v%s: %s", doc.id, doc.current_version, e)


def _onlyoffice_snapshot_pdf(request, doc):
    """Converts doc's current saved .docx to PDF and stores it as the
    snapshot for doc.current_version — call this right after doc.save()
    at a checkpoint where content could have changed (filing, and the
    amendment-finalize points at Committee/Second Reading exit), so the
    Archives row just created has a matching exact-fidelity PDF.

    Deliberately non-blocking: a Document Server hiccup shouldn't stop a
    secretariat member from filing or advancing a document. On failure this
    just logs and leaves no snapshot for this version — viewers fall back
    to the live docx view (for the current version) or stored HTML (for
    older ones), same as before this feature existed."""
    try:
        pdf_bytes = _onlyoffice_convert_docx_to_pdf(request, doc.id)
    except Exception as e:
        logger.warning("PDF snapshot failed for doc %s v%s: %s", doc.id, doc.current_version, e)
        return False

    if pdf_bytes is None:
        return False

    snapshot_path = _onlyoffice_saved_pdf_path(doc.id, doc.current_version)
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    with open(snapshot_path, "wb") as f:
        f.write(pdf_bytes)
    return True


def _onlyoffice_convert_docx_to_pdf(request, doc_id):
    """Converts this document's saved .docx straight to PDF via a local
    LibreOffice conversion (see documents/libreoffice.py). Downloads
    should hand back exactly what the councilor authored (fonts, colors,
    tables), not a reconstruction from stripped HTML, so this is the
    primary path for download_document_pdf/download_official_pdf.
    Returns None if this document has no saved .docx yet (never opened in
    an editor) — callers fall back to the legacy xhtml2pdf template
    render. `request` is unused now (kept for call-site compatibility —
    an older remote-conversion flow needed it to build a fetchable URL;
    the local converter takes the file directly, no URL round trip).
    Raises documents.libreoffice.LibreOfficeUnavailable if LibreOffice
    isn't installed on this server — download_document_pdf/
    download_official_pdf already catch that (logged as a warning) and
    fall back to the legacy HTML render, same as any other conversion
    failure; _onlyoffice_snapshot_pdf's caller does the same and simply
    leaves that version without a snapshot."""
    docx_path = _onlyoffice_saved_docx_path(doc_id)
    if not os.path.exists(docx_path):
        return None

    from documents import libreoffice
    with open(docx_path, "rb") as f:
        docx_bytes = f.read()
    return libreoffice.convert(docx_bytes, ".docx", "pdf", label=f"pdf_{doc_id}")


@login_required
@xframe_options_exempt
def document_snapshot_pdf(request, doc_id):
    """Serves the PDF snapshot for a live document's *current* version —
    the primary way every passive viewer (incoming docs, first/second/third
    reading, approved, general document-view pages) shows a document now:
    a plain PDF is far lighter than mounting a full document editor just
    to look at something read-only. 404s if no snapshot exists yet for the
    current version (e.g. a DRAFT/GHOST that's never been filed, or a
    Document Server hiccup skipped the snapshot at the last checkpoint) —
    callers fall back to the live docx viewer, then stored HTML."""
    from django.http import FileResponse
    doc = get_object_or_404(Document, pk=doc_id)
    if not _can_view_document(request.user, doc):
        raise Http404

    path = _onlyoffice_saved_pdf_path(doc.id, doc.current_version)
    if not os.path.exists(path):
        raise Http404
    return FileResponse(open(path, "rb"), content_type="application/pdf")


_W14_PARAID_ATTR = "{http://schemas.microsoft.com/office/word/2010/wordml}paraId"


def document_original_html(request, doc_id):
    """Serves the current-version archived .docx as HTML, each paragraph
    tagged data-para-id with its Word w14:paraId. Feeds the read-only
    "original" panel in amending_table.html's side-by-side comparison —
    the live Casual Docs editor exposes the same paraId via
    ref.current.getSelection(), so a click there can look up the matching
    element here by data-para-id. 404s if no archived docx exists yet for
    the current version, same as document_snapshot_pdf.

    Walks docx.element.body's children directly, in actual document
    order, handling both <w:p> and <w:tbl> — not docx.paragraphs, which
    only returns top-level paragraphs and silently skips every table.
    Confirmed directly against a real committee-stage ordinance this was
    dropping two real tables for: the "Sponsored by" box (a real,
    directly-edited field baked into the docx itself, not the same as
    Document.sponsor_councilors() — that computed value can name someone
    different once a document has been through committee-level editing,
    confirmed live) and an actual legal-content table (a roles/offices
    table inside the ordinance body) that isn't just metadata at all —
    the previous paragraph-only extraction was silently losing real
    ordinance content, not just cosmetic."""
    doc = get_object_or_404(Document, pk=doc_id)
    if not _can_view_document(request.user, doc):
        raise Http404

    path = _onlyoffice_saved_docx_version_path(doc.id, doc.current_version)
    if not os.path.exists(path):
        raise Http404

    from django.utils.html import escape
    from docx import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    def render_run_html(run):
        # Walking the run's own XML children (not run.text) so a <w:br/>
        # inside a run becomes a real <br> instead of silently vanishing —
        # confirmed directly: the "Sponsored by" cell is one paragraph
        # with a line break before the name, not two paragraphs, so
        # run.text alone would jam them together as "Sponsored
        # by:Councilor ...".
        html_parts = []
        for child in run._r:
            tag = child.tag.split("}")[-1]
            if tag == "t":
                html_parts.append(escape(child.text or ""))
            elif tag == "br":
                html_parts.append("<br>")
            elif tag == "tab":
                html_parts.append("&emsp;")
        text = "".join(html_parts)
        if run.bold:
            text = f"<strong>{text}</strong>"
        if run.italic:
            text = f"<em>{text}</em>"
        if run.underline:
            text = f"<u>{text}</u>"
        return text

    def render_runs(para):
        # Preserves bold/italic/underline per run — paragraph.text alone
        # collapses to plain text, losing exactly the formatting (e.g. bold
        # "SECTION 1. TITLE.") that makes the original readable as a legal
        # document instead of a wall of undifferentiated text.
        return "".join(render_run_html(run) for run in para.runs)

    def pt_to_px(length, default=0):
        # Word stores paragraph spacing/indent in points/EMU — python-docx
        # already exposes these as Length objects with a .pt accessor, no
        # raw XML digging needed. A flat CSS margin on every paragraph (the
        # previous approach) collapses a tight list into the same rhythm as
        # a spaced-out section break; reading the document's own values
        # keeps that distinction.
        return round(length.pt * 96 / 72, 1) if length is not None else default

    def render_paragraph_html(para):
        if not para.text.strip():
            return None
        para_id = para._p.get(_W14_PARAID_ATTR)
        attr = f' data-para-id="{para_id}"' if para_id else ""
        align = "center" if para.alignment == 1 else "justify"  # WD_ALIGN_PARAGRAPH.CENTER == 1
        pf = para.paragraph_format
        indent = pt_to_px(pf.left_indent, default=0)
        # No explicit space_before/space_after exists at the paragraph or
        # style level in these documents (confirmed directly) — falling
        # back to Word's own document-wide defaults would be a much deeper
        # rabbit hole for a read-only preview panel. Indentation is
        # reliable data we do have: an indented paragraph is a list item,
        # which reads as a tighter-knit group than regular flowing text.
        default_after = 4 if indent > 0 else 14
        space_before = pt_to_px(pf.space_before, default=0)
        space_after = pt_to_px(pf.space_after, default=default_after)
        style = f"text-align:{align};margin:{space_before}px 0 {space_after}px {indent}px;"
        return f'<p{attr} style="{style}">{render_runs(para)}</p>'

    def render_table_html(table):
        # A plain, minimal table rendering — this is a read-only preview
        # panel, not a pixel-exact reconstruction (column widths/merged
        # cells aren't attempted). Cell paragraphs get the same
        # data-para-id treatment as body ones, so a click/highlight inside
        # a table cell (e.g. the Sponsored by box) works the same way.
        rows_html = []
        for row in table.rows:
            cells_html = []
            for cell in row.cells:
                cell_parts = [render_paragraph_html(p) for p in cell.paragraphs]
                cell_html = "".join(p for p in cell_parts if p)
                cells_html.append(
                    f'<td style="border:1px solid #cbd5e1;padding:6px 10px;'
                    f'vertical-align:top;">{cell_html}</td>'
                )
            rows_html.append(f"<tr>{''.join(cells_html)}</tr>")
        return f'<table style="width:100%;border-collapse:collapse;margin:10px 0;">{"".join(rows_html)}</table>'

    docx = DocxDocument(path)
    parts = []
    for child in docx.element.body:
        tag = child.tag.split("}")[-1]
        if tag == "p":
            html = render_paragraph_html(Paragraph(child, docx))
            if html:
                parts.append(html)
        elif tag == "tbl":
            parts.append(render_table_html(Table(child, docx)))
        # Anything else (sectPr, bookmarks, etc.) carries no visible
        # content for a read-only preview — same as before this rewrite.

    html = "\n".join(parts) if parts else '<p class="empty">This version has no text content.</p>'
    return HttpResponse(html)


@login_required
@xframe_options_exempt
def archive_snapshot_pdf(request, archive_id):
    """Serves the PDF snapshot for one specific historical Archives
    version — used by the trail/history viewer so a past version shows
    exactly how it looked at that stage, not the document's current state.
    404s if that version predates this feature (no snapshot was ever taken
    for it) — the trail viewer falls back to Archives.content (stored
    HTML) in that case."""
    from django.http import FileResponse
    trail = get_object_or_404(Archives, pk=archive_id)
    if not _can_view_document(request.user, trail.original_doc):
        raise Http404

    path = _onlyoffice_saved_pdf_path(trail.original_doc_id, trail.version)
    if not os.path.exists(path):
        raise Http404
    return FileResponse(open(path, "rb"), content_type="application/pdf")


def _sync_docx_to_document_content(doc):
    """Converts doc's saved .docx (already written to
    _onlyoffice_saved_docx_path(doc.id) by the caller) into HTML and writes
    it into Document.content — the shared second half of "the editor just
    saved a docx, now reflect that in the document record". Called by
    wopi.casualdocs_save right after it writes the pushed docx bytes to
    disk.

    Round-trips through a local LibreOffice conversion (see
    documents/libreoffice.py) — a single subprocess call, not a
    multi-request re-hosting dance. Document.save() re-sanitizes content
    down to the same restricted tag set the export gets mapped onto (see
    documents/sanitize.py), so anything the export doesn't map onto that
    set is dropped, not stored as-is.

    Raises on failure — casualdocs_save translates that into a 500.
    """
    from bs4 import BeautifulSoup
    from documents import libreoffice

    docx_path = _onlyoffice_saved_docx_path(doc.id)
    with open(docx_path, "rb") as f:
        docx_bytes = f.read()
    html_bytes = libreoffice.convert(docx_bytes, ".docx", "html", label=f"sync_{doc.id}")

    soup = BeautifulSoup(html_bytes, "html.parser")

    # LibreOffice's HTML export represents bold/italic as <b>/<i> (wrapped
    # in <span style="..."> that the sanitizer already strips harmlessly)
    # rather than <strong>/<em>. The sanitizer's allowlist only recognizes
    # strong/em, so <b>/<i> would otherwise be silently dropped — tag
    # removed, text kept, styling gone, no error. Renaming them here keeps
    # that formatting. Confirmed directly against real Collabora output
    # (which uses the same LibreOffice HTML export filter) — same
    # convention OnlyOffice's export used. Re-verify against LibreOffice's
    # own output once it's installed here, in case its HTML filter differs.
    for b_tag in soup.find_all("b"):
        b_tag.name = "strong"
    for i_tag in soup.find_all("i"):
        i_tag.name = "em"

    body_tag = soup.find("body")
    new_content = body_tag.decode_contents() if body_tag else str(soup)

    doc.content = new_content
    doc.save(update_fields=["content", "updated_at"])


def _run_similarity_check_on_docx(doc):
    """Shared core of the "inline check" feature's post-save replacement —
    called from wopi.similarity_check. Operates purely on doc's
    saved .docx file via python-docx; has no dependency on which editor
    produced it.

    Checks each paragraph of the freshly-saved content with the exact same
    matching logic ai_inline_check uses (same retrieve() call, same 0.78
    score floor, same _similarity_keyword_overlap filter), and writes any
    match as a real Word comment directly onto that paragraph in the saved
    .docx — visible the next time the document is reopened, not instantly,
    but a genuine native per-paragraph marker rather than a generic "N
    similar paragraphs" summary.

    Paragraphs already checked once (by content hash, in
    Document.flagged_similarity_hashes) are skipped on later calls — both
    to avoid piling up duplicate comments on the same unchanged paragraph
    (python-docx 1.2.0 can only add comments, not enumerate or remove
    existing ones — confirmed directly against the installed library, not
    assumed) and to avoid re-embedding unchanged text on every save.

    Returns the number of newly-flagged paragraphs. No-ops (returns 0) if
    doc has no saved .docx yet.
    """
    import hashlib
    from bs4 import BeautifulSoup

    docx_path = _onlyoffice_saved_docx_path(doc.id)
    if not os.path.exists(docx_path):
        return 0

    soup = BeautifulSoup(doc.content or "", "html.parser")
    already_flagged = set(doc.flagged_similarity_hashes or [])
    newly_checked_hashes = []
    to_flag = []  # [(paragraph_text, best_match), ...]

    for node in soup.find_all(["p", "li", "h1", "h2", "h3", "blockquote"]):
        text = node.get_text(strip=True)
        if len(text) < 20:
            continue
        text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
        if text_hash in already_flagged:
            continue

        try:
            # 20s, not ai_inline_check's 6s — that value is tuned for a
            # live, per-keystroke check where speed matters; this runs
            # once in the background after a save, so it can afford to
            # wait out a cold-start Ollama model rather than silently skip
            # the paragraph. Confirmed empirically: a cold model missed
            # this same 6s window during testing, with no user-visible
            # sign anything had failed.
            results = retrieve(text, top_k=3, timeout=20)
        except Exception as e:
            logger.warning("[similarity_check] retrieve failed for doc %s: %s", doc.id, e)
            continue

        newly_checked_hashes.append(text_hash)
        matches = [
            r for r in results
            if r["score"] >= 0.78 and _similarity_keyword_overlap(text, r["snippet"])
        ]
        if matches:
            to_flag.append((text, matches[0]))

    flagged_count = 0
    if to_flag:
        from docx import Document as DocxDocument

        def _norm(s):
            return " ".join(s.split())

        docx_doc = DocxDocument(docx_path)
        for text, match in to_flag:
            target = _norm(text)
            for para in docx_doc.paragraphs:
                if para.runs and _norm(para.text) == target:
                    source_label = "an existing legacy record" if match.get("source") == "legacy" else "an active document"
                    ref = f" ({match['reference_no']})" if match.get("reference_no") else ""
                    comment_text = (
                        f'Possible similarity to "{match["title"]}"{ref} — '
                        f'{int(match["score"] * 100)}% match, {source_label}.'
                    )
                    try:
                        docx_doc.add_comment(
                            runs=para.runs, text=comment_text,
                            author="LePMITS AI", initials="AI",
                        )
                        flagged_count += 1
                    except Exception as e:
                        logger.warning("[similarity_check] add_comment failed: %s", e)
                    break

        if flagged_count:
            docx_doc.save(docx_path)

    if newly_checked_hashes:
        doc.flagged_similarity_hashes = list(already_flagged | set(newly_checked_hashes))
        doc.save(update_fields=["flagged_similarity_hashes"])

    return flagged_count


