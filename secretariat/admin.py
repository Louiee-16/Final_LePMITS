from django.contrib import admin
from .models import Session


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ['session_number', 'council_number', 'session_date', 'date_started']
    search_fields = ['session_number', 'council_number']
