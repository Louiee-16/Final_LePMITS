from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse
from django.utils.html import strip_tags
from django.contrib.auth.decorators import login_required
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib import colors
from django.contrib import messages
from .models import CommitteeReport, HearingLog
from documents.models import Document
from documents.urls import urlpatterns
from django.db.models import Q


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sync_report_status_from_hearing(report, outcome):
    """
    Keep CommitteeReport.status in sync with the latest hearing outcome,
    and advance (or revert) the parent Document's status accordingly.
    """
    report.status = outcome
    report.save()

    draft = report.draft

    if outcome == 'FAILED' and draft.status != 'RETURNED':
        draft.status = 'RETURNED'   # sent back to the authoring councilor
        draft.save()

    # RESET / PENDING — document status stays where it is (still in committee)


# ---------------------------------------------------------------------------
# Workbench view
# ---------------------------------------------------------------------------
def move_to_second_reading(request, doc_id):
    referred_doc = get_object_or_404(Document, id=doc_id)
    referred_doc.content = referred_doc.amended_content
    referred_doc.status = 'SECOND_READING'
    referred_doc.save()
    return redirect('view-committee')

def view_committee(request):
    committee_docs = Document.objects.filter(Q(status__icontains='REFERRED') | Q(status__icontains='COMMITTEE'))

    return render(request, 'documents/tracking/committee_page.html',{'committee_docs':committee_docs})





def report_workbench(request, draft_id):
    draft  = get_object_or_404(Document, id=draft_id)
    report, _ = CommitteeReport.objects.get_or_create(draft=draft)

    if request.method == 'POST':
        action = request.POST.get('action')

        # ---- Always persist the remarks content ----
        report.content = request.POST.get('content', '')
        report.save()

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
                _sync_report_status_from_hearing(report, outcome)

        return redirect('report_workbench', draft_id=draft.id)

    return render(request, 'documents/committee_level/committee_workbench.html', {
        'report': report,
        'draft':  draft,
    })


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

def _build_pdf_styles():
    """Return a dict of named ParagraphStyles."""
    def s(name, **kw):
        defaults = dict(fontName='Times-Roman', fontSize=10, leading=14, spaceAfter=0)
        defaults.update(kw)
        return ParagraphStyle(name, **defaults)

    return {
        'center':      s('center',      alignment=TA_CENTER),
        'center_bold': s('center_bold', alignment=TA_CENTER, fontName='Times-Bold'),
        'left':        s('left',        alignment=TA_LEFT),
        'left_bold':   s('left_bold',   alignment=TA_LEFT,   fontName='Times-Bold'),
        'label':       s('label',       alignment=TA_LEFT,   fontSize=9,  fontName='Times-Roman', textColor=colors.HexColor('#555555')),
        'value':       s('value',       alignment=TA_LEFT,   fontName='Times-Bold', fontSize=10),
        'body':        s('body',        alignment=TA_JUSTIFY, fontSize=10, leading=16),
        'italic':      s('italic',      alignment=TA_JUSTIFY, fontSize=10, fontName='Times-Italic', leading=16),
        'title':       s('title',       alignment=TA_CENTER, fontName='Times-Bold', fontSize=14, spaceAfter=4),
        'small_bold':  s('small_bold',  alignment=TA_LEFT,   fontName='Times-Bold', fontSize=9),
    }


def _meta_row(label, value_para, st, top_border=False):
    """Single label/value table row."""
    tbl = Table(
        [[Paragraph(label, st['label']), value_para]],
        colWidths=[1.3 * inch, 4.5 * inch],
    )
    cmds = [
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',   (0, 0), (-1, -1), 0),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
    ]
    if top_border:
        cmds.append(('LINEABOVE', (0, 0), (-1, 0), 0.5, colors.HexColor('#cccccc')))
    tbl.setStyle(TableStyle(cmds))
    return tbl


