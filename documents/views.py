from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
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
        status='GHOST'
    ).order_by('-updated_at').first()

    return render(request, 'documents/draft.html', {
        'committees': committees,
        'ghost': ghost,
    })
############# MOVING FUNCTIONS$#########################
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
    approved = Document.objects.filter(status='APPROVED').order_by('-updated_at')
    

    ordinances_total = Document.objects.filter(status='APPROVED', doc_type='ORDINANCE').count()
    resolutions_total = Document.objects.filter(status='APPROVED', doc_type='RESOLUTION').count()


    available_years = Document.objects.filter(status='DISAPPROVED').dates('updated_at', 'year', order='DESC')

    return render(request, 'documents/tracking/approved.html', {
        'approved': approved,
        'ordinances_count': ordinances_total,
        'resolutions_count': resolutions_total,
        'available_years': available_years,

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