from audit.models import AuditLog

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


import os
from django.conf import settings

def get_media_storage_size():
    """Calculates total size of the MEDIA_ROOT directory."""
    total_size = 0
    start_path = settings.MEDIA_ROOT
    
    # Check if the media directory exists to prevent errors
    if not os.path.exists(start_path):
        return "0 KB"

    for dirpath, dirnames, filenames in os.walk(start_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            # skip if it is symbolic link
            if not os.path.islink(fp):
                total_size += os.path.getsize(fp)

    # Convert bytes to human-readable format
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if total_size < 1024:
            return f"{total_size:.1f} {unit}"
        total_size /= 1024