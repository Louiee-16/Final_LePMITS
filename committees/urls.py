#committee/urls.py

from django.urls import path
from . import views
urlpatterns = [
    path('committee',views.committee_list, name='committee_list'),
    path('add-committee/', views.add_committee, name='add_committee'),
    path('edit-committee/<int:committee_id>/', views.edit_committee, name='edit_committee'),
]