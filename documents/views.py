from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, Http404, HttpResponse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_POST
import json
from django.db.models import Q
from .models import Document, AmendmentNote
from committees.models import Committee
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from django.contrib import messages
from archives.models import Archives
from barangay.models import BarangayFiles
from secretariat.models import Session
from audit.utils import log_action


@login_required
def create_referred_draft(request, doc_id):

    doc = get_object_or_404(BarangayFiles, id=doc_id)
    
    existing_draft = Document.objects.filter(
        author=request.user,
        source_barangay_doc=doc,
        status__in=['DRAFT', 'GHOST']
    ).first()

    committees = Committee.objects.all()

    if request.method == 'POST':
        action = request.POST.get('action')
        doc_id_field = request.POST.get('doc_id', '').strip()

        if doc_id_field:
            draft = get_object_or_404(Document, id=doc_id_field, author=request.user)
        else:
            draft = Document(author=request.user, source_barangay_doc=doc)

        draft.title    = request.POST.get('title', '').strip() or 'Untitled Draft'
        # content is intentionally left untouched — saved directly to the DB
        # by the OnlyOffice callback (see onlyoffice_callback), same as
        # create_draft.
        draft.doc_type = 'RESOLUTION'


        committee_id = request.POST.get('referred_committee')
        draft.referred_committee_id = committee_id if committee_id else None
        if action =='save':
            draft.status   = 'DRAFT'
            draft.save()
            return redirect('referral-drafting-page', doc_id=doc.id)
            
        elif action == 'submit':
            draft.status = 'REFERRED'
            doc.status = 'DRAFT_CREATED'
            
            if not draft.reference_no:
                current_year = timezone.now().year
                prefix = 'DR'
                count = Document.objects.filter(
                    doc_type=draft.doc_type,
                    status__in=['FILED', 'FIRST_READING', 'COMMITTEE', 'SECOND_READING', 'THIRD_READING', 'APPROVED'],
                    created_at__year=current_year
                ).count()
                draft.reference_no = f"{prefix}-{count + 1:03d}-{current_year}"
            doc.save()
            draft.save()

            # Same checkpoint as create_draft's File Draft — first PDF
            # snapshot, so the committee stage this feeds into can show a
            # PDF instead of a live editor for read-only views.
            _onlyoffice_snapshot_pdf(request, draft)

            return redirect('referrals-from-other-matters')

            

    return render(request, 'documents/referral_drafting_page.html', {
        'doc': doc,
        'existing_draft': existing_draft,
        'committees': committees,
    })


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
        # draft.html's own TipTap editor submits a `content` field directly
        # (sanitized on save() anyway, see Document.save()). view_draft.html's
        # edit branch still runs on OnlyOffice for now and posts to this same
        # view without a `content` field — its content is saved separately by
        # onlyoffice_callback, so the .get() fallback leaves it untouched
        # rather than blanking it out.
        if 'content' in request.POST:
            doc.content = request.POST.get('content', '')
        doc.doc_type = request.POST.get('type', 'ORDINANCE')

        # Layout tab (Page Setup) — same "only touch it if the form
        # actually sent it" reasoning as `content` above, since
        # view_draft.html's OnlyOffice branch posts to this same view
        # without these fields either. Falls back to whatever's already on
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
            doc.status = 'FILED'
 
            # Assign reference number only once
            if not doc.reference_no:
                current_year = timezone.now().year
                prefix = 'DO' if doc.doc_type == 'ORDINANCE' else 'DR'
 
                count = Document.objects.filter(
                    doc_type=doc.doc_type,
                    status__in=[
                        'FILED', 'FIRST_READING', 'COMMITTEE',
                        'SECOND_READING', 'THIRD_READING', 'APPROVED'
                    ],
                    created_at__year=current_year
                ).count()
 
                doc.reference_no = f"{prefix}-{count + 1:03d}-{current_year}"
 
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

        log_action(
            request,
            action='FILE',
            target=f'{doc.doc_type} — {doc.reference_no}',
            detail=f'"{doc.title}" filed by {request.user.username}'
        )
        return redirect('dashboard')
 
    committees = Committee.objects.all()
    ghost = Document.objects.filter(
        author=request.user,
        status__in=['GHOST', 'DRAFT']
    ).order_by('-updated_at').first()

    # OnlyOffice needs a real Document id to attach its source/callback
    # endpoints to from the very first keystroke — unlike Quill, there's no
    # JS-driven autosave to create that row lazily. So a brand-new visit
    # (no existing ghost/draft) gets one created up front here instead.
    # is_new_ghost tells the template not to show the "unsaved work
    # recovered" banner for a placeholder that was just silently created,
    # as opposed to genuine pre-existing in-progress work.
    is_new_ghost = not ghost
    if is_new_ghost:
        ghost = Document.objects.create(
            author=request.user,
            title='Untitled Draft',
            content='',
            status='GHOST',
        )

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
        'onlyoffice_server_url': settings.ONLYOFFICE_SERVER_URL,
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
    if request.method != 'POST' or request.user.role not in ['SECRETARIAT', 'STAFF']:
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
        committee_check = Archives.objects.filter(original_doc = doc_id, status__icontains= 'COMMITTEE')
        if committee_check:
            x = committee_check.count()
            doc.status = f'COMMITTEE [{x+1}]'
        else:
            doc.status = 'COMMITTEE'
    doc.save()
    return redirect('second-reading')


