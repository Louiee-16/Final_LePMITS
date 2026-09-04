"""Builds the starting .docx for a committee report by loading the
committee's own real template (report_templates/committee_report_template.docx,
provided directly by the user) and substituting real data into its known
fields at the run level — not reconstructing the layout from scratch, so
formatting/spacing/fonts match exactly, byte-for-byte what the template
already looks like in Word.

Single committee only for now (the template's second signatory column is
blanked out, not filled) — no joint-committee support yet, deferred by
explicit request.

Used once, the first time report_workbench opens for a draft: after that,
the docx is a real live document edited through Casual Docs, same as any
other document in this app — this module never runs again for that report.
"""
import io
import os
import re

from docx import Document as DocxDocument

_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "report_templates", "committee_report_template.docx")


def _set_run(paragraph, index, text):
    """Overwrites one run's text by index, leaving its formatting (bold,
    font, etc.) untouched — python-docx's run.text setter only changes
    the text content, not the run's rPr."""
    paragraph.runs[index].text = text


def _clear_runs_from(paragraph, start_index):
    for run in paragraph.runs[start_index:]:
        run.text = ""


def _clear_paragraph(paragraph):
    for run in paragraph.runs:
        run.text = ""


def _find_paragraph(doc, label_prefix):
    for p in doc.paragraphs:
        if p.text.startswith(label_prefix):
            return p
    return None


def build_subject_line(draft) -> str:
    """"DRAFT ORDINANCE NO. 11, SERIES OF 2026" — the one place this gets
    built, reused by both the seed docx (below) and report_workbench's
    breadcrumb, so the two can't drift out of sync with each other."""
    ref = draft.reference_no or str(draft.id)
    series_year = draft.created_at.strftime("%Y")
    # reference_no's shape depends on the draft's stage: "DO-003-2026"
    # before approval, but already a full "Ordinance No. 3" string after
    # (see approve_measure). A committee report is written before
    # approval, so the template's own default reads "DRAFT RESOLUTION NO.
    # ___" — matching that means always leading with "DRAFT". The
    # already-formatted-reference_no case is only reachable if a report
    # somehow gets regenerated post-approval, where "DRAFT" no longer
    # applies since it's an enacted measure by then, not a draft.
    if draft.doc_type.lower() in ref.lower():
        return f"{ref}, SERIES OF {series_year}"
    # "DO-011-2026" is the internal tracking code — the report should
    # cite just the number, leading zeros stripped ("11", not "011"),
    # matching how a resolution/ordinance is normally referred to in
    # text ("Draft Ordinance No. 11"), not the raw tracking format.
    match = re.search(r"(\d+)", ref)
    number = str(int(match.group(1))) if match else ref
    return f"DRAFT {draft.doc_type.upper()} NO. {number}, SERIES OF {series_year}"


def has_remarks_content(docx_path) -> bool:
    """Whether the REMARKS table (see build_committee_report_docx) has
    anything typed into it — gates hearing-outcome selection in
    report_workbench, so a hearing can't be logged without an explanation
    of what happened. REMARKS lives in a table cell (doc.tables[1]), not a
    body paragraph — confirmed directly against a real saved report."""
    if not os.path.exists(docx_path):
        return False
    doc = DocxDocument(docx_path)
    if len(doc.tables) < 2:
        return False
    for row in doc.tables[1].rows:
        for cell in row.cells:
            text = cell.text.strip()
            if text and text.upper() != "REMARKS:":
                return True
    return False


def build_committee_report_docx(draft, report) -> bytes:
    """Returns the seed .docx bytes for this draft's committee report."""
    doc = DocxDocument(_TEMPLATE_PATH)

    committee = draft.referred_committee
    if committee:
        committee_line = f"Committee on {committee.name}"
    else:
        committee_line = "No committee assigned"

    subject = build_subject_line(draft)
    doc_type_title = draft.doc_type.title()  # "Ordinance" / "Resolution"
    try:
        sponsor = draft.author.councilor_profile.name
    except Exception:
        sponsor = f"{draft.author.last_name}, {draft.author.first_name}".upper()

    # -- "Submitted by\t:\tCommittees on ..." --
    p = _find_paragraph(doc, "Submitted by")
    if p:
        _set_run(p, 2, f"\t{committee_line}")
        _clear_runs_from(p, 3)

    # -- "Subject\t\t:\tDRAFT RESOLUTION NO. ___, SERIES OF 2026" --
    p = _find_paragraph(doc, "Subject")
    if p:
        _set_run(p, 4, subject)
        _clear_runs_from(p, 5)

    # -- "Sponsor\t:\t..." --
    p = _find_paragraph(doc, "Sponsor")
    if p:
        _set_run(p, 3, sponsor)

    # -- "To which was referred: DRAFT RESOLUTION NO. ___, SERIES OF 2026" --
    p = _find_paragraph(doc, "To which was referred")
    if p:
        _set_run(p, 2, subject)
        _clear_runs_from(p, 3)

    # -- "Entitled\t: “...”" — value sits between two literal quote-mark
    # runs, so only the middle run is replaced. --
    p = _find_paragraph(doc, "Entitled")
    if p and len(p.runs) >= 4:
        _set_run(p, 3, draft.title)

    # -- "...with the recommendation that the said Draft Resolution:" —
    # matches the draft's actual type instead of always saying Resolution. --
    p = _find_paragraph(doc, "Has considered the same")
    if p and len(p.runs) >= 3:
        _set_run(p, 2, doc_type_title)

    # -- Signatory table: single committee only. Column 0 gets real data;
    # column 1 (the template's second, joint committee) is blanked rather
    # than left showing sample data, since this app has no concept of a
    # joint committee report yet. --
    if len(doc.tables) >= 3:
        sig_table = doc.tables[2]
        if committee:
            header = sig_table.rows[0].cells[0].paragraphs[0]
            if len(header.runs) >= 3:
                _set_run(header, 2, committee.name)

            role_members = [
                (1, committee.chairman),
                (2, committee.vice_chairman),
                (3, committee.member),
            ]
            for row_index, member in role_members:
                cell = sig_table.rows[row_index].cells[0]
                name_para = next((p for p in cell.paragraphs if p.runs and "COUN." in p.text.upper()), None)
                if name_para:
                    if member:
                        text = f"COUN. {member.name.upper()}"
                        name_para.runs[0].text = text
                        for extra in name_para.runs[1:]:
                            extra.text = ""
                    else:
                        _clear_paragraph(name_para)

        # Column 1 — no joint-committee support yet, blank it entirely.
        for row in sig_table.rows:
            for para in row.cells[1].paragraphs:
                _clear_paragraph(para)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
