import json
import time

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import authenticate, login as auth_login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_protect
from django.core.mail import send_mail
from django.conf import settings
from documents.models import Document
from audit.utils import log_action, get_client_ip
from . import ratelimit
from .models import TwoFactorCode


def _mask_email(email):
    """Show j***e@gmail.com so the user knows where to look."""
    try:
        local, domain = email.split('@', 1)
        masked = local[0] + '***' + local[-1] if len(local) > 2 else local[0] + '***'
        return f"{masked}@{domain}"
    except Exception:
        return email


@ensure_csrf_cookie
@csrf_protect
def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    form = AuthenticationForm(request, data=request.POST or None)
    error = None

    if request.method == 'POST':
        client_ip = get_client_ip(request)
        if ratelimit.is_locked_out('login', client_ip):
            error = 'Too many failed login attempts. Please try again in a few minutes.'
            return render(request, 'accounts/login.html', {'form': form, 'error': error})

        if form.is_valid():
            user = form.get_user()
            ratelimit.clear('login', client_ip)

            if not user.email:
                # No email on record — log straight in (edge case for admin accounts).
                # auth_login() fires the user_logged_in signal, which audit.signals
                # already records — do not log this again here.
                auth_login(request, user)
                return redirect('dashboard')

            # Generate OTP and send email
            otp = TwoFactorCode.generate_for(user)
            try:
                send_mail(
                    subject='LePMITS — Your Verification Code',
                    message=(
                        f"Hello {user.get_full_name() or user.username},\n\n"
                        f"Your one-time verification code is:\n\n"
                        f"  {otp.code}\n\n"
                        f"This code is valid for 10 minutes. Do not share it with anyone.\n\n"
                        f"If you did not attempt to sign in, please contact the Secretariat immediately.\n\n"
                        f"— LePMITS, Sangguniang Panlungsod ng San Juan City"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    fail_silently=False,
                )
            except Exception:
                error = 'Could not send verification email. Contact the Secretariat.'
                return render(request, 'accounts/login.html', {'form': form, 'error': error})

            request.session['2fa_user_id'] = user.pk
            request.session['2fa_backend'] = user.backend if hasattr(user, 'backend') else 'django.contrib.auth.backends.ModelBackend'
            return redirect('otp-verify')
        else:
            error = 'Invalid username or password.'
            ratelimit.record_failure('login', client_ip)
            attempted_username = request.POST.get('username', '').strip()
            log_action(
                request, 'FAILED_LOGIN',
                target=attempted_username or '(unknown)',
                detail='Invalid username or password at login.',
                severity='HIGH',
            )

    return render(request, 'accounts/login.html', {'form': form, 'error': error})


@csrf_protect
def verify_otp(request):
    user_id = request.session.get('2fa_user_id')
    if not user_id:
        return redirect('login')

    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = get_object_or_404(User, pk=user_id)
    masked = _mask_email(user.email)
    error = None

    if request.method == 'POST':
        if 'resend' in request.POST:
            otp = TwoFactorCode.generate_for(user)
            try:
                send_mail(
                    subject='LePMITS — New Verification Code',
                    message=(
                        f"Hello {user.get_full_name() or user.username},\n\n"
                        f"Your new verification code is:\n\n"
                        f"  {otp.code}\n\n"
                        f"This code is valid for 10 minutes.\n\n"
                        f"— LePMITS, Sangguniang Panlungsod ng San Juan City"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    fail_silently=True,
                )
            except Exception:
                pass
            return render(request, 'accounts/otp_verify.html', {
                'masked_email': masked, 'resent': True
            })

        if ratelimit.is_locked_out('otp', user.pk):
            error = 'Too many incorrect attempts. Please request a new code and try again in a few minutes.'
            return render(request, 'accounts/otp_verify.html', {
                'masked_email': masked, 'error': error,
            })

        code = request.POST.get('code', '').strip()
        otp = TwoFactorCode.objects.filter(
            user=user, code=code, is_used=False
        ).order_by('-created_at').first()

        if not otp:
            error = 'Invalid code. Please check your email and try again.'
            ratelimit.record_failure('otp', user.pk)
        elif otp.is_expired():
            error = 'This code has expired. Please request a new one.'
            ratelimit.record_failure('otp', user.pk)
        else:
            otp.is_used = True
            otp.save()
            ratelimit.clear('otp', user.pk)
            backend = request.session.pop('2fa_backend', 'django.contrib.auth.backends.ModelBackend')
            user.backend = backend
            # auth_login() fires the user_logged_in signal, which audit.signals
            # already records — do not log this again here.
            auth_login(request, user)
            request.session.pop('2fa_user_id', None)
            return redirect('dashboard')

    return render(request, 'accounts/otp_verify.html', {
        'masked_email': masked,
        'error': error,
    })


@login_required
def change_password_required(request):
    if not request.user.must_change_password:
        return redirect('dashboard')

    error = None
    if request.method == 'POST':
        new_password = request.POST.get('new_password', '')
        confirm = request.POST.get('confirm_password', '')

        if new_password != confirm:
            error = 'Passwords do not match.'
        else:
            try:
                validate_password(new_password, user=request.user)
            except ValidationError as exc:
                error = ' '.join(exc.messages)

        if not error:
            request.user.set_password(new_password)
            request.user.must_change_password = False
            request.user.save()
            update_session_auth_hash(request, request.user)
            log_action(
                request, 'PASSWORD_CHANGE',
                target=request.user.username,
                detail='Mandatory password change completed after admin provisioning/reset.',
            )
            return redirect('dashboard')

    return render(request, 'accounts/change_password_required.html', {'error': error})


# Keep this alias so config/urls.py import doesn't break while we update it
class CustomLoginView:
    @staticmethod
    def as_view():
        return login_view


def index(request):
    from documents.models import Document, LegacyDocument
    from secretariat.models import Session
    from django.utils import timezone

    approved_count   = Document.objects.filter(status='APPROVED').count()
    legacy_count     = LegacyDocument.objects.count()
    ordinance_count  = Document.objects.filter(status='APPROVED', doc_type='ORDINANCE').count() + \
                       LegacyDocument.objects.filter(doc_type='ORDINANCE').count()
    resolution_count = Document.objects.filter(status='APPROVED', doc_type='RESOLUTION').count() + \
                       LegacyDocument.objects.filter(doc_type='RESOLUTION').count()

    recent_docs = list(Document.objects.filter(status='APPROVED').order_by('-updated_at')[:5]) + \
                  list(LegacyDocument.objects.order_by('-uploaded_at')[:3])

    return render(request, 'index.html', {
        'total_docs':      approved_count + legacy_count,
        'ordinance_count': ordinance_count,
        'resolution_count': resolution_count,
        'recent_docs':     recent_docs,
        'gazette_site_url': settings.GAZETTE_SITE_URL,
    })

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
    



def session_status(request):
    if not request.user.is_authenticated:
        return JsonResponse({'alive': False, 'remaining': 0})
    
    last_activity = request.session.get('last_activity', int(time.time()))
    remaining = 1800 - (int(time.time()) - last_activity)
    
    return JsonResponse({
        'alive': True,
        'remaining': max(remaining, 0)
    })

def session_relogin(request):
    if request.method == "POST":
        client_ip = get_client_ip(request)
        if ratelimit.is_locked_out('relogin', client_ip):
            return JsonResponse({'success': False, 'error': 'Too many failed attempts. Please try again in a few minutes.'}, status=429)

        data = json.loads(request.body)
        username = data.get('username')
        password = data.get('password')
        user = authenticate(request, username=username, password=password)

        if user:
            ratelimit.clear('relogin', client_ip)
            auth_login(request, user)
            request.session['last_activity'] = int(time.time())
            return JsonResponse({'success': True})

        ratelimit.record_failure('relogin', client_ip)
        return JsonResponse({'success': False, 'error': 'Invalid credentials.'})
    return JsonResponse({'error': 'Method not allowed.'}, status=405)


def session_heartbeat(request):
    if request.user.is_authenticated:
        return JsonResponse({'alive': True})
    return JsonResponse({'alive': False})