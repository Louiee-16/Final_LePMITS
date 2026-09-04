from django.urls import path
from . import views

urlpatterns = [
    path('systemadmin/dashboard', views.admin_dashboard, name ='admin-dashboard'),
    path('systemadmin/user_management/', views.user_management, name= 'user-management'),
    path('systemadmin/user_management/export/', views.export_users_csv, name='export-users-csv'),
    path('systemadmin/general_settings',views.general_settings, name='general-settings'),
    path('systemadmin/council_setup',views.council_setup, name ='council-setup'),  

    ########################SYSTEM ADMIN DASHBOARD FUNCTIONS 
    path('systemadmin/createuser/page',views.create_user_page, name= 'create-user-page'),
    path('systemadmin/create_user/',views.create_user, name = 'create-user'),

    ###################### USER MANAGEMENT ####################
    path('systemadmin/user/<int:user_id>/edit/', views.edit_user, name='edit-user'),
    path('systemadmin/user/<int:user_id>/reset-password/', views.reset_password, name='reset-password'),
    path('systemadmin/user/<int:user_id>/toggle-status/', views.toggle_user_status, name='toggle-status'),
    ########## FOR BACKEND DATABASE ################################3
    path('systemadmin/backend_database',views.backend_database, name ='backend-database'),
    path('backend_database/clear_system_cache', views.clear_system_cache, name ='clear-system-cache'),
    path('backend_database/optimize_database/', views.optimize_database, name='optimize-database'),
    path('backend_database/trigger_backup/',views.trigger_backup, name='trigger-backup'),
    ########## AUDIT LOGS ########################################
    path('systemadmin/audit_logs/', views.audit_logs, name='audit-logs'),
    path('systemadmin/audit_logs/export/', views.export_logs_csv, name='export-logs-csv'),
    ########## SYSTEM ERRORS ######################################
    path('systemadmin/system_errors/', views.system_errors, name='system-errors'),
    path('systemadmin/system_errors/<int:error_id>/resolve/', views.resolve_system_error, name='resolve-system-error'),
    path('systemadmin/system_errors/clear_resolved/', views.clear_resolved_errors, name='clear-resolved-errors'),
]
