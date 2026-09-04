from django.contrib import admin
from .models import Committee


@admin.register(Committee)
class CommitteeAdmin(admin.ModelAdmin):
    list_display = ['name', 'chairman', 'vice_chairman', 'member', 'created_at']
    search_fields = ['name']
    autocomplete_fields = ['chairman', 'vice_chairman', 'member']
