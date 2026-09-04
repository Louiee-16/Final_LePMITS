"""Tests for the barangay-to-committee referral pipeline restructuring
(2026-09-04): barangay uploads now go straight from Other Matters into a
real committee-level Document at referral time — see barangay_to_referral
in views.py — instead of a separate "Pending Referrals" stage where a
councilor manually retyped the measure. Reference numbering for these
documents happens later, at committee approval, not at referral (see
committee_level/tests.py for that half).
"""
import io
import os
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from docx import Document as DocxDocument

from barangay.models import Barangay, BarangayFiles
from barangay.forms import MeasureUploadForm
from committees.models import Committee
from councilors.models import Councilor
from documents.models import Document
from documents.views import _onlyoffice_saved_docx_path

User = get_user_model()


def make_user(username, role, password='TestPass!2345'):
    return User.objects.create_user(username=username, password=password, role=role)


def _real_docx_bytes(text='A real barangay resolution.'):
    doc = DocxDocument()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class MeasureUploadFormValidationTests(TestCase):
    def _base_data(self):
        return {'title': 'Barangay Ordinance No. 1, Series of 2026', 'subject': '', 'remarks': ''}

    def test_rejects_wrong_extension(self):
        f = SimpleUploadedFile('measure.pdf', _real_docx_bytes(), content_type='application/pdf')
        form = MeasureUploadForm(data=self._base_data(), files={'uploaded_docx': f})
        self.assertFalse(form.is_valid())
        self.assertIn('uploaded_docx', form.errors)

    def test_rejects_docx_extension_with_fake_content(self):
        f = SimpleUploadedFile('measure.docx', b'not actually a docx', content_type='application/octet-stream')
        form = MeasureUploadForm(data=self._base_data(), files={'uploaded_docx': f})
        self.assertFalse(form.is_valid())
        self.assertIn('uploaded_docx', form.errors)

    def test_accepts_real_docx(self):
        f = SimpleUploadedFile('measure.docx', _real_docx_bytes(), content_type='application/octet-stream')
        form = MeasureUploadForm(data=self._base_data(), files={'uploaded_docx': f})
        self.assertTrue(form.is_valid(), form.errors)


class BarangayToReferralCreatesDocumentTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='lepmits_test_barangay_')
        self.override = override_settings(DOCUMENT_EDITOR_STORAGE_ROOT=self._tmp)
        self.override.enable()
        self.addCleanup(self.override.disable)

        self.secretariat = make_user('brgy_secretariat', 'SECRETARIAT')
        barangay_user = make_user('brgy_user', 'BARANGAY')
        self.barangay = Barangay.objects.create(
            user=barangay_user, barangay_name='Test Barangay', captain='Cap. Test', email='b@test.local'
        )
        chair_user = make_user('brgy_chair', 'COUNCILOR')
        self.chair = Councilor.objects.create(user=chair_user, name='Hon. Chair', email='chair@test.local', district=1)
        self.committee = Committee.objects.create(name='Test Committee', chairman=self.chair)

        self.docx_bytes = _real_docx_bytes('This is the barangay-submitted resolution text.')
        self.measure = BarangayFiles.objects.create(
            origin_barangay=self.barangay,
            title='Barangay Ordinance No. 1, Series of 2026',
            status='OTHER_MATTERS',
        )
        self.measure.uploaded_docx.save('measure.docx', io.BytesIO(self.docx_bytes), save=True)

        self.client = Client()
        self.client.force_login(self.secretariat)

    def test_referral_creates_document_directly(self):
        resp = self.client.post(
            reverse('barangay-to-referral', args=[self.measure.id]),
            {'referred_committee': self.committee.id},
        )
        self.assertEqual(resp.status_code, 302)

        self.measure.refresh_from_db()
        self.assertEqual(self.measure.status, 'DRAFT_CREATED')
        self.assertEqual(self.measure.referred_committee_id, self.committee.id)

        doc = Document.objects.get(source_barangay_doc=self.measure)
        self.assertEqual(doc.status, 'REFERRED')
        self.assertEqual(doc.doc_type, 'RESOLUTION')
        self.assertEqual(doc.referred_committee_id, self.committee.id)
        self.assertIsNone(doc.reference_no)
        # No separate "Pending Referrals" drafting stage — it's already at
        # committee level, the chair set as author since they didn't
        # manually create it themselves.
        self.assertEqual(doc.author_id, self.chair.user_id)

    def test_working_docx_is_the_barangays_uploaded_file(self):
        self.client.post(
            reverse('barangay-to-referral', args=[self.measure.id]),
            {'referred_committee': self.committee.id},
        )
        doc = Document.objects.get(source_barangay_doc=self.measure)
        working_path = _onlyoffice_saved_docx_path(doc.id)
        self.assertTrue(os.path.exists(working_path))
        with open(working_path, 'rb') as f:
            self.assertEqual(f.read(), self.docx_bytes)

    def test_content_synced_from_uploaded_docx(self):
        self.client.post(
            reverse('barangay-to-referral', args=[self.measure.id]),
            {'referred_committee': self.committee.id},
        )
        doc = Document.objects.get(source_barangay_doc=self.measure)
        self.assertIn('This is the barangay-submitted resolution text.', doc.content)

    def test_blocked_without_committee_selected(self):
        resp = self.client.post(reverse('barangay-to-referral', args=[self.measure.id]), {})
        self.assertEqual(resp.status_code, 302)
        self.measure.refresh_from_db()
        self.assertEqual(self.measure.status, 'OTHER_MATTERS')
        self.assertFalse(Document.objects.filter(source_barangay_doc=self.measure).exists())

    def test_blocked_without_uploaded_file(self):
        empty_measure = BarangayFiles.objects.create(
            origin_barangay=self.barangay, title='No file', status='OTHER_MATTERS'
        )
        resp = self.client.post(
            reverse('barangay-to-referral', args=[empty_measure.id]),
            {'referred_committee': self.committee.id},
        )
        self.assertEqual(resp.status_code, 302)
        empty_measure.refresh_from_db()
        self.assertEqual(empty_measure.status, 'OTHER_MATTERS')
        self.assertFalse(Document.objects.filter(source_barangay_doc=empty_measure).exists())


