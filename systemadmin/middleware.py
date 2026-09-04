import logging
import time
import json
import traceback as tb_module
from django.contrib.auth import logout
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse, HttpResponseRedirect
from django.middleware.csrf import rotate_token
from django.shortcuts import render

logger = logging.getLogger(__name__)

# Exceptions that represent normal control flow, not a bug worth an admin's
# attention — never logged as a System Error.
_IGNORED_EXCEPTIONS = (Http404, PermissionDenied)


class SystemErrorLoggingMiddleware:
    """
    Catches every unhandled exception raised while processing a request and
    stores it (path, user, traceback) so it shows up on the System Errors
    page instead of only ever reaching the server console log.

    process_exception() is Django's dedicated hook for this — it fires
    after a view raises, before Django converts that into a 500 response,
    which is why this doesn't also need a try/except around __call__ (that
    would only catch exceptions raised by middleware itself, and risks a
    double-logged entry for the same crash).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, _IGNORED_EXCEPTIONS):
            return None

        try:
            from .models import SystemError
            from .utils import get_client_ip

            user = getattr(request, 'user', None)
            SystemError.objects.create(
                method=request.method,
                path=request.path[:500],
                exception_type=type(exception).__name__,
                message=str(exception)[:5000],
                traceback=tb_module.format_exc(),
                user=user if (user is not None and user.is_authenticated) else None,
                ip_address=get_client_ip(request),
            )
        except Exception:
            # Never let the error-logger itself crash the crash — fall back
            # to the console log Django already writes for 500s.
            logger.exception("SystemErrorLoggingMiddleware failed to record an exception")

        return None  # let Django's normal 500/DEBUG handling continue


class MaintenanceModeMiddleware:
    """
    Enforces the "Maintenance Mode" toggle from General Settings.
    Previously that switch was saved to the database but nothing ever
    read it back — the feature had no effect. When enabled, everyone
    except system admins is shown a maintenance page instead of the app.
    """

    EXEMPT_PREFIXES = ('/static/', '/media/', '/admin/', '/systemadmin/')
    EXEMPT_PATHS = {
        '/login/', '/logout/', '/otp/verify/',
        '/session/status/', '/session/relogin/', '/session/heartbeat/',
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .models import SystemSetting

        setting = SystemSetting.objects.filter(id=1).only('maintenance_mode').first()
        if setting and setting.maintenance_mode:
            user = request.user
            is_admin_user = user.is_authenticated and (
                user.is_superuser or getattr(user, 'role', None) == 'ADMIN'
            )
            path = request.path
            exempt = path in self.EXEMPT_PATHS or path.startswith(self.EXEMPT_PREFIXES)

            if not is_admin_user and not exempt:
                return render(request, 'systemadmin/maintenance.html', status=503)

        return self.get_response(request)


class SessionIdleTimeoutMiddleware:
    
    EXEMPT_URLS = [
        '/session/status/',
        '/session/relogin/',
    ]
    
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            last_activity = request.session.get('last_activity')
            now = int(time.time())

            if last_activity and (now - last_activity) > 1800:
                logout(request)
                rotate_token(request)

                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'session_expired': True}, status=401)

                return HttpResponseRedirect('/login/?reason=timeout')

            # ✅ Only update last_activity for real user actions
            elif request.path not in self.EXEMPT_URLS:
                request.session['last_activity'] = now
                request.session.modified = True

        return self.get_response(request)