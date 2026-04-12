from django.urls import path,include
from . import views

urlpatterns = [
    path('systemadmin/audit/', views.audit_logs, name='audit-logs'),
    path('systemadmin/audit/export/', views.export_logs_csv, name='export-logs-csv'),

]
