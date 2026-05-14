from django.contrib import admin
from .models import Document, LegacyDocument

@admin.register(LegacyDocument)
class LegacyDocumentAdmin(admin.ModelAdmin):
    list_display = ['title', 'reference_no', 'doc_type', 'year', 'ocr_processed']
    list_filter = ['doc_type', 'year', 'ocr_processed']
    search_fields = ['title', 'reference_no']
    readonly_fields = ['extracted_text', 'ocr_processed', 'uploaded_at']