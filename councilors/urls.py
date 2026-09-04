from django.urls import path, include
from django.contrib.auth import views
from . import views


urlpatterns = [
    path('councilors/add', views.add_councilor, name='add'),
    path('councilors/',views.councilors, name='councilors_list'),
    path('edit/<int:councilor_id>',views.editCouncilor, name='edit_councilor'),
    path('end_term/<int:councilor_id>',views.end_term,name='end_term'),
    path('councilor/', views.councilor_dashboard, name='councilor-dashboard'),
    path('councilor/view/<int:id>',views.view_draft, name="view-draft"),

    path('view/<int:committee_id>/committee/',views.referred_to_committee, name='referred-to-committee'),
    path('view/draft_measures',views.draft_measures, name='draft-measures'),
    path('view/filed_measures', views.filed_measures, name='filed-measures'),
    path('delete/<int:id>/draft', views.delete_draft, name ='delete-draft'),

]
