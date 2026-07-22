from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.apps import apps
from django.db import connection
from django.core.cache import cache
from django.http import HttpResponse
from django.core.management import call_command
from django.contrib import messages
from django.utils import timezone
from django.utils.crypto import get_random_string
import csv, io, time
from datetime import timedelta

from .apps import START_TIME
from .models import SystemSetting
from .utils import log_action, get_media_storage_size
from audit.models import AuditLog

User = get_user_model()


def is_admin(user):
    return user.is_authenticated and (user.is_superuser or getattr(user, 'role', None) == 'ADMIN')


def get_uptime():
    return str(timedelta(seconds=int(time.time() - START_TIME)))


# ── DASHBOARD ──────────────────────────────────────────────────────────────

@login_required
def admin_dashboard(request):
    active_users  = User.objects.filter(is_active=True).count()
    failed_logins = AuditLog.objects.filter(action='FAILED_LOGIN').count()
    storage_used  = cache.get('media_storage_size') or get_media_storage_size()
    cache.set('media_storage_size', storage_used, 3600)

    return render(request, 'dashboards/systemadmin.html', {
        'stats': {
            'uptime':        get_uptime(),
            'active_users':  active_users,
            'storage_used':  storage_used,
            'failed_logins': failed_logins,
        },
        'recent_logs': AuditLog.objects.all().order_by('-timestamp')[:10],
        'now':         timezone.now(),
    })


# ── USER MANAGEMENT ────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def create_user_page(request):
    return render(request, 'systemadmin/create_user.html')


@login_required
@user_passes_test(is_admin)
def create_user(request):
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name  = request.POST.get('last_name',  '').strip()
        email      = request.POST.get('email',      '').strip()
        username   = request.POST.get('username',   '').strip()
        role       = request.POST.get('role',       'STAFF')
        password   = request.POST.get('password',   '')
        confirm    = request.POST.get('confirm_password', '')
        is_active  = request.POST.get('is_active') == 'on'

        if password != confirm:
            messages.error(request, "Passwords do not match.")
            return redirect('create-user-page')

        if User.objects.filter(username=username).exists():
            messages.error(request, f"Username '{username}' is already taken.")
            return redirect('create-user-page')

        user = User.objects.create(
            first_name=first_name,
            last_name=last_name,
            username=username,
            email=email,
            role=role,
            is_active=is_active,
        )
        user.set_password(password)
        user.save()

        if role == 'COUNCILOR':
            from councilors.models import Councilor
            district = int(request.POST.get('district', 1))
            Councilor.objects.create(
                user=user,
                name=f"{first_name} {last_name}".strip(),
                email=email,
                district=district,
            )

        if role == 'BARANGAY':
            from barangay.models import Barangay
            Barangay.objects.get_or_create(
                user=user,
                defaults={
                    'barangay_name': request.POST.get('office_or_district', f"Brgy. {last_name}"),
                    'captain':       f"{first_name} {last_name}".strip(),
                    'email':         email,
                }
            )

        log_action(request, action='CREATE', target=f"Created account for {username} (Role: {role})")
        messages.success(request, f"Account for {username} created successfully.")
        return redirect('user-management')

    return redirect('create-user-page')


@login_required
def user_management(request):
    users       = User.objects.all().order_by('-date_joined')
    councilors  = users.filter(role='COUNCILOR')
    barangay    = users.filter(role='BARANGAY')
    secretariat = users.filter(role='SECRETARIAT')
    return render(request, 'systemadmin/user_management.html', {
        'users':       users,
        'total':       users.count(),
        'councilors':  councilors,
        'barangay':    barangay,
        'secretariat': secretariat,
    })


@login_required
@user_passes_test(is_admin)
def edit_user(request, user_id):
    if request.method == 'POST':
        target_user = get_object_or_404(User, id=user_id)
        old_role = getattr(target_user, 'role', 'N/A')
        target_user.first_name = request.POST.get('first_name', target_user.first_name)
        target_user.last_name  = request.POST.get('last_name',  target_user.last_name)
        target_user.email      = request.POST.get('email',      target_user.email)
        new_role = request.POST.get('role', target_user.role)
        target_user.role = new_role
        target_user.save()
        log_action(request, action='UPDATE', target=f"Updated {target_user.username} (Role: {old_role} → {new_role})")
        messages.success(request, f"Account for {target_user.username} updated.")
    return redirect('user-management')


@login_required
@user_passes_test(is_admin)
def reset_password(request, user_id):
    if request.method == 'POST':
        target_user = get_object_or_404(User, id=user_id)
        temp_pass = get_random_string(length=12)
        target_user.set_password(temp_pass)
        target_user.save()
        log_action(request, action='UPDATE', target=f"Reset password for {target_user.username}", severity='HIGH')
        messages.warning(request, f"Password for {target_user.username} reset to: {temp_pass}. Provide to user securely.")
    return redirect('user-management')


