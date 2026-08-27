"""
documents/templatetags/document_extras.py
Print-formatting helpers for rendering Document.content as an actual
justified legal document, independent of how it was line-broken while
being typed.
"""

import re

from django import template
from django.utils.safestring import mark_safe
from bs4 import BeautifulSoup, NavigableString

register = template.Library()

_CLAUSE_LEAD_RE = re.compile(r"^\s*(WHEREAS|NOW,?\s+THEREFORE)\b", re.IGNORECASE)


def _is_blank_paragraph(tag) -> bool:
    """A <p> with no real text — typically <p><br></p>, left in place as a
    deliberate blank line between clauses."""
    return not tag.get_text(strip=True)


@register.filter(name="merge_wrapped_paragraphs")
def merge_wrapped_paragraphs(html: str) -> str:
    """
    Two print-formatting passes over Document.content, matching the actual
    house style used in this city's real ordinances/resolutions (see the
    scanned samples in media/legacy_documents/):

    1. MERGE: Quill — and anyone used to a typewriter or plain-text editor —
       often presses Enter at the end of every visual line instead of
       letting the editor wrap text naturally, so each line becomes its own
       single-line <p>. CSS `text-align: justify` correctly never stretches
       the last (only) line of a paragraph, so text authored this way
       renders with every line looking unjustified/ragged, even though the
       justify rule itself is working exactly as intended — there's just
       nothing to justify within a one-line paragraph. This merges runs of
       consecutive, non-empty single-line paragraphs back into one flowing
       paragraph, using the deliberate blank <p><br></p> lines already
       present between clauses as the real paragraph-break signal.

    2. INDENT: in the real documents, WHEREAS / "NOW, THEREFORE" clauses
       get a first-line indent, while SECTION clauses stay flush-left. This
       tags any paragraph that starts with one of those leads with a
       `clause-indent` class so the stylesheet can apply that indent
       specifically, not uniformly to every paragraph.

    Only <p> tags are touched — headings, lists, and blockquotes are left
    exactly as-is.
    """
    if not html:
        return html

    soup = BeautifulSoup(html, "html.parser")
    run: list = []

    def flush_run():
        if len(run) < 2:
            return
        merged_text = " ".join(p.decode_contents().strip() for p in run)
        new_p = soup.new_tag("p")
        for child in list(BeautifulSoup(merged_text, "html.parser").contents):
            new_p.append(child)
        run[0].replace_with(new_p)
        for p in run[1:]:
            p.decompose()

    for node in list(soup.contents):
        if isinstance(node, NavigableString):
            continue
        if node.name == "p" and not _is_blank_paragraph(node):
            run.append(node)
            continue
        flush_run()
        run = []

    flush_run()

    for p in soup.find_all("p"):
        if _CLAUSE_LEAD_RE.match(p.get_text(strip=True)):
            p["class"] = p.get("class", []) + ["clause-indent"]

    return mark_safe(str(soup))
