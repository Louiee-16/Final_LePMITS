from django.urls import path, include
from . import views
from .views import create_draft

urlpatterns = [
    path('create/referred_draft/<int:doc_id>/',views.create_referred_draft, name='create-referred-draft'),
    path('create/',views.create_draft, name = 'create_draft'),
    path('autosave/',views.autosave_draft, name = 'autosave_draft'),
    path('draft/upload-image/', views.upload_draft_image, name='upload-draft-image'),
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
    path('return/<int:doc_id>/filed/', views.return_filed_doc, name='return-filed-doc'),
    path('return/<int:doc_id>/first-reading/', views.return_from_first_reading, name='return-from-first-reading'),
    path('return/<int:doc_id>/barangay_file/', views.return_barangay_file, name='return-barangay-file'),
    path('move/<int:doc_id>/third_reading/',views.move_to_third_reading, name = 'move-to-third-reading'),
    path('approve/<int:pk>/', views.approve_measure, name = 'approve-measure'),
    path('document/<int:doc_id>/councilors/', views.update_document_councilors, name='update-document-councilors'),
    path('move/<int:doc_id>/other_matters/',views.move_to_other_matters, name='move-to-other-matters'),
    path('return/<int:doc_id>/committee_level/', views.return_to_committee, name='return-to-committee'),
    path('move/<int:doc_id>/disapproved/',views.move_to_disapproved, name='move-to-disapproved'),
    path('unfinished/<int:doc_id>/third_reading/', views.unfinished_to_third, name='unfinished-to-third'),
    ############################ PAPER TRAIL #########################
    path('history/<int:pk>/',views.document_history, name = 'document-history'),
    path('view_archive/<int:doc_id>/', views.view_document, name='view-document'), #url for viewing while on progress
    path('modal/document/<int:doc_id>/',views.modal_document_viewer, name='modal-document-view'),
    path('modal/legacy/<int:pk>/', views.modal_legacy_viewer, name='modal-legacy-view'),
    path('legacy/<int:pk>/pdf/', views.serve_legacy_pdf, name='legacy-pdf'),
    path('drafts/discard-ghost/', views.discard_ghost, name='discard_ghost'),
    path('history/<str:doc_id>/trail_version',views.view_trail_version, name="trail-version"), #viewing kapag sa audit trail na
    ########################## for downloading###########################
    path('download-official/<int:pk>/', views.download_official_pdf, name='download_official_pdf'), #download button for approved files
    path('documents/<int:doc_id>/', views.download_document_pdf, name='download_document_pdf'), # download for any document version
    ##################################################################################
    path("ai-legal-basis/", views.ai_legal_basis, name="ai_legal_basis"),

    path("ai-inline-check/", views.ai_inline_check, name="ai_inline_check"),

    path("document/upload/legacy",views.Upload_legacy, name = "UPLOAD-LEGACY"),
    path('upload-legacy/', views.upload_legacy_document, name='UPLOAD-LEGACY-DOCUMENT'),


#############################################################################
    path('documents/extract-legacy-metadata/',views.extract_legacy_metadata, name="EXTRACT-LEGACY-METADATA"),
    path('documents/validate-ocr/', views.validate_ocr_with_ai, name='validate-ocr-with-ai'),

    ###################### ONLYOFFICE EDITOR TRIAL ######################
    path('onlyoffice/<int:doc_id>/', views.onlyoffice_editor_test, name='onlyoffice-editor-test'),
    path('onlyoffice/<int:doc_id>/config/', views.onlyoffice_editor_config, name='onlyoffice-editor-config'),
    path('onlyoffice/<int:doc_id>/amend-config/', views.onlyoffice_amend_config, name='onlyoffice-amend-config'),
    path('onlyoffice/<int:doc_id>/source/', views.onlyoffice_document_source, name='onlyoffice-document-source'),
    path('onlyoffice/<int:doc_id>/pending-docx/', views.onlyoffice_pending_docx, name='onlyoffice-pending-docx'),
    path('onlyoffice/<int:doc_id>/forcesave/', views.onlyoffice_forcesave, name='onlyoffice-forcesave'),
    path('onlyoffice/<int:doc_id>/view-config/', views.onlyoffice_view_config, name='onlyoffice-view-config'),
    path('onlyoffice/<int:doc_id>/snapshot.pdf', views.document_snapshot_pdf, name='document-snapshot-pdf'),
    path('onlyoffice/archive/<int:archive_id>/snapshot.pdf', views.archive_snapshot_pdf, name='archive-snapshot-pdf'),
    path('onlyoffice/<int:doc_id>/similarity-check/', views.onlyoffice_similarity_check, name='onlyoffice-similarity-check'),
    path('onlyoffice/<int:doc_id>/callback/', views.onlyoffice_callback, name='onlyoffice-callback'),
]