@login_required
def move_to_disapproved(request, doc_id):
    third_reading_doc = get_object_or_404(Document, id = doc_id)
    user = request.user
    if third_reading_doc.status == 'THIRD_READING' and user.role in ['SECRETARIAT','STAFF']:
        third_reading_doc.status = 'DISAPPROVED'
        third_reading_doc.save()
    return redirect('third-reading')

@login_required
def move_to_first(request, pk):
    doc = get_object_or_404(Document, pk=pk)

    if request.method != 'POST' or doc.status != 'FILED' or request.user.role != 'SECRETARIAT':
        messages.error(request, "You don't have permission to perform this action.")
        return redirect('incoming_docs')

    ref_no = request.POST.get('reference_no')
    if ref_no:
        doc.reference_no = ref_no
    doc.status = 'FIRST_READING'
    doc.save()
    return redirect('incoming_docs')

@login_required
def move_to_other_matters(request, doc_id):
    doc = get_object_or_404(BarangayFiles, id=doc_id)

    if request.method != 'POST' or doc.status != 'FILED' or request.user.role != 'SECRETARIAT':
        messages.error(request, "You don't have permission to perform this action.")
        return redirect('incoming_docs')

    doc.status = 'OTHER_MATTERS'
    doc.save()
    return redirect('incoming_docs')

@login_required
def unfinished_to_third(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, status = 'UNFINISHED_BUSINESS')
    if request.user.role not in ['SECRETARIAT', 'STAFF']:
        messages.error(request, "You don't have permission to perform this action.")
        return redirect('unfinished-business')
    if request.method != 'POST':
        return redirect('unfinished-business')
    doc.status = 'THIRD_READING'
    doc.save()
    return redirect('unfinished-business')

@login_required
def move_to_third_reading(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, status='SECOND_READING')

    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to perform this action.")
        return redirect('second_reading')

    if request.method != 'POST':
        return redirect('second_reading')

    # Apply reference number if provided
    ref_no = request.POST.get('reference_no', '').strip()
    if ref_no:
        doc.reference_no = ref_no

    # Amendments during floor deliberation are now made live, directly in
    # the .docx via OnlyOffice (see floor_amendments.html) — doc.content is
    # already current by the time this runs, no amended_content to
    # promote. amendment_status stays purely informational (the "In
    # Progress / Finalized / No Amendments" label for this floor session)
    # and is just cleared on exit, same housekeeping as before.
    doc.amended_content = None
    doc.amendment_status = None

    # This triggers your save() override → version bump + Archives snapshot
    doc.status = 'THIRD_READING'
    doc.save()

    # New checkpoint's PDF, keyed to the version Archives just snapshotted.
    _onlyoffice_snapshot_pdf(request, doc)

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
        Document.objects.filter(
            author=request.user,
            status='GHOST'
        ).delete()
    return JsonResponse({'status': 'discarded'})



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
        
        doc.status = 'REFERRED'
        doc.referred_committee = committee
        doc.save() 
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


    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to make amendments.")
        return redirect('second_reading')

    amendments = doc.amendment_notes.select_related('author').all()
    from councilors.models import Councilor

    return render(request, 'documents/tracking/floor_amendments.html', {
        'doc': doc,
        'amendments': amendments,
        'onlyoffice_server_url': settings.ONLYOFFICE_SERVER_URL,
        'all_councilors': Councilor.objects.filter(is_active=True).order_by('name'),
    })