@login_required
@user_passes_test(is_admin)
def toggle_user_status(request, user_id):
    if request.method == 'POST':
        target_user = get_object_or_404(User, id=user_id)
        if target_user == request.user:
            messages.error(request, "You cannot deactivate your own account.")
            return redirect('user-management')
        target_user.is_active = not target_user.is_active
        target_user.save()
        label = "Enabled" if target_user.is_active else "Deactivated"
        log_action(request, action='UPDATE', target=f"{label} account: {target_user.username}")
        messages.success(request, f"Account {target_user.username} is now {label}.")
    return redirect('user-management')


# ── GENERAL SETTINGS ───────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def general_settings(request):
    setting, _ = SystemSetting.objects.get_or_create(id=1)

    if request.method == 'POST':
        setting.republic_name          = request.POST.get('republic_name', setting.republic_name)
        setting.city_name              = request.POST.get('city_name',     setting.city_name)
        setting.office_name            = request.POST.get('office_name',   setting.office_name)
        setting.current_council_number = request.POST.get('council_number', setting.current_council_number)
        setting.default_venue          = request.POST.get('default_venue', setting.default_venue)
        setting.maintenance_mode       = request.POST.get('maintenance_mode') == 'on'
        if request.FILES.get('system_logo'):
            setting.system_logo = request.FILES['system_logo']
        setting.save()
        messages.success(request, "System settings updated. All documents will now reflect these changes.")
        return redirect('general-settings')

    return render(request, 'systemadmin/general_settings.html', {'setting': setting})


# ── COUNCIL SETUP ──────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def council_setup(request):
    from councilors.models import Councilor
    councilors = Councilor.objects.filter(is_active=True).select_related('user').order_by('district', 'name')
    district_1 = councilors.filter(district=1)
    district_2 = councilors.filter(district=2)
    setting, _ = SystemSetting.objects.get_or_create(id=1)
    return render(request, 'systemadmin/council_setup.html', {
        'councilors': councilors,
        'district_1': district_1,
        'district_2': district_2,
        'setting':    setting,
    })


# ── AUDIT LOGS ─────────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def audit_logs(request):
    today = timezone.now().date()
    logs  = AuditLog.objects.all().order_by('-timestamp')
    return render(request, 'systemadmin/audit_logs.html', {
        'logs':            logs,
        'events_today':    logs.filter(timestamp__date=today).count(),
        'security_alerts': logs.filter(severity='HIGH').count(),
    })


@login_required
@user_passes_test(is_admin)
def export_logs_csv(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="audit_logs.csv"'
    writer = csv.writer(response)
    writer.writerow(['Timestamp', 'User', 'Action', 'Target', 'Detail', 'Severity', 'IP Address'])
    for log in AuditLog.objects.all().order_by('-timestamp'):
        writer.writerow([
            log.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            log.user.username if log.user else 'System',
            log.get_action_display(),
            log.target,
            log.detail,
            log.severity,
            log.ip_address or '',
        ])
    return response


# ── BACKEND DATABASE ───────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def backend_database(request):
    start = time.perf_counter()
    User.objects.count()
    query_speed = round((time.perf_counter() - start) * 1000, 2)

    excluded_apps = {'admin', 'contenttypes', 'sessions', 'auth'}
    model_registry = []
    for model in apps.get_models():
        label = model._meta.app_label
        if label not in excluded_apps and not label.startswith('django'):
            try:
                count = model.objects.count()
            except Exception:
                count = '—'
            model_registry.append({
                'name':      model._meta.verbose_name.title(),
                'app_label': label,
                'records':   count,
                'admin_url': f"/admin/{label}/{model._meta.model_name}/",
            })

    return render(request, 'systemadmin/backend_database.html', {
        'model_registry': model_registry,
        'db_vendor':      connection.vendor.upper(),
        'query_speed':    query_speed,
        'db_name':        connection.settings_dict['NAME'],
        'model_count':    len(model_registry),
    })


@login_required
@user_passes_test(is_admin)
def clear_system_cache(request):
    cache.clear()
    messages.success(request, "System cache cleared successfully.")
    return redirect('backend-database')


@login_required
@user_passes_test(is_admin)
def trigger_backup(request):
    output = io.StringIO()
    call_command('dumpdata', indent=2, stdout=output)
    response = HttpResponse(output.getvalue(), content_type='application/json')
    response['Content-Disposition'] = 'attachment; filename="lepmits_backup.json"'
    return response
