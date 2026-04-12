from django.urls import path, include
from . import views
from .views import create_draft

urlpatterns = [
    path('create/referred_draft/',views.create_referred_draft, name='create-referred-draft'),
    path('create/',views.create_draft, name = 'create_draft'),
    path('autosave/',views.autosave_draft, name = 'autosave_draft'),
    path('incoming_docs/', views.incoming_docs, name ='incoming_docs'),


    #########___________tracking stages views_________############
    path('view/first_reading/',views.first_reading, name='first_reading'),

    path('view/second_reading/', views.second_reading, name = 'second-reading'),
    path('view/third_reading/', views.third_reading, name ='third-reading'),
    path('view/approved/', views.approved_registry, name='approved'),
    path('view/other_matters/',views.other_matters, name='other-matters'),
    path('view/disapproved/',views.view_disapproved, name = 'disapproved-docs'),
    path('view/unfinished_business',views.view_unfinished, name='unfinished-business'),
    #########___________tracking stages functions urls_________############
    path('refer_to_committee/<int:doc_id>/',views.refer_to_committee, name='refer_to_committee'),

    #########___________second reading stage functions urls_________############
    path('second-reading/<int:doc_id>/amendments/', views.floor_amendments, name='floor_amendments'),
    path('second-reading/<int:doc_id>/amendments/save/', views.save_amendments, name='save_amendments'),
    path('second-reading/<int:doc_id>/move-to-third/', views.move_to_third_reading, name='move_to_third_reading'),

    #########___________moving functions urls_________############
    path('move/<int:pk>/first_reading/',views.move_to_first, name='move_to_first'),
    path('move/<int:doc_id>/third_reading/',views.move_to_third_reading, name = 'move-to-third-reading'),
    path('approve/<int:pk>/', views.approve_measure, name = 'approve-measure'),
    path('move/<int:doc_id>/other_matters/',views.move_to_other_matters, name='move-to-other-matters'),
    path('return/<int:doc_id>/committee_level/', views.return_to_committee, name='return-to-committee'),
    path('move/<int:doc_id>/disapproved/',views.move_to_disapproved, name='move-to-disapproved'),
    path('unfinished/<int:doc_id>/third_reading/', views.unfinished_to_third, name='unfinished-to-third'),
    ############################ PAPER TRAIL #########################
    path('history/<int:pk>/',views.document_history, name = 'document-history'),
    path('view_archive/<int:doc_id>/', views.view_document, name='view-document'), #url for viewing while on progress
    path('modal/document/<int:doc_id>/',views.modal_document_viewer, name='modal-document_view'), #url for viewing document modal version
    path('drafts/discard-ghost/', views.discard_ghost, name='discard_ghost'),
    path('history/<str:doc_id>/trail_version',views.view_trail_version, name="trail-version"), #viewing kapag sa audit trail na
    ########################## for downloading###########################
    path('download-official/<int:pk>/', views.download_official_pdf, name='download_official_pdf'), #download button for approved files
    path('documents/<int:doc_id>/', views.download_document_pdf, name='download_document_pdf'), # download for any document version
    ##################################################################################
]
