from .forms import CouncilorForm
from django.shortcuts import render,redirect, get_object_or_404
from .models import Councilor
from committees.models import Committee
from accounts.models import User
from barangay.models import BarangayFiles
from documents.models import Document
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from documents.urls import urlpatterns
from django.utils import timezone
from django.db.models import Q
from documents.models import Document
from datetime import datetime, timedelta
from django.views.decorators.clickjacking import xframe_options_sameorigin, xframe_options_exempt
from django.http import FileResponse, HttpResponse, Http404
from django.utils.crypto import get_random_string
from django.conf import settings

################## DRAFT CREATION
def referral_drafting_page(request, doc_id):
    doc = get_object_or_404(BarangayFiles, id=doc_id,status = 'REFERRED')
    existing_draft = Document.objects.filter(
            author=request.user,
            source_barangay_doc=doc,
            status__in=['DRAFT', 'GHOST']
        ).first()

    # OnlyOffice needs a real Document id to attach to from the very first
    # keystroke, same reasoning as create_draft's auto-create — see
    # documents/views.py.
    if not existing_draft:
        existing_draft = Document.objects.create(
            author=request.user,
            source_barangay_doc=doc,
            title='Untitled Draft',
            content='',
            doc_type='RESOLUTION',
            status='GHOST',
        )

    context = {
        'doc':doc,
        'existing_draft': existing_draft,
        'committees': Committee.objects.all(),
        'onlyoffice_server_url': settings.ONLYOFFICE_SERVER_URL,
    }
    return render(request,'documents/referral_drafting_page.html',context)


################### FOR VIEWING
def referrals_from_other_matters(request):
    councilor = Councilor.objects.get(user=request.user)
    committees = councilor.chaired_committees.all()
    referral_from_otherMatters = BarangayFiles.objects.filter(referred_committee__in = committees, status = 'REFERRED')

    context={
        'referrals':referral_from_otherMatters,

    }

    return render(request,'councilors/referrals_from_other_matters.html',context)


def referred_to_committee(request, committee_id):
    councilor = request.user
    committees = get_object_or_404(Committee, id = committee_id )
    referred_docs = Document.objects.filter( Q(status='REFERRED') | Q(status__icontains='COMMITTEE'))
    context = {
        'referred_docs' : referred_docs
    }
    return render(request, 'councilors/referred_to_committee.html', context)


def councilor_dashboard(request):
    now = datetime.now()
    days_ahead = 0 - now.weekday() 
    if days_ahead <= 0:
        days_ahead += 7

    next_monday = now + timedelta(days=days_ahead)

    session_datetime = next_monday.replace(hour=9, minute=30, second=0, microsecond=0)
    councilor = request.user.councilor_profile
    committees = councilor.chaired_committees.all()
    draft_measures = Document.objects.filter(status = 'DRAFT')
    referred_from_otherMatters = BarangayFiles.objects.filter(referred_committee__in = committees, status = 'REFERRED')
    referred_to_my_committees = Document.objects.filter(referred_committee__in = committees)
    context={
        'committees': committees,
        'draft_measures':draft_measures,
        'referred_other_matters':referred_from_otherMatters,
        'referred_to_my_committees': referred_to_my_committees,

        "month": session_datetime.strftime("%B"),
        "day": session_datetime.strftime("%d"),
        "weekday": session_datetime.strftime("%A"),
        "time": session_datetime.strftime("%I:%M %p"),
        "session_datetime": session_datetime,

    }
    return render(request, 'dashboards/councilor.html', context)
def add_councilor(request):
    if request.method == "POST":
        form = CouncilorForm(request.POST, request.FILES)
        if form.is_valid():
            temp_password = get_random_string(length=12)
            user = User.objects.create_user(
                role = 'COUNCILOR',
                username=form.cleaned_data['email'],
                email=form.cleaned_data['email'],
                password=temp_password
            )

            councilor = form.save(commit=False)
            councilor.user = user
            councilor.save()

            messages.warning(
                request,
                f"Account for {user.username} created. Temporary password: {temp_password} "
                "— provide it to the councilor securely; they should change it after first login."
            )
            return redirect("councilors_list")
    else:
        form = CouncilorForm()
    return render(request, 'councilors/add_councilor.html',{'form':form})

def councilors(request):
    councilor = Councilor.objects.filter(is_active=True)
    return render(request,'councilors/councilor_list.html', {'councilor':councilor})

