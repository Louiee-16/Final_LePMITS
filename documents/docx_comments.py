"""Stopgap workaround for a Casual Docs (@casualoffice/docs, currently
pinned at 1.4.2 — confirmed the latest available version on npm) comment
round-trip gap.

Comments added via its addComment API (see frontend/casualdocs-entry.jsx's
live inline similarity check, and documents/views.py's
_run_similarity_check_on_docx, which adds the same kind of comment via
python-docx after a save) don't survive the document being reopened.
Confirmed directly, twice, against a real saved file: (1) reopening a
draft with existing comments shows the highlighted range but an empty
"No comments yet" panel — the comment *anchors*
(commentRangeStart/commentRangeEnd/commentReference in document.xml) come
through, but the comment *content* doesn't get reconstructed into the
editor's live session; (2) exporting straight from that same reopened
session confirms it — the downloaded .docx still has the highlight but
the comment itself is gone. So the loss isn't cosmetic: the very next
save (autosave included, now that saves are debounced — see
AUTOSAVE_DEBOUNCE_MS in frontend/casualdocs-entry.jsx) overwrites the
working .docx with a version that's permanently missing those comments.

No documented fix or tracked issue exists for this (checked
casualoffice.org's docs/SDK reference/changelog and the
github.com/CasualOffice/docs issue tracker directly — nothing there
addresses comment import/round-trip; addComment itself is documented only
in passing as one of several "document-agent" helpers, with no round-trip
guarantee stated either way).

This module is a targeted stopgap, not a real fix: right before a save
overwrites the working .docx, restore_missing_comments() restores any
comment whose anchor markers survived into the *new* file but whose
comment body didn't, by copying the matching comment back in from the
version already on disk. It's deliberately conservative — it only
restores a comment when the new document's own anchor markers still
reference the *exact same* comment ID the old file used, and does
nothing at all otherwise, so it can never make a save worse than not
having this function, only better in the specific scenario it targets.

The real fix is moving similarity flags to a database-backed panel that
doesn't depend on Casual Docs' own comment round-trip at all — see
HANDOFF_NOTES.md.
"""
from __future__ import annotations

import io
import logging
import os
import zipfile
from copy import deepcopy

from lxml import etree

logger = logging.getLogger(__name__)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
_W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
_W16CID_NS = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
_W16CEX_NS = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _w(tag):
    return f"{{{_W_NS}}}{tag}"


def _w14(tag):
    return f"{{{_W14_NS}}}{tag}"


def _w15(tag):
    return f"{{{_W15_NS}}}{tag}"


def _w16cid(tag):
    return f"{{{_W16CID_NS}}}{tag}"


def _w16cex(tag):
    return f"{{{_W16CEX_NS}}}{tag}"


_COMMENTS_XML = "word/comments.xml"
_COMMENTS_EXTENDED_XML = "word/commentsExtended.xml"
_COMMENTS_IDS_XML = "word/commentsIds.xml"
_COMMENTS_EXTENSIBLE_XML = "word/commentsExtensible.xml"
_COMMENT_PART_NAMES = (
    _COMMENTS_XML,
    _COMMENTS_EXTENDED_XML,
    _COMMENTS_IDS_XML,
    _COMMENTS_EXTENSIBLE_XML,
)

# Confirmed directly against a real Casual Docs-saved .docx (see this
# module's docstring) — not assumed from the OOXML spec alone, since a
# wrong content-type/relationship-type string here would silently produce
# a package Word itself might refuse to open cleanly.
_PART_CONTENT_TYPES = {
    _COMMENTS_XML: "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
    _COMMENTS_EXTENDED_XML: "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml",
    _COMMENTS_IDS_XML: "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml",
    _COMMENTS_EXTENSIBLE_XML: "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtensible+xml",
}
_PART_RELATIONSHIP_TYPES = {
    _COMMENTS_XML: "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
    _COMMENTS_EXTENDED_XML: "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
    _COMMENTS_IDS_XML: "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
    _COMMENTS_EXTENSIBLE_XML: "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible",
}


def _read_part(zf: zipfile.ZipFile, name: str):
    try:
        return zf.read(name)
    except KeyError:
        return None


