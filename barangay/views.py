import os

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse
from django.views.decorators.clickjacking import xframe_options_exempt
from .models import Barangay, BarangayFiles
from accounts.models import User
from .forms import BarangayForm, MeasureUploadForm
from django.http import JsonResponse
from django.db.models import Q
from committees.models import Committee
from django.core.mail import send_mail
from django.conf import settings
from django.utils.crypto import get_random_string
from audit.utils import log_action
from documents.models import Document
from documents.views import (
    _onlyoffice_saved_docx_path,
    _onlyoffice_checkpoint_sync,
    _onlyoffice_snapshot_pdf,
    _onlyoffice_archive_docx,
)

@login_required
def barangay_dashboard(request):
    form = MeasureUploadForm()
    context = {
        'form': form,
        'barangays': Barangay.objects.all(),
    }
    return render(request, 'dashboards/barangay.html', context)

@login_required
def upload_measure(request):
    if request.method == "POST":
        form = MeasureUploadForm(request.POST, request.FILES)
        if form.is_valid():

            barangay = form.save(commit=False)
            barangay.origin_barangay = request.user.barangay
            barangay.reference_no = request.POST.get('title')
            barangay.status = 'FILED'
            barangay.save()
            log_action(request, action='FILE', target=barangay.title or f'Barangay measure #{barangay.id}',
                       detail=f'Filed by {request.user.username}.')
            return redirect('barangay-dashboard')
    else:
        form = MeasureUploadForm() 

    return render(request, 'dashboards/barangay.html', {'form': form})

@login_required
def barangay_list(request):
    """
    Main Registry View: Lists all Barangays in the system.
    """
    barangays = Barangay.objects.all().order_by('barangay_name')
    return render(request, 'barangay/barangay_list.html', {'barangays': barangays})

@login_required
def add_barangay(request):
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to add barangay accounts.")
        return redirect('barangay-list')

    if request.method == "POST":
        form = BarangayForm(request.POST)
        if form.is_valid():
            temp_password = get_random_string(length=12)
            user = User.objects.create_user(
                role = 'BARANGAY',
                username = form.cleaned_data['email'],
                email = form.cleaned_data['email'],
                password = temp_password
            )
            barangay = form.save(commit=False)
            barangay.user = user
            barangay.save()
            log_action(request, action='CREATE', target=f"Created barangay account for {user.username}")
            messages.warning(
                request,
                f"Account for {user.username} created. Temporary password: {temp_password} "
                "— provide it to the barangay contact securely; they should change it after first login."
            )
            return redirect("barangay-list")
    else:
        form = BarangayForm()
    return render(request,'barangay/add_barangay.html', {'form':form})


def checker(request):
    barangay_name = request.GET.get('barangay_name', '').strip()
    email = request.GET.get('email', '').strip()

    errors = []

    if barangay_name and Barangay.objects.filter(barangay_name=barangay_name).exists():
        errors.append('barangay_name')

    if email and Barangay.objects.filter(user__email=email).exists():
        errors.append('email')

    return JsonResponse({'errors': errors})