@login_required
def save_amendments(request, doc_id):
    """Handle save and add_note actions from the amendments form."""
    doc = get_object_or_404(Document, id=doc_id, status='SECOND_READING')

    # Role guard
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to make amendments.")
        return redirect('second_reading')

    if request.method != 'POST':
        return redirect('floor-amendments', doc_id=doc_id)

    action = request.POST.get('action')

    # Amendments themselves now save live to the docx via OnlyOffice (see
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

        doc.signed_pdf = signed_pdf
        doc.status = 'APPROVED'

        doc.save()

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

        doc.status = 'FAILED'
        doc.save()
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
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to do this.")
        return redirect(request.META.get('HTTP_REFERER') or 'dashboard')

    if request.method == 'POST':
        councilor_ids = request.POST.getlist('councilor_ids')
        doc.participating_councilors.set(councilor_ids)
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
        'onlyoffice_server_url': settings.ONLYOFFICE_SERVER_URL,
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
        'onlyoffice_server_url': settings.ONLYOFFICE_SERVER_URL,
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
    # document_view.html. Never the live OnlyOffice docx viewer — that
    # always reflects the document's *current* state, which would be wrong
    # under a historical version.
    trail.has_snapshot_pdf = os.path.exists(_onlyoffice_saved_pdf_path(trail.original_doc_id, trail.version))
    from django.urls import reverse
    return render(request, 'documents/tracking/document_view.html', {
        'doc': trail,
        'pdf_doc_id': trail.original_doc_id,
        'snapshot_pdf_url': reverse('archive-snapshot-pdf', args=[trail.id]),
    })

############## FOR DOWNLOADING #################################################
from django.http import HttpResponse
from django.template.loader import get_template
from xhtml2pdf import pisa
from io import BytesIO

from django.template.loader import render_to_string # Use this instead of get_template for more stability

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
        logger.warning("OnlyOffice PDF conversion failed for doc %s: %s", doc.id, e)
        pdf_bytes = None

    if pdf_bytes is not None:
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # Fallback: this document was never opened in OnlyOffice (legacy,
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

import re


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
        logger.warning("OnlyOffice PDF conversion failed for doc %s: %s", doc.id, e)
        pdf_bytes = None

    if pdf_bytes is not None:
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # Fallback: this document was never opened in OnlyOffice (legacy,
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



import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from documents.models import Document
from documents.rag.generator import generate_legal_basis

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
from django.views.decorators.csrf import csrf_exempt
from .rag.embedder import embed_text
from .rag.retriever import retrieve

logger = logging.getLogger(__name__)


# Common legislative boilerplate words that appear in every ordinance —
# excluded from keyword overlap so they don't create false positives. Shared
# between ai_inline_check (live, per-keystroke) and onlyoffice_similarity_check
# (post-save, OnlyOffice-only) so the two checks agree on what counts as a
# real match rather than silently drifting apart over time.
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


@csrf_exempt
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

@login_required
def Upload_legacy(request):
    return render(request,'documents/upload_legacy.html')


@login_required
def legacy_document_detail(request, pk):
    from .models import LegacyDocument
    doc = get_object_or_404(LegacyDocument, pk=pk)
    return render(request, 'documents/legacy_document_detail.html', {'doc': doc})



import os
import pytesseract
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect

from .models import LegacyDocument, DocumentChunk
from documents.rag.embedder import embed_document_chunks


@login_required
def upload_legacy_document(request):
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

        legacy_doc = LegacyDocument.objects.create(
            title          = title,
            reference_no   = reference_no,
            doc_type       = doc_type,
            year           = year,
            pdf_file       = pdf_file,
            extracted_text = validated_text,
            ocr_processed  = bool(validated_text),
        )

        # ── Chunk + embed immediately ──────────────────────────────
        if validated_text:
            try:
                chunk_records = embed_document_chunks(legacy_doc, source_type="legacy_document")
                if chunk_records:
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


##################### FOR UPLOADING OF LEGACY DOCUMENTS ##############################

import re
import pdfplumber
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required

