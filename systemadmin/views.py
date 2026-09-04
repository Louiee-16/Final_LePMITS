from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.apps import apps
from django.db import connection, transaction
from django.core.cache import cache
from django.http import HttpResponse
from django.views.decorators.http import require_POST
from django.core.management import call_command
from django.core.paginator import Paginator
from django.contrib import messages
from django.utils import timezone
from django.utils.crypto import get_random_string
import csv, io, time
from datetime import timedelta

from .apps import START_TIME
from .models import SystemSetting, SystemError
from .utils import log_action, get_media_storage_size
from audit.models import AuditLog

User = get_user_model()


def is_admin(user):
    return user.is_authenticated and (user.is_superuser or getattr(user, 'role', None) == 'ADMIN')


def _is_only_active_admin(user):
    """True if `user` is currently the sole active System Administrator —
    used to block actions that would lock everyone out of the console."""
    return user.role == 'ADMIN' and user.is_active and not User.objects.filter(
        role='ADMIN', is_active=True
    ).exclude(id=user.id).exists()


def get_uptime():
    return str(timedelta(seconds=int(time.time() - START_TIME)))


# ── DASHBOARD ──────────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def admin_dashboard(request):
    active_users  = User.objects.filter(is_active=True).count()
    failed_logins = AuditLog.objects.filter(action='FAILED_LOGIN').count()
    storage_used  = cache.get('media_storage_size') or get_media_storage_size()
    cache.set('media_storage_size', storage_used, 3600)

    total_users = User.objects.count() or 1
    role_breakdown = []
    for code, label in User.ROLES:
        count = User.objects.filter(role=code).count()
        if count:
            role_breakdown.append({
                'label':   label,
                'count':   count,
                'percent': round(count * 100 / total_users),
            })

    return render(request, 'dashboards/systemadmin.html', {
        'stats': {
            'uptime':          get_uptime(),
            'active_users':    active_users,
            'storage_used':    storage_used,
            'failed_logins':   failed_logins,
            'unresolved_errors': SystemError.objects.filter(resolved=False).count(),
        },
        'recent_logs':    AuditLog.objects.select_related('user').order_by('-timestamp')[:10],
        'recent_errors':  SystemError.objects.filter(resolved=False).order_by('-timestamp')[:5],
        'role_breakdown': role_breakdown,
        'setting':        SystemSetting.objects.get_or_create(id=1)[0],
        'now':            timezone.now(),
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

        valid_roles = {choice for choice, _ in User.ROLES}

        if not username or not password:
            messages.error(request, "Username and password are required.")
            return redirect('create-user-page')

        if password != confirm:
            messages.error(request, "Passwords do not match.")
            return redirect('create-user-page')

        try:
            validate_password(password, user=User(username=username, email=email,
                                                    first_name=first_name, last_name=last_name))
        except ValidationError as exc:
            for err in exc.messages:
                messages.error(request, err)
            return redirect('create-user-page')

        if role not in valid_roles:
            messages.error(request, "Invalid role selected.")
            return redirect('create-user-page')

        if User.objects.filter(username=username).exists():
            messages.error(request, f"Username '{username}' is already taken.")
            return redirect('create-user-page')

        if email and User.objects.filter(email__iexact=email).exists():
            messages.error(request, f"An account with email '{email}' already exists.")
            return redirect('create-user-page')

        barangay_name = request.POST.get('office_or_district', '').strip() or f"Brgy. {last_name}".strip()
        if role == 'BARANGAY':
            from barangay.models import Barangay
            if Barangay.objects.filter(barangay_name__iexact=barangay_name).exists():
                messages.error(
                    request,
                    f"A barangay named '{barangay_name}' is already registered. "
                    "Provide a distinct barangay/office name."
                )
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
            Barangay.objects.create(
                user=user,
                barangay_name=barangay_name,
                captain=f"{first_name} {last_name}".strip(),
                email=email,
            )

        log_action(request, action='CREATE', target=f"Created account for {username} (Role: {role})")
        messages.success(request, f"Account for {username} created successfully.")
        return redirect('user-management')

    return redirect('create-user-page')


@login_required
@user_passes_test(is_admin)
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
def export_users_csv(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="lepmits_users.csv"'
    writer = csv.writer(response)
    writer.writerow(['Username', 'First Name', 'Last Name', 'Email', 'Role', 'Status', 'Last Login', 'Date Joined'])
    for u in User.objects.all().order_by('-date_joined'):
        writer.writerow([
            u.username,
            u.first_name,
            u.last_name,
            u.email,
            u.get_role_display(),
            'Active' if u.is_active else 'Disabled',
            u.last_login.strftime('%Y-%m-%d %H:%M:%S') if u.last_login else 'Never',
            u.date_joined.strftime('%Y-%m-%d %H:%M:%S'),
        ])
    return response


@login_required
@user_passes_test(is_admin)
def edit_user(request, user_id):
    if request.method == 'POST':
        target_user = get_object_or_404(User, id=user_id)
        old_role = getattr(target_user, 'role', 'N/A')
        new_role = request.POST.get('role', target_user.role)
        valid_roles = {choice for choice, _ in User.ROLES}

        if new_role not in valid_roles:
            messages.error(request, "Invalid role selected — no changes were saved.")
            return redirect('user-management')

        if target_user == request.user and old_role == 'ADMIN' and new_role != 'ADMIN':
            messages.error(request, "You cannot remove your own System Administrator role.")
            return redirect('user-management')

        if old_role == 'ADMIN' and new_role != 'ADMIN' and _is_only_active_admin(target_user):
            messages.error(
                request,
                f"Cannot change {target_user.username}'s role — they are the only active "
                "System Administrator. Promote another account first."
            )
            return redirect('user-management')

        email = request.POST.get('email', target_user.email)
        if email and User.objects.filter(email__iexact=email).exclude(id=target_user.id).exists():
            messages.error(request, f"An account with email '{email}' already exists.")
            return redirect('user-management')

        target_user.first_name = request.POST.get('first_name', target_user.first_name)
        target_user.last_name  = request.POST.get('last_name',  target_user.last_name)
        target_user.email      = email
        target_user.role       = new_role
        target_user.save()

        if old_role != new_role:
            from councilors.models import Councilor
            from barangay.models import Barangay

            if old_role == 'COUNCILOR' and new_role != 'COUNCILOR':
                Councilor.objects.filter(user=target_user).update(is_active=False)
            if old_role == 'BARANGAY' and new_role != 'BARANGAY':
                Barangay.objects.filter(user=target_user).update(is_active=False)

            if new_role == 'COUNCILOR' and old_role != 'COUNCILOR':
                if not Councilor.objects.filter(user=target_user).update(is_active=True):
                    messages.warning(
                        request,
                        f"{target_user.username} has no councilor seat on file — "
                        "assign a district for them via Council Setup."
                    )
            if new_role == 'BARANGAY' and old_role != 'BARANGAY':
                if not Barangay.objects.filter(user=target_user).update(is_active=True):
                    messages.warning(
                        request,
                        f"{target_user.username} has no barangay office on file — "
                        "register one separately."
                    )

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

        if _is_only_active_admin(target_user):
            messages.error(
                request,
                f"Cannot deactivate {target_user.username} — they are the only active "
                "System Administrator. Promote another account first."
            )
            return redirect('user-management')

        target_user.is_active = not target_user.is_active
        target_user.save()

        # Keep linked profile records in sync with login access — otherwise a
        # deactivated councilor/barangay account still shows as "active" on
        # rosters that query Councilor/Barangay directly instead of User.
        from councilors.models import Councilor
        from barangay.models import Barangay
        Councilor.objects.filter(user=target_user).update(is_active=target_user.is_active)
        Barangay.objects.filter(user=target_user).update(is_active=target_user.is_active)

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
        messages.success(request, "System settings updated.")
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
    logs  = AuditLog.objects.select_related('user').order_by('-timestamp')

    events_today    = logs.filter(timestamp__date=today).count()
    security_alerts = logs.filter(severity='HIGH').count()

    paginator = Paginator(logs, 50)
    page_obj  = paginator.get_page(request.GET.get('page'))

    return render(request, 'systemadmin/audit_logs.html', {
        'logs':            page_obj,
        'page_obj':        page_obj,
        'events_today':    events_today,
        'security_alerts': security_alerts,
    })


@login_required
@user_passes_test(is_admin)
def export_logs_csv(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="audit_logs.csv"'
    writer = csv.writer(response)
    writer.writerow(['Timestamp', 'User', 'Action', 'Target', 'Detail', 'Severity', 'IP Address'])
    for log in AuditLog.objects.select_related('user').order_by('-timestamp'):
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


# ── SYSTEM ERRORS ──────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def system_errors(request):
    errors = SystemError.objects.select_related('user').order_by('-timestamp')

    status = request.GET.get('status', 'unresolved')
    if status == 'unresolved':
        errors = errors.filter(resolved=False)
    elif status == 'resolved':
        errors = errors.filter(resolved=True)

    paginator = Paginator(errors, 25)
    page_obj  = paginator.get_page(request.GET.get('page'))

    return render(request, 'systemadmin/system_errors.html', {
        'errors':            page_obj,
        'page_obj':          page_obj,
        'status':            status,
        'unresolved_count':  SystemError.objects.filter(resolved=False).count(),
        'total_count':       SystemError.objects.count(),
    })


@login_required
@user_passes_test(is_admin)
@require_POST
def resolve_system_error(request, error_id):
    err = get_object_or_404(SystemError, id=error_id)
    err.resolved = True
    err.save(update_fields=['resolved'])
    messages.success(request, "Error marked as resolved.")
    return redirect(request.META.get('HTTP_REFERER') or 'system-errors')


@login_required
@user_passes_test(is_admin)
@require_POST
def clear_resolved_errors(request):
    deleted, _ = SystemError.objects.filter(resolved=True).delete()
    messages.success(request, f"Cleared {deleted} resolved error record(s).")
    return redirect('system-errors')


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
                # Savepoint, not a bare try/except — on Postgres a failed
                # query poisons the whole surrounding transaction (every
                # later query errors with "current transaction is
                # aborted") unless the failure is contained to its own
                # savepoint here.
                with transaction.atomic():
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
@require_POST
def clear_system_cache(request):
    cache.clear()
    log_action(request, action='UPDATE', target='Cleared system cache')
    messages.success(request, "System cache cleared successfully.")
    return redirect('backend-database')


@login_required
@user_passes_test(is_admin)
@require_POST
def optimize_database(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('ANALYZE;' if connection.vendor in ('postgresql', 'sqlite') else 'SELECT 1;')
        log_action(request, action='UPDATE', target='Ran database optimization (ANALYZE)')
        messages.success(request, "Database statistics optimized.")
    except Exception as exc:
        messages.error(request, f"Optimization failed: {exc}")
    return redirect('backend-database')


@login_required
@user_passes_test(is_admin)
@require_POST
def trigger_backup(request):
    output = io.StringIO()
    call_command('dumpdata', indent=2, stdout=output)
    log_action(request, action='CREATE', target='Downloaded full database backup', severity='HIGH')
    response = HttpResponse(output.getvalue(), content_type='application/json')
    response['Content-Disposition'] = 'attachment; filename="lepmits_backup.json"'
    return response
