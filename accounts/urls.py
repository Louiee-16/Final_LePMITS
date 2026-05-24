from django.urls import path
from . import views
urlpatterns = [
    path('session/status/', views.session_status, name='session_status'),
    path('session/relogin/', views.session_relogin, name='session_relogin'),
    path('session/heartbeat/', views.session_heartbeat, name='session_heartbeat'),
]
