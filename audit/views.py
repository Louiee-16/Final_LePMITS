# audit/views.py
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from .models import AuditLog
import csv
from django.shortcuts import get_object_or_404, redirect, render


@login_required
def audit_logs(request):
    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    logs = AuditLog.objects.select_related('user').all()

    # Filters
    action   = request.GET.get('action', 'ALL')
    severity = request.GET.get('severity', 'ALL')
    search   = request.GET.get('search', '').strip()

    if action != 'ALL':
        logs = logs.filter(action=action)
    if severity != 'ALL':
        logs = logs.filter(severity=severity)
    if search:
        logs = logs.filter(target__icontains=search) | \
               logs.filter(detail__icontains=search)

    # Counts for the summary cards
    from django.utils import timezone
    today = timezone.now().date()

    return render(request, 'systemadmin/audit_logs.html', {
        'logs':           logs[:200],  # paginate later
        'events_today':   AuditLog.objects.filter(timestamp__date=today).count(),
        'security_alerts': AuditLog.objects.filter(severity='HIGH').count(),
    })


@login_required
def export_logs_csv(request):
    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="audit_logs.csv"'

    writer = csv.writer(response)
    writer.writerow(['Timestamp', 'User', 'Action', 'Target', 'Detail', 'IP', 'Severity'])

    for log in AuditLog.objects.select_related('user').all():
        writer.writerow([
            log.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            log.user.username if log.user else '—',
            log.get_action_display(),
            log.target,
            log.detail,
            log.ip_address,
            log.severity,
        ])

    return response