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
    



from django.http import HttpResponseRedirect, JsonResponse
import json
from django.contrib.auth import authenticate, login
import time
def session_status(request):
    if not request.user.is_authenticated:
        return JsonResponse({'alive': False, 'remaining': 0})
    
    last_activity = request.session.get('last_activity', int(time.time()))
    remaining = 1800 - (int(time.time()) - last_activity)
    print(remaining)
    return JsonResponse({
        'alive': True,
        'remaining': max(remaining, 0)
    })

def session_relogin(request):
    if request.method == "POST":
        data = json.loads(request.body)
        username = data.get('username')
        password = data.get('password')
        user = authenticate(request,username=username, password=password)

        if user:
            login(request, user)
            request.session['last_activity'] = int(time.time())
            return JsonResponse({'success':True})

        return JsonResponse({'success':False, 'error':'Invalid credentials.'})
    return JsonResponse({'error':'Method not allowed.'}, status =405)


def session_heartbeat(request):
    if request.user.is_authenticated:
        return JsonResponse({'alive': True})
    return JsonResponse({'alive': False})