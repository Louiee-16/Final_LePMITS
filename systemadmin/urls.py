from django.urls import path, include
from django.contrib.auth import views
from . import views

urlpatterns = [
    path('systemadmin/dashboard', views.admin_dashboard, name ='admin-dashboard'),
    path('systemadmin/user_management/', views.user_management, name= 'user-management'),
    path('systemadmin/general_settings',views.general_settings, name='general-settings'),
    path('systemadmin/council_setup',views.council_setup, name ='council-setup'),  

    ########################SYSTEM ADMIN DASHBOARD FUNCTIONS 
    path('systemadmin/createuser/page',views.create_user_page, name= 'create-user-page'),
    path('systemadmim/create_user/',views.create_user, name = 'create-user'),

    ###################### USER MANAGEMENT ####################
    path('systemadmin/user/<int:user_id>/edit/', views.edit_user, name='edit-user'),
    path('systemadmin/user/<int:user_id>/reset-password/', views.reset_password, name='reset-password'),
    path('systemadmin/user/<int:user_id>/toggle-status/', views.toggle_user_status, name='toggle-status'),
    ########## FOR BACKEND DATABASE ################################3
    path('systemadmin/backend_database',views.backend_database, name ='backend-database'),
    path('backend_database/clear_system_cache', views.clear_system_cache, name ='clear-system-cache'),
    path('backend_database/trigget_backup/',views.trigger_backup, name='trigger-backup'),
    

]