def _anchor_comment_ids(document_xml: bytes) -> set:
    """Comment IDs the document body still anchors to via
    commentRangeStart/commentRangeEnd/commentReference — present
    regardless of whether that comment's own content survived."""
    root = etree.fromstring(document_xml)
    ids = set()
    for tag in ("commentRangeStart", "commentRangeEnd", "commentReference"):
        for el in root.iter(_w(tag)):
            wid = el.get(_w("id"))
            if wid is not None:
                ids.add(wid)
    return ids


def _comment_body_ids(comments_xml: bytes) -> set:
    root = etree.fromstring(comments_xml)
    return {
        el.get(_w("id"))
        for el in root.findall(_w("comment"))
        if el.get(_w("id")) is not None
    }


def restore_missing_comments(existing_docx_path: str, new_docx_bytes: bytes) -> bytes:
    """Returns new_docx_bytes, possibly with comments restored from
    existing_docx_path where the new document still anchors a comment
    (its highlight survived) but the comment body itself is missing.

    Pure no-op (returns new_docx_bytes untouched) whenever there's
    nothing to restore, including: no existing file yet, the existing
    file has no comments, or — the key safety property — the new
    document's own anchor IDs don't overlap with the old file's at all
    (which is also what happens harmlessly if Casual Docs ever starts
    renumbering these IDs on export; this function would just stop
    finding anything to restore rather than restoring something wrong).

    Raises on any structural problem (corrupt zip, unexpected XML shape)
    rather than guessing — the caller (documents/wopi.py::casualdocs_save)
    treats this as non-blocking and falls back to the original bytes.
    """
    if not os.path.exists(existing_docx_path):
        return new_docx_bytes

    with open(existing_docx_path, "rb") as f:
        old_bytes = f.read()

    with zipfile.ZipFile(io.BytesIO(old_bytes)) as old_zip:
        old_comments_xml = _read_part(old_zip, _COMMENTS_XML)
        if not old_comments_xml:
            return new_docx_bytes  # nothing to protect

        old_document_xml = _read_part(old_zip, "word/document.xml")
        old_anchor_ids = _anchor_comment_ids(old_document_xml) if old_document_xml else set()

    with zipfile.ZipFile(io.BytesIO(new_docx_bytes)) as new_zip:
        new_document_xml = _read_part(new_zip, "word/document.xml")
        new_anchor_ids = _anchor_comment_ids(new_document_xml) if new_document_xml else set()
        new_comments_xml = _read_part(new_zip, _COMMENTS_XML)
        new_body_ids = _comment_body_ids(new_comments_xml) if new_comments_xml else set()

    ids_to_restore = (new_anchor_ids & old_anchor_ids) - new_body_ids
    if not ids_to_restore:
        return new_docx_bytes

    logger.info(
        "Restoring %d comment(s) Casual Docs' reimport dropped: ids=%s",
        len(ids_to_restore), sorted(ids_to_restore),
    )
    return _inject_comments(new_docx_bytes, old_bytes, ids_to_restore)


def _paraids_for_comment_ids(comments_root, comment_ids: set) -> dict:
    """Maps each restored w:comment's own w:id to the w14:paraId of its
    (single) child <w:p> — the cross-reference key commentsExtended.xml/
    commentsIds.xml actually use, which is a different value from the
    comment's own w:id (confirmed directly — see this module's
    docstring)."""
    result = {}
    for comment_el in comments_root.findall(_w("comment")):
        wid = comment_el.get(_w("id"))
        if wid not in comment_ids:
            continue
        p_el = comment_el.find(_w("p"))
        para_id = p_el.get(_w14("paraId")) if p_el is not None else None
        if para_id:
            result[wid] = para_id
    return result


def _next_relationship_id(rels_root) -> str:
    """A fresh, non-colliding rId for a relationship being added to an
    existing .rels file — never reuses an rId straight from the old
    package, since the new package's own existing rIds may already use
    the same numbers for something unrelated."""
    max_n = 0
    for rel in rels_root.findall(f"{{{_RELS_NS}}}Relationship"):
        rid = rel.get("Id") or ""
        if rid.startswith("rId") and rid[3:].isdigit():
            max_n = max(max_n, int(rid[3:]))
    return f"rId{max_n + 1}"


