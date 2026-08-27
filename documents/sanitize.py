"""
documents/sanitize.py
Sanitizes rich-text HTML produced by the drafting editor before it is stored.

The editor's toolbar (templates/documents/draft.html) only ever emits a
fixed, known set of tags — headings, bold/italic/underline/strike, ordered
and bullet lists with indent levels, blockquotes, tables, links, images, and
inline color/highlight/font marks. Anything outside that set — <script>,
<iframe>, event-handler attributes, javascript: URLs, arbitrary CSS, etc. —
is stripped rather than trusted, since the same field is later rendered with
the `|safe` template filter in multiple views.

Color/highlight/font-family/font-size/line-height are intentionally allowed
via a narrow `style` allowlist rather than stripped outright — councilors
use color as a "where I left off" marker across drafting sessions, and the
rest are standard document-formatting needs.
"""

from __future__ import annotations

import html
import re

import nh3

_ALLOWED_TAGS = {
    "p", "br", "span", "div",
    "strong", "em", "u", "s",
    "h1", "h2", "h3",
    "ol", "ul", "li",
    "blockquote", "hr",
    "a", "img",
    "table", "tbody", "thead", "tr", "th", "td",
    # @tiptap/extension-subscript / -superscript render as plain <sub>/<sup>
    # with no attributes at all — nothing to allowlist beyond the tag name
    # itself.
    "sub", "sup",
}

_ALLOWED_ATTRIBUTES = {
    "li": {"class", "data-list"},
    "p": {"class", "style"},
    "span": {"style"},
    "div": {"class"},
    "ol": {"class"},
    "ul": {"class"},
    "a": {"href", "target", "rel"},
    "img": {"src", "alt", "width", "height", "containerstyle", "wrapperstyle"},
    # colwidth is how @tiptap/extension-table's resizable columns persist a
    # dragged width (a ProseMirror node attr serialized straight onto the
    # <td>/<th>, e.g. colwidth="180") — without it here, nh3 silently strips
    # the attribute on every save and a resized column reverts on the next
    # autosave. The value is always a PM-generated integer string, not
    # attacker-controlled markup, so no further constraint is needed.
    "th": {"colspan", "rowspan", "colwidth"},
    "td": {"colspan", "rowspan", "colwidth"},
}

# nh3 doesn't itself validate CSS property values inside a `style`
# attribute — it only decides whether the attribute is allowed to exist at
# all. Anything let through here still needs its *value* constrained
# separately (_clean_style below), or a draft could carry
# `style="position:fixed"` or a `url(javascript:...)` background straight
# through. Splitting on ";" first (rather than one combined regex trying to
# match "prop: value;prop: value") avoids a subtle bug where a value
# containing a comma (e.g. `font-family: Georgia, serif`) could break the
# boundary between declarations.
_STYLE_ALLOWED_PROPS = {"color", "background-color", "font-family", "font-size", "line-height"}
_STYLE_DECLARATION_PATTERN = re.compile(r"^([a-zA-Z-]+)\s*:\s*(.+)$")
# Values are free-form (font stacks need commas/quotes/spaces) but must not
# smuggle in another CSS mechanism — url(), expression(), nested comments,
# or a scheme like javascript:.
_STYLE_VALUE_BLOCKLIST = re.compile(r"url\(|expression\(|javascript:|/\*|\\", re.IGNORECASE)


def _clean_style(html_str: str) -> str:
    """Rewrites every style="..." attribute down to just the known-safe
    declarations it contains, dropping anything else (position,
    background-image/url(), etc.)."""
    def repl(match: "re.Match[str]") -> str:
        # The attribute value nh3 hands back is still HTML-entity-encoded
        # (e.g. a quoted font stack becomes `&quot;Times New Roman&quot;`),
        # and that entity's own trailing `;` would otherwise be mistaken
        # for a declaration separator — decode first, split/filter on the
        # real characters, then re-encode once at the end.
        style_value = html.unescape(match.group(1))
        kept = []
        for declaration in style_value.split(";"):
            declaration = declaration.strip()
            if not declaration:
                continue
            m = _STYLE_DECLARATION_PATTERN.match(declaration)
            if not m:
                continue
            prop, value = m.group(1).lower(), m.group(2).strip()
            if prop not in _STYLE_ALLOWED_PROPS:
                continue
            if _STYLE_VALUE_BLOCKLIST.search(value):
                continue
            kept.append(f"{prop}: {value}")
        if not kept:
            return ""
        return f' style="{html.escape("; ".join(kept), quote=True)}"'

    return re.sub(r'\sstyle="([^"]*)"', repl, html_str)


# tiptap-extension-resize-image stores an image's resized dimensions in
# plain containerstyle/wrapperstyle HTML attributes (not the standard
# style="..." attribute _clean_style handles above), generated only from a
# fixed, known set of CSS properties — see StyleManager.getContainerStyle /
# getWrapperStyle in that package. Same value-blocklist approach as
# _clean_style, just a different (smaller) allowed-property set and a
# plain-attribute rather than style="" syntax.
_IMG_STYLE_ATTR_ALLOWED_PROPS = {"width", "height", "cursor", "display", "float", "padding-right", "margin"}


def _clean_img_style_attrs(html_str: str) -> str:
    def repl(match: "re.Match[str]") -> str:
        attr_name = match.group(1)
        raw_value = html.unescape(match.group(2))
        kept = []
        for declaration in raw_value.split(";"):
            declaration = declaration.strip()
            if not declaration:
                continue
            m = _STYLE_DECLARATION_PATTERN.match(declaration)
            if not m:
                continue
            prop, value = m.group(1).lower(), m.group(2).strip()
            if prop not in _IMG_STYLE_ATTR_ALLOWED_PROPS:
                continue
            if _STYLE_VALUE_BLOCKLIST.search(value):
                continue
            kept.append(f"{prop}: {value}")
        if not kept:
            return ""
        return f' {attr_name}="{html.escape("; ".join(kept), quote=True)}"'

    return re.sub(r'\s(containerstyle|wrapperstyle)="([^"]*)"', repl, html_str)


# Uploaded drafting images are only ever served from this one path (see
# documents/views.py:upload_draft_image) — anything else in an <img src=...>
# (an external URL, a data: URI, a javascript: scheme) is dropped rather
# than trusted, since <img> tags render unescaped via |safe in every viewer.
_ALLOWED_IMG_SRC_PREFIX = "/media/draft_images/"
_IMG_TAG_PATTERN = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_SRC_PATTERN = re.compile(r'src="([^"]*)"')


def _strip_untrusted_images(html: str) -> str:
    def repl(match: "re.Match[str]") -> str:
        tag = match.group(0)
        src_match = _IMG_SRC_PATTERN.search(tag)
        if not src_match or not src_match.group(1).startswith(_ALLOWED_IMG_SRC_PREFIX):
            return ""
        return tag

    return _IMG_TAG_PATTERN.sub(repl, html)


def sanitize_document_html(html: str) -> str:
    """Strip everything except the drafting editor's known output tags,
    with a narrow style-property allowlist and images restricted to ones
    this app itself hosts."""
    if not html:
        return ""
    cleaned = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        clean_content_tags={"script", "style"},
        link_rel=None,
    )
    cleaned = _clean_style(cleaned)
    cleaned = _clean_img_style_attrs(cleaned)
    return _strip_untrusted_images(cleaned)