def editCouncilor(request, councilor_id):
    councilor = get_object_or_404(Councilor,id=councilor_id)

    if request.method == "POST":
        form = CouncilorForm(request.POST, request.FILES, instance=councilor)
        if form.is_valid():
            form.save()
            return redirect('councilors_list')
    else:
        form = CouncilorForm(instance=councilor)

    return render(request, 'councilors/edit_councilor.html',{'form':form, 'councilor':councilor})

def end_term(request, councilor_id):
    councilor = get_object_or_404(Councilor, id=councilor_id)
    councilor.is_active = False
    councilor.save()
    return redirect('councilors_list')

def filed_measures(request):
    filed_measures = request.user.my_docs.filter(status = 'FILED')
    context = {
        'filed_measures' : filed_measures,
        'author' : request.user.councilor_profile
    }
    return render(request, 'councilors/filed_measures.html', context)
def draft_measures(request):
    from documents.models import ReturnReason
    drafts = request.user.my_docs.filter(status='DRAFT').order_by('-updated_at')

    # Attach the latest return reason to each draft (if any)
    for draft in drafts:
        draft.return_info = ReturnReason.objects.filter(document=draft).first()

    context = {
        'draft_measures': drafts,
        'author': request.user.councilor_profile,
    }
    return render(request, 'councilors/draft_measures.html', context)

def delete_draft(request, id):
    draft = get_object_or_404(Document, id = id)
    
    if draft.status == 'DRAFT':
        if draft.author == request.user:
            draft.delete()
    return redirect('draft-measures')
        


@login_required
def view_draft(request, id):
    draft = get_object_or_404(Document, id=id)

    is_owner = draft.author_id == request.user.id
    can_view_any = request.user.role in ('SECRETARIAT', 'STAFF', 'ADMIN')
    if not (is_owner or can_view_any):
        messages.error(request, "You don't have permission to view this draft.")
        return redirect('draft-measures')

    import os
    from django.urls import reverse
    from documents.views import _onlyoffice_saved_docx_path, _onlyoffice_saved_pdf_path
    draft.has_onlyoffice_docx = os.path.exists(_onlyoffice_saved_docx_path(draft.id))
    draft.has_snapshot_pdf = os.path.exists(_onlyoffice_saved_pdf_path(draft.id, draft.current_version))

    context ={
        'draft':draft,
        'snapshot_pdf_url': reverse('document-snapshot-pdf', args=[draft.id]),
        'onlyoffice_server_url': settings.ONLYOFFICE_SERVER_URL,
    }
    return render(request, 'councilors/view_draft.html', context)
@login_required
def draft_detail(request, doc_id):
    """View or edit a draft depending on its status."""
    draft = get_object_or_404(Document, id=doc_id, author=request.user)
    committees = Committee.objects.all()
    return render(request, 'documents/draft_detail.html', {
        'draft': draft,
        'committees': committees,
    })


@login_required
def update_draft(request, doc_id):
    """Handle saves and filing from the draft detail edit form."""
    doc = get_object_or_404(Document, id=doc_id, author=request.user)

    # Hard block — filed docs cannot be edited via POST
    if doc.status != 'DRAFT':
        messages.error(request, "This document has already been filed and cannot be edited.")
        return redirect('draft_detail', doc_id=doc_id)

    if request.method != 'POST':
        return redirect('draft_detail', doc_id=doc_id)

    action = request.POST.get('action')

    doc.title    = request.POST.get('title', '').strip() or doc.title
    doc.content  = request.POST.get('content', '')
    doc.doc_type = request.POST.get('type', doc.doc_type)

    committee_id = request.POST.get('referred_committee')
    doc.referred_committee_id = committee_id if committee_id else None

    if action == 'submit':
        doc.status = 'FILED'

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

        messages.success(request, f'"{doc.title}" has been officially filed.')
    else:
        messages.success(request, "Draft saved.")

    doc.save()
    return redirect('draft_detail', doc_id=doc.id)


    ############## for document handling
from django.http import FileResponse
import os

@xframe_options_exempt
def serve_pdf(request, barangay_doc_id):
    doc = get_object_or_404(BarangayFiles, id=barangay_doc_id)

    return FileResponse(
        open(doc.scanned_pdf.path, 'rb'),
        as_attachment=False,
        content_type='application/pdf'
    )

