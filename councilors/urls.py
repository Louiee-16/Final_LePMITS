from django.urls import path, include
from django.contrib.auth import views
from . import views


urlpatterns = [
    path('councilors/add', views.add_councilor, name='add'),
    path('councilors/',views.councilors, name='councilors_list'),
    path('edit/<int:councilor_id>',views.editCouncilor, name='edit_councilor'),
    path('end_term/<int:councilor_id>',views.end_term,name='end_term'),

]
