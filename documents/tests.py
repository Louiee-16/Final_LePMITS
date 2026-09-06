"""
Regression tests for the document editing/viewing security fixes made
2026-09-01 (see HANDOFF_NOTES.md):
  - IDOR on documents/wopi.py's casualdocs_document_bytes/casualdocs_save/
    similarity_check (no permission check at all beyond @login_required).
  - casualdocs_save rejecting real-sized .docx saves because
    DATA_UPLOAD_MAX_MEMORY_SIZE was left at Django's 2.5MB default.
  - ai_inline_check being unnecessarily @csrf_exempt.
  - Ambiguous (absent, not zero) docDefaults/Normal-style paragraph
    spacing in Casual Docs' own saved .docx making the same content
    render with visibly more spacing once reopened than it had while
    being typed — see documents/wopi.py::_normalize_docx_default_spacing.
  - Comments (inline check / post-save similarity check) vanishing from
    Casual Docs' live session on reopen, and the next save permanently
    erasing them from the stored .docx as a result — see
    documents/docx_comments.py and its own dedicated test module,
    documents/test_docx_comments.py, for the restoration logic itself;
    this file just covers the end-to-end wiring through casualdocs_save.
  - sanitize_document_html's allowlist (pre-existing behavior, not part of
    this round of fixes, but previously untested).

This was previously the stock 3-line TestCase stub — the app had zero
test coverage for any of this.
"""
import io
import os
import tempfile
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from docx import Document as DocxDocument

from committees.models import Committee
from documents.models import Document, DocumentChunk, LegacyDocument
from documents.rag.embedder import embed_document_chunks
from documents.sanitize import sanitize_document_html
from documents.test_docx_comments import _build_docx, _comment_texts
from documents.views import _can_edit_document, _can_view_document, _onlyoffice_saved_docx_path, _page_ocr_confidence
from documents.wopi import _normalize_docx_default_spacing

User = get_user_model()


def make_user(username, role, password='TestPass!2345'):
    return User.objects.create_user(username=username, password=password, role=role)


def make_document(author, status='DRAFT', **extra):
    defaults = dict(title='Test Measure', author=author, content='', status=status)
    defaults.update(extra)
    return Document.objects.create(**defaults)


# ---------------------------------------------------------------------------
# _can_view_document / _can_edit_document — the actual authorization policy
# for every document-editing/viewing surface in the app.
# ---------------------------------------------------------------------------
class PermissionHelperTests(TestCase):
    def setUp(self):
        self.author = make_user('phelper_author', 'COUNCILOR')
        self.other_councilor = make_user('phelper_other', 'COUNCILOR')
        self.secretariat = make_user('phelper_secretariat', 'SECRETARIAT')
        self.staff = make_user('phelper_staff', 'STAFF')
        self.admin = make_user('phelper_admin', 'ADMIN')

    def test_draft_viewable_only_by_author_or_office(self):
        doc = make_document(self.author, status='DRAFT')
        self.assertTrue(_can_view_document(self.author, doc))
        self.assertTrue(_can_view_document(self.secretariat, doc))
        self.assertTrue(_can_view_document(self.staff, doc))
        self.assertTrue(_can_view_document(self.admin, doc))
        self.assertFalse(_can_view_document(self.other_councilor, doc))

    def test_filed_document_viewable_by_anyone_logged_in(self):
        doc = make_document(self.author, status='FILED')
        self.assertTrue(_can_view_document(self.other_councilor, doc))

    def test_draft_editable_only_by_author_not_office(self):
        doc = make_document(self.author, status='DRAFT')
        self.assertTrue(_can_edit_document(self.author, doc))
        self.assertFalse(_can_edit_document(self.other_councilor, doc))
        # Secretariat/staff can *view* a councilor's draft (e.g. once
        # returned with a reason) but that's not the same as being allowed
        # to push edits into someone else's personal draft.
        self.assertFalse(_can_edit_document(self.secretariat, doc))

    def test_ghost_editable_only_by_author(self):
        doc = make_document(self.author, status='GHOST')
        self.assertTrue(_can_edit_document(self.author, doc))
        self.assertFalse(_can_edit_document(self.secretariat, doc))

    def test_second_reading_editable_by_office_not_author(self):
        doc = make_document(self.author, status='SECOND_READING')
        self.assertTrue(_can_edit_document(self.secretariat, doc))
        self.assertTrue(_can_edit_document(self.staff, doc))
        self.assertTrue(_can_edit_document(self.admin, doc))
        self.assertFalse(_can_edit_document(self.author, doc))
        self.assertFalse(_can_edit_document(self.other_councilor, doc))

    def test_committee_status_variants_editable_by_office(self):
        # return_to_committee sets dynamic labels like 'COMMITTEE [2]'.
        for status in ('REFERRED', 'COMMITTEE', 'COMMITTEE [2]'):
            with self.subTest(status=status):
                doc = make_document(self.author, status=status)
                self.assertTrue(_can_edit_document(self.staff, doc))
                self.assertFalse(_can_edit_document(self.author, doc))

    def test_non_editable_stages_reject_everyone(self):
        # No page in the app wires a saveUrl for these statuses — the
        # editor endpoints themselves should refuse them too, regardless
        # of who's asking.
        for status in ('FILED', 'FIRST_READING', 'THIRD_READING', 'APPROVED'):
            with self.subTest(status=status):
                doc = make_document(self.author, status=status)
                self.assertFalse(_can_edit_document(self.author, doc))
                self.assertFalse(_can_edit_document(self.secretariat, doc))
                self.assertFalse(_can_edit_document(self.admin, doc))