@login_required
@require_POST
def extract_legacy_metadata(request):
    """
    POST /documents/extract-legacy-metadata/
    Accepts a PDF file, reads first 2 pages, extracts
    title, reference_no, year, doc_type using regex.
    Returns JSON with extracted fields.
    """
    pdf_file = request.FILES.get('pdf_file')
    if not pdf_file:
        return JsonResponse({'error': 'No file provided.'}, status=400)

    try:
        with pdfplumber.open(pdf_file) as pdf:
            total_pages = len(pdf.pages)
            all_pages_text = []   # all pages — for display
            meta_pages_text = []  # first 2 pages — for metadata extraction

            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                if text.strip():
                    all_pages_text.append(text)
                    if i < 2:
                        meta_pages_text.append(text)
                else:
                    # fallback to Tesseract OCR for image-only pages
                    try:
                        import pytesseract
                        pil_image = page.to_image(resolution=200).original.convert("RGB")
                        ocr_text = pytesseract.image_to_string(pil_image, lang="eng")
                        if ocr_text.strip():
                            all_pages_text.append(ocr_text)
                            if i < 2:
                                meta_pages_text.append(ocr_text)
                    except Exception as ocr_err:
                        logger.warning("Tesseract OCR failed for page %d: %s", i + 1, ocr_err)

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

        refnumber = refnumber_match.group(1) if refnumber_match else "0"
        refnumber = int(refnumber)

        # format number with leading zeros
        if refnumber > 99:
            refnumber = str(refnumber)
        elif refnumber > 9:
            refnumber = f"0{refnumber}"
        else:
            refnumber = f"00{refnumber}"

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
        # Title is "AN ORDINANCE/RESOLUTION..." up to "Sponsored by"
        title_pattern = re.search(
            r'Series\s+of\s+\d{4}\s*(.*?)\s*Sponsored',
            full_text,
            re.IGNORECASE | re.DOTALL
        )
        title = ""
        if title_pattern:
            title = re.sub(r'\s+', ' ', title_pattern.group(1)).strip()
            # Remove trailing punctuation
            title = title.rstrip('.,;')

        return JsonResponse({
            'title': title,
            'reference_no': reference_no,
            'year': year,
            'doc_type': doc_type,
            'full_text': display_text,
            'total_pages': total_pages,
            'word_count': len(display_text.split()),
        })
    
    except Exception as e:
        logger.error("extract_legacy_metadata error: %s", e, exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def validate_ocr_with_ai(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    # Accept pre-extracted text directly — no PDF scan needed
    raw_text = request.POST.get('raw_text', '').strip()

    if not raw_text:
        return JsonResponse({'error': 'No text provided.'}, status=400)

    import requests as req

    endpoint  = getattr(settings, 'OLLAMA_ENDPOINT',  'http://localhost:11434/api/generate')
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
        print(ocr_model)
        cleaned_text = re.sub(r'<think>.*?</think>', '', cleaned_text, flags=re.DOTALL).strip()
        logger.info("AI cleanup done: %d chars %s", len(cleaned_text))

    except Exception as e:
        logger.warning("AI cleanup failed (%s) — returning raw text.", e)
        cleaned_text = raw_text

    return JsonResponse({'cleaned_text': cleaned_text})


############################# ONLYOFFICE EDITOR TRIAL #############################
# Trial integration with a self-hosted OnlyOffice Document Server (run via
# Docker, see project notes) — evaluating it as a possible replacement for
# the Quill editor used in draft.html. This is intentionally additive and
# read-only with respect to the rest of the app: it never writes to
# Document.content, so the existing Quill drafting flow is untouched no
# matter what happens here.
#
# Docker networking note: the Document Server runs in its own container,
# not on the host, so from *its* point of view "localhost" means the
# container itself, not this machine. Docker Desktop's special DNS name
# host.docker.internal is what actually resolves back to this host — that's
# why the source/callback URLs below are built differently from a normal
# request.build_absolute_uri() call.
def _onlyoffice_host_url(request, path):
    host_header = request.get_host()
    port = host_header.split(':')[1] if ':' in host_header else ('443' if request.is_secure() else '80')
    scheme = 'https' if request.is_secure() else 'http'
    return f"{scheme}://host.docker.internal:{port}{path}"


def _onlyoffice_sign(payload):
    """Signs an outbound request body for ConvertService/CommandService —
    confirmed empirically (2026-08-25) that both accept a "token" field
    whose JWT payload is the request body itself, keyed with our shared
    secret. Returns the payload dict with "token" added, unchanged if JWT
    is off."""
    if not settings.ONLYOFFICE_JWT_SECRET:
        return payload
    import jwt
    signed = dict(payload)
    signed["token"] = jwt.encode(payload, settings.ONLYOFFICE_JWT_SECRET, algorithm="HS256")
    return signed


def _onlyoffice_verify_inbound(request):
    """Verifies a request claiming to be from the Document Server — used
    on every endpoint the container calls (source, pending-docx, callback).
    Per ONLYOFFICE's documented JWT scheme (confirmed via their docs,
    2026-08-25): outbound requests carry `Authorization: Bearer <token>`,
    where the JWT's claims are `{"payload": {...the actual request data...}}`
    — for a GET fetch that's `{"payload": {"url": ...}}`, for the callback
    it's `{"payload": <callback body>}`.

    Returns True if verification passes OR if ONLYOFFICE_JWT_SECRET isn't
    configured (JWT off — matches the same opt-in pattern already used for
    signing the editor config). Returns False otherwise, including when
    JWT is on but no/invalid token was presented — the caller should
    reject the request in that case rather than silently trust it, since
    that's the entire point of turning JWT on.
    """
    if not settings.ONLYOFFICE_JWT_SECRET:
        return True

    import jwt

    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else None
    if not token and request.method == "POST":
        try:
            body = json.loads(request.body.decode("utf-8"))
            token = body.get("token")
        except (ValueError, UnicodeDecodeError):
            token = None

    if not token:
        return False

    try:
        jwt.decode(token, settings.ONLYOFFICE_JWT_SECRET, algorithms=["HS256"])
        return True
    except jwt.InvalidTokenError:
        return False


def _onlyoffice_saved_docx_path(doc_id):
    """Path to the real docx from this document's last OnlyOffice save (see
    onlyoffice_callback). Once one exists, it's what reopening the editor
    should hand back to OnlyOffice — not a reconstruction from the
    sanitized Document.content HTML, which is deliberately stripped of
    anything outside Quill's original tag set (no <span>, no style
    attributes) and would silently lose color/highlighting a councilor
    left as a "where I stopped" marker across drafting sessions."""
    return os.path.join(settings.MEDIA_ROOT, "onlyoffice_pending", f"doc_{doc_id}.docx")


def _onlyoffice_saved_pdf_path(doc_id, version):
    """Path to the PDF snapshot for one specific Archives version of a
    document (see Document.save() — every non-DRAFT/GHOST status change
    creates an Archives row stamped with Document.current_version at that
    moment). Keyed by (doc_id, version) so each archived version can carry
    its own exact-fidelity PDF, the same way it already carries its own
    HTML snapshot in Archives.content."""
    return os.path.join(settings.MEDIA_ROOT, "onlyoffice_pending", f"doc_{doc_id}_v{version}.pdf")


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
    """Converts this document's saved .docx straight to PDF via the
    Document Server's ConvertService — same round trip onlyoffice_callback
    uses for docx->html, just a different outputtype. Downloads should hand
    back exactly what the councilor authored (fonts, colors, tables), not a
    reconstruction from stripped HTML, so this is the primary path for
    download_document_pdf/download_official_pdf. Returns None if this
    document has no saved .docx yet (never opened in OnlyOffice) — callers
    fall back to the legacy xhtml2pdf template render."""
    if not os.path.exists(_onlyoffice_saved_docx_path(doc_id)):
        return None

    import time
    import requests as req

    pending_url = _onlyoffice_host_url(request, f"/onlyoffice/{doc_id}/pending-docx/")
    convert_resp = req.post(
        f"{settings.ONLYOFFICE_SERVER_URL}/ConvertService.ashx",
        json=_onlyoffice_sign({
            "async": False,
            "filetype": "docx",
            "key": f"pdf{doc_id}-{int(time.time())}",
            "outputtype": "pdf",
            "title": "export.docx",
            "url": pending_url,
        }),
        headers={"Accept": "application/json"},
        timeout=60,
    )
    convert_resp.raise_for_status()
    convert_data = convert_resp.json()
    pdf_url = convert_data.get("fileUrl") or convert_data.get("FileUrl")
    if not pdf_url:
        raise ValueError(f"ConvertService returned no fileUrl: {convert_data}")

    pdf_resp = req.get(pdf_url, timeout=30)
    pdf_resp.raise_for_status()
    return pdf_resp.content


def _build_onlyoffice_editor_config(request, doc, track_changes=False):
    from django.urls import reverse

    document_key = f"doc{doc.id}-{int(doc.updated_at.timestamp())}"
    callback_url = _onlyoffice_host_url(request, reverse('onlyoffice-callback', args=[doc.id]))

    if os.path.exists(_onlyoffice_saved_docx_path(doc.id)):
        # Full-fidelity reopen: hand back the actual docx from the last
        # save, so nothing outside the HTML sanitizer's allowlist (color,
        # highlighting, etc.) gets lost between editing sessions.
        file_type = "docx"
        source_url = _onlyoffice_host_url(request, reverse('onlyoffice-pending-docx', args=[doc.id]))
        title = f"{doc.title or 'Untitled'}.docx"
    else:
        # No saved docx yet (never opened in OnlyOffice before) — fall
        # back to converting the stored HTML, same as the original trial.
        file_type = "html"
        source_url = _onlyoffice_host_url(request, reverse('onlyoffice-document-source', args=[doc.id]))
        title = f"{doc.title or 'Untitled'}.html"

    editor_config = {
        "document": {
            "fileType": file_type,
            "key": document_key,
            "title": title,
            "url": source_url,
        },
        "documentType": "word",
        "editorConfig": {
            "callbackUrl": callback_url,
            "user": {
                "id": str(request.user.id),
                "name": request.user.get_full_name() or request.user.username,
            },
            "customization": {
                "forcesave": True,
                # Explicit even though it's already the Document Server's
                # own default — saves automatically shortly after the user
                # pauses typing (governed server-side by the Document
                # Server's savetimeoutdelay, ~5s by default), as the
                # replacement for Quill's fixed-interval autosave.
                "autosave": True,
            },
        },
    }

    if track_changes:
        # Used only by the Committee/Second-Reading amendment pages — not
        # initial drafting, where there's no prior version to track against.
        # document.permissions.review (+ edit) turns reviewing on for this
        # user; customization.review.trackChanges forces it ON by default
        # rather than leaving it as something the user has to switch on via
        # the Review tab themselves. Confirmed via ONLYOFFICE's own docs
        # (api.onlyoffice.com/docs/docs-api/get-started/how-it-works/reviewing)
        # — a core docx feature, not gated to a paid edition.
        editor_config["document"]["permissions"] = {"edit": True, "review": True}
        editor_config["editorConfig"]["customization"]["review"] = {"trackChanges": True}

    if settings.ONLYOFFICE_JWT_SECRET:
        import jwt
        editor_config["token"] = jwt.encode(
            editor_config, settings.ONLYOFFICE_JWT_SECRET, algorithm="HS256"
        )

    return editor_config


@login_required
def onlyoffice_view_config(request, doc_id):
    """Read-only counterpart to _build_onlyoffice_editor_config, used by the
    document viewer templates at every legislative stage (secretariat
    incoming, first/second/third reading, approved, etc.) so a filed
    document is shown exactly as the councilor authored it — same fonts,
    colors, tables, everything the sanitized Document.content HTML can't
    carry. Returns 404 if this document was never opened in OnlyOffice (no
    saved .docx yet, e.g. legacy pre-migration data); callers fall back to
    rendering the stored HTML in that case."""
    doc = get_object_or_404(Document, pk=doc_id)
    if not os.path.exists(_onlyoffice_saved_docx_path(doc.id)):
        return JsonResponse({"error": "no_docx"}, status=404)

    from django.urls import reverse

    document_key = f"view-doc{doc.id}-{int(doc.updated_at.timestamp())}"
    source_url = _onlyoffice_host_url(request, reverse('onlyoffice-pending-docx', args=[doc.id]))

    editor_config = {
        "document": {
            "fileType": "docx",
            "key": document_key,
            "title": f"{doc.title or 'Untitled'}.docx",
            "url": source_url,
            "permissions": {"edit": False, "comment": False, "download": True, "print": True},
        },
        "documentType": "word",
        "editorConfig": {
            "mode": "view",
            "user": {
                "id": str(request.user.id),
                "name": request.user.get_full_name() or request.user.username,
            },
            "customization": {"chat": False},
        },
    }

    if settings.ONLYOFFICE_JWT_SECRET:
        import jwt
        editor_config["token"] = jwt.encode(
            editor_config, settings.ONLYOFFICE_JWT_SECRET, algorithm="HS256"
        )

    editor_config["onlyofficeServerUrl"] = settings.ONLYOFFICE_SERVER_URL
    return JsonResponse(editor_config)


@login_required
def document_snapshot_pdf(request, doc_id):
    """Serves the PDF snapshot for a live document's *current* version —
    the primary way every passive viewer (incoming docs, first/second/third
    reading, approved, general document-view pages) shows a document now:
    a plain PDF is far lighter than mounting a full OnlyOffice viewer just
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


@login_required
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


@login_required
def onlyoffice_editor_test(request, doc_id):
    doc = get_object_or_404(Document, pk=doc_id)
    editor_config = _build_onlyoffice_editor_config(request, doc)

    return render(request, "documents/onlyoffice_test.html", {
        "doc": doc,
        "onlyoffice_server_url": settings.ONLYOFFICE_SERVER_URL,
        "editor_config_json": json.dumps(editor_config),
    })


@login_required
def onlyoffice_editor_config(request, doc_id):
    """JSON config endpoint used by the inline editor embedded directly in
    draft.html, as opposed to the standalone onlyoffice_editor_test page."""
    doc = get_object_or_404(Document, pk=doc_id)
    editor_config = _build_onlyoffice_editor_config(request, doc)
    editor_config["onlyofficeServerUrl"] = settings.ONLYOFFICE_SERVER_URL
    return JsonResponse(editor_config)


@login_required
def onlyoffice_amend_config(request, doc_id):
    """Same live-editing config as onlyoffice_editor_config, but with Track
    Changes forced on — used only by the Committee/Second-Reading "Insert
    Amendments" pages (amending_table.html, floor_amendments.html), so
    edits made there show up as redlined insertions/deletions in the docx
    itself instead of a live side-by-side diff view (which isn't possible
    with OnlyOffice's iframe editor — it doesn't expose cursor/selection
    events to the host page the way Quill did)."""
    doc = get_object_or_404(Document, pk=doc_id)
    editor_config = _build_onlyoffice_editor_config(request, doc, track_changes=True)
    editor_config["onlyofficeServerUrl"] = settings.ONLYOFFICE_SERVER_URL
    return JsonResponse(editor_config)


def onlyoffice_document_source(request, doc_id):
    """Fetched directly by the Document Server container (not the browser),
    so it deliberately doesn't require a Django login — the container has
    no session/cookies to send. Instead, when ONLYOFFICE_JWT_SECRET is set,
    it requires the container's own Authorization: Bearer JWT proving the
    request really came from the Document Server — see
    _onlyoffice_verify_inbound."""
    if not _onlyoffice_verify_inbound(request):
        return HttpResponse(status=403)
    doc = get_object_or_404(Document, pk=doc_id)
    html = f"<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>{doc.content or ''}</body></html>"
    return HttpResponse(html, content_type="text/html; charset=utf-8")


def onlyoffice_pending_docx(request, doc_id):
    """Serves the most recently OnlyOffice-edited copy of a document. Two
    callers: the Document Server container fetches this mid-callback to
    convert the file to HTML for Document.content (see onlyoffice_callback),
    and _build_onlyoffice_editor_config points the editor here directly on
    reopen, once a saved copy exists, for full-fidelity round-tripping.
    Fetched by the container either way, not the browser — no login;
    JWT-verified instead when ONLYOFFICE_JWT_SECRET is set, same as
    onlyoffice_document_source."""
    if not _onlyoffice_verify_inbound(request):
        return HttpResponse(status=403)
    path = _onlyoffice_saved_docx_path(doc_id)
    if not os.path.exists(path):
        raise Http404
    with open(path, "rb") as f:
        data = f.read()
    return HttpResponse(
        data,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@login_required
def onlyoffice_forcesave(request, doc_id):
    """Called by the drafting pages right before Save Draft/File Draft
    navigates away, so an edit the user just made isn't lost.

    Why this exists: OnlyOffice's own autosave is idle-triggered (fires a
    few seconds after typing stops — see ONLYOFFICE Integration notes). If
    a user types something and immediately clicks Save Draft, the page can
    navigate away before that autosave ever fires, silently losing the
    edit even though the Save Draft form POST itself succeeds (it only
    ever touches title/type/committee, never content). This command tells
    the Document Server to save *right now* regardless of idle state, via
    its CommandService API, then blocks briefly until the resulting
    callback (see onlyoffice_callback) has actually landed, so the caller
    knows it's safe to navigate.
    """
    if request.method != 'POST':
        return JsonResponse({"error": 1})

    doc = get_object_or_404(Document, pk=doc_id)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": 1})

    document_key = body.get("key")
    if not document_key:
        return JsonResponse({"error": 1})

    import time
    import requests as req

    before_updated_at = doc.updated_at

    try:
        cmd_resp = req.post(
            f"{settings.ONLYOFFICE_SERVER_URL}/coauthoring/CommandService.ashx",
            json=_onlyoffice_sign({"c": "forcesave", "key": document_key}),
            timeout=10,
        )
        cmd_data = cmd_resp.json()
    except Exception as e:
        logger.warning("OnlyOffice forcesave command failed: %s", e)
        return JsonResponse({"error": 1})

    # error 4 = "no changes to save" (document server docs) — the editor
    # had nothing unsaved, which is a success case, not a failure.
    if cmd_data.get("error") not in (0, 4):
        logger.warning("OnlyOffice forcesave command returned %s", cmd_data)
        return JsonResponse({"error": 1, "detail": cmd_data})
    if cmd_data.get("error") == 4:
        return JsonResponse({"error": 0, "no_changes": True})

    # Poll briefly for onlyoffice_callback to have actually landed and
    # updated the row, rather than trusting the command response alone —
    # the callback is a separate, Document-Server-initiated request that
    # can arrive slightly after this command call returns.
    for _ in range(25):  # ~5s ceiling
        time.sleep(0.2)
        doc.refresh_from_db(fields=["updated_at"])
        if doc.updated_at != before_updated_at:
            return JsonResponse({"error": 0})

    logger.warning("OnlyOffice forcesave: callback did not land within timeout for doc %s", doc_id)
    return JsonResponse({"error": 0, "timeout": True})


@csrf_exempt
def onlyoffice_callback(request, doc_id):
    """Receives OnlyOffice's save notifications and writes the edit back to
    the real Document.content — this is the one place in the trial that
    actually mutates live data. Status codes that matter: 2 = ready for
    saving (editor closed), 6 = force-saved while still open.

    OnlyOffice only ever hands us the edited file as a .docx, so getting it
    into Document.content (plain HTML, same shape Quill produces) takes an
    extra round trip: download the .docx, re-host it at a URL the container
    can reach, ask the Document Server to convert *that* to HTML, then pull
    the result back. Document.save() already sanitizes content down to the
    same restricted tag set Quill output uses (see documents/sanitize.py),
    so whatever formatting OnlyOffice's HTML export doesn't map onto that
    set is dropped, not stored as-is.
    """
    if request.method != 'POST':
        return JsonResponse({"error": 1})

    if not _onlyoffice_verify_inbound(request):
        return JsonResponse({"error": 1})

    doc = get_object_or_404(Document, pk=doc_id)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": 1})

    status = body.get("status")
    if status in (2, 6) and body.get("url"):
        import time
        import requests as req
        from bs4 import BeautifulSoup

        try:
            docx_resp = req.get(body["url"], timeout=30)
            docx_resp.raise_for_status()

            pending_path = _onlyoffice_saved_docx_path(doc_id)
            os.makedirs(os.path.dirname(pending_path), exist_ok=True)
            with open(pending_path, "wb") as f:
                f.write(docx_resp.content)

            pending_url = _onlyoffice_host_url(
                request, f"/onlyoffice/{doc_id}/pending-docx/"
            )
            convert_resp = req.post(
                f"{settings.ONLYOFFICE_SERVER_URL}/ConvertService.ashx",
                json=_onlyoffice_sign({
                    "async": False,
                    "filetype": "docx",
                    "key": f"save{doc_id}-{int(time.time())}",
                    "outputtype": "html",
                    "title": "edited.docx",
                    "url": pending_url,
                }),
                headers={"Accept": "application/json"},
                timeout=60,
            )
            convert_resp.raise_for_status()
            convert_data = convert_resp.json()
            html_url = convert_data.get("fileUrl") or convert_data.get("FileUrl")
            if not html_url:
                raise ValueError(f"ConvertService returned no fileUrl: {convert_data}")

            html_resp = req.get(html_url, timeout=30)
            html_resp.raise_for_status()

            soup = BeautifulSoup(html_resp.content, "html.parser")

            # OnlyOffice's HTML export represents bold/italic as <b>/<i>
            # (wrapped in <span style="..."> that the sanitizer already
            # strips harmlessly) rather than Quill's <strong>/<em>. The
            # sanitizer's allowlist only recognizes strong/em, so <b>/<i>
            # were being silently dropped — tag removed, text kept, styling
            # gone, no error. Renaming them here keeps that formatting.
            for b_tag in soup.find_all("b"):
                b_tag.name = "strong"
            for i_tag in soup.find_all("i"):
                i_tag.name = "em"

            body_tag = soup.find("body")
            new_content = body_tag.decode_contents() if body_tag else str(soup)

            doc.content = new_content
            doc.save(update_fields=["content", "updated_at"])
            logger.info("OnlyOffice trial: saved edits back into Document %s content", doc_id)
        except Exception as e:
            logger.warning("OnlyOffice trial: save-back failed (%s)", e)
            return JsonResponse({"error": 1})

    return JsonResponse({"error": 0})


@login_required
@require_POST
def onlyoffice_similarity_check(request, doc_id):
    """Runs right after the frontend detects a save has just landed (see
    config.events.onDocumentStateChange in draft.html/view_draft.html/
    referral_drafting_page.html — fires with event.data === false once
    OnlyOffice reports no unsaved changes remain).

    This is the "inline check" feature's OnlyOffice-era replacement. The
    original version highlighted a matching paragraph live, inline, as you
    typed — not reproducible here, since OnlyOffice's canvas is an opaque
    iframe, not a DOM we can reach into or highlight. Instead: each
    paragraph of the freshly-saved content is checked with the exact same
    matching logic ai_inline_check uses (same retrieve() call, same 0.78
    score floor, same _similarity_keyword_overlap filter), and any match
    gets written as a real Word comment directly onto that paragraph in
    the saved .docx — visible the next time the document is reopened, not
    instantly, but a genuine native per-paragraph marker rather than a
    generic "N similar paragraphs" summary.

    Paragraphs already checked once (by content hash, in
    Document.flagged_similarity_hashes) are skipped on later calls — both
    to avoid piling up duplicate comments on the same unchanged paragraph
    (python-docx 1.2.0 can only add comments, not enumerate or remove
    existing ones — confirmed directly against the installed library, not
    assumed) and to avoid re-embedding unchanged text on every save.
    """
    import hashlib
    from bs4 import BeautifulSoup

    doc = get_object_or_404(Document, pk=doc_id)

    docx_path = _onlyoffice_saved_docx_path(doc_id)
    if not os.path.exists(docx_path):
        return JsonResponse({"error": 0, "new_flags": 0})

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
            logger.warning("[onlyoffice_similarity_check] retrieve failed for doc %s: %s", doc_id, e)
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
                        logger.warning("[onlyoffice_similarity_check] add_comment failed: %s", e)
                    break

        if flagged_count:
            docx_doc.save(docx_path)

    if newly_checked_hashes:
        doc.flagged_similarity_hashes = list(already_flagged | set(newly_checked_hashes))
        doc.save(update_fields=["flagged_similarity_hashes"])

    return JsonResponse({"error": 0, "new_flags": flagged_count})