class BarangayFilePreviewTests(TestCase):
    """A raw barangay upload isn't a Document yet (see
    barangay_to_referral above), so it needs its own preview endpoint
    rather than the Document-based modal_document_viewer every other row
    on the Incoming Docs page uses — see barangay_file_preview/
    barangay_file_snapshot_pdf in views.py, fixing a real 404 hit live:
    clicking a barangay row was calling /modal/document/<id>/ with a
    BarangayFiles id, which is never a real Document."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='lepmits_test_barangay_preview_')
        self.override = override_settings(DOCUMENT_EDITOR_STORAGE_ROOT=self._tmp)
        self.override.enable()
        self.addCleanup(self.override.disable)

        self.secretariat = make_user('preview_secretariat', 'SECRETARIAT')
        self.councilor = make_user('preview_councilor', 'COUNCILOR')
        barangay_user = make_user('preview_brgy', 'BARANGAY')
        self.barangay = Barangay.objects.create(
            user=barangay_user, barangay_name='Preview Barangay', captain='Cap.', email='b@test.local'
        )
        self.measure = BarangayFiles.objects.create(
            origin_barangay=self.barangay, title='Measure', status='OTHER_MATTERS'
        )
        self.measure.uploaded_docx.save(
            'measure.docx', io.BytesIO(_real_docx_bytes('Preview test content.')), save=True
        )
        self.client = Client()
        self.client.force_login(self.secretariat)

    def test_preview_page_renders(self):
        resp = self.client.get(reverse('barangay-file-preview', args=[self.measure.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse('barangay-file-snapshot-pdf', args=[self.measure.id]))

    def test_snapshot_pdf_is_a_real_pdf(self):
        resp = self.client.get(reverse('barangay-file-snapshot-pdf', args=[self.measure.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertTrue(resp.content.startswith(b'%PDF-'))
        self.assertGreater(len(resp.content), 500)

    def test_preview_blocked_for_non_office_role(self):
        resp = self.client.get(reverse('barangay-file-preview', args=[self.measure.id]))
        self.client.force_login(self.councilor)
        resp = self.client.get(reverse('barangay-file-preview', args=[self.measure.id]))
        self.assertEqual(resp.status_code, 404)

    def test_snapshot_pdf_404s_without_uploaded_file(self):
        empty_measure = BarangayFiles.objects.create(
            origin_barangay=self.barangay, title='No file', status='OTHER_MATTERS'
        )
        resp = self.client.get(reverse('barangay-file-snapshot-pdf', args=[empty_measure.id]))
        self.assertEqual(resp.status_code, 404)

    def test_second_request_is_served_from_cache_not_reconverted(self):
        from unittest.mock import patch
        from documents import libreoffice
        real_convert = libreoffice.convert
        with patch('documents.libreoffice.convert', side_effect=real_convert) as mock_convert:
            first = self.client.get(reverse('barangay-file-snapshot-pdf', args=[self.measure.id]))
            second = self.client.get(reverse('barangay-file-snapshot-pdf', args=[self.measure.id]))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.content, second.content)
        mock_convert.assert_called_once()

    def test_cache_file_written_to_disk(self):
        from barangay.views import _barangay_file_snapshot_pdf_path
        cache_path = _barangay_file_snapshot_pdf_path(self.measure.id)
        self.assertFalse(os.path.exists(cache_path))
        self.client.get(reverse('barangay-file-snapshot-pdf', args=[self.measure.id]))
        self.assertTrue(os.path.exists(cache_path))


class BarangayDashboardUploadPreviewTests(TestCase):
    """The upload-measure modal (dashboards/barangay.html) now previews
    the picked .docx entirely client-side (Casual Docs mounted directly
    against the File, no server round trip — see casualdocs-mount.js's
    documentBuffer option) before it's ever uploaded. Nothing server-side
    to test for the preview itself (pure browser behavior), but a
    template typo in the new markup/script would show up here as a 500
    or a missing element."""

    def setUp(self):
        barangay_user = make_user('dash_preview_brgy', 'BARANGAY')
        Barangay.objects.create(
            user=barangay_user, barangay_name='Dashboard Preview Barangay', captain='Cap.', email='b2@test.local'
        )
        self.client = Client()
        self.client.force_login(barangay_user)

    def test_dashboard_renders_with_preview_scaffolding(self):
        resp = self.client.get(reverse('barangay-dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="filePreviewWrap"')
        self.assertContains(resp, 'id="filePreviewContainer"')
        self.assertContains(resp, 'documentBuffer: file')
        self.assertContains(resp, 'casualdocs-mount.js')
