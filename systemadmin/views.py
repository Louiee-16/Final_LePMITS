from django.shortcuts import render, redirect
from accounts.models import User
from django.apps import apps
from django.db import connection
from django.core.cache import cache
from django.http import HttpResponse
from django.core.management import call_command
from django.contrib import messages
import io, time
from .apps import START_TIME
from django.contrib.auth.decorators import login_required
from .utils import get_media_storage_size
from audit.models import AuditLog
from datetime import timedelta
from django.utils import timezone
from django.contrib.auth import logout
from .models import SystemSetting
from django.contrib.auth.decorators import user_passes_test


###################### FOR DASHBOARD #######################33
@login_required
def admin_dashboard(request):
    user  = get_user_model()
      # Fetch your other stats...
    active_users = user.objects.filter(is_active=True).count()
    failed_logins = AuditLog.objects.filter(action='FAILED_LOGIN').count()
    
   
    storage_used = get_media_storage_size() 
    if not storage_used:
        # If not in cache, calculate it and save for 60 minutes
        storage_used = get_media_storage_size()
        cache.set('media_storage_size', storage_used, 3600)
    stats = {
        
        'uptime': get_uptime(),
        'active_users': active_users,
        'storage_used': storage_used,
        'failed_logins': failed_logins,
    }
    
    recent_logs = AuditLog.objects.all()[:10]
    
    return render(request, 'dashboards/systemadmin.html', {
        'stats': stats,
        'recent_logs': recent_logs,
        'now':timezone.now(),
    })

def get_uptime():
    # Current time minus the start time
    uptime_seconds = int(time.time() - START_TIME)
    # Format as "2 days, 4:12:05"
    return str(timedelta(seconds=uptime_seconds))
@login_required
def create_user_page(request):
    return render(request, 'systemadmin/create_user.html')

from django.shortcuts import redirect
from django.contrib.auth import get_user_model

User = get_user_model()

def create_user(request):
    if not request.user.is_authenticated or request.user.role != 'ADMIN':
        return redirect('logout')
    User = get_user_model()
    if request.method == 'POST':
        first_name = request.POST.get('first_name')
        last_name = request.POST.get('last_name')
        email = request.POST.get('email')
        username = request.POST.get('username')
        role = request.POST.get('role')
        password = request.POST.get('password')

        user = User.objects.create(
            first_name=first_name,
            last_name=last_name,
            username=username,
            email=email,
            role=role
        )

    
        user.set_password(password)
        user.save()

        return redirect('user-management')


################# USER MANAGEMENT ########################33

def user_management(request):
    User = get_user_model() 
    users = User.objects.all()
    total = User.objects.all().count
    councilors = User.objects.filter(role= 'COUNCILOR')
    barangay = User.objects.filter(role = 'BARANGAY')
    secretariat = User.objects.filter(role = 'SECRETARIAT')
    context = {
        'users': users,
        'total':total,
        'councilors':councilors,
        'barangay': barangay,
        'secretariat': secretariat,
    }
    return render(request,'systemadmin/user_management.html', context)

def is_admin(user):
    return user.is_authenticated and (user.is_superuser or getattr(user, 'role', None) == 'ADMIN')
@login_required
@user_passes_test(is_admin)
def general_settings(request):
    settings = SystemSetting.objects.get(id=1)

    if request.method == 'POST':
        # Update Branding
        settings.republic_name = request.POST.get('republic_name')
        settings.city_name = request.POST.get('city_name')
        settings.office_name = request.POST.get('office_name')
        
        # Update Legislative
        settings.current_council_number = request.POST.get('council_number')
        settings.default_venue = request.POST.get('default_venue')
        
        # Update Logo if uploaded
        if request.FILES.get('system_logo'):
            settings.system_logo = request.FILES.get('system_logo')
            
        settings.save()
        messages.success(request, "System settings updated. All documents will now reflect these changes.")
        return redirect('general-settings')

    return render(request, 'systemadmin/general_settings.html')



##################### COUNCIL SETUP ########################333

def council_setup(request):
    return render(request, 'systemadmin/council_setup.html')

