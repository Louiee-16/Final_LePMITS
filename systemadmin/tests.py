from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from audit.models import AuditLog
from barangay.models import Barangay
from councilors.models import Councilor
from systemadmin.middleware import SystemErrorLoggingMiddleware
from systemadmin.models import SystemError, SystemSetting

User = get_user_model()


def make_user(username, role, is_active=True, password='TestPass!2345'):
    return User.objects.create_user(username=username, password=password, role=role, is_active=is_active)


class PermissionGateTests(TestCase):
    """Every systemadmin view must be admin-only. admin_dashboard was
    previously missing its @user_passes_test guard entirely — any
    authenticated user could reach it."""

    def setUp(self):
        self.admin = make_user('gate_admin', 'ADMIN')
        self.staff = make_user('gate_staff', 'STAFF')

    def test_non_admin_blocked_from_every_systemadmin_page(self):
        urls = [
            reverse('admin-dashboard'),
            reverse('user-management'),
            reverse('general-settings'),
            reverse('council-setup'),
            reverse('backend-database'),
            reverse('audit-logs'),
            reverse('create-user-page'),
        ]
        c = Client()
        c.force_login(self.staff)
        for url in urls:
            with self.subTest(url=url):
                resp = c.get(url)
                self.assertNotEqual(resp.status_code, 200, f'{url} should not be reachable by STAFF')

    def test_admin_can_reach_every_systemadmin_page(self):
        urls = [
            reverse('admin-dashboard'),
            reverse('user-management'),
            reverse('general-settings'),
            reverse('council-setup'),
            reverse('backend-database'),
            reverse('audit-logs'),
            reverse('create-user-page'),
        ]
        c = Client()
        c.force_login(self.admin)
        for url in urls:
            with self.subTest(url=url):
                resp = c.get(url)
                self.assertEqual(resp.status_code, 200)


class CreateUserTests(TestCase):
    def setUp(self):
        self.admin = make_user('cu_admin', 'ADMIN')
        self.client.force_login(self.admin)

    def _post(self, **overrides):
        data = {
            'first_name': 'Test', 'last_name': 'User', 'email': 'newuser@example.com',
            'username': 'newuser', 'role': 'STAFF',
            'password': 'Xk9#mQ2vL7pR', 'confirm_password': 'Xk9#mQ2vL7pR',
        }
        data.update(overrides)
        return self.client.post(reverse('create-user'), data, follow=True)

    def test_weak_password_rejected(self):
        self._post(password='12345678', confirm_password='12345678')
        self.assertFalse(User.objects.filter(username='newuser').exists())

    def test_mismatched_passwords_rejected(self):
        self._post(confirm_password='somethingElse123!')
        self.assertFalse(User.objects.filter(username='newuser').exists())

    def test_duplicate_username_rejected(self):
        make_user('newuser', 'STAFF')
        self._post()
        self.assertEqual(User.objects.filter(username='newuser').count(), 1)

    def test_duplicate_email_rejected(self):
        make_user('someoneelse', 'STAFF')
        User.objects.filter(username='someoneelse').update(email='newuser@example.com')
        self._post()
        self.assertFalse(User.objects.filter(username='newuser').exists())

    def test_invalid_role_rejected(self):
        self._post(role='SUPERVILLAIN')
        self.assertFalse(User.objects.filter(username='newuser').exists())

    def test_valid_creation_succeeds_and_is_logged(self):
        self._post()
        self.assertTrue(User.objects.filter(username='newuser', role='STAFF').exists())
        self.assertTrue(AuditLog.objects.filter(action='CREATE', target__icontains='newuser').exists())

    def test_barangay_creation_requires_distinct_name_not_a_crash(self):
        first = make_user('brgy_owner_1', 'BARANGAY')
        Barangay.objects.create(user=first, barangay_name='Brgy. User', captain='Cap', email='a@example.com')
        # Same last_name -> same default-generated barangay_name -> must be
        # rejected with a message, not a 500 from a unique-constraint crash.
        resp = self._post(username='brgy_owner_2', email='b@example.com', role='BARANGAY',
                           first_name='Another', last_name='User')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username='brgy_owner_2').exists())


