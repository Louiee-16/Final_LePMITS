from django.contrib import admin
from .models import Councilor


@admin.register(Councilor)
class CouncilorAdmin(admin.ModelAdmin):
    # search_fields is also what makes committees.CommitteeAdmin's
    # autocomplete_fields (chairman/vice_chairman/member) work.
    list_display = ['name', 'district', 'email', 'is_active']
    list_filter = ['district', 'is_active']
    search_fields = ['name', 'email']
    autocomplete_fields = ['user']
