from django.shortcuts import render, get_object_or_404, redirect
from documents.models import Document
from datetime import datetime, timedelta
from committee_level.models import CommitteeReport
from barangay.models import BarangayFiles
from django.contrib.auth.decorators import login_required
from .models import Session
from django.http import HttpResponse, HttpResponseForbidden
from django.template.loader import render_to_string
from django.core.mail import EmailMessage
from django.contrib import messages
from django.conf import settings
from audit.utils import log_action


@login_required
def public_participation(request, doc_id):
    if not request.user.is_authenticated:
        return redirect('login')

    if request.user.role != 'SECRETARIAT':
        return HttpResponseForbidden()

    if request.method == 'POST':
        doc = get_object_or_404(Document, id=doc_id)

        doc.public_participation = not doc.public_participation
        doc.save()
        label = "enabled" if doc.public_participation else "disabled"
        log_action(request, action='UPDATE', target=f'{doc.doc_type} — {doc.reference_no or doc.id}',
                   detail=f'Public participation {label}.')
    return redirect(request.META.get('HTTP_REFERER') or reverse('view-committee'))
        





@login_required
def Secretariat_dashboard(request):
    now = datetime.now()

    days_ahead = 0 - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    next_monday = now + timedelta(days=days_ahead)
    session_datetime = next_monday.replace(hour=9, minute=30, second=0, microsecond=0)

    active_statuses = ['FILED', 'FIRST_READING', 'REFERRED', 'COMMITTEE', 'SECOND_READING', 'THIRD_READING']

    context = {
        'latest_docs': Document.objects.exclude(status__in=['GHOST', 'DRAFT']).order_by('-updated_at')[:8],
        'active_count':        Document.objects.filter(status__in=active_statuses).count(),
        'first_reading_count': Document.objects.filter(status='FIRST_READING').count(),
        'in_committee':        Document.objects.filter(status__in=['REFERRED', 'COMMITTEE']).count(),
        'approved_count':      Document.objects.filter(status='APPROVED').count(),
        'pipeline': [
            {'label': 'First Reading',    'count': Document.objects.filter(status='FIRST_READING').count(),    'color': 'bg-slate-400'},
            {'label': 'In Committee',     'count': Document.objects.filter(status__in=['REFERRED','COMMITTEE']).count(), 'color': 'bg-slate-500'},
            {'label': 'Second Reading',   'count': Document.objects.filter(status='SECOND_READING').count(),   'color': 'bg-slate-600'},
            {'label': 'Third Reading',    'count': Document.objects.filter(status='THIRD_READING').count(),    'color': 'bg-slate-700'},
        ],
        "month":            session_datetime.strftime("%B"),
        "day":              session_datetime.strftime("%d"),
        "weekday":          session_datetime.strftime("%A"),
        "time":             session_datetime.strftime("%I:%M %p"),
        "session_datetime": session_datetime,
    }

    return render(request, 'dashboards/secretariat.html', context)




@login_required
def session_list(request):
    sessions = Session.objects.all()

    context ={
        'sessions':sessions
        
    }
    return render(request, 'documents/agenda_list.html',context)





    ############ FOR AGENDA #####################################


from django.core.files.base import ContentFile
import io, os
from xhtml2pdf import pisa
from django.utils import timezone
from django.http import FileResponse


def download_order_of_business(request, session_id):
    session = get_object_or_404(Session, id=session_id)
    filename = os.path.basename(session.order_of_business.name)
    return FileResponse(session.order_of_business.open(), as_attachment=True, filename=filename)

from django.template.loader import get_template

@login_required
def finalize_agenda(request):
    """Generates the PDF and saves it to the session model."""
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to finalize the agenda.")
        return redirect('order-of-business')

    now = datetime.now()

    days_ahead = 0 - now.weekday() 
    if days_ahead <= 0:
        days_ahead += 7

    next_monday = now + timedelta(days=days_ahead)

    session_date = next_monday
    session, _ = Session.objects.get_or_create(
        session_number = 1,
        council_number = 8,
        session_time = '10:00am',
        session_date = session_date,
    )


    context = get_agenda_context()
    template = get_template('documents/pdf/order_of_business.html')
    html_string = template.render(context)
    ##html_string = render_to_string('documents/pdf/order_of_business.html', context)
    
    pdf_buffer = io.BytesIO()
    pisa.CreatePDF(html_string, dest=pdf_buffer)
    pdf_buffer.seek(0)
    
    filename = f"Agenda-Session-{session.id}-{session.session_date}.pdf"
    session.order_of_business.save(filename, ContentFile(pdf_buffer.read()), save=True)
    session.agenda_finalized_date = timezone.now()
    session.save()
    log_action(request, action='CREATE', target=f'Order of Business — Session #{session.session_number}',
               detail='Agenda finalized and PDF generated.')
    
    return redirect('order-of-business')






