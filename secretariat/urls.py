from django.urls import path, include
from django.contrib.auth import views
from . import views

urlpatterns = [
    path('secretariat/dashboard', views.Secretariat_dashboard, name ="secretariat-dashboard"),
    path('view/incoming_session/',views.order_of_business, name='order-of-business'),

    path('view/session_list/',views.session_list, name='session_list'),
    path('download/<int:session_id>/order_of_business',views.download_order_of_business, name = 'download'),
    path('finalize/order_of_business/',views.finalize_agenda, name = 'finalize'),
    path('agenda/<int:session_id>/email/', views.send_agenda_email, name='send_agenda_email'),
    path('agenda/<int:session_id>/pdf/', views.generate_agenda_pdf, name='generate_agenda_pdf'),
    path('send/<int:session_id>/order_of_business/', views.send_agenda_email, name = 'send-agenda-email')
]