def _build_pdf_story(draft, report, st):
    """Assemble the ReportLab story list for the committee report PDF."""
    story = []

    # -- Letterhead --
    story += [
        Paragraph('Republic of the Philippines', st['center']),
        Paragraph('<b>City of San Juan, Metro Manila</b>', st['center_bold']),
        Paragraph('Office of the Sangguniang Panlungsod', st['center']),
        Spacer(1, 0.3 * inch),
        Paragraph('COMMITTEE REPORT', st['title']),
        HRFlowable(width='100%', thickness=1, color=colors.black),
        Spacer(1, 0.2 * inch),
    ]

    # -- Submitted By --
    committee = draft.referred_committee
    if committee:
        submitted_by = f'Committee on {committee.name}'
        members = [m.name for m in [committee.chairman, committee.vice_chairman, committee.member] if m]
        if members:
            submitted_by += f'<br/><font size="9">({", ".join(members)})</font>'
    else:
        submitted_by = 'No committee assigned'

    story.append(_meta_row('Submitted By:', Paragraph(f'<b>{submitted_by}</b>', st['value']), st))

    # -- Subject / Sponsor / To / Entitled --
    ref          = draft.reference_no or str(draft.id)
    series_year  = draft.created_at.strftime('%Y')
    subject_text = f'{draft.doc_type} NO. {ref}, SERIES OF {series_year}'

    try:
        sponsor_name = draft.author.councilor_profile.name
    except Exception:
        sponsor_name = f'{draft.author.last_name}, {draft.author.first_name}'.upper()

    for label, text, border in [
        ('Subject:',           subject_text,          True),
        ('Sponsor:',           sponsor_name,           True),
        ('To:',                'The Presiding Officer',True),
        ('To which was referred:', subject_text,       True),
        ('Entitled:',          draft.title.upper(),    True),
    ]:
        story.append(_meta_row(label, Paragraph(f'<b>{text}</b>', st['value']), st, top_border=border))

    story += [
        Spacer(1, 0.25 * inch),
        HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#cccccc')),
        Spacer(1, 0.2 * inch),
        Paragraph(
            'Has considered the same and has the honor to return to the Sangguniang Panlungsod '
            'with the recommendation that the said Draft Ordinance:',
            st['italic'],
        ),
        Spacer(1, 0.2 * inch),
    ]

    # -- Recommendation checkboxes --
    STATUS_LABELS = {
        'APPROVED': 'APPROVED ON COMMITTEE LEVEL',
        'FAILED':   'DID NOT PASS THE COMMITTEE LEVEL',

    }
    for value, label in STATUS_LABELS.items():
        checked = '✓' if report.status == value else ' '
        row = Table(
            [[Paragraph(f'<b>{checked}</b>', st['left_bold']), Paragraph(f'<b>{label}</b>', st['left_bold'])]],
            colWidths=[0.35 * inch, 5.45 * inch],
        )
        row.setStyle(TableStyle([
            ('BOX',           (0, 0), (0, 0), 1,   colors.black),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING',   (0, 0), (0, 0),  4),
            ('RIGHTPADDING',  (0, 0), (0, 0),  4),
            ('TOPPADDING',    (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING',   (1, 0), (1, 0),  8),
        ]))
        story.append(row)
        story.append(Spacer(1, 0.08 * inch))

    story.append(Spacer(1, 0.2 * inch))

    # -- Remarks --
    remarks_plain = strip_tags(report.content or '').strip()
    story.append(Paragraph('<b>Remarks:</b>', st['left_bold']))
    story.append(Spacer(1, 0.05 * inch))

    if remarks_plain:
        for line in remarks_plain.split('\n'):
            line = line.strip()
            if line:
                story.append(Paragraph(f'- {line}', st['body']))
    else:
        story.append(HRFlowable(width='80%', thickness=0.5, color=colors.black))
        story.append(Spacer(1, 0.05 * inch))
        story.append(HRFlowable(width='80%', thickness=0.5, color=colors.black))

    story.append(Spacer(1, 0.5 * inch))

    # -- Hearing history --
    hearings = report.hearings.all()
    if hearings:
        story += [
            HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#cccccc')),
            Spacer(1, 0.1 * inch),
            Paragraph('<b>Hearing History</b>', st['small_bold']),
            Spacer(1, 0.05 * inch),
        ]
        for h in hearings:
            outcome_label = dict(HearingLog.OUTCOME_CHOICES).get(h.outcome, h.outcome)
            notes         = f'. {h.attendance_notes}' if h.attendance_notes else ''
            story.append(Paragraph(
                f'• Hearing {h.version_discussed} — {h.hearing_date.strftime("%B %d, %Y")} '
                f'[{outcome_label}]{notes}',
                st['body'],
            ))

    return story


def generate_committee_pdf(request, draft_id):
    draft  = get_object_or_404(Document, id=draft_id)
    report = get_object_or_404(CommitteeReport, draft=draft)

    response = HttpResponse(content_type='application/pdf')
    filename = f"Committee_Report_{draft.doc_type}_{draft.id}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    doc = SimpleDocTemplate(
        response,
        pagesize=letter,
        leftMargin=1.2 * inch,
        rightMargin=1.2 * inch,
        topMargin=1 * inch,
        bottomMargin=1 * inch,
    )
    st    = _build_pdf_styles()
    story = _build_pdf_story(draft, report, st)
    doc.build(story)
    return response
def set_hearing_date(request):
    if request.method == "POST":
        doc_id = request.POST.get('doc_id')
        h_date = request.POST.get('hearing_date')

        doc = get_object_or_404(Document, id = doc_id)
        doc.hearing_date = h_date
        doc.save()

    return redirect(request.META.get('HTTP_REFERER','view-committee'))


@login_required
def committee_amendments(request, doc_id):
    """Render the floor amendments workbench for a second-reading document."""

    doc = get_object_or_404(Document, Q(id=doc_id) & (Q(status='REFERRED') | Q(status__icontains='COMMITTEE')))
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to make amendments.")
        return redirect('second_reading')

    amendments = doc.amendment_notes.select_related('author').all()

    return render(request, 'documents/committee_level/amending_table.html', {
        'doc': doc,
        'amendments': amendments,
    })


@login_required
def move_to_unfinished(request, doc_id):
    doc = get_object_or_404(Document,id=doc_id, status__icontains='COMMITTEE' )
    if request.method != 'POST':
        return redirect('report_workbench',draft_id = doc_id)
    doc.content = doc.amended_content
    doc.status = 'UNFINISHED_BUSINESS'
    doc.amended_content = None
    doc.amendment_status = None
    doc.save()
    return redirect('view-committee')


@login_required
def save_committee_amendments(request, doc_id):
    """Handle save and add_note actions from the amendments form."""
    doc = get_object_or_404(Document, Q(id=doc_id) & (Q(status='REFERRED') | Q(status__icontains='COMMITTEE')))

    # Role guard
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        messages.error(request, "You don't have permission to make amendments.")
        return redirect('second_reading')

    if request.method != 'POST':
        return redirect('committee-amendments', doc_id=doc_id)

    amended_content = request.POST.get('amended_content', '').strip()
    amendment_status = request.POST.get('amendment_status', 'IN_PROGRESS')

    if amended_content:
        doc.amended_content = amended_content
    doc.amendment_status = amendment_status
    doc.save()
    return redirect('committee-amendments',doc_id=doc_id)