def get_agenda_context():
    now = datetime.now()

    days_ahead = 0 - now.weekday() 
    if days_ahead <= 0:
        days_ahead += 7

    next_monday = now + timedelta(days=days_ahead)

    session_date = next_monday
    session = Session.objects.all().order_by('-agenda_finalized_date').first()
    print(session)
    return {

        'session': session,
        'next_session_date': session_date,
        'today': timezone.now(),
        'first_reading': Document.objects.filter(
            status='FIRST_READING'
        ).order_by('reference_no'),

        'committee_reports': CommitteeReport.objects.filter(
            status='SECOND_READING'
        ),

        'second_reading': Document.objects.filter(
            status='SECOND_READING'
        ).order_by('reference_no'),

        'third_reading': Document.objects.filter(
            status='THIRD_READING'
        ).order_by('reference_no'),

        'other_matters': BarangayFiles.objects.filter(
            status='OTHER_MATTERS'
        ),
    }


@login_required
def order_of_business(request):
    """Main agenda view."""
 
    context = get_agenda_context()

        
    return render(request, 'documents/agenda.html', context)

###############################3###############################################################3


@login_required
def generate_agenda_pdf(request, session_id):
    """Generate and download the agenda as a PDF."""
    session = get_object_or_404(Session, id=session_id)
 
    context = get_agenda_context()
 
    html_string = render_to_string('agenda/agenda_pdf.html', context)
 
    try:
        from xhtml2pdf import pisa
        import io
 
        pdf_buffer = io.BytesIO()
        pisa.CreatePDF(html_string, dest=pdf_buffer)
        pdf_buffer.seek(0)
 
        filename = f"Order-of-Business-{session.session_date}.pdf"
        response = HttpResponse(pdf_buffer.read(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
 
    except ImportError:
        return HttpResponse('PDF generation requires xhtml2pdf. Run: pip install xhtml2pdf', status=500)

@login_required
def send_agenda_email(request, session_id):
    """Email the Order of Business PDF to all active councilors."""
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to send the agenda email.")
        return redirect('order-of-business')

    from councilors.models import Councilor

    session = get_object_or_404(Session, id=session_id)

    councilor_emails = list(
        Councilor.objects.filter(is_active=True)
        .exclude(email__isnull=True).exclude(email='')
        .values_list('email', flat=True)
    )

    if not councilor_emails:
        messages.warning(request, 'No councilor email addresses found.')
        return redirect('order-of-business')

    # Use the already-finalized PDF if available, otherwise generate one
    if session.order_of_business:
        pdf_bytes = session.order_of_business.read()
        session.order_of_business.seek(0)
    else:
        context = get_agenda_context()
        html_string = get_template('documents/pdf/order_of_business.html').render(context)
        pdf_buffer = io.BytesIO()
        pisa.CreatePDF(html_string, dest=pdf_buffer)
        pdf_buffer.seek(0)
        pdf_bytes = pdf_buffer.read()

    subject = (
        f"Order of Business — "
        f"{session.session_number}th Regular Session, "
        f"{session.session_date.strftime('%B %d, %Y')}"
    )

    body = (
        f"Dear Honorable Councilors,\n\n"
        f"Please find attached the Order of Business for the "
        f"{session.session_number}th Regular Session of the 8th City Council "
        f"of the City Government of San Juan, Metro Manila "
        f"on {session.session_date.strftime('%B %d, %Y')} at {session.session_time}.\n\n"
        f"Respectfully,\n"
        f"Office of the Sangguniang Panlungsod\n"
        f"City of San Juan, Metro Manila"
    )

    filename = f"Order-of-Business-{session.session_date}.pdf"

    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=councilor_emails,
    )
    email.attach(filename, pdf_bytes, 'application/pdf')

    try:
        email.send()
        log_action(request, action='UPDATE', target=f'Order of Business — Session #{session.session_number}',
                   detail=f'Emailed to {len(councilor_emails)} councilor(s).')
        messages.success(request, f'Order of Business emailed to {len(councilor_emails)} councilor(s).')
    except Exception as e:
        messages.error(request, f'Failed to send email: {str(e)}')

    return redirect('order-of-business')