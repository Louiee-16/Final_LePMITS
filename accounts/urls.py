from django.urls import path
from . import views

urlpatterns = [
    path('otp/verify/', views.verify_otp, name='otp-verify'),
    path('change-password/', views.change_password_required, name='change-password-required'),
    path('session/status/', views.session_status, name='session_status'),
    path('session/relogin/', views.session_relogin, name='session_relogin'),
    path('session/heartbeat/', views.session_heartbeat, name='session_heartbeat'),
]