class EditUserLockoutTests(TestCase):
    """Guards against a system admin locking everyone out of the console."""

    def setUp(self):
        self.sole_admin = make_user('sole_admin', 'ADMIN')

    def test_admin_cannot_demote_self(self):
        c = Client()
        c.force_login(self.sole_admin)
        c.post(reverse('edit-user', args=[self.sole_admin.id]),
               {'first_name': 'Sole', 'last_name': 'Admin', 'email': '', 'role': 'STAFF'})
        self.sole_admin.refresh_from_db()
        self.assertEqual(self.sole_admin.role, 'ADMIN')

    def test_cannot_demote_the_only_active_admin(self):
        other = make_user('other_super', 'STAFF', )
        other.is_superuser = True
        other.save()
        c = Client()
        c.force_login(other)
        c.post(reverse('edit-user', args=[self.sole_admin.id]),
               {'first_name': 'Sole', 'last_name': 'Admin', 'email': '', 'role': 'STAFF'})
        self.sole_admin.refresh_from_db()
        self.assertEqual(self.sole_admin.role, 'ADMIN')

    def test_demotion_allowed_once_a_second_admin_exists(self):
        second_admin = make_user('second_admin', 'ADMIN')
        c = Client()
        c.force_login(second_admin)
        c.post(reverse('edit-user', args=[self.sole_admin.id]),
               {'first_name': 'Sole', 'last_name': 'Admin', 'email': '', 'role': 'STAFF'})
        self.sole_admin.refresh_from_db()
        self.assertEqual(self.sole_admin.role, 'STAFF')

    def test_cannot_deactivate_the_only_active_admin(self):
        other = make_user('other_super2', 'STAFF')
        other.is_superuser = True
        other.save()
        c = Client()
        c.force_login(other)
        c.post(reverse('toggle-status', args=[self.sole_admin.id]))
        self.sole_admin.refresh_from_db()
        self.assertTrue(self.sole_admin.is_active)


class ProfileCascadeTests(TestCase):
    """Deactivating/re-roling a user must keep the linked Councilor/Barangay
    row's is_active flag consistent, since other pages (Council Setup) query
    those tables directly instead of through User."""

    def setUp(self):
        self.admin = make_user('cascade_admin', 'ADMIN')
        self.client.force_login(self.admin)

    def test_toggle_status_cascades_to_councilor(self):
        u = make_user('cascade_councilor', 'COUNCILOR')
        profile = Councilor.objects.create(user=u, name='Cascade Councilor', email='cc@example.com', district=1)
        self.client.post(reverse('toggle-status', args=[u.id]))
        profile.refresh_from_db()
        self.assertFalse(profile.is_active)

    def test_toggle_status_cascades_to_barangay(self):
        u = make_user('cascade_barangay', 'BARANGAY')
        profile = Barangay.objects.create(user=u, barangay_name='Cascade Brgy', captain='Cap', email='cb@example.com')
        self.client.post(reverse('toggle-status', args=[u.id]))
        profile.refresh_from_db()
        self.assertFalse(profile.is_active)

    def test_role_change_away_deactivates_councilor_profile(self):
        u = make_user('role_change_councilor', 'COUNCILOR')
        profile = Councilor.objects.create(user=u, name='RC', email='rc2@example.com', district=1)
        self.client.post(reverse('edit-user', args=[u.id]),
                          {'first_name': 'R', 'last_name': 'C', 'email': 'rc2@example.com', 'role': 'STAFF'})
        profile.refresh_from_db()
        self.assertFalse(profile.is_active)

    def test_role_change_back_reactivates_existing_profile(self):
        # Was a councilor, demoted to STAFF (leaving a dormant profile), now
        # being moved back to COUNCILOR — the existing profile should be
        # reactivated rather than left orphaned and inactive.
        u = make_user('role_back_councilor', 'STAFF')
        profile = Councilor.objects.create(user=u, name='RB', email='rb2@example.com', district=1, is_active=False)
        self.client.post(reverse('edit-user', args=[u.id]),
                          {'first_name': 'R', 'last_name': 'B', 'email': 'rb2@example.com', 'role': 'COUNCILOR'})
        profile.refresh_from_db()
        self.assertTrue(profile.is_active)


class ResetPasswordTests(TestCase):
    def test_reset_password_message_is_visible_and_usable(self):
        admin = make_user('reset_admin', 'ADMIN')
        target = make_user('reset_target', 'STAFF', password='OldPassword123!')
        c = Client()
        c.force_login(admin)
        resp = c.post(reverse('reset-password', args=[target.id]), follow=True)
        self.assertIn(b'reset to', resp.content)

        target.refresh_from_db()
        self.assertFalse(target.check_password('OldPassword123!'))


class MaintenanceModeMiddlewareTests(TestCase):
    def setUp(self):
        self.admin = make_user('maint_admin', 'ADMIN')
        self.staff = make_user('maint_staff', 'STAFF')
        self.setting, _ = SystemSetting.objects.get_or_create(id=1)

    def tearDown(self):
        self.setting.maintenance_mode = False
        self.setting.save()

    def test_blocks_non_admin_when_enabled(self):
        self.setting.maintenance_mode = True
        self.setting.save()
        c = Client()
        c.force_login(self.staff)
        resp = c.get('/dashboard/')
        self.assertEqual(resp.status_code, 503)

    def test_allows_admin_when_enabled(self):
        self.setting.maintenance_mode = True
        self.setting.save()
        c = Client()
        c.force_login(self.admin)
        resp = c.get(reverse('admin-dashboard'))
        self.assertEqual(resp.status_code, 200)

    def test_allows_anonymous_login_page_when_enabled(self):
        self.setting.maintenance_mode = True
        self.setting.save()
        c = Client()
        resp = c.get('/login/')
        self.assertEqual(resp.status_code, 200)

    def test_does_not_block_when_disabled(self):
        c = Client()
        c.force_login(self.staff)
        resp = c.get('/dashboard/')
        self.assertNotEqual(resp.status_code, 503)


