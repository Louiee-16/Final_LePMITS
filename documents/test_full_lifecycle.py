"""
One-off diagnostic walkthrough of a Document's entire lifecycle (draft ->
filed -> first reading -> referred -> committee -> second reading -> third
reading -> approved -> download), driven through the real views via
Django's test Client exactly as the UI would, to catch real runtime
errors rather than just static code review.

Not meant to stay as permanent regression coverage in its current form
(it prints a running commentary rather than asserting cleanly per-case) —
written for a one-time lifecycle audit. Safe to delete or convert into
proper assertions later.
"""
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase
from django.urls import reverse

from committees.models import Committee
from documents.models import Archives, Document

User = get_user_model()


class FullLifecycleWalkthroughTests(TransactionTestCase):
    # Plain TestCase wraps the whole test in one enclosing transaction for
    # rollback-based isolation, which doesn't match how real requests
    # actually run here (settings.py has no ATOMIC_REQUESTS, so each
    # request/query commits independently in real usage). That wrapping
    # was actively misleading for this specific test: a genuinely-caught
    # ProgrammingError against the Gazette's unmanaged public-comment
    # table (not created in the isolated test DB, unlike the real dev DB
    # where it does exist — confirmed directly) poisoned the *entire*
    # enclosing transaction under TestCase, cascading into unrelated
    # queries later in the same test — a test-harness artifact, not a
    # reflection of real request behavior. TransactionTestCase matches
    # production's actual (non-atomic) behavior instead.
    def setUp(self):
        self.councilor = User.objects.create_user(username='walk_councilor', password='x', role='COUNCILOR')
        self.secretariat = User.objects.create_user(username='walk_secretariat', password='x', role='SECRETARIAT')
        self.committee = Committee.objects.create(name='Walkthrough Committee')
        self.c_councilor = Client()
        self.c_councilor.force_login(self.councilor)
        self.c_secretariat = Client()
        self.c_secretariat.force_login(self.secretariat)

    def test_full_lifecycle(self):
        # 1. Create + save draft
        resp = self.c_councilor.get(reverse('create_draft'))
        self.assertEqual(resp.status_code, 200)
        doc = Document.objects.filter(author=self.councilor, status='GHOST').first()
        self.assertIsNotNone(doc, 'ghost draft was created')

        resp = self.c_councilor.post(reverse('create_draft'), {
            'doc_id': doc.id,
            'title': 'AN ORDINANCE FOR WALKTHROUGH TESTING PURPOSES',
            'type': 'ORDINANCE',
            'content': '<p>WHEREAS this is a test.</p>',
            'action': 'save',
        })
        doc.refresh_from_db()
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(doc.status, 'DRAFT')

        # 2. File
        resp = self.c_councilor.post(reverse('create_draft'), {
            'doc_id': doc.id,
            'title': doc.title,
            'type': 'ORDINANCE',
            'content': doc.content,
            'action': 'submit',
        })
        doc.refresh_from_db()
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(doc.status, 'FILED')
        # No reference number yet at filing — assigned at First Reading
        # instead (move_to_first), see documents/views.py's
        # assign_reference_number.
        self.assertFalse(doc.reference_no)
        self.assertEqual(Archives.objects.filter(original_doc=doc).count(), 1)

        # 3. incoming -> first reading
        resp = self.c_secretariat.get(reverse('incoming_docs'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(doc, resp.context['incoming_docs'])

        resp = self.c_secretariat.post(reverse('move_to_first', args=[doc.id]))
        doc.refresh_from_db()
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(doc.status, 'FIRST_READING')
        self.assertTrue(doc.reference_no)

        resp = self.c_secretariat.get(reverse('first_reading'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(doc, resp.context['first_reading'])

        # 4. Refer to committee
        resp = self.c_secretariat.post(reverse('refer_to_committee', args=[doc.id]), {'referred_committee': self.committee.id})
        doc.refresh_from_db()
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(doc.status, 'REFERRED')
        self.assertEqual(doc.referred_committee_id, self.committee.id)

        # 5. Committee -> second reading
        resp = self.c_secretariat.get(reverse('view-committee'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(doc, resp.context['committee_docs'])

        # committee_amendments now requires a hearing_date to be set first —
        # see committee_level/views.py.
        doc.hearing_date = '2026-08-01'
        doc.save()

        resp = self.c_secretariat.get(reverse('committee-amendments', args=[doc.id]))
        self.assertEqual(resp.status_code, 200)

        resp = self.c_secretariat.post(reverse('move-to-second-reading', args=[doc.id]))
        doc.refresh_from_db()
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(doc.status, 'SECOND_READING')
        # Every status transition archives (FILED, FIRST_READING, REFERRED,
        # SECOND_READING so far) — not just committee-related ones. Full
        # paper trail by design, not a bug in the count.
        self.assertEqual(Archives.objects.filter(original_doc=doc).count(), 4)

        # 6. Second reading -> third reading
        resp = self.c_secretariat.get(reverse('second-reading'))
        self.assertEqual(resp.status_code, 200)

        resp = self.c_secretariat.get(reverse('floor_amendments', args=[doc.id]))
        self.assertEqual(resp.status_code, 200)

        # move_to_third_reading now requires having gone through Insert
        # Amendments at least once this session (even just to record "No
        # Amendments") — see documents/views.py.
        resp = self.c_secretariat.post(
            reverse('save_amendments', args=[doc.id]),
            {'action': 'save', 'amendment_status': 'NO_AMENDMENTS'},
        )
        self.assertIn(resp.status_code, (200, 302))

        resp = self.c_secretariat.post(reverse('move_to_third_reading', args=[doc.id]), {'reference_no': ''})
        doc.refresh_from_db()
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(doc.status, 'THIRD_READING')

        # 7. Third reading -> pre-signature download -> approve
        resp = self.c_secretariat.get(reverse('third-reading'))
        self.assertEqual(resp.status_code, 200)

        resp = self.c_secretariat.get(reverse('download_document_pdf', args=[doc.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get('Content-Type'), 'application/pdf')
        self.assertGreater(len(resp.content), 500)

        fake_signed_pdf = BytesIO(b'%PDF-1.4\n%fake test pdf\n')
        fake_signed_pdf.name = 'signed.pdf'
        resp = self.c_secretariat.post(reverse('approve-measure', args=[doc.id]), {'signed_pdf': fake_signed_pdf})
        doc.refresh_from_db()
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(doc.status, 'APPROVED')
        self.assertTrue(doc.signed_pdf)
        self.assertTrue(doc.reference_no and 'No.' in doc.reference_no)

        # 8. Approved registry -> official download
        resp = self.c_secretariat.get(reverse('approved'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(doc, resp.context['approved'])

        resp = self.c_secretariat.get(reverse('download_official_pdf', args=[doc.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get('Content-Type'), 'application/pdf')

        print('\nFinal state: status=%s ref=%s version=%s archives=%s' % (
            doc.status, doc.reference_no, doc.current_version,
            list(Archives.objects.filter(original_doc=doc).order_by('version').values_list('version', 'status')),
        ))
