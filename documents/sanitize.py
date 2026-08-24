"""
documents/sanitize.py
Sanitizes rich-text HTML produced by the Quill editor before it is stored.

The draft editor's toolbar (templates/documents/draft.html) only ever emits
a fixed, known set of tags: headings, bold/italic/underline/strike, ordered
and bullet lists with indent levels, and blockquotes. Anything outside that
set — <script>, <iframe>, event-handler attributes, javascript: URLs, etc. —
is stripped rather than trusted, since the same field is later rendered with
the `|safe` template filter in multiple views.
"""

from __future__ import annotations

import nh3

_ALLOWED_TAGS = {
    "p", "br",
    "strong", "em", "u", "s",
    "h1", "h2", "h3",
    "ol", "ul", "li",
    "blockquote",
}

_ALLOWED_ATTRIBUTES = {
    "li": {"class", "data-list"},
    "p": {"class"},
    "ol": {"class"},
    "ul": {"class"},
}


def sanitize_document_html(html: str) -> str:
    """Strip everything except the Quill toolbar's known output tags."""
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        clean_content_tags={"script", "style"},
        link_rel=None,
    )