class SystemErrorLoggingTests(TestCase):
    """The middleware that captures unhandled exceptions system-wide."""

    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = SystemErrorLoggingMiddleware(get_response=lambda r: None)

    def _request(self, user=None):
        from django.contrib.auth.models import AnonymousUser
        req = self.factory.get('/some/broken/page/')
        req.user = user or AnonymousUser()
        return req

    def test_records_an_unhandled_exception(self):
        req = self._request()
        try:
            raise ValueError("boom")
        except ValueError as exc:
            self.middleware.process_exception(req, exc)

        err = SystemError.objects.get()
        self.assertEqual(err.exception_type, 'ValueError')
        self.assertIn('boom', err.message)
        self.assertIn('ValueError', err.traceback)
        self.assertFalse(err.resolved)

    def test_ignores_http404_and_permission_denied(self):
        req = self._request()
        for exc_class in (Http404, PermissionDenied):
            try:
                raise exc_class("nope")
            except exc_class as exc:
                self.middleware.process_exception(req, exc)
        self.assertEqual(SystemError.objects.count(), 0)

    def test_records_authenticated_user(self):
        admin = make_user('err_admin', 'ADMIN')
        req = self._request(user=admin)
        try:
            raise KeyError("missing")
        except KeyError as exc:
            self.middleware.process_exception(req, exc)
        err = SystemError.objects.get()
        self.assertEqual(err.user_id, admin.id)

    def test_a_broken_logger_does_not_crash_the_handler_itself(self):
        """If SystemError.objects.create() itself fails for some reason,
        process_exception must still return None rather than raising —
        otherwise a bug in the error logger would mask the original error."""
        from unittest.mock import patch
        req = self._request()
        with patch('systemadmin.models.SystemError.objects.create', side_effect=RuntimeError('db down')):
            try:
                raise RuntimeError("original problem")
            except RuntimeError as exc:
                result = self.middleware.process_exception(req, exc)
        self.assertIsNone(result)


class SystemErrorEndToEndTests(TestCase):
    """Confirms the middleware is actually wired into MIDDLEWARE in
    settings.py and fires through a real request/response cycle — the
    process_exception unit tests above call the middleware directly and
    wouldn't catch a settings.py registration mistake."""

    def test_a_real_crash_through_the_full_stack_gets_recorded(self):
        from unittest.mock import patch
        admin = make_user('e2e_admin', 'ADMIN')
        c = Client(raise_request_exception=False)
        c.force_login(admin)

        with patch('systemadmin.views.get_uptime', side_effect=RuntimeError('simulated crash')):
            resp = c.get(reverse('admin-dashboard'))

        self.assertEqual(resp.status_code, 500)
        err = SystemError.objects.get()
        self.assertEqual(err.exception_type, 'RuntimeError')
        self.assertIn('simulated crash', err.message)
        self.assertEqual(err.user_id, admin.id)
        self.assertEqual(err.path, reverse('admin-dashboard'))


class SystemErrorsPageTests(TestCase):
    def setUp(self):
        self.admin = make_user('errpage_admin', 'ADMIN')
        self.client.force_login(self.admin)

    def test_page_shows_unresolved_by_default(self):
        SystemError.objects.create(exception_type='ValueError', path='/x/', resolved=False)
        SystemError.objects.create(exception_type='KeyError', path='/y/', resolved=True)
        resp = self.client.get(reverse('system-errors'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'ValueError')
        self.assertNotContains(resp, 'KeyError')

    def test_resolve_marks_error_resolved(self):
        err = SystemError.objects.create(exception_type='ValueError', path='/x/', resolved=False)
        self.client.post(reverse('resolve-system-error', args=[err.id]))
        err.refresh_from_db()
        self.assertTrue(err.resolved)

    def test_clear_resolved_deletes_only_resolved(self):
        SystemError.objects.create(exception_type='A', path='/a/', resolved=True)
        SystemError.objects.create(exception_type='B', path='/b/', resolved=False)
        self.client.post(reverse('clear-resolved-errors'))
        self.assertEqual(SystemError.objects.count(), 1)
        self.assertFalse(SystemError.objects.first().resolved)

    def test_non_admin_cannot_reach_system_errors(self):
        staff = make_user('errpage_staff', 'STAFF')
        c = Client()
        c.force_login(staff)
        resp = c.get(reverse('system-errors'))
        self.assertNotEqual(resp.status_code, 200)


class AuditLogsTests(TestCase):
    def setUp(self):
        self.admin = make_user('audit_admin', 'ADMIN')
        self.client.force_login(self.admin)

    def test_audit_logs_page_paginates(self):
        for i in range(60):
            AuditLog.objects.create(action='UPDATE', target=f'item {i}')
        resp = self.client.get(reverse('audit-logs'))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['page_obj'].has_next())

    def test_export_csv_returns_csv(self):
        AuditLog.objects.create(action='LOGIN', target='someone')
        resp = self.client.get(reverse('export-logs-csv'))
        self.assertEqual(resp['Content-Type'], 'text/csv')
