from django.urls import path
from . import views

urlpatterns = [

    path('', views.gazette_list, name='gazette_list'),
    path('view/<int:pk>/', views.gazette_detail, name='gazette_detail'),
    path('law/<int:pk>/', views.national_law_detail, name='national_law_detail'),
]