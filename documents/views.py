from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
import json
from .models import Document
from committees.models import Committee
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from django.contrib import messages
@login_required
def create_draft(request):
    if request.method == "POST":
        doc_id = request.POST.get('doc_id')
        action = request.POST.get('action') 
        
        if doc_id and doc_id != "":
            doc = get_object_or_404(Document, id=doc_id, author=request.user)
        else:
            doc = Document(author=request.user)

        doc.title = request.POST.get('title')
        doc.content = request.POST.get('content')
        doc.doc_type = request.POST.get('type') 
        
        committee_id = request.POST.get('target_committee')
        if committee_id:
            doc.referred_committee_id = committee_id

        if action == 'submit':
            doc.status = 'FILED'

            if not doc.reference_no:
                current_year = timezone.now().year
                prefix = 'DO' if doc.doc_type == 'ORDINANCE' else 'DR'
                

                count = Document.objects.filter(
                    doc_type=doc.doc_type,
                    status__in=['FILED', 'FIRST_READING', 'COMMITTEE', 'SECOND_READING', 'THIRD_READING', 'APPROVED'],
                    created_at__year=current_year
                ).count()
                
                new_sequence = count + 1

                doc.reference_no = f"{prefix}-{new_sequence:03d}-{current_year}"
        else:
            doc.status = 'DRAFT'
        
        doc.save() # Archive is triggered here if status is now 'FILED'
        return redirect('dashboard') 

    committees = Committee.objects.all()
    return render(request, 'documents/draft.html', {'committees': committees})

@login_required
def autosave_draft(request):
    """Background task: No archive creation here, status remains DRAFT."""
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            doc_id = data.get('doc_id')
            
            if doc_id and doc_id != "null":
                doc = get_object_or_404(Document, id=doc_id, author=request.user)
                doc.title = data.get('title', doc.title)
                doc.content = data.get('content', doc.content)
                doc.save()
            else:
                doc = Document.objects.create(
                    title=data.get('title', 'Untitled Draft'),
                    content=data.get('content', ''),
                    author=request.user,
                    status='DRAFT'
                )
            return JsonResponse({'status': 'saved', 'doc_id': doc.id})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@login_required
def move_document_status(request, pk):
    """Secretary/Staff View: Moves official levels and creates archives."""
    doc = get_object_or_404(Document, pk=pk)
    user = request.user

    if doc.status == 'FILED' and user.role == 'SECRETARY':
        ref_no = request.POST.get('reference_no')
        if ref_no: doc.reference_no = ref_no
        doc.status = 'FIRST_READING'
    
    elif doc.status == 'FIRST_READING' and user.role in ['SECRETARY', 'STAFF']:
        doc.status = 'COMMITTEE'
    
    elif doc.status == 'COMMITTEE' and user.role in ['SECRETARY', 'STAFF']:
        doc.status = 'SECOND_READING'

    doc.save() 
    return redirect('document_detail', pk=doc.pk)


def incoming_docs(request):
    incoming_docs = Document.objects.filter(status = 'FILED')
    print(request.user)
    return render(request,'documents/incoming.html', {'incoming_docs': incoming_docs})

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
def agenda_list(request):
    return render(request, 'documents/agenda.html')


######################___________first reading________________######################

def first_reading(request):
    first_reading = Document.objects.filter(status ='FIRST_READING')
    committees = Committee.objects.all()
    return render(request,'documents/tracking/first_reading.html',{'first_reading':first_reading, 'committees':committees})

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

    #########___________tracking stages urls_________############
def view_committee(request):
    committee_docs = Document.objects.filter(status = 'REFERRED')
    return render(request, 'documents/tracking/committee_page.html',{'committee_docs':committee_docs})