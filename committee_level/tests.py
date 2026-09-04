"""
Regression tests for the 2026-09-02 lifecycle-audit fixes (see
HANDOFF_NOTES.md):
  - A 'FAILED' committee hearing outcome used to send the document to a
    bespoke 'RETURNED' status nothing in the app ever queried or
    displayed (not Draft Measures, not Django admin — Document isn't
    even registered there). Now returns it to DRAFT with a ReturnReason,
    the same pattern documents/views.py's return_filed_doc/
    return_from_first_reading already use.
  - move_to_second_reading had no status filter at all — any
    SECRETARIAT/STAFF/ADMIN user could POST it against a document in any
    status, including an already-APPROVED one, and silently regress it.
    Also converted from a plain GET link to a CSRF-protected POST form.

This was previously the stock 3-line TestCase stub.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from committee_level.models import CommitteeReport, HearingLog
from committees.models import Committee
from councilors.models import Councilor
from documents.models import Document, ReturnReason

User = get_user_model()


def make_user(username, role, password='TestPass!2345'):
    return User.objects.create_user(username=username, password=password, role=role)


def make_document(author, status='REFERRED', **extra):
    defaults = dict(title='Test Measure', author=author, content='', status=status)
    defaults.update(extra)
    return Document.objects.create(**defaults)


class HearingFailureReturnsToDraftTests(TestCase):
    def setUp(self):
        self.author = make_user('hearing_author', 'COUNCILOR')
        # draft_measures unconditionally reads request.user.councilor_profile
        # — every real councilor account has one; a test user needs it too.
        Councilor.objects.create(user=self.author, name='Hon. Test Author', email='hearing_author@test.local', district=1)
        self.secretariat = make_user('hearing_secretariat', 'SECRETARIAT')
        self.committee = Committee.objects.create(name='Test Committee')
        self.doc = make_document(self.author, status='COMMITTEE', referred_committee=self.committee)
        # report_workbench now requires a hearing_date before it'll accept
        # add_hearing POSTs — see committee_level/views.py.
        self.doc.hearing_date = '2026-08-01'
        self.doc.save()
        self.report = CommitteeReport.objects.create(draft=self.doc)
        self.client = Client()
        self.client.force_login(self.secretariat)

    def _log_hearing(self, outcome, notes=''):
        return self.client.post(reverse('report_workbench', args=[self.doc.id]), {
            'action': 'add_hearing',
            'content': '',
            'hearing_date': '2026-09-01',
            'hearing_outcome': outcome,
            'attendance_notes': notes,
        })

    def test_failed_hearing_returns_document_to_draft(self):
        resp = self._log_hearing('FAILED', notes='Missing budget certification.')
        self.assertIn(resp.status_code, (200, 302))
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, 'DRAFT')

    def test_failed_hearing_is_visible_in_draft_measures(self):
        self._log_hearing('FAILED')
        c = Client()
        c.force_login(self.author)
        resp = c.get(reverse('draft-measures'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.doc, resp.context['draft_measures'])

    def test_failed_hearing_creates_return_reason_with_details(self):
        self._log_hearing('FAILED', notes='Missing budget certification.')
        reason = ReturnReason.objects.filter(document=self.doc).first()
        self.assertIsNotNone(reason)
        self.assertEqual(reason.previous_status, 'COMMITTEE')
        self.assertEqual(reason.returned_by, self.secretariat)
        self.assertIn('Missing budget certification.', reason.reason)

    def test_approved_hearing_does_not_touch_document_status(self):
        resp = self._log_hearing('APPROVED')
        self.assertIn(resp.status_code, (200, 302))
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, 'COMMITTEE')

    def test_second_failed_hearing_does_not_duplicate_return_reason(self):
        self._log_hearing('FAILED')
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, 'DRAFT')
        # Logging another hearing against the same (now-DRAFT) document
        # must not re-trigger the return path a second time.
        self._log_hearing('FAILED')
        self.assertEqual(ReturnReason.objects.filter(document=self.doc).count(), 1)


class MoveToSecondReadingStatusGuardTests(TestCase):
    def setUp(self):
        self.author = make_user('m2sr_author', 'COUNCILOR')
        self.secretariat = make_user('m2sr_secretariat', 'SECRETARIAT')
        self.client = Client()
        self.client.force_login(self.secretariat)

    def test_allowed_from_referred(self):
        doc = make_document(self.author, status='REFERRED')
        resp = self.client.post(reverse('move-to-second-reading', args=[doc.id]))
        doc.refresh_from_db()
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(doc.status, 'SECOND_READING')

    def test_allowed_from_committee(self):
        doc = make_document(self.author, status='COMMITTEE')
        resp = self.client.post(reverse('move-to-second-reading', args=[doc.id]))
        doc.refresh_from_db()
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(doc.status, 'SECOND_READING')

    def test_rejected_from_approved(self):
        # The core bug: an already-enacted measure could previously be
        # silently regressed back to SECOND_READING.
        doc = make_document(self.author, status='APPROVED')
        resp = self.client.post(reverse('move-to-second-reading', args=[doc.id]))
        doc.refresh_from_db()
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(doc.status, 'APPROVED')

    def test_rejected_from_third_reading(self):
        doc = make_document(self.author, status='THIRD_READING')
        resp = self.client.post(reverse('move-to-second-reading', args=[doc.id]))
        doc.refresh_from_db()
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(doc.status, 'THIRD_READING')

    def test_get_request_is_rejected(self):
        # Was previously a plain GET-triggerable link with no CSRF
        # protection at all.
        doc = make_document(self.author, status='REFERRED')
        resp = self.client.get(reverse('move-to-second-reading', args=[doc.id]))
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'REFERRED')


class CommitteeReportSaveBumpsUpdatedAtTests(TestCase):
    """committee_report_save previously wrote the docx bytes to disk but
    never called report.save() — CommitteeReport.updated_at (auto_now)
    silently never moved on a live Casual Docs save, which is the field a
    "saved"/"last saved" indicator in committee_workbench.html would
    actually read."""

    def setUp(self):
        self.author = make_user('crsave_author', 'COUNCILOR')
        self.secretariat = make_user('crsave_secretariat', 'SECRETARIAT')
        self.doc = make_document(self.author, status='REFERRED')
        self.report = CommitteeReport.objects.create(draft=self.doc)
        self.client = Client()
        self.client.force_login(self.secretariat)

    def test_save_bumps_updated_at_and_returns_it(self):
        before = self.report.updated_at
        resp = self.client.post(
            reverse('committee-report-save', args=[self.report.id]),
            data=b'fake docx bytes',
            content_type='application/octet-stream',
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body.get('error'), 0)
        self.assertIn('saved_at', body)

        self.report.refresh_from_db()
        self.assertGreater(self.report.updated_at, before)
        self.assertEqual(body['saved_at'], self.report.updated_at.isoformat())


class SavedTagPagesRenderTests(TestCase):
    """committee_workbench.html and amending_table.html both got a live
    "saved" indicator wired to the new onSaved callback (frontend/
    casualdocs-entry.jsx, static/js/document_editors/casualdocs-mount.js)
    — a template typo there would only surface as a 500 on render, not
    anywhere the Python-level tests above would catch it."""

    def setUp(self):
        self.author = make_user('savedtag_author', 'COUNCILOR')
        Councilor.objects.create(user=self.author, name='Hon. Saved Tag', email='savedtag_author@test.local', district=1)
        self.secretariat = make_user('savedtag_secretariat', 'SECRETARIAT')
        self.committee = Committee.objects.create(name='Saved Tag Committee')
        self.client = Client()
        self.client.force_login(self.secretariat)

    def test_report_workbench_renders(self):
        doc = make_document(self.author, status='COMMITTEE', referred_committee=self.committee, hearing_date='2026-08-01')
        resp = self.client.get(reverse('report_workbench', args=[doc.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="saveStatus"')

    def test_committee_amendments_renders(self):
        doc = make_document(self.author, status='REFERRED', referred_committee=self.committee, hearing_date='2026-08-01')
        resp = self.client.get(reverse('committee-amendments', args=[doc.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="saveStatusTag"')


class AmendingTableInsertedMarkerTests(TestCase):
    """Covers the "new section inserted" marker's static scaffolding in
    amending_table.html — the actual insert-detection logic is pure
    browser JS (getOrderedParaIds/highlightSelectedParagraph in
    frontend/casualdocs-entry.jsx + amending_table.html) and can't be
    exercised through Django's test client, but a template typo in it
    would still show up here as a 500 or a missing element.

    (Sponsored by on this page turned out to already be real content
    inside the archived .docx itself — not something missing that needed
    a separate template block — see documents/tests.py's
    DocumentOriginalHtmlTests for that fix, in document_original_html.)"""

    def setUp(self):
        self.author = make_user('markertest_author', 'COUNCILOR')
        Councilor.objects.create(user=self.author, name='Hon. Marker Test', email='markertest@test.local', district=1)
        self.secretariat = make_user('markertest_secretariat', 'SECRETARIAT')
        self.committee = Committee.objects.create(name='Marker Test Committee')
        self.client = Client()
        self.client.force_login(self.secretariat)

    def test_new_section_marker_scaffolding_present(self):
        doc = make_document(self.author, status='REFERRED', referred_committee=self.committee, hearing_date='2026-08-01')
        resp = self.client.get(reverse('committee-amendments', args=[doc.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'new-section-marker')
        self.assertContains(resp, 'getOrderedParaIds')


class ApprovedHearingAssignsBarangayReferenceNumberTests(TestCase):
    """Barangay-originated documents skip First Reading entirely (see
    barangay/views.py's barangay_to_referral), so they never get numbered
    at move_to_first the way councilor-authored ones do. Instead,
    _sync_report_status_from_hearing assigns it the moment the hearing
    outcome is logged as Approved — but only for documents that actually
    came from a barangay; a councilor-authored document is already
    numbered by the time it reaches committee level, so this must be a
    no-op for those."""

    def setUp(self):
        self.author = make_user('approvedref_author', 'COUNCILOR')
        Councilor.objects.create(user=self.author, name='Hon. Approved Ref', email='approvedref@test.local', district=1)
        self.secretariat = make_user('approvedref_secretariat', 'SECRETARIAT')
        self.client = Client()
        self.client.force_login(self.secretariat)

    def _log_hearing(self, doc, outcome):
        CommitteeReport.objects.create(draft=doc)
        return self.client.post(reverse('report_workbench', args=[doc.id]), {
            'action': 'add_hearing',
            'hearing_date': '2026-09-01',
            'hearing_outcome': outcome,
            'attendance_notes': '',
        })

    def test_barangay_originated_document_gets_numbered_on_approval(self):
        from barangay.models import Barangay, BarangayFiles
        barangay_user = make_user('approvedref_brgy', 'BARANGAY')
        barangay = Barangay.objects.create(user=barangay_user, barangay_name='Approved Ref Brgy', captain='Cap.', email='b@test.local')
        measure = BarangayFiles.objects.create(origin_barangay=barangay, title='Measure', status='DRAFT_CREATED')

        doc = make_document(
            self.author, status='COMMITTEE', doc_type='RESOLUTION',
            source_barangay_doc=measure, hearing_date='2026-08-01',
        )
        self.assertFalse(doc.reference_no)

        self._log_hearing(doc, 'APPROVED')
        doc.refresh_from_db()
        self.assertRegex(doc.reference_no, r'^DR-\d+-\d{4}$')

    def test_councilor_originated_document_untouched_by_approval(self):
        # Already numbered (as any real councilor-authored document
        # reaching committee level would be, from move_to_first) — the
        # approval branch must no-op, not assign a second/different one.
        doc = make_document(
            self.author, status='COMMITTEE', doc_type='ORDINANCE',
            reference_no='DO-5-2026', hearing_date='2026-08-01',
        )
        self._log_hearing(doc, 'APPROVED')
        doc.refresh_from_db()
        self.assertEqual(doc.reference_no, 'DO-5-2026')


class DeleteHearingLogRecomputesReportStatusTests(TestCase):
    """Reported bug: log a hearing as Approved, delete that same-day
    entry, go back to the Committee Referral Registry — it still shows
    Approved. delete_hearing_log used to just delete the row and redirect,
    never touching CommitteeReport.status, which _sync_report_status_from_
    hearing had already set to APPROVED when the (now-deleted) hearing was
    logged. delete_hearing_log is now a soft delete (HearingLog.deleted_at)
    rather than a hard one — see DeleteThenRestoreHearingLogTests below for
    the same-day Restore / next-day purge behavior that adds."""

    def setUp(self):
        self.author = make_user('delhearing_author', 'COUNCILOR')
        Councilor.objects.create(user=self.author, name='Hon. Del Hearing', email='delhearing@test.local', district=1)
        self.secretariat = make_user('delhearing_secretariat', 'SECRETARIAT')
        self.committee = Committee.objects.create(name='Del Hearing Committee')
        self.doc = make_document(self.author, status='COMMITTEE', referred_committee=self.committee)
        self.doc.hearing_date = '2026-08-01'
        self.doc.save()
        self.report = CommitteeReport.objects.create(draft=self.doc)
        self.client = Client()
        self.client.force_login(self.secretariat)

    def _log_hearing(self, outcome, hearing_date='2026-09-01'):
        self.client.post(reverse('report_workbench', args=[self.doc.id]), {
            'action': 'add_hearing',
            'content': '',
            'hearing_date': hearing_date,
            'hearing_outcome': outcome,
            'attendance_notes': '',
        })
        return HearingLog.objects.filter(report=self.report).order_by('-id').first()

    def test_deleting_the_approving_hearing_reverts_status_to_pending(self):
        hearing = self._log_hearing('APPROVED')
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, 'APPROVED')

        self.client.post(reverse('delete-hearing-log', args=[hearing.id]))
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, 'PENDING')

    def test_deleting_falls_back_to_the_next_most_recent_hearing(self):
        # An earlier hearing already set it to APPROVED; a same-day second
        # hearing (e.g. logged by mistake) reset it — deleting that RESET
        # entry should fall back to the still-real APPROVED one underneath,
        # not just blank it out to PENDING.
        older = self._log_hearing('APPROVED', hearing_date='2026-08-15')
        newer = self._log_hearing('RESET', hearing_date='2026-09-01')
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, 'PENDING')  # RESET -> PENDING

        self.client.post(reverse('delete-hearing-log', args=[newer.id]))
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, 'APPROVED')
        self.assertTrue(HearingLog.objects.filter(id=older.id).exists())

    def test_deleting_the_only_hearing_leaves_report_pending(self):
        hearing = self._log_hearing('FAILED')
        self.client.post(reverse('delete-hearing-log', args=[hearing.id]))
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, 'PENDING')
        # Soft delete — the row is still there (deleted_at set), just no
        # longer counted as "active" by anything that reads it.
        hearing.refresh_from_db()
        self.assertIsNotNone(hearing.deleted_at)
        self.assertEqual(HearingLog.objects.filter(report=self.report, deleted_at__isnull=True).count(), 0)


class DeleteThenRestoreHearingLogTests(TestCase):
    """The same-day undo window delete_hearing_log's soft delete makes
    possible: Restore brings a same-day-deleted entry back (and its
    outcome back into report.status); the purge in report_workbench hard-
    deletes anything whose deletion day has actually passed, at which
    point Restore correctly refuses (nothing left to restore)."""

    def setUp(self):
        self.author = make_user('restorehearing_author', 'COUNCILOR')
        Councilor.objects.create(user=self.author, name='Hon. Restore Hearing', email='restorehearing@test.local', district=1)
        self.secretariat = make_user('restorehearing_secretariat', 'SECRETARIAT')
        self.committee = Committee.objects.create(name='Restore Hearing Committee')
        self.doc = make_document(self.author, status='COMMITTEE', referred_committee=self.committee)
        self.doc.hearing_date = '2026-08-01'
        self.doc.save()
        self.report = CommitteeReport.objects.create(draft=self.doc)
        self.client = Client()
        self.client.force_login(self.secretariat)

    def _log_hearing(self, outcome, hearing_date='2026-09-01'):
        self.client.post(reverse('report_workbench', args=[self.doc.id]), {
            'action': 'add_hearing',
            'content': '',
            'hearing_date': hearing_date,
            'hearing_outcome': outcome,
            'attendance_notes': '',
        })
        return HearingLog.objects.filter(report=self.report).order_by('-id').first()

    def test_restoring_a_same_day_deleted_hearing_brings_back_its_outcome(self):
        hearing = self._log_hearing('APPROVED')
        self.client.post(reverse('delete-hearing-log', args=[hearing.id]))
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, 'PENDING')

        self.client.post(reverse('restore-hearing-log', args=[hearing.id]))
        hearing.refresh_from_db()
        self.report.refresh_from_db()
        self.assertIsNone(hearing.deleted_at)
        self.assertEqual(self.report.status, 'APPROVED')

    def test_restore_refuses_once_the_deletion_day_has_passed(self):
        hearing = self._log_hearing('APPROVED')
        hearing.deleted_at = timezone.now() - timedelta(days=1)
        hearing.save(update_fields=['deleted_at'])

        self.client.post(reverse('restore-hearing-log', args=[hearing.id]))
        hearing.refresh_from_db()
        self.assertIsNotNone(hearing.deleted_at)  # still deleted — restore refused

    def test_report_workbench_load_purges_hearings_deleted_before_today(self):
        hearing = self._log_hearing('APPROVED')
        hearing.deleted_at = timezone.now() - timedelta(days=2)
        hearing.save(update_fields=['deleted_at'])

        self.client.get(reverse('report_workbench', args=[self.doc.id]))
        self.assertFalse(HearingLog.objects.filter(id=hearing.id).exists())

    def test_report_workbench_load_does_not_purge_hearings_deleted_today(self):
        hearing = self._log_hearing('APPROVED')
        self.client.post(reverse('delete-hearing-log', args=[hearing.id]))

        self.client.get(reverse('report_workbench', args=[self.doc.id]))
        self.assertTrue(HearingLog.objects.filter(id=hearing.id).exists())
