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
    """Calculates total size of MEDIA_ROOT plus DOCUMENT_EDITOR_STORAGE_ROOT.

    The latter (documents/wopi.py's working + archived .docx/PDF files)
    was moved out from under MEDIA_ROOT specifically so it's never
    reachable via a raw MEDIA_URL path in production — see that setting's
    comment in config/settings.py. It's still real disk usage this app is
    responsible for, so it's still counted here; it just isn't under
    MEDIA_ROOT itself anymore."""
    total_size = 0
    start_paths = [settings.MEDIA_ROOT, settings.DOCUMENT_EDITOR_STORAGE_ROOT]

    for start_path in start_paths:
        if not os.path.exists(start_path):
            continue
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