def _inject_comments(new_docx_bytes: bytes, old_docx_bytes: bytes, ids_to_restore: set) -> bytes:
    with zipfile.ZipFile(io.BytesIO(old_docx_bytes)) as old_zip:
        old_parts = {name: _read_part(old_zip, name) for name in _COMMENT_PART_NAMES}
        old_comments_root = etree.fromstring(old_parts[_COMMENTS_XML])
        old_para_ids = _paraids_for_comment_ids(old_comments_root, ids_to_restore)

    restored_comment_elements = [
        deepcopy(el)
        for el in old_comments_root.findall(_w("comment"))
        if el.get(_w("id")) in ids_to_restore
    ]

    # commentsExtended.xml/commentsIds.xml key off the comment's own
    # paragraph paraId, not its w:id — and commentsExtensible.xml keys
    # off commentsIds.xml's durableId in turn. Missing any of the three
    # is fine (older/simpler docx producers may omit them entirely) —
    # only comments.xml itself is required for the comment to actually
    # show its content.
    wanted_para_ids = set(old_para_ids.values())
    restored_extended, restored_ids, restored_extensible = [], [], []
    durable_ids_wanted = set()

    if old_parts[_COMMENTS_EXTENDED_XML]:
        root = etree.fromstring(old_parts[_COMMENTS_EXTENDED_XML])
        for el in root.findall(_w15("commentEx")):
            if el.get(_w15("paraId")) in wanted_para_ids:
                restored_extended.append(deepcopy(el))

    if old_parts[_COMMENTS_IDS_XML]:
        root = etree.fromstring(old_parts[_COMMENTS_IDS_XML])
        for el in root.findall(_w16cid("commentId")):
            if el.get(_w16cid("paraId")) in wanted_para_ids:
                restored_ids.append(deepcopy(el))
                durable_id = el.get(_w16cid("durableId"))
                if durable_id:
                    durable_ids_wanted.add(durable_id)

    if old_parts[_COMMENTS_EXTENSIBLE_XML] and durable_ids_wanted:
        root = etree.fromstring(old_parts[_COMMENTS_EXTENSIBLE_XML])
        for el in root.findall(_w16cex("commentExtensible")):
            if el.get(_w16cex("durableId")) in durable_ids_wanted:
                restored_extensible.append(deepcopy(el))

    restored_by_part = {
        _COMMENTS_XML: restored_comment_elements,
        _COMMENTS_EXTENDED_XML: restored_extended,
        _COMMENTS_IDS_XML: restored_ids,
        _COMMENTS_EXTENSIBLE_XML: restored_extensible,
    }

    with zipfile.ZipFile(io.BytesIO(new_docx_bytes)) as new_zip:
        existing_names = set(new_zip.namelist())
        new_part_bytes = {}
        newly_added_parts = []

        for part_name, restored_elements in restored_by_part.items():
            if not restored_elements:
                continue
            existing_bytes = _read_part(new_zip, part_name)
            if existing_bytes is not None:
                root = etree.fromstring(existing_bytes)
            else:
                # Part doesn't exist in the new package at all yet —
                # build a fresh one from the old part's own root (same
                # tag + full namespace declarations), just emptied out.
                old_root = etree.fromstring(old_parts[part_name])
                root = deepcopy(old_root)
                for child in list(root):
                    root.remove(child)
                newly_added_parts.append(part_name)
            for el in restored_elements:
                root.append(el)
            new_part_bytes[part_name] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

        content_types_bytes = _read_part(new_zip, "[Content_Types].xml")
        rels_bytes = _read_part(new_zip, "word/_rels/document.xml.rels")
        if newly_added_parts and content_types_bytes and rels_bytes:
            ct_root = etree.fromstring(content_types_bytes)
            for part_name in newly_added_parts:
                override = etree.SubElement(ct_root, f"{{{_CT_NS}}}Override")
                override.set("PartName", "/" + part_name)
                override.set("ContentType", _PART_CONTENT_TYPES[part_name])
            new_part_bytes["[Content_Types].xml"] = etree.tostring(
                ct_root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

            rels_root = etree.fromstring(rels_bytes)
            for part_name in newly_added_parts:
                rel = etree.SubElement(rels_root, f"{{{_RELS_NS}}}Relationship")
                rel.set("Id", _next_relationship_id(rels_root))
                rel.set("Type", _PART_RELATIONSHIP_TYPES[part_name])
                rel.set("Target", os.path.basename(part_name))
            new_part_bytes["word/_rels/document.xml.rels"] = etree.tostring(
                rels_root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out_zip:
            for item in new_zip.infolist():
                data = new_part_bytes.pop(item.filename, None)
                out_zip.writestr(item, data if data is not None else new_zip.read(item.filename))
            # Any part that's brand new to this package (didn't exist in
            # new_zip.infolist() at all) still needs writing.
            for part_name, data in new_part_bytes.items():
                out_zip.writestr(part_name, data)

    return out_buf.getvalue()


# ---------------------------------------------------------------------------
# Stripping AI inline-check comments at filing time.
#
# Two code paths add these, with two different literal author strings —
# both need to be caught:
#   - Casual Docs live inline check, added client-side via
#     editorApi.addComment (frontend/casualdocs-entry.jsx's
#     runInlineCheck()): author "Similarity Check (AI)".
#   - The background post-save check, added server-side via python-docx
#     (documents/views.py's _run_similarity_check_on_docx): author
#     "LePMITS AI".
# These are scratch flags for the councilor while drafting, not meant to
# follow the document past filing — see create_draft's submit branch.
#
# Also strips ORPHANED anchors — commentRangeStart/commentRangeEnd/
# commentReference markers in document.xml with no matching <w:comment> in
# comments.xml at all (not just a different author). Confirmed directly
# against a real filed document: this happens when the same Casual Docs
# reimport bug restore_missing_comments() works around (see this module's
# top docstring) fires again on a LATER save, after restore_missing_comments
# already ran out of a same-session chance to fix it — the comment body
# never makes it back into comments.xml, but the anchor (and the highlight
# Casual Docs renders from it) survives untouched in the document model, so
# it sails straight through strip_ai_comments_from_file's author check
# (nothing there to match an author against) and out into the filed
# document. Safe to strip unconditionally: every comment in this app is
# added programmatically by the AI features (see above) — there is no
# human "add comment" UI anywhere, so an anchor with no body left dangling
# can never be a reviewer's real comment.
# ---------------------------------------------------------------------------

AI_COMMENT_AUTHORS = frozenset({"Similarity Check (AI)", "LePMITS AI"})


def _remove_comment_anchors(document_root, ids_to_remove: set) -> None:
    for tag in ("commentRangeStart", "commentRangeEnd"):
        for el in list(document_root.iter(_w(tag))):
            if el.get(_w("id")) in ids_to_remove and el.getparent() is not None:
                el.getparent().remove(el)
    # commentReference sits inside its own <w:r> (Casual Docs and Word
    # both emit it as that run's only content) — drop the whole run
    # rather than just the reference marker, so no empty run is left
    # behind in the paragraph.
    for el in list(document_root.iter(_w("commentReference"))):
        if el.get(_w("id")) not in ids_to_remove:
            continue
        run = el.getparent()
        if run is not None and run.tag == _w("r") and run.getparent() is not None:
            run.getparent().remove(run)
        elif el.getparent() is not None:
            el.getparent().remove(el)


def strip_comments_by_author(docx_bytes: bytes, authors=AI_COMMENT_AUTHORS):
    """Returns (new_docx_bytes, removed_count): docx_bytes with every
    comment whose w:author is in `authors` fully removed — the anchor
    markers in document.xml, the comment body in comments.xml, and its
    cross-referenced entries in commentsExtended.xml/commentsIds.xml/
    commentsExtensible.xml — plus any ORPHANED anchor (a
    commentRangeStart/commentRangeEnd/commentReference in document.xml
    with no matching comment body anywhere, author unknown because there's
    nothing left to check it against — see this section's module-level
    comment for why that's still safe to remove here). Any other comment
    (e.g. a human reviewer's) is left untouched.

    Pure no-op (returns docx_bytes unchanged, removed_count 0) if
    document.xml has no comment anchors and there's nothing by a matching
    author in comments.xml either.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        document_xml = _read_part(zf, "word/document.xml")
        anchor_ids = _anchor_comment_ids(document_xml) if document_xml else set()

        comments_xml = _read_part(zf, _COMMENTS_XML)
        comments_root = etree.fromstring(comments_xml) if comments_xml else None

        author_matched_ids = set()
        body_ids = set()
        if comments_root is not None:
            for el in comments_root.findall(_w("comment")):
                cid = el.get(_w("id"))
                if cid is None:
                    continue
                body_ids.add(cid)
                if el.get(_w("author")) in authors:
                    author_matched_ids.add(cid)

        orphaned_ids = anchor_ids - body_ids
        ids_to_remove = author_matched_ids | orphaned_ids
        if not ids_to_remove:
            return docx_bytes, 0

        new_part_bytes = {}
        wanted_para_ids = set()
        if comments_root is not None:
            wanted_para_ids = set(_paraids_for_comment_ids(comments_root, ids_to_remove).values())
            for el in list(comments_root.findall(_w("comment"))):
                if el.get(_w("id")) in ids_to_remove:
                    comments_root.remove(el)
            new_part_bytes[_COMMENTS_XML] = etree.tostring(
                comments_root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

        if document_xml:
            document_root = etree.fromstring(document_xml)
            _remove_comment_anchors(document_root, ids_to_remove)
            new_part_bytes["word/document.xml"] = etree.tostring(
                document_root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

        if wanted_para_ids:
            ext_bytes = _read_part(zf, _COMMENTS_EXTENDED_XML)
            if ext_bytes:
                ext_root = etree.fromstring(ext_bytes)
                for el in list(ext_root.findall(_w15("commentEx"))):
                    if el.get(_w15("paraId")) in wanted_para_ids:
                        ext_root.remove(el)
                new_part_bytes[_COMMENTS_EXTENDED_XML] = etree.tostring(
                    ext_root, xml_declaration=True, encoding="UTF-8", standalone=True
                )

            durable_ids_removed = set()
            ids_bytes = _read_part(zf, _COMMENTS_IDS_XML)
            if ids_bytes:
                ids_root = etree.fromstring(ids_bytes)
                for el in list(ids_root.findall(_w16cid("commentId"))):
                    if el.get(_w16cid("paraId")) in wanted_para_ids:
                        durable_id = el.get(_w16cid("durableId"))
                        if durable_id:
                            durable_ids_removed.add(durable_id)
                        ids_root.remove(el)
                new_part_bytes[_COMMENTS_IDS_XML] = etree.tostring(
                    ids_root, xml_declaration=True, encoding="UTF-8", standalone=True
                )

            if durable_ids_removed:
                cex_bytes = _read_part(zf, _COMMENTS_EXTENSIBLE_XML)
                if cex_bytes:
                    cex_root = etree.fromstring(cex_bytes)
                    for el in list(cex_root.findall(_w16cex("commentExtensible"))):
                        if el.get(_w16cex("durableId")) in durable_ids_removed:
                            cex_root.remove(el)
                    new_part_bytes[_COMMENTS_EXTENSIBLE_XML] = etree.tostring(
                        cex_root, xml_declaration=True, encoding="UTF-8", standalone=True
                    )

        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out_zip:
            for item in zf.infolist():
                data = new_part_bytes.pop(item.filename, None)
                out_zip.writestr(item, data if data is not None else zf.read(item.filename))

    return out_buf.getvalue(), len(ids_to_remove)


def strip_ai_comments_from_file(docx_path: str) -> int:
    """In-place strip_comments_by_author for a file already on disk —
    call at filing time (documents/views.py::create_draft) so the AI
    inline-check's own scratch comments never survive into the filed
    document. Returns the number of comments removed; leaves the file
    untouched and returns 0 if there was nothing to strip.

    Deliberately fails loudly on a structural problem (corrupt zip,
    unexpected XML shape) rather than silently leaving AI comments in a
    filed document — unlike restore_missing_comments, this isn't a
    best-effort convenience the caller can shrug off; the caller should
    let this propagate so filing surfaces the failure instead of quietly
    filing a document that still has AI review comments in it.
    """
    if not os.path.exists(docx_path):
        return 0
    with open(docx_path, "rb") as f:
        original = f.read()
    stripped, removed_count = strip_comments_by_author(original)
    if not removed_count:
        return 0
    with open(docx_path, "wb") as f:
        f.write(stripped)
    logger.info("Stripped %d AI inline-check comment(s) from %s at filing", removed_count, docx_path)
    return removed_count