# ---------------------------------------------------------------------------
# documents/wopi.py endpoints — IDOR fix.
# ---------------------------------------------------------------------------
@override_settings()
class WopiEndpointPermissionTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='lepmits_test_docx_')
        self.override = override_settings(DOCUMENT_EDITOR_STORAGE_ROOT=self._tmp)
        self.override.enable()
        self.addCleanup(self.override.disable)

        self.author = make_user('wopi_author', 'COUNCILOR')
        self.other_councilor = make_user('wopi_other', 'COUNCILOR')
        self.secretariat = make_user('wopi_secretariat', 'SECRETARIAT')

    def _client_for(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_file_bytes_blocked_for_non_owner_of_draft(self):
        doc = make_document(self.author, status='DRAFT')
        resp = self._client_for(self.other_councilor).get(
            reverse('casualdocs-file', args=[doc.id])
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json().get('error'), 'not_found')

    def test_file_bytes_allowed_for_owner(self):
        doc = make_document(self.author, status='DRAFT')
        resp = self._client_for(self.author).get(
            reverse('casualdocs-file', args=[doc.id])
        )
        # No docx saved yet for a brand-new draft -> the distinct
        # "no_docx" 404 (not a permission failure) — see
        # documents/wopi.py's NoDocxYet.
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json().get('error'), 'no_docx')

    def test_save_blocked_for_non_owner_of_draft(self):
        doc = make_document(self.author, status='DRAFT')
        resp = self._client_for(self.other_councilor).post(
            reverse('casualdocs-save', args=[doc.id]),
            data=b'fake docx bytes',
            content_type='application/octet-stream',
        )
        self.assertEqual(resp.status_code, 403)
        path = os.path.join(self._tmp, f'doc_{doc.id}.docx')
        self.assertFalse(os.path.exists(path), "non-owner's write must not land on disk")

    def test_save_allowed_for_owner_of_draft(self):
        doc = make_document(self.author, status='DRAFT')
        resp = self._client_for(self.author).post(
            reverse('casualdocs-save', args=[doc.id]),
            data=b'fake docx bytes',
            content_type='application/octet-stream',
        )
        self.assertEqual(resp.status_code, 200)
        path = os.path.join(self._tmp, f'doc_{doc.id}.docx')
        self.assertTrue(os.path.exists(path))
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), b'fake docx bytes')

    def test_save_returns_docx_mtime_as_saved_at(self):
        # doc.save() is deliberately never called here (see
        # casualdocs_save's docstring) — the docx file's own on-disk mtime
        # is what a "saved" indicator in amending_table.html should read
        # instead, since Document.updated_at doesn't move on a live save.
        import datetime
        doc = make_document(self.author, status='DRAFT')
        resp = self._client_for(self.author).post(
            reverse('casualdocs-save', args=[doc.id]),
            data=b'fake docx bytes',
            content_type='application/octet-stream',
        )
        body = resp.json()
        self.assertEqual(body.get('error'), 0)
        self.assertIn('saved_at', body)
        # Parses as a real, recent timestamp.
        saved_at = datetime.datetime.fromisoformat(body['saved_at'])
        age = datetime.datetime.now(datetime.timezone.utc) - saved_at
        self.assertLess(age.total_seconds(), 30)

    def test_save_blocked_for_secretariat_on_someone_elses_draft(self):
        # Secretariat can *view* a DRAFT (e.g. after a return-with-reason),
        # but that's not edit access to a councilor's personal draft.
        doc = make_document(self.author, status='DRAFT')
        resp = self._client_for(self.secretariat).post(
            reverse('casualdocs-save', args=[doc.id]),
            data=b'fake docx bytes',
            content_type='application/octet-stream',
        )
        self.assertEqual(resp.status_code, 403)

    def test_save_allowed_for_secretariat_on_second_reading(self):
        doc = make_document(self.author, status='SECOND_READING')
        resp = self._client_for(self.secretariat).post(
            reverse('casualdocs-save', args=[doc.id]),
            data=b'fake docx bytes',
            content_type='application/octet-stream',
        )
        self.assertEqual(resp.status_code, 200)

    def test_save_blocked_on_non_editable_stage_even_for_author(self):
        # The decision item from HANDOFF_NOTES.md: casualdocs_save had no
        # status guard at all — a round-trip test against an already-FILED
        # document previously succeeded with zero resistance.
        doc = make_document(self.author, status='APPROVED')
        resp = self._client_for(self.author).post(
            reverse('casualdocs-save', args=[doc.id]),
            data=b'fake docx bytes',
            content_type='application/octet-stream',
        )
        self.assertEqual(resp.status_code, 403)

    def test_save_normalizes_spacing_on_a_real_docx(self):
        # End-to-end version of DocxSpacingNormalizationTests — through the
        # actual endpoint, not just the helper function directly.
        doc = make_document(self.author, status='DRAFT')
        resp = self._client_for(self.author).post(
            reverse('casualdocs-save', args=[doc.id]),
            data=make_minimal_docx_bytes(),
            content_type='application/octet-stream',
        )
        self.assertEqual(resp.status_code, 200)
        path = os.path.join(self._tmp, f'doc_{doc.id}.docx')
        pf = DocxDocument(path).styles['Normal'].paragraph_format
        self.assertEqual(pf.space_after.pt, 0)

    def test_save_restores_comment_dropped_by_reimport(self):
        # End-to-end version of documents/test_docx_comments.py's core
        # case, through the real endpoint: a comment on disk, an incoming
        # save that kept the anchor but lost the comment body (exactly
        # what a reopened Casual Docs session produces — see
        # documents/docx_comments.py's docstring), and confirmation the
        # stored file ends up with the comment restored, not lost.
        doc = make_document(self.author, status='DRAFT')
        path = os.path.join(self._tmp, f'doc_{doc.id}.docx')
        with open(path, 'wb') as f:
            f.write(_build_docx(anchor_ids=['19'], comments={'19': 'Flagged passage.'}))

        resp = self._client_for(self.author).post(
            reverse('casualdocs-save', args=[doc.id]),
            data=_build_docx(anchor_ids=['19']),  # anchor kept, comment body gone
            content_type='application/octet-stream',
        )
        self.assertEqual(resp.status_code, 200)
        with open(path, 'rb') as f:
            saved_bytes = f.read()
        self.assertEqual(_comment_texts(saved_bytes).get('19'), 'Flagged passage.')

    def test_save_accepts_upload_over_the_old_2_5mb_default(self):
        # Regression test for the DATA_UPLOAD_MAX_MEMORY_SIZE issue: a
        # .docx with a couple of inserted images crosses Django's default
        # 2.5MB cap on request.body easily, which previously 400'd before
        # the view ever ran.
        doc = make_document(self.author, status='DRAFT')
        big_payload = b'x' * (3 * 1024 * 1024)  # 3MB > old 2.5MB default
        resp = self._client_for(self.author).post(
            reverse('casualdocs-save', args=[doc.id]),
            data=big_payload,
            content_type='application/octet-stream',
        )
        self.assertEqual(resp.status_code, 200)

    def test_similarity_check_blocked_for_non_editor(self):
        doc = make_document(self.author, status='DRAFT')
        resp = self._client_for(self.other_councilor).post(
            reverse('similarity-check', args=[doc.id])
        )
        self.assertEqual(resp.status_code, 403)

    def test_endpoints_require_login(self):
        doc = make_document(self.author, status='DRAFT')
        c = Client()
        for url in (
            reverse('casualdocs-file', args=[doc.id]),
            reverse('casualdocs-save', args=[doc.id]),
            reverse('similarity-check', args=[doc.id]),
        ):
            with self.subTest(url=url):
                resp = c.get(url)
                self.assertIn(resp.status_code, (302, 403))


# ---------------------------------------------------------------------------
# ai_inline_check — was @csrf_exempt for no functional reason (the frontend
# already sends X-CSRFToken; see frontend/casualdocs-entry.jsx).
# ---------------------------------------------------------------------------
class AiInlineCheckCsrfTests(TestCase):
    def test_view_is_not_csrf_exempt(self):
        from documents.views import ai_inline_check
        self.assertFalse(getattr(ai_inline_check, 'csrf_exempt', False))

    def test_post_without_csrf_token_is_rejected(self):
        user = make_user('inline_csrf_user', 'COUNCILOR')
        c = Client(enforce_csrf_checks=True)
        c.force_login(user)
        resp = c.post(
            reverse('ai_inline_check'),
            data='{"paragraphs": []}',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)


def make_minimal_docx_bytes(with_explicit_normal_spacing=False):
    """A from-scratch, real .docx built to have NO <w:spacing> anywhere —
    the same shape Casual Docs' own export actually has (confirmed
    directly against a real saved file, see
    _normalize_docx_default_spacing's docstring). python-docx's own blank
    Document() template is NOT a good stand-in for that on its own: it
    ships with docDefaults already carrying Word's real modern-default
    <w:spacing w:after="200" w:line="276" .../> (confirmed directly — see
    this function's own history) — the opposite of what needs testing
    here — so that's stripped out below to accurately simulate a document
    where the spacing is genuinely absent, not merely defaulted."""
    from docx.oxml.ns import qn

    doc = DocxDocument()
    doc.add_paragraph("Republic of the Philippines")

    styles_el = doc.styles.element
    ppr = styles_el.find(qn('w:docDefaults')).find(qn('w:pPrDefault')).find(qn('w:pPr'))
    spacing = ppr.find(qn('w:spacing')) if ppr is not None else None
    if spacing is not None:
        ppr.remove(spacing)

    if with_explicit_normal_spacing:
        from docx.shared import Pt
        pf = doc.styles["Normal"].paragraph_format
        pf.space_before = Pt(12)
        pf.space_after = Pt(12)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _normalize_docx_default_spacing — Casual Docs' own saved .docx has no