from django.shortcuts import get_object_or_404, redirect
from django.contrib.auth.models import User
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.utils.crypto import get_random_string
from .utils import log_action # Assuming the log utility we built earlier

# Security: Only allow System Admins to access these functions
def is_admin(user):
    return user.is_authenticated and (user.is_superuser or getattr(user, 'role', None) == 'ADMIN')

@login_required
@user_passes_test(is_admin)
def edit_user(request, user_id):
    User = get_user_model()

    if request.method == 'POST':
        target_user = get_object_or_404(User, id=user_id)
        
        # Capture old data for the log
        old_role = getattr(target_user, 'role', 'N/A')
        
        # Update standard fields
        target_user.first_name = request.POST.get('first_name')
        target_user.last_name = request.POST.get('last_name')
        target_user.email = request.POST.get('email')
        
        # Update custom role field (assuming it's on the User model or Profile)
        new_role = request.POST.get('role')
        target_user.role = new_role
        
        target_user.save()
        
        log_action(request, action='UPDATE', target=f"Updated profile for {target_user.username} (Role: {new_role})")
        messages.success(request, f"Account for {target_user.username} has been updated.")
        
    return redirect('user-management')

@login_required
@user_passes_test(is_admin)
def reset_password(request, user_id):
    User = get_user_model()
    if request.method == 'POST':
        target_user = get_object_or_404(User, id=user_id)
        
        # Generate a professional random temporary password
        temp_pass = get_random_string(length=12)
        target_user.set_password(temp_pass)
        target_user.save()
        
        log_action(request, action='SECURITY', target=f"Reset password for {target_user.username}")
        
    
        messages.warning(request, f"Password for {target_user.username} reset to: {temp_pass}. Please provide this to the user.")
        
    return redirect('user-management')

@login_required
@user_passes_test(is_admin)
def toggle_user_status(request, user_id):
    User = get_user_model()
    if request.method == 'POST':
        target_user = get_object_or_404(User, id=user_id)
        
 
        if target_user == request.user:
            messages.error(request, "You cannot deactivate your own account.")
            return redirect('user-management')
            

        target_user.is_active = not target_user.is_active
        target_user.save()
        
        status_label = "Enabled" if target_user.is_active else "Deactivated"
        action_type = "UPDATE" if target_user.is_active else "DELETE"
        
        log_action(request, action=action_type, target=f"{status_label} account: {target_user.username}")
        messages.success(request, f"Account {target_user.username} is now {status_label}.")
        
    return redirect('user-management')

############## FOR BACKEND DATABASE ##########################

@login_required
def backend_database(request):
    model_registry = []
    
    start_time = time.perf_counter()
    user  = get_user_model()
    user.objects.count()

    # 3. END THE TIMER
    end_time = time.perf_counter()
    
    query_speed = round((end_time - start_time) * 1000, 2)
    all_models = apps.get_models()
    
    for model in all_models:
        if not model._meta.app_label.startswith('django') and not model._meta.app_label == 'admin':
            model_registry.append({
                'name': model._meta.verbose_name.title(),
                'app_label': model._meta.app_label,
                'records': model.objects.count(),
                'admin_url': f"/admin/{model._meta.app_label}/{model._meta.model_name}/"
            })

    # 2. Get DB Metadata
    context = {
        'model_registry': model_registry,
        'db_vendor': connection.vendor.upper(),  
        'query_speed': query_speed, # <── Pass the real speed here
        'db_name': connection.settings_dict['NAME'],
    }
    
    return render(request, 'systemadmin/backend_database.html', context)

# ── MAINTENANCE ACTIONS ──

@login_required
def clear_system_cache(request):
    """Clears the Django cache."""
    cache.clear()
    messages.success(request, "System cache cleared successfully.")
    return redirect('backend-database')

@login_required
def trigger_backup(request):
    """Generates a JSON backup of the entire database."""
    output = io.StringIO()

    call_command('dumpdata', indent=2, stdout=output)
    
    response = HttpResponse(output.getvalue(), content_type="application/json")
    response['Content-Disposition'] = 'attachment; filename="lepmits_backup.json"'
    return response




