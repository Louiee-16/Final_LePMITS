from django.urls import path, include
from . import views
from .views import create_draft

urlpatterns = [
    path('create/',views.create_draft, name = 'create_draft'),
    path('autosave/',views.autosave_draft, name = 'autosave_draft'),
    path('incoming_docs/', views.incoming_docs, name ='incoming_docs'),
    path('agenda/',views.agenda_list, name='agenda_list'),

    #########___________tracking stages views_________############
    path('view_first_reading/',views.first_reading, name='first_reading'),
    path('view_committee_level/',views.view_committee, name='view_committee'),
    #########___________tracking stages functions urls_________############
    path('refer_to_committee/<int:doc_id>/',views.refer_to_committee, name='refer_to_committee'),
    path('move_to_first/<int:pk>/',views.move_to_first, name='move_to_first')


]