# <w:spacing> anywhere (docDefaults, Normal style, or per-paragraph), which
# resolves inconsistently across readers (tight while live-typing, visibly
# larger once reopened from the saved file) — see the function's docstring
# in documents/wopi.py for the full diagnosis.
# ---------------------------------------------------------------------------
class DocxSpacingNormalizationTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='lepmits_test_spacing_')

    def _write(self, docx_bytes):
        path = os.path.join(self._tmp, 'test.docx')
        with open(path, 'wb') as f:
            f.write(docx_bytes)
        return path

    def test_fills_in_missing_normal_style_spacing(self):
        path = self._write(make_minimal_docx_bytes())
        _normalize_docx_default_spacing(path)
        pf = DocxDocument(path).styles['Normal'].paragraph_format
        self.assertEqual(pf.space_before.pt, 0)
        self.assertEqual(pf.space_after.pt, 0)
        self.assertEqual(pf.line_spacing, 1.0)

    def test_does_not_override_an_explicit_normal_style_spacing(self):
        path = self._write(make_minimal_docx_bytes(with_explicit_normal_spacing=True))
        _normalize_docx_default_spacing(path)
        pf = DocxDocument(path).styles['Normal'].paragraph_format
        # Must be left exactly as the (hypothetical, intentional) author
        # set it — this function only fills genuine gaps, never overwrites.
        self.assertEqual(pf.space_before.pt, 12)
        self.assertEqual(pf.space_after.pt, 12)

    def test_fills_in_missing_doc_defaults_spacing(self):
        from docx.oxml.ns import qn
        path = self._write(make_minimal_docx_bytes())
        _normalize_docx_default_spacing(path)
        styles_el = DocxDocument(path).styles.element
        spacing = styles_el.find(qn('w:docDefaults')).find(qn('w:pPrDefault')).find(qn('w:pPr')).find(qn('w:spacing'))
        self.assertIsNotNone(spacing)
        self.assertEqual(spacing.get(qn('w:before')), '0')
        self.assertEqual(spacing.get(qn('w:after')), '0')

    def test_is_idempotent(self):
        path = self._write(make_minimal_docx_bytes())
        _normalize_docx_default_spacing(path)
        _normalize_docx_default_spacing(path)  # must not raise or double-add
        pf = DocxDocument(path).styles['Normal'].paragraph_format
        self.assertEqual(pf.space_after.pt, 0)


# ---------------------------------------------------------------------------
# sanitize_document_html — pre-existing allowlist, previously untested.
# ---------------------------------------------------------------------------
class SanitizeDocumentHtmlTests(TestCase):
    def test_script_tags_are_stripped(self):
        out = sanitize_document_html('<p>hi</p><script>alert(1)</script>')
        self.assertNotIn('<script', out)
        self.assertNotIn('alert', out)

    def test_event_handler_attributes_are_stripped(self):
        out = sanitize_document_html('<p onclick="alert(1)">hi</p>')
        self.assertNotIn('onclick', out)

    def test_disallowed_style_properties_are_dropped(self):
        out = sanitize_document_html('<p style="position:fixed;color:red">hi</p>')
        self.assertNotIn('position', out)
        self.assertIn('color: red', out)

    def test_style_url_scheme_is_blocked(self):
        out = sanitize_document_html(
            '<p style="background-color:url(javascript:alert(1))">hi</p>'
        )
        self.assertNotIn('url(', out)

    def test_image_outside_allowed_prefix_is_dropped(self):
        out = sanitize_document_html('<img src="https://evil.example/x.png">')
        self.assertNotIn('<img', out)

    def test_image_under_draft_images_prefix_is_kept(self):
        out = sanitize_document_html('<img src="/media/draft_images/abc.png">')
        self.assertIn('<img', out)
        self.assertIn('/media/draft_images/abc.png', out)


# ---------------------------------------------------------------------------
# create_draft's filing checkpoint (action=submit) strips the AI inline
# check's own scratch comments (see documents/docx_comments.py's
# strip_comments_by_author) so they don't follow the document past filing.
# ---------------------------------------------------------------------------
class CreateDraftStripsAiCommentsAtFilingTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='lepmits_test_filing_')
        self.override = override_settings(DOCUMENT_EDITOR_STORAGE_ROOT=self._tmp)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.author = make_user('filing_author', 'COUNCILOR')

    def _client(self):
        c = Client()
        c.force_login(self.author)
        return c

    def _seed_docx(self, doc_id, author):
        path = _onlyoffice_saved_docx_path(doc_id)
        docx = _build_docx(
            anchor_ids=["19"],
            comments={"19": ("Possible similarity to X.", author)},
        )
        with open(path, 'wb') as f:
            f.write(docx)
        return path

    def test_filing_strips_ai_comment_from_working_docx(self):
        doc = make_document(self.author, status='DRAFT', title='Real Title', doc_type='ORDINANCE')
        path = self._seed_docx(doc.id, 'Similarity Check (AI)')

        resp = self._client().post(reverse('create_draft'), data={
            'doc_id': str(doc.id), 'action': 'submit',
            'title': 'Real Title', 'type': 'ORDINANCE',
        })

        self.assertEqual(resp.status_code, 302)
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'FILED')
        with open(path, 'rb') as f:
            self.assertEqual(_comment_texts(f.read()), {})

    def test_filing_strips_server_side_ai_author_too(self):
        doc = make_document(self.author, status='DRAFT', title='Real Title', doc_type='ORDINANCE')
        path = self._seed_docx(doc.id, 'LePMITS AI')

        self._client().post(reverse('create_draft'), data={
            'doc_id': str(doc.id), 'action': 'submit',
            'title': 'Real Title', 'type': 'ORDINANCE',
        })

        with open(path, 'rb') as f:
            self.assertEqual(_comment_texts(f.read()), {})

    def test_filing_leaves_non_ai_comment_untouched(self):
        doc = make_document(self.author, status='DRAFT', title='Real Title', doc_type='ORDINANCE')
        path = _onlyoffice_saved_docx_path(doc.id)
        docx = _build_docx(
            anchor_ids=["19", "20"],
            comments={
                "19": ("AI flag.", "Similarity Check (AI)"),
                "20": ("Human note.", "A Reviewer"),
            },
        )
        with open(path, 'wb') as f:
            f.write(docx)

        self._client().post(reverse('create_draft'), data={
            'doc_id': str(doc.id), 'action': 'submit',
            'title': 'Real Title', 'type': 'ORDINANCE',
        })

        with open(path, 'rb') as f:
            texts = _comment_texts(f.read())
        self.assertNotIn('19', texts)
        self.assertEqual(texts.get('20'), 'Human note.')


