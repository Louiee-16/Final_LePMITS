"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
import re
from django.contrib import admin
from django.conf import settings
from django.urls import path, re_path, include
from accounts.views import login_view, dashboard_redirect
from django.contrib.auth import views as auth_views
from django.views.static import serve as static_serve
from django.views.decorators.clickjacking import xframe_options_exempt
from accounts import views
urlpatterns = [
    path('', views.index, name='index'),
    path('admin/', admin.site.urls),
    path('login/', login_view, name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('dashboard/', dashboard_redirect, name='dashboard'),
    path('',include('accounts.urls')),
    path('',include('documents.urls')),
    path('wopi/',include('documents.wopi_urls')),
    path('',include('committees.urls')),
    path('',include('councilors.urls')),
    path('',include('committee_level.urls')),
    path('',include('barangay.urls')),
    path('',include('OfficialGazette.urls')),
    path('',include('secretariat.urls')),
    path('',include('systemadmin.urls')),
]
if settings.DEBUG:
    # Media files (uploaded PDFs, etc.) are meant to be embedded in an <iframe>
    # by the separate Gazette site, so this route is exempted from
    # X-Frame-Options — everything else in the app keeps the default DENY.
    urlpatterns += [
        re_path(
            r'^%s(?P<path>.*)$' % re.escape(settings.MEDIA_URL.lstrip('/')),
            xframe_options_exempt(static_serve),
            {'document_root': settings.MEDIA_ROOT},
        ),
    ]