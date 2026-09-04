from django.contrib import admin
from .models import Barangay, BarangayFiles


@admin.register(Barangay)
class BarangayAdmin(admin.ModelAdmin):
    list_display = ['barangay_name', 'captain', 'email', 'is_active']
    search_fields = ['barangay_name', 'captain']
    autocomplete_fields = ['user']


@admin.register(BarangayFiles)
class BarangayFilesAdmin(admin.ModelAdmin):
    list_display = ['title', 'origin_barangay', 'subject', 'status', 'date_submitted']
    list_filter = ['status', 'origin_barangay']
    search_fields = ['title', 'subject']
    autocomplete_fields = ['origin_barangay', 'referred_committee']