# ---------------------------------------------------------------------------
# move_to_third_reading requires having gone through Insert Amendments
# (save_amendments) at least once this SECOND_READING session first — see
# documents/views.py. amendment_status is explicitly reset to None whenever
# a document enters SECOND_READING, so None here means the amendment step
# was never opened/confirmed this session.
# ---------------------------------------------------------------------------
class MoveToThirdReadingAmendmentStatusTests(TestCase):
    """move_to_third_reading no longer blocks on amendment_status being
    unset — see documents/views.py's comment at that removed check for
    why (floor_amendments.html's own controls for setting it were
    removed in a UI simplification, making the old guard permanently
    unsatisfiable; this URL being reachable only from that page already
    gives the same guarantee). Still covers that the transition itself
    works regardless of amendment_status's value, and that it's cleared
    on the way through either way (matches the pre-existing housekeeping,
    unrelated to the removed guard)."""

    def setUp(self):
        self.secretariat = make_user('m2t_secretariat', 'SECRETARIAT')
        self.author = make_user('m2t_author', 'COUNCILOR')

    def _client(self):
        c = Client()
        c.force_login(self.secretariat)
        return c

    def test_allowed_when_amendment_status_untouched(self):
        doc = make_document(self.author, status='SECOND_READING', amendment_status=None)
        resp = self._client().post(reverse('move_to_third_reading', args=[doc.id]))
        doc.refresh_from_db()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(doc.status, 'THIRD_READING')

    def test_allowed_when_amendment_status_is_set(self):
        for status in ('IN_PROGRESS', 'FINALIZED', 'NO_AMENDMENTS'):
            with self.subTest(amendment_status=status):
                doc = make_document(self.author, status='SECOND_READING', amendment_status=status)
                resp = self._client().post(reverse('move_to_third_reading', args=[doc.id]))
                doc.refresh_from_db()
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(doc.status, 'THIRD_READING')

    def test_amendment_status_cleared_after_transition(self):
        doc = make_document(self.author, status='SECOND_READING', amendment_status='FINALIZED')
        self._client().post(reverse('move_to_third_reading', args=[doc.id]))
        doc.refresh_from_db()
        self.assertIsNone(doc.amendment_status)


