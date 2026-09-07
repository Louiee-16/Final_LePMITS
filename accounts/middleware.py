from django.http import HttpResponseRedirect
from django.urls import reverse


class ForcePasswordChangeMiddleware:
    """
    Blocks a user flagged must_change_password from reaching anything but
    the change-password page (and logout/static/session-keepalive) until
    they set their own password. Covers admin-provisioned accounts and
    admin password resets, both created with a password only the admin knows.
    """

    EXEMPT_PATHS = {'/logout/', '/session/status/', '/session/heartbeat/'}
    EXEMPT_PREFIXES = ('/static/', '/media/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and getattr(user, 'must_change_password', False):
            path = request.path
            change_password_path = reverse('change-password-required')
            if (
                path != change_password_path
                and path not in self.EXEMPT_PATHS
                and not path.startswith(self.EXEMPT_PREFIXES)
            ):
                return HttpResponseRedirect(change_password_path)

        return self.get_response(request)
