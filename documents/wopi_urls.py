from django.urls import path

from . import wopi

urlpatterns = [
    path('casualdocs/<int:doc_id>/file/', wopi.casualdocs_document_bytes, name='casualdocs-file'),
    path('casualdocs/<int:doc_id>/save/', wopi.casualdocs_save, name='casualdocs-save'),
    path('similarity-check/<int:doc_id>/', wopi.similarity_check, name='similarity-check'),
]