@login_required
def barangay_to_referral(request, doc_id):
    if request.method == "POST" and request.user.role in ('SECRETARIAT', 'STAFF'):
        doc = get_object_or_404(BarangayFiles, id=doc_id, status='OTHER_MATTERS')
        committee_id = request.POST.get('referred_committee')

        if not committee_id:
            messages.error(request, "Please select a target committee.")
            return redirect('other-matters')

        committee = get_object_or_404(Committee, id=committee_id)

        if not doc.uploaded_docx:
            messages.error(request, "This barangay measure has no uploaded file to refer.")
            return redirect('other-matters')

        # A barangay measure arrives as an already-complete draft, not a
        # request for a councilor to write from scratch — so referring it
        # to committee creates the real Document immediately, with the
        # barangay's own .docx as its working file. Committee level is
        # supervision (review, request changes) on the real thing, not a
        # separate manual drafting stage first — that stage (previously
        # "Pending Referrals") is gone; see docs/activity-log.md.
        # No reference number here — barangay-originated documents skip
        # First Reading, so they're numbered later, at committee
        # approval (see committee_level/views.py's
        # _sync_report_status_from_hearing).
        chair_user = committee.chairman.user if committee.chairman_id else None
        new_document = Document.objects.create(
            author=chair_user or request.user,
            source_barangay_doc=doc,
            title=doc.title or 'Untitled',
            content='',
            doc_type='RESOLUTION',
            status='GHOST',
            referred_committee=committee,
        )
        working_docx_path = _onlyoffice_saved_docx_path(new_document.id)
        os.makedirs(os.path.dirname(working_docx_path), exist_ok=True)
        with open(doc.uploaded_docx.path, 'rb') as uploaded:
            uploaded_bytes = uploaded.read()
        with open(working_docx_path, 'wb') as f:
            f.write(uploaded_bytes)
        # Must run before status changes — see
        # _onlyoffice_checkpoint_sync's docstring. Populates Document.content
        # from the barangay's actual docx instead of leaving it blank.
        _onlyoffice_checkpoint_sync(new_document)
        new_document.status = 'REFERRED'
        new_document.save()
        _onlyoffice_snapshot_pdf(request, new_document)
        _onlyoffice_archive_docx(new_document)

        doc.status = 'DRAFT_CREATED'
        doc.referred_committee = committee
        doc.save()
        log_action(request, action='MOVE', target=doc.title or f'Barangay measure #{doc.id}',
                   detail=f'Referred to committee: {committee.name}.')
        recipients = []

        def add_email(councilor):
            if councilor and hasattr(councilor, 'user') and councilor.user.email:
                recipients.append(councilor.user.email)
            elif councilor and hasattr(councilor, 'email') and councilor.email:
                recipients.append(councilor.email)

        add_email(committee.chairman)
        recipients = list(set(recipients))
        if recipients:
            subject = f"Official Referral: {doc.title} - {doc.title}"
            message = (
                f"Honorable Members of the {committee.name},\\n\\n"
                f"The legislative measure entitled '{doc.title}' ({doc.title}) "
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

        messages.success(request, f"{doc.title} successfully referred to {committee.name}.")
        return redirect('other-matters')

    return redirect('other-matters')


@login_required
def barangay_file_preview(request, doc_id):
    """Modal preview for a raw barangay upload, before it's been referred
    to a committee (i.e. before barangay_to_referral has created a real
    Document for it) — same viewing experience as a filed Document's own
    modal_document_viewer (a PDF shown in an iframe). See
    barangay_file_snapshot_pdf for how that PDF gets produced."""
    doc = get_object_or_404(BarangayFiles, id=doc_id)
    if request.user.role not in ('SECRETARIAT', 'STAFF', 'ADMIN'):
        return HttpResponse('Not found.', status=404)
    return render(request, 'barangay/modal_barangay_viewer.html', {'doc': doc})


def _barangay_file_snapshot_pdf_path(doc_id):
    """Cache path for a barangay file's converted preview PDF — keyed
    only by doc_id, unlike a Document's own per-version snapshot
    (_onlyoffice_saved_pdf_path), since a BarangayFiles row's
    uploaded_docx never changes after upload (no re-upload/replace
    feature exists), so there's nothing to key a version against."""
    return os.path.join(settings.DOCUMENT_EDITOR_STORAGE_ROOT, f"barangay_{doc_id}.pdf")


@login_required
@xframe_options_exempt
def barangay_file_snapshot_pdf(request, doc_id):
    """Serves this barangay file's uploaded .docx converted to PDF —
    converted once via the same local LibreOffice conversion every other
    document-to-PDF path in this app already uses
    (documents/libreoffice.py; see documents/views.py's
    _onlyoffice_convert_docx_to_pdf for the Document equivalent), then
    cached to disk so every preview after the first is an instant file
    read instead of paying LibreOffice's startup cost again. Safe to
    cache indefinitely since the source file itself can't change (see
    _barangay_file_snapshot_pdf_path)."""
    doc = get_object_or_404(BarangayFiles, id=doc_id)
    if request.user.role not in ('SECRETARIAT', 'STAFF', 'ADMIN'):
        return HttpResponse('Not found.', status=404)
    if not doc.uploaded_docx:
        return HttpResponse('No file uploaded.', status=404)

    cache_path = _barangay_file_snapshot_pdf_path(doc.id)
    if os.path.exists(cache_path):
        with open(cache_path, 'rb') as f:
            return HttpResponse(f.read(), content_type='application/pdf')

    from documents import libreoffice
    with open(doc.uploaded_docx.path, 'rb') as f:
        docx_bytes = f.read()
    try:
        pdf_bytes = libreoffice.convert(docx_bytes, '.docx', 'pdf', label=f'barangay_{doc.id}')
    except Exception as e:
        return HttpResponse(f'PDF conversion failed: {e}', status=502)

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, 'wb') as f:
        f.write(pdf_bytes)
    return HttpResponse(pdf_bytes, content_type='application/pdf')
