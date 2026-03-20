from django.shortcuts import render, redirect
from django.contrib.auth.views import LoginView
from django.contrib.auth.decorators import login_required
from documents.models import Document


class CustomLoginView(LoginView):
    template_name = 'accounts/login.html'
    redirect_authenticated_user = True
@login_required
def dashboard_redirect(request):
    """Redirect users to their specific dashboard based on role"""
    user = request.user
    print(f"DEBUG: User {user.username} has role: '{user.role}'")

    if user.role == "ADMIN":
        return render(request, 'dashboards/admin.html')
    elif user.role == "SECRETARIAT":
            latest_docs = Document.objects.all().order_by('-updated_at')[:5]
            return render(request, 'dashboards/secretariat.html',{'latest_docs':latest_docs})
    elif user.role == "STAFF":
        return render(request, 'dashboards/staff.html')
    elif user.role == "BARANGAY_SEC":
        return render(request, 'dashboards/barangay_sec.html')
    elif user.role == "COUNCILOR":
        return render(request, 'dashboards/councilor.html')
    else:
        return redirect('login')
    