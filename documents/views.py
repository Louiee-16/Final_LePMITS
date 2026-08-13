from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.clickjacking import xframe_options_exempt
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
        draft.content  = request.POST.get('content', '')
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
        doc.content  = request.POST.get('content', '')
        doc.doc_type = request.POST.get('type', 'ORDINANCE')
 
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

    # Check if the draft was returned with a reason
    return_reason = None
    if ghost:
        from .models import ReturnReason
        return_reason = ReturnReason.objects.filter(document=ghost).first()

    return render(request, 'documents/draft.html', {
        'committees': committees,
        'ghost': ghost,
        'return_reason': return_reason,
    })
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
def move_to_first(request,pk):
    doc = get_object_or_404(Document, pk=pk)
    user = request.user
    if doc.status == 'FILED' and user.role == 'SECRETARIAT':
        ref_no = request.POST.get('reference_no')
        if ref_no: doc.reference_no = ref_no
        doc.status = 'FIRST_READING'
    doc.save()
    return redirect('incoming_docs')

@login_required
def move_to_other_matters(request, doc_id):
    doc = get_object_or_404(BarangayFiles, id= doc_id)
    user = request.user
    if doc.status == 'FILED' and user.role == 'SECRETARIAT':
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

    # Promote amended content → working content if amendments were made
    if doc.amended_content and doc.amendment_status != 'NO_AMENDMENTS':
        doc.content = doc.amended_content

    # Clean up Second Reading fields
    doc.amended_content = None
    doc.amendment_status = None

    # This triggers your save() override → version bump + Archives snapshot
    doc.status = 'THIRD_READING'
    doc.save()

    messages.success(request, f'"{doc.title}" has been moved to Third Reading.')
    return redirect('second-reading')

########## for autosaving #####################################

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
            doc.save()
        else:
            doc = Document.objects.filter(
                author=request.user,
                status='GHOST'
            ).first()

            if doc:
                doc.title = data.get('title', doc.title)
                doc.content = data.get('content', doc.content)
                doc.save()
            else:
                doc = Document.objects.create(
                    title=data.get('title', 'Untitled Draft'),
                    content=data.get('content', ''),
                    author=request.user,
                    status='GHOST'
                )

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
    if request.method == "POST" and request.user.role == 'SECRETARIAT' or request.user.role == 'STAFF':
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

    return render(request, 'documents/tracking/floor_amendments.html', {
        'doc': doc,
        'amendments': amendments,
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

    # Always save the editor content and status on every submit
    amended_content = request.POST.get('amended_content', '').strip()
    amendment_status = request.POST.get('amendment_status', 'IN_PROGRESS')

    if amended_content:
        doc.amended_content = amended_content
    doc.amendment_status = amendment_status
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
@login_required
def approve_measure(request, pk):

    if request.method == "POST" and request.user.role in ['SECRETARIAT', 'STAFF']:
        doc = get_object_or_404(Document, pk=pk)
        
        # Prevent double approval
        if doc.status == 'APPROVED':
            messages.warning(request, "This measure is already approved.")
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
        doc.status = 'FAILED' 
        doc.save()
        messages.error(request, f"{doc.title} was marked as Failed.")
    return redirect('third_reading')
    

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
@login_required
def modal_document_viewer(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id)

    try:
        parts = doc.reference_no.split("-")
        doc.ref_number = int(parts[1])
    except (IndexError, ValueError):
        doc.ref_number = doc.reference_no
    return render(request, 'documents/modal_document_viewer.html', {'doc': doc})


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
    try:
        parts = doc.reference_no.split("-")
        doc.ref_number = int(parts[1])
    except (IndexError, ValueError):
        doc.ref_number = doc.reference_no
    return render(request, 'documents/tracking/document_view.html', {'doc': doc})


@login_required
def view_trail_version(request, doc_id):
    trail = get_object_or_404(Archives, id = doc_id)
    try:
        parts = trail.reference_no.split("-")
        trail.ref_number = int(parts[1])
    except (IndexError, ValueError):
        trail.ref_number = trail.reference_no
    return render(request, 'documents/tracking/document_view.html', {'doc': trail})

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


    html_string = render_to_string('documents/pdf/official_copy.html', {'doc': doc})
    
    result = BytesIO()
    
    # Generate PDF
    # We pass the string directly to pisa
    pisa_status = pisa.CreatePDF(html_string, dest=result)
    
    if pisa_status.err:
        return HttpResponse('We had some errors <pre>' + html_string + '</pre>')

    # Prepare Response
    filename = f"{doc.reference_no.replace(' ', '_')}.pdf"
    response = HttpResponse(result.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    return response

import re

@login_required
def download_document_pdf(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id)
    try:
        parts = doc.reference_no.split("-")
        doc.ref_number = int(parts[1])
    except (IndexError, ValueError):
        doc.ref_number = doc.reference_no
    html_string = render_to_string('documents/document_pdf.html', {
        'doc': doc,
        'request': request,
    })
    html_string = re.sub(r'<p[^>]*>\s*<br\s*/?>\s*</p>', '', html_string)
    # Also collapse multiple consecutive empty lines
    html_string = re.sub(r'(\s*<br\s*/?>\s*){2,}', '<br>', html_string)
    response = HttpResponse(content_type='application/pdf')
    filename = f"{doc.doc_type}-{doc.reference_no or doc.id}-{doc.created_at.year}.pdf"
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

    Reads title + doc_type from the DB record (never trusts user-supplied strings),
    calls generate_legal_basis(), returns { "result": <str> } or { "error": <str> }.

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

    # Common legislative boilerplate words that appear in every ordinance —
    # excluded from keyword overlap so they don't create false positives.
    _STOP_WORDS = {
        'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'is', 'it', 'be',
        'that', 'this', 'for', 'on', 'are', 'with', 'as', 'by', 'at', 'from',
        'not', 'but', 'its', 'may', 'shall', 'any', 'all', 'which', 'such',
        'city', 'san', 'juan', 'whereas', 'section', 'ordinance', 'resolution',
        'provided', 'hereby', 'hereof', 'thereof', 'therefore', 'now',
        'sangguniang', 'panlungsod', 'barangay', 'pursuant', 'under',
        'directly', 'indirectly', 'thereby', 'resulting', 'cause', 'effect',
        'duly', 'enacted', 'ordained', 'resolved', 'government', 'local',
    }

    def _keyword_overlap(query_text: str, snippet: str, min_shared: int = 3) -> bool:
        """Return True if query and snippet share at least min_shared meaningful words."""
        def keywords(text):
            return {
                w for w in re.sub(r'[^a-z\s]', '', text.lower()).split()
                if len(w) > 3 and w not in _STOP_WORDS
            }
        shared = keywords(query_text) & keywords(snippet)
        return len(shared) >= min_shared

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
                and _keyword_overlap(query_text, r["snippet"])
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