from django.shortcuts import render, redirect
from django.contrib.auth.views import LoginView
from django.contrib.auth.decorators import login_required
from documents.models import Document
from django.contrib.auth.decorators import user_passes_test
from audit.utils import log_action

class CustomLoginView(LoginView):
    template_name = 'accounts/login.html'
    redirect_authenticated_user = True


def index(request):
    return render(request, 'index.html')

@login_required
def dashboard_redirect(request):
    """Redirect users to their specific dashboard based on role"""
    user = request.user
    if user.role == "ADMIN":
        return redirect( 'admin-dashboard')
    elif user.role == "SECRETARIAT":
        return redirect('secretariat-dashboard')

    elif user.role == "STAFF":
        return render(request, 'dashboards/staff.html')
    elif user.role == "BARANGAY":
        return redirect('barangay-dashboard')
    elif user.role == "COUNCILOR":
        return redirect('councilor-dashboard')
    else:
        return redirect('login')
    