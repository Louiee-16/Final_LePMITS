# audit/utils.py
from .models import AuditLog

def log_action(request, action, target='', detail='', severity='NORMAL'):
    # getattr, not request.user directly: django.contrib.auth.login() only
    # assigns request.user when it's already present (set by
    # AuthenticationMiddleware in the normal request/response cycle). Code
    # paths that call login() on a bare request — Client.login()/
    # force_login() in tests being the standard example — skip that
    # assignment entirely, so request.user may not exist at all here.
    raw_user = getattr(request, 'user', None)
    user = raw_user if raw_user is not None and raw_user.is_authenticated else None
    ip   = get_client_ip(request)
    AuditLog.objects.create(
        user=user,
        action=action,
        target=target,
        detail=detail,
        severity=severity,
        ip_address=ip
    )

def get_client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')