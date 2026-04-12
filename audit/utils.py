# audit/utils.py
from .models import AuditLog

def log_action(request, action, target='', detail='', severity='NORMAL'):
    user = request.user if request.user.is_authenticated else None
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