# ---------------------------------------------------------------------------
# Several permission/method guards in the second-reading and committee-
# amendment views redirected to URL names that were never actually
# registered ('second_reading' instead of 'second-reading',
# 'floor-amendments' instead of 'floor_amendments') — a guaranteed
# NoReverseMatch/500 on every hit, not just a wrong destination. Confirms
# the fixed targets actually resolve.
# ---------------------------------------------------------------------------
class BrokenRedirectNameRegressionTests(TestCase):
    def setUp(self):
        self.author = make_user('redir_author', 'COUNCILOR')
        self.other_councilor = make_user('redir_other', 'COUNCILOR')

    def _client_for(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_floor_amendments_permission_denied_redirects(self):
        doc = make_document(self.author, status='SECOND_READING')
        resp = self._client_for(self.other_councilor).get(reverse('floor_amendments', args=[doc.id]))
        self.assertEqual(resp.status_code, 302)

    def test_save_amendments_permission_denied_redirects(self):
        doc = make_document(self.author, status='SECOND_READING')
        resp = self._client_for(self.other_councilor).post(reverse('save_amendments', args=[doc.id]))
        self.assertEqual(resp.status_code, 302)

    def test_save_amendments_non_post_redirects(self):
        doc = make_document(self.author, status='SECOND_READING')
        secretariat = make_user('redir_secretariat', 'SECRETARIAT')
        resp = self._client_for(secretariat).get(reverse('save_amendments', args=[doc.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('floor_amendments', args=[doc.id]), resp.url)

    def test_committee_amendments_permission_denied_redirects(self):
        doc = make_document(self.author, status='REFERRED', hearing_date='2026-08-01')
        resp = self._client_for(self.other_councilor).get(reverse('committee-amendments', args=[doc.id]))
        self.assertEqual(resp.status_code, 302)

    def test_save_committee_amendments_permission_denied_redirects(self):
        doc = make_document(self.author, status='REFERRED', hearing_date='2026-08-01')
        resp = self._client_for(self.other_councilor).post(reverse('save-committee-amendments', args=[doc.id]))
        self.assertEqual(resp.status_code, 302)


# ---------------------------------------------------------------------------
# assign_reference_number — the persistent per-(doc_type, year) counter that
# replaced the old "count existing rows in these statuses" approach (which
# collided the moment a document moved into a status the hardcoded list
# didn't include — see docs/activity-log.md). Also covers the trigger-point
# move: numbering now happens at move_to_first (First Reading), not filing.
# ---------------------------------------------------------------------------
class AssignReferenceNumberTests(TestCase):
    def setUp(self):
        self.author = make_user('refno_author', 'COUNCILOR')

    def test_format_has_no_zero_padding(self):
        from documents.views import assign_reference_number
        doc = make_document(self.author, status='FIRST_READING', doc_type='ORDINANCE')
        assign_reference_number(doc)
        self.assertRegex(doc.reference_no, r'^DO-1-\d{4}$')

    def test_resolution_uses_dr_prefix(self):
        from documents.views import assign_reference_number
        doc = make_document(self.author, status='FIRST_READING', doc_type='RESOLUTION')
        assign_reference_number(doc)
        self.assertRegex(doc.reference_no, r'^DR-1-\d{4}$')

    def test_sequential_within_same_doc_type_and_year(self):
        from documents.views import assign_reference_number
        doc1 = make_document(self.author, status='FIRST_READING', doc_type='ORDINANCE')
        doc2 = make_document(self.author, status='FIRST_READING', doc_type='ORDINANCE')
        assign_reference_number(doc1)
        assign_reference_number(doc2)
        n1 = int(doc1.reference_no.split('-')[1])
        n2 = int(doc2.reference_no.split('-')[1])
        self.assertEqual(n2, n1 + 1)

    def test_noop_when_already_assigned(self):
        from documents.views import assign_reference_number
        doc = make_document(self.author, status='FIRST_READING', doc_type='ORDINANCE', reference_no='DO-999-2020')
        assign_reference_number(doc)
        self.assertEqual(doc.reference_no, 'DO-999-2020')

    def test_councilor_and_barangay_resolutions_share_one_sequence(self):
        # Both are doc_type=RESOLUTION regardless of origin — must never
        # produce the same number for two different documents.
        from documents.views import assign_reference_number
        councilor_doc = make_document(self.author, status='FIRST_READING', doc_type='RESOLUTION')
        barangay_doc = make_document(self.author, status='REFERRED', doc_type='RESOLUTION')
        assign_reference_number(councilor_doc)
        assign_reference_number(barangay_doc)
        self.assertNotEqual(councilor_doc.reference_no, barangay_doc.reference_no)
        n1 = int(councilor_doc.reference_no.split('-')[1])
        n2 = int(barangay_doc.reference_no.split('-')[1])
        self.assertEqual(n2, n1 + 1)


class MoveToFirstAssignsReferenceNumberTests(TestCase):
    def setUp(self):
        self.author = make_user('m2f_author', 'COUNCILOR')
        self.secretariat = make_user('m2f_secretariat', 'SECRETARIAT')

    def _client(self):
        c = Client()
        c.force_login(self.secretariat)
        return c

    def test_filing_no_longer_assigns_a_number(self):
        doc = make_document(self.author, status='DRAFT', title='Real Title', doc_type='ORDINANCE')
        author_client = Client()
        author_client.force_login(self.author)
        author_client.post(reverse('create_draft'), data={
            'doc_id': str(doc.id), 'action': 'submit',
            'title': 'Real Title', 'type': 'ORDINANCE',
        })
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'FILED')
        self.assertFalse(doc.reference_no)

    def test_move_to_first_assigns_the_number(self):
        doc = make_document(self.author, status='FILED', doc_type='ORDINANCE')
        resp = self._client().post(reverse('move_to_first', args=[doc.id]))
        doc.refresh_from_db()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(doc.status, 'FIRST_READING')
        self.assertTrue(doc.reference_no)


# ---------------------------------------------------------------------------
# document_original_html — feeds amending_table.html's read-only "original"
# panel. Used to only walk docx.paragraphs (python-docx's flattened,
# table-blind view), silently dropping any table in the document — caught
# live against a real committee-stage ordinance where that meant both a
# "Sponsored by" box (a real field baked into the docx, not the same value
# as Document.sponsor_councilors()) and an actual legal-content table
# (a roles/offices table) never showed up in the original panel at all.
# Rewritten to walk docx.element.body in document order, handling <w:p>
# and <w:tbl> as they're actually interleaved.
# ---------------------------------------------------------------------------
class DocumentOriginalHtmlTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='lepmits_test_original_html_')
        self.override = override_settings(DOCUMENT_EDITOR_STORAGE_ROOT=self._tmp)
        self.override.enable()
        self.addCleanup(self.override.disable)

        self.author = make_user('orightml_author', 'COUNCILOR')
        self.other = make_user('orightml_other', 'COUNCILOR')
        self.client = Client()
        self.client.force_login(self.author)

    def _seed_archived_docx(self, doc, build_fn):
        from documents.views import _onlyoffice_saved_docx_version_path
        path = _onlyoffice_saved_docx_version_path(doc.id, doc.current_version)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        build_fn().save(path)

    def _build_real_ordinance_docx(self):
        docx_doc = DocxDocument()
        docx_doc.add_paragraph('A plain paragraph before the sponsor box.')

        sponsor_table = docx_doc.add_table(rows=1, cols=1)
        cell_para = sponsor_table.rows[0].cells[0].paragraphs[0]
        run1 = cell_para.add_run('Sponsored by:')
        run1.bold = True
        cell_para.add_run().add_break()
        run2 = cell_para.add_run('Councilor Bea D. De Guzman-Cabatbat')
        run2.bold = True

        docx_doc.add_paragraph('A paragraph between the two tables.')

        content_table = docx_doc.add_table(rows=2, cols=2)
        content_table.rows[0].cells[0].paragraphs[0].add_run('Office').bold = True
        content_table.rows[0].cells[1].paragraphs[0].add_run('Role').bold = True
        content_table.rows[1].cells[0].paragraphs[0].add_run('Public Assistance Center')
        content_table.rows[1].cells[1].paragraphs[0].add_run('Chairperson')

        docx_doc.add_paragraph('A plain paragraph after the second table.')
        return docx_doc

    def test_renders_tables_in_document_order_between_paragraphs(self):
        doc = make_document(self.author, status='DRAFT')
        self._seed_archived_docx(doc, self._build_real_ordinance_docx)

        resp = self.client.get(reverse('document-original-html', args=[doc.id]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode('utf-8')

        # Real document order: paragraph, sponsor table, paragraph,
        # content table, paragraph — not tables dropped/reordered.
        first_p = html.find('A plain paragraph before')
        sponsor = html.find('Sponsored by')
        between = html.find('A paragraph between the two tables')
        office_role = html.find('<table')
        after = html.find('A plain paragraph after')
        self.assertTrue(-1 < first_p < sponsor < between < after)
        self.assertGreaterEqual(html.count('<table'), 2)
        self.assertIn('Public Assistance Center', html)
        self.assertIn('Chairperson', html)

    def test_sponsor_box_line_break_preserved_and_bold(self):
        doc = make_document(self.author, status='DRAFT')
        self._seed_archived_docx(doc, self._build_real_ordinance_docx)

        resp = self.client.get(reverse('document-original-html', args=[doc.id]))
        html = resp.content.decode('utf-8')
        # The <w:br/> between "Sponsored by:" and the name must survive as
        # a real line break, not get silently dropped (which previously
        # jammed them together: "Sponsored by:Councilor ...").
        self.assertIn('<strong>Sponsored by:</strong><br><strong>Councilor Bea D. De Guzman-Cabatbat</strong>', html)

    def test_no_archived_docx_404s(self):
        doc = make_document(self.author, status='DRAFT')
        resp = self.client.get(reverse('document-original-html', args=[doc.id]))
        self.assertEqual(resp.status_code, 404)

    def test_blocked_for_non_viewer(self):
        doc = make_document(self.author, status='DRAFT')
        self._seed_archived_docx(doc, self._build_real_ordinance_docx)
        self.client.force_login(self.other)
        resp = self.client.get(reverse('document-original-html', args=[doc.id]))
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Legacy-document upload role gate. Upload_legacy/upload_legacy_document/
# extract_legacy_metadata/validate_ocr_with_ai previously carried only
# @login_required — any authenticated role (including COUNCILOR/BARANGAY)
# could POST straight into LegacyDocument, which has no status/approval
# workflow at all and is read unconditionally by the public-facing Gazette
# site. The "Upload Doc" nav tab (templates/base.html) was always scoped to
# SECRETARIAT/STAFF only — these views now enforce the same restriction
# server-side instead of relying on the tab simply not being shown.
# ---------------------------------------------------------------------------
class LegacyUploadPermissionTests(TestCase):
    def setUp(self):
        self.staff = make_user('legacyup_staff', 'STAFF')
        self.secretariat = make_user('legacyup_secretariat', 'SECRETARIAT')
        self.councilor = make_user('legacyup_councilor', 'COUNCILOR')
        self.barangay = make_user('legacyup_barangay', 'BARANGAY')
        self.admin = make_user('legacyup_admin', 'ADMIN')

    def _client_for(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_upload_legacy_page_denied_for_councilor(self):
        resp = self._client_for(self.councilor).get(reverse('UPLOAD-LEGACY'))
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)

    def test_upload_legacy_page_denied_for_barangay(self):
        resp = self._client_for(self.barangay).get(reverse('UPLOAD-LEGACY'))
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)

    def test_upload_legacy_page_denied_for_admin(self):
        # ADMIN is a superset role elsewhere in this app, but the "Upload
        # Doc" tab is specifically a SECRETARIAT/STAFF module tab (see
        # templates/base.html) — deliberately not extended to ADMIN here.
        resp = self._client_for(self.admin).get(reverse('UPLOAD-LEGACY'))
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)

    def test_upload_legacy_page_allowed_for_staff(self):
        resp = self._client_for(self.staff).get(reverse('UPLOAD-LEGACY'))
        self.assertEqual(resp.status_code, 200)

    def test_upload_legacy_page_allowed_for_secretariat(self):
        resp = self._client_for(self.secretariat).get(reverse('UPLOAD-LEGACY'))
        self.assertEqual(resp.status_code, 200)

    def test_upload_legacy_document_get_denied_for_councilor(self):
        resp = self._client_for(self.councilor).get(reverse('UPLOAD-LEGACY-DOCUMENT'))
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)

    def test_upload_legacy_document_get_allowed_for_staff(self):
        resp = self._client_for(self.staff).get(reverse('UPLOAD-LEGACY-DOCUMENT'))
        self.assertEqual(resp.status_code, 200)

    def test_upload_legacy_document_post_denied_for_councilor_creates_nothing(self):
        resp = self._client_for(self.councilor).post(reverse('UPLOAD-LEGACY-DOCUMENT'), data={
            'title': 'Fake Ordinance', 'reference_no': 'CO-1-2026',
            'doc_type': 'ORDINANCE', 'year': '2026',
        })
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)
        self.assertEqual(LegacyDocument.objects.count(), 0)

    def test_upload_legacy_document_post_denied_for_barangay_creates_nothing(self):
        resp = self._client_for(self.barangay).post(reverse('UPLOAD-LEGACY-DOCUMENT'), data={
            'title': 'Fake Ordinance', 'reference_no': 'CO-1-2026',
            'doc_type': 'ORDINANCE', 'year': '2026',
        })
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)
        self.assertEqual(LegacyDocument.objects.count(), 0)

    def test_upload_legacy_document_post_allowed_for_staff_reaches_validation(self):
        # No pdf_file attached — this should fail the view's own field
        # validation (re-rendering the form with an error), not the role
        # gate (which would redirect instead). Distinguishes "role check
        # passed, existing validation logic still runs" from a false pass.
        resp = self._client_for(self.staff).post(reverse('UPLOAD-LEGACY-DOCUMENT'), data={
            'title': 'Real Ordinance', 'reference_no': 'CO-1-2026',
            'doc_type': 'ORDINANCE', 'year': '2026',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyDocument.objects.count(), 0)

    def test_extract_legacy_metadata_denied_for_councilor(self):
        resp = self._client_for(self.councilor).post(reverse('EXTRACT-LEGACY-METADATA'))
        self.assertEqual(resp.status_code, 403)

    def test_extract_legacy_metadata_allowed_for_staff_reaches_file_check(self):
        # No file attached — falls through to the view's own "No file
        # provided" 400, proving the role gate let it past rather than
        # blocking it with a 403.
        resp = self._client_for(self.staff).post(reverse('EXTRACT-LEGACY-METADATA'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('No file provided', resp.json().get('error', ''))

    def test_validate_ocr_denied_for_councilor(self):
        resp = self._client_for(self.councilor).post(reverse('validate-ocr-with-ai'), data={'raw_text': 'x'})
        self.assertEqual(resp.status_code, 403)

    def test_validate_ocr_allowed_for_staff_reaches_text_check(self):
        # No raw_text — falls through to the view's own "No text provided"
        # 400, proving the role gate let it past without calling Ollama.
        resp = self._client_for(self.staff).post(reverse('validate-ocr-with-ai'), data={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('No text provided', resp.json().get('error', ''))


# ---------------------------------------------------------------------------
# embed_document_chunks was rewritten to embed chunks concurrently (a
# ThreadPoolExecutor of blocking Ollama HTTP calls) instead of one at a
# time, to cut the wall-clock cost of uploading a multi-section legacy
# document. Concurrency changes the order chunks *finish* in but must not
# change the order they're *returned* in, and one chunk failing must still
# skip only that chunk — both were true of the old sequential loop and had
# to stay true after parallelizing it.
# ---------------------------------------------------------------------------
class EmbedDocumentChunksConcurrencyTests(TestCase):
    def setUp(self):
        self.author = make_user('embedchunks_author', 'STAFF')
        self.doc = LegacyDocument.objects.create(
            title='Test Ordinance',
            reference_no='CO-1-2026',
            doc_type='ORDINANCE',
            year=2026,
            pdf_file='',
            # chunk_legal_text's marker regex only matches at the start of
            # a line (re.MULTILINE ^), matching how real legal text always
            # opens a new section on its own line.
            extracted_text=(
                "SECTION 1. Short Title. This Ordinance shall be known as "
                "the Test Ordinance of 2026, enacted for testing purposes.\n"
                "SECTION 2. Coverage. This shall cover all matters "
                "mentioned herein for the purpose of testing chunk "
                "embedding across multiple legislative sections.\n"
                "SECTION 3. Effectivity. This Ordinance shall take effect "
                "immediately upon approval and publication thereof."
            ),
        )

    def test_one_failed_chunk_is_skipped_others_still_embedded(self):
        def fake_embed_text(text, timeout=60):
            if 'Short Title' in text:
                raise RuntimeError('embedding service unreachable')
            return [0.1, 0.2, 0.3]

        with patch('documents.rag.embedder.embed_text', side_effect=fake_embed_text):
            results = embed_document_chunks(self.doc, source_type='legacy_document')

        # 3 sections chunked, 1 fails -> 2 survive, not a total failure.
        self.assertEqual(len(results), 2)
        self.assertNotIn(0, [r['chunk_index'] for r in results])

    def test_result_order_matches_chunk_index_despite_concurrent_completion(self):
        # Deliberately make the *first* chunk the slowest to embed, so a
        # naive "append as each future completes" implementation would
        # return chunk 0 last instead of first.
        def fake_embed_text(text, timeout=60):
            if 'Short Title' in text:
                time.sleep(0.1)
            return [0.1, 0.2, 0.3]

        with patch('documents.rag.embedder.embed_text', side_effect=fake_embed_text):
            results = embed_document_chunks(self.doc, source_type='legacy_document')

        indices = [r['chunk_index'] for r in results]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(indices, [0, 1, 2])

    def test_chunk_input_still_prefixed_with_document_title(self):
        seen_inputs = []

        def fake_embed_text(text, timeout=60):
            seen_inputs.append(text)
            return [0.1, 0.2, 0.3]

        with patch('documents.rag.embedder.embed_text', side_effect=fake_embed_text):
            embed_document_chunks(self.doc, source_type='legacy_document')

        self.assertEqual(len(seen_inputs), 3)
        self.assertTrue(all(text.startswith('Test Ordinance. ') for text in seen_inputs))


# ---------------------------------------------------------------------------
# _page_ocr_confidence — the averaging logic behind the confidence-based
# skip: high-confidence scans skip the AI cleanup pass automatically, so a
# bug here would either silently skip review it shouldn't have, or force
# every document through the ~15-20s AI pass again by never registering as
# confident. Tesseract's own -1 sentinel (structural elements — page/block/
# paragraph/line boundaries with no associated word) must be excluded from
# the average, not counted as a 0.
# ---------------------------------------------------------------------------
class PageOcrConfidenceTests(TestCase):
    def test_averages_only_non_negative_confidences(self):
        with patch('documents.views.pytesseract.image_to_data', return_value={
            'conf': ['-1', '-1', '95', '90', '-1', '85'],
        }):
            result = _page_ocr_confidence(object())
        self.assertAlmostEqual(result, (95 + 90 + 85) / 3)

    def test_all_negative_confidences_returns_none(self):
        with patch('documents.views.pytesseract.image_to_data', return_value={
            'conf': ['-1', '-1', '-1'],
        }):
            result = _page_ocr_confidence(object())
        self.assertIsNone(result)

    def test_tesseract_failure_returns_none_not_an_exception(self):
        with patch('documents.views.pytesseract.image_to_data', side_effect=RuntimeError('tesseract crashed')):
            result = _page_ocr_confidence(object())
        self.assertIsNone(result)

    def test_malformed_conf_values_are_skipped_not_fatal(self):
        with patch('documents.views.pytesseract.image_to_data', return_value={
            'conf': ['not-a-number', '80', None, '70'],
        }):
            result = _page_ocr_confidence(object())
        self.assertAlmostEqual(result, (80 + 70) / 2)


# ---------------------------------------------------------------------------
# LegacyDocument.reference_no uniqueness. Previously a free-text field with
# no duplicate check at all — two uploads could silently share a reference
# number and both show up as valid citations in Gazette search and the AI
# Legal Basis assistant's retrieval. Now enforced at both layers: a DB-level
# unique constraint (migration 0025) as the actual guarantee, and an
# application-level check in upload_legacy_document for a clean error
# message instead of a raw IntegrityError/500.
# ---------------------------------------------------------------------------
class LegacyDocumentReferenceNoUniquenessTests(TestCase):
    def setUp(self):
        self.staff = make_user('refuniq_staff', 'STAFF')

    def _client(self):
        c = Client()
        c.force_login(self.staff)
        return c

    def _payload(self, reference_no, **overrides):
        data = {
            'title': 'Test Ordinance',
            'reference_no': reference_no,
            'doc_type': 'ORDINANCE',
            'year': '2026',
            'pdf_file': SimpleUploadedFile('test.pdf', b'%PDF-1.4 fake', content_type='application/pdf'),
        }
        data.update(overrides)
        return data

    def test_duplicate_reference_no_rejected_case_insensitively(self):
        LegacyDocument.objects.create(
            title='Existing', reference_no='CO-1-2026', doc_type='ORDINANCE',
            year=2026, pdf_file='',
        )
        resp = self._client().post(reverse('UPLOAD-LEGACY-DOCUMENT'), self._payload('co-1-2026'))
        # Re-rendered with a validation error, not redirected to success.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyDocument.objects.filter(reference_no__iexact='co-1-2026').count(), 1)

    def test_unique_reference_no_is_accepted(self):
        resp = self._client().post(reverse('UPLOAD-LEGACY-DOCUMENT'), self._payload('CO-2-2026'))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(LegacyDocument.objects.filter(reference_no='CO-2-2026').exists())

    def test_db_level_unique_constraint_is_enforced(self):
        # Belt-and-suspenders check on the constraint itself, independent
        # of the view-level guard above — a direct .create() bypassing the
        # view's own .exists() check must still be rejected by the DB.
        LegacyDocument.objects.create(
            title='Existing', reference_no='CO-3-2026', doc_type='ORDINANCE',
            year=2026, pdf_file='',
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LegacyDocument.objects.create(
                    title='Duplicate', reference_no='CO-3-2026', doc_type='ORDINANCE',
                    year=2026, pdf_file='',
                )

    def test_race_past_the_view_check_still_gets_a_clean_error_not_a_500(self):
        # Simulate another request's row already landing (and committing)
        # before this request's .create() runs, but *after* this request's
        # own .exists() check already ran and (truthfully, at that instant)
        # found nothing — force that check to report "not found" so the
        # view reaches .create() and collides for real, instead of being
        # short-circuited by the .exists() guard this test isn't targeting.
        LegacyDocument.objects.create(
            title='Raced in first', reference_no='CO-4-2026', doc_type='ORDINANCE',
            year=2026, pdf_file='',
        )
        with patch('documents.views.LegacyDocument.objects.filter') as mock_filter:
            mock_filter.return_value.exists.return_value = False
            resp = self._client().post(reverse('UPLOAD-LEGACY-DOCUMENT'), self._payload('CO-4-2026'))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyDocument.objects.filter(reference_no__iexact='CO-4-2026').count(), 1)


def _make_pdf_bytes(lines):
    """Single-page born-digital PDF with each string on its own line —
    real pdfplumber text extraction, no Tesseract/OCR involved, so these
    tests exercise extract_legacy_metadata's actual regex logic
    deterministically instead of depending on OCR quality."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 750
    for line in lines:
        c.drawString(72, y, line)
        y -= 14
    c.save()
    buf.seek(0)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# extract_legacy_metadata's regex extraction. Previously title extraction
# required the literal sequence "Series of YYYY ... Sponsored" and nothing
# else — any real document missing/garbling "Sponsored by" right after the
# title silently ended up with a blank title, with no signal to the user
# that anything went wrong. Now falls back through other common
# terminators (WHEREAS / NOW THEREFORE) and, failing that, anchors on the
# ordinance/resolution number line instead of "Series of YYYY".
# ---------------------------------------------------------------------------
class ExtractLegacyMetadataRegexTests(TestCase):
    def setUp(self):
        self.staff = make_user('extract_staff', 'STAFF')
        self.client.force_login(self.staff)

    def _extract(self, lines):
        pdf_bytes = _make_pdf_bytes(lines)
        resp = self.client.post(reverse('EXTRACT-LEGACY-METADATA'), {
            'pdf_file': SimpleUploadedFile('test.pdf', pdf_bytes, content_type='application/pdf'),
        })
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_primary_pattern_series_of_year_then_sponsored(self):
        data = self._extract([
            'CITY ORDINANCE NO. 7',
            'Series of 2023',
            'AN ORDINANCE REGULATING THE OPERATION OF TRICYCLES',
            'Sponsored by: Councilor Juan Dela Cruz',
        ])
        self.assertEqual(data['title'], 'AN ORDINANCE REGULATING THE OPERATION OF TRICYCLES')
        self.assertEqual(data['reference_no'], 'CO-7-2023')
        self.assertEqual(data['year'], '2023')
        self.assertEqual(data['doc_type'], 'ORDINANCE')

    def test_fallback_terminator_whereas_when_sponsored_missing(self):
        data = self._extract([
            'CITY ORDINANCE NO. 9',
            'Series of 2024',
            'AN ORDINANCE ESTABLISHING A CITY HEALTH PROGRAM',
            'WHEREAS the City Government recognizes the need for healthcare;',
        ])
        self.assertEqual(data['title'], 'AN ORDINANCE ESTABLISHING A CITY HEALTH PROGRAM')

    def test_fallback_anchor_on_ordinance_number_when_series_of_missing(self):
        data = self._extract([
            'CITY RESOLUTION NO. 12',
            'AN RESOLUTION COMMENDING THE FIRE DEPARTMENT',
            'WHEREAS the Fire Department performed heroically;',
        ])
        self.assertEqual(data['title'], 'AN RESOLUTION COMMENDING THE FIRE DEPARTMENT')
        self.assertEqual(data['doc_type'], 'RESOLUTION')

    def test_no_terminator_anywhere_leaves_title_blank_not_garbage(self):
        # No "Sponsored"/"WHEREAS"/"NOW THEREFORE" anywhere at all — should
        # give up cleanly rather than grabbing unrelated trailing text.
        data = self._extract([
            'CITY ORDINANCE NO. 3',
            'Series of 2022',
            'AN ORDINANCE ON WASTE MANAGEMENT',
            'Some unrelated closing remark with no real terminator present.',
        ])
        self.assertEqual(data['title'], '')

    def test_resolution_type_detected(self):
        data = self._extract([
            'CITY RESOLUTION NO. 5',
            'Series of 2021',
            'A RESOLUTION EXPRESSING SUPPORT FOR LOCAL FARMERS',
            'Sponsored by: Councilor Maria Santos',
        ])
        self.assertEqual(data['doc_type'], 'RESOLUTION')
        self.assertEqual(data['reference_no'], 'CR-5-2021')


# ---------------------------------------------------------------------------
# upload_legacy_document's own field validation. Previously untested at
# all — a bug in any of these branches (e.g. a bad doc_type sneaking
# through, or the size/extension check silently no-op'ing) would only
# surface as bad data in a publicly-readable table, not a test failure.
# ---------------------------------------------------------------------------
class UploadLegacyDocumentValidationTests(TestCase):
    def setUp(self):
        self.staff = make_user('upload_val_staff', 'STAFF')
        self.client.force_login(self.staff)

    def _payload(self, **overrides):
        data = {
            'title': 'Test Ordinance',
            'reference_no': 'CO-100-2026',
            'doc_type': 'ORDINANCE',
            'year': '2026',
            'pdf_file': SimpleUploadedFile('test.pdf', b'%PDF-1.4 fake', content_type='application/pdf'),
        }
        data.update(overrides)
        return data

    def _post(self, **overrides):
        return self.client.post(reverse('UPLOAD-LEGACY-DOCUMENT'), self._payload(**overrides))

    def test_valid_submission_creates_and_redirects(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(LegacyDocument.objects.filter(reference_no='CO-100-2026').exists())

    def test_missing_title_rejected(self):
        resp = self._post(title='')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(LegacyDocument.objects.filter(reference_no='CO-100-2026').exists())

    def test_missing_reference_no_rejected(self):
        resp = self._post(reference_no='')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyDocument.objects.count(), 0)

    def test_invalid_doc_type_rejected(self):
        resp = self._post(doc_type='MEMO')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyDocument.objects.count(), 0)

    def test_missing_year_rejected(self):
        resp = self._post(year='')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyDocument.objects.count(), 0)

    def test_non_numeric_year_rejected(self):
        resp = self._post(year='not-a-year')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyDocument.objects.count(), 0)

    def test_year_out_of_range_rejected(self):
        resp = self._post(year='1500')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyDocument.objects.count(), 0)

    def test_missing_pdf_file_rejected(self):
        payload = self._payload()
        del payload['pdf_file']
        resp = self.client.post(reverse('UPLOAD-LEGACY-DOCUMENT'), payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyDocument.objects.count(), 0)

    def test_non_pdf_extension_rejected(self):
        resp = self._post(pdf_file=SimpleUploadedFile('test.docx', b'fake', content_type='application/msword'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyDocument.objects.count(), 0)

    def test_oversized_file_rejected(self):
        oversized = SimpleUploadedFile('test.pdf', b'x' * (20 * 1024 * 1024 + 1), content_type='application/pdf')
        resp = self._post(pdf_file=oversized)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyDocument.objects.count(), 0)


# ---------------------------------------------------------------------------
# The embed-on-upload path through the real view, not just
# embed_document_chunks in isolation — confirms DocumentChunk rows actually
# get created off a real upload request when validated_text is present.
# ---------------------------------------------------------------------------
class UploadLegacyDocumentEmbedOnUploadTests(TestCase):
    def setUp(self):
        self.staff = make_user('upload_embed_staff', 'STAFF')
        self.client.force_login(self.staff)

    def test_embed_on_upload_creates_document_chunks(self):
        def fake_embed_text(text, timeout=60):
            # Must match documents.models.EMBEDDING_DIMENSIONS — pgvector
            # enforces the column's fixed width at insert time, so a
            # wrong-sized fake vector here fails for real at bulk_create,
            # not just in embed_text's own (bypassed, since mocked) check.
            return [0.0] * 4096

        with patch('documents.rag.embedder.embed_text', side_effect=fake_embed_text):
            resp = self.client.post(reverse('UPLOAD-LEGACY-DOCUMENT'), {
                'title': 'Test Ordinance',
                'reference_no': 'CO-200-2026',
                'doc_type': 'ORDINANCE',
                'year': '2026',
                'validated_text': (
                    "SECTION 1. Short Title. This Ordinance shall be known "
                    "as the Test Ordinance of 2026, for embedding purposes.\n"
                    "SECTION 2. Effectivity. This Ordinance takes effect "
                    "immediately upon approval and publication."
                ),
                'pdf_file': SimpleUploadedFile('test.pdf', b'%PDF-1.4 fake', content_type='application/pdf'),
            })

        self.assertEqual(resp.status_code, 302)
        doc = LegacyDocument.objects.get(reference_no='CO-200-2026')
        self.assertTrue(doc.ocr_processed)
        chunks = DocumentChunk.objects.filter(legacy_document=doc)
        self.assertEqual(chunks.count(), 2)

    def test_embed_failure_does_not_fail_the_upload(self):
        with patch('documents.rag.embedder.embed_text', side_effect=RuntimeError('Ollama unreachable')):
            resp = self.client.post(reverse('UPLOAD-LEGACY-DOCUMENT'), {
                'title': 'Test Ordinance',
                'reference_no': 'CO-201-2026',
                'doc_type': 'ORDINANCE',
                'year': '2026',
                'validated_text': "SECTION 1. Short Title. This Ordinance shall be known as the Test Ordinance.",
                'pdf_file': SimpleUploadedFile('test.pdf', b'%PDF-1.4 fake', content_type='application/pdf'),
            })

        # Upload still succeeds even though embedding failed entirely.
        self.assertEqual(resp.status_code, 302)
        doc = LegacyDocument.objects.get(reference_no='CO-201-2026')
        self.assertEqual(DocumentChunk.objects.filter(legacy_document=doc).count(), 0)


# ---------------------------------------------------------------------------
# filter_by_similarity — the AI Legal Basis assistant's retrieval floor.
# Calibrated against the real Republic Acts corpus: a pure-gibberish query
# still scores ~0.69-0.71 against its closest (coincidental) matches, while
# genuinely on-topic queries score ~0.79+. Known limitation, not fixed by
# this: a real false-positive citation (RA 538, cited against an unrelated
# public accident-assistance ordinance) scored 0.7999 on real data — inside
# the "genuinely relevant" band, not a low-scoring outlier. This floor
# catches clearly-irrelevant noise on off-corpus topics; it is not a
# substitute for the LLM's own relevance judgment on near-miss candidates.
# ---------------------------------------------------------------------------
class FilterBySimilarityTests(TestCase):
    def test_scores_at_or_above_threshold_survive(self):
        from documents.rag.generator import filter_by_similarity, MIN_ENACTED_LAW_SIMILARITY
        chunks = [
            {"law_number": "RA 1", "score": MIN_ENACTED_LAW_SIMILARITY},
            {"law_number": "RA 2", "score": MIN_ENACTED_LAW_SIMILARITY + 0.05},
        ]
        result = filter_by_similarity(chunks)
        self.assertEqual([c["law_number"] for c in result], ["RA 1", "RA 2"])

    def test_scores_below_threshold_are_dropped(self):
        from documents.rag.generator import filter_by_similarity, MIN_ENACTED_LAW_SIMILARITY
        chunks = [
            {"law_number": "RA 1", "score": MIN_ENACTED_LAW_SIMILARITY - 0.01},
            {"law_number": "RA 2", "score": 0.1},
        ]
        result = filter_by_similarity(chunks)
        self.assertEqual(result, [])

    def test_mixed_scores_only_keeps_the_qualifying_ones(self):
        from documents.rag.generator import filter_by_similarity
        chunks = [
            {"law_number": "RA 1", "score": 0.81},
            {"law_number": "RA 2", "score": 0.50},
            {"law_number": "RA 3", "score": 0.80},
        ]
        result = filter_by_similarity(chunks, min_score=0.72)
        self.assertEqual([c["law_number"] for c in result], ["RA 1", "RA 3"])

    def test_missing_score_key_treated_as_zero_not_fatal(self):
        from documents.rag.generator import filter_by_similarity
        result = filter_by_similarity([{"law_number": "RA 1"}], min_score=0.72)
        self.assertEqual(result, [])
