from django.contrib import admin
from .models import Document, LegacyDocument, DocumentChunk, NationalLaw, NationalLawChunk


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    # Read-only-ish by design: this is for lookup/troubleshooting, not
    # editing content, since Document.save() has real side effects
    # (auto-archiving, version bumping, HTML sanitizing — see documents/
    # models.py) that a raw admin form edit could trigger unexpectedly.
    list_display = ['title', 'reference_no', 'doc_type', 'status', 'author', 'current_version', 'updated_at']
    list_filter = ['status', 'doc_type']
    search_fields = ['title', 'reference_no']
    readonly_fields = [f.name for f in Document._meta.fields if f.name not in ('status', 'referred_committee', 'hearing_date')]
    autocomplete_fields = ['author']


@admin.register(LegacyDocument)
class LegacyDocumentAdmin(admin.ModelAdmin):
    list_display  = ['title', 'reference_no', 'doc_type', 'year', 'ocr_processed', 'chunk_count']
    list_filter   = ['doc_type', 'year', 'ocr_processed']
    search_fields = ['title', 'reference_no']
    readonly_fields = ['extracted_text', 'ocr_processed', 'uploaded_at']

    def chunk_count(self, obj):
        return obj.chunks.count()
    chunk_count.short_description = 'Chunks'


@admin.register(NationalLaw)
class NationalLawAdmin(admin.ModelAdmin):
    list_display   = ['law_number', 'title', 'year', 'ocr_processed', 'chunk_count']
    search_fields  = ['law_number', 'title']
    readonly_fields = ['uploaded_at', 'ocr_processed', 'content']
    fields         = ['law_number', 'title', 'year', 'description', 'pdf_file',
                      'content', 'ocr_processed', 'uploaded_at']

    def chunk_count(self, obj):
        return obj.chunks.count()
    chunk_count.short_description = 'Chunks'

    def save_model(self, request, obj, form, change):
        # Extract text from PDF if a new file was uploaded
        if obj.pdf_file and not obj.ocr_processed:
            try:
                import pdfplumber, pytesseract
                from PIL import Image
                pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
                pages = []
                with pdfplumber.open(obj.pdf_file) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text() or ''
                        if not text.strip():
                            img = page.to_image(resolution=200).original.convert('RGB')
                            text = pytesseract.image_to_string(img, lang='eng')
                        if text.strip():
                            pages.append(text.strip())
                obj.content = '\n\n'.join(pages)
                obj.ocr_processed = True
            except Exception as e:
                self.message_user(request, f'PDF extraction failed: {e}', level='warning')

        super().save_model(request, obj, form, change)

        if obj.content.strip():
            count = self._chunk_and_embed(obj)
            self.message_user(request, f'✓ {obj.law_number} saved and indexed ({count} chunks).')

    def _chunk_and_embed(self, law):
        from documents.rag.embedder import chunk_legal_text, embed_text
        NationalLawChunk.objects.filter(law=law).delete()
        chunks = chunk_legal_text(law.content)
        bulk = []
        for cr in chunks:
            try:
                vec = embed_text(f"{law.law_number}. {cr['chunk_text']}")
            except Exception:
                vec = None
            bulk.append(NationalLawChunk(
                law=law,
                chunk_text=cr['chunk_text'],
                chunk_type=cr['chunk_type'],
                chunk_index=cr['chunk_index'],
                embedding=vec,
            ))
        NationalLawChunk.objects.bulk_create(bulk)
        return len(bulk)


@admin.register(NationalLawChunk)
class NationalLawChunkAdmin(admin.ModelAdmin):
    list_display  = ['id', 'law', 'chunk_type', 'chunk_index', 'has_embedding']
    list_filter   = ['law', 'chunk_type']
    readonly_fields = ['law', 'chunk_text', 'chunk_type', 'chunk_index', 'embedding']

    def has_embedding(self, obj):
        return obj.embedding is not None
    has_embedding.boolean = True
    has_embedding.short_description = 'Embedded'


@admin.register(DocumentChunk)
class DocumentChunkAdmin(admin.ModelAdmin):
    list_display  = ['id', 'parent_title', 'chunk_type', 'chunk_index', 'has_embedding']
    list_filter   = ['chunk_type']
    search_fields = ['chunk_text']
    readonly_fields = ['chunk_text', 'embedding', 'chunk_index', 'chunk_type']

    def parent_title(self, obj):
        if obj.legacy_document:
            return obj.legacy_document.title
        if obj.document:
            return obj.document.title
        return '—'
    parent_title.short_description = 'Document'

    def has_embedding(self, obj):
        return obj.embedding is not None
    has_embedding.boolean = True
    has_embedding.short_description = 'Embedded'