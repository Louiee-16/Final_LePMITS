#documents/models.py
from django.db import models
from django.conf import settings
from committees.models import Committee
from archives.models import Archives
from barangay.models import BarangayFiles
from councilors.models import Councilor
from pgvector.django import VectorField
from documents.sanitize import sanitize_document_html

# Vector width shared by every VectorField below — must match whatever
# settings.OLLAMA_EMBED_MODEL actually outputs (see documents/rag/embedder.py's
# embed_text, which asserts against this constant at call time rather than
# letting a mismatch surface as an opaque pgvector insert error). Changing
# this value requires a migration that wipes and rebuilds every stored
# embedding — see documents/migrations/0016_vector_4096.py's comment for why
# (pgvector/Postgres can't resize a vector column in place) and don't repeat
# that silently: back up first, then `python manage.py embed_documents --force`.
EMBEDDING_DIMENSIONS = 4096


class Document(models.Model):
    STATUS_CHOICES = [
        ('GHOST','Ghost Draft'),
        ('DRAFT', 'Draft'),
        ('FILED', 'Filed'),
        ('FIRST_READING', 'First Reading'),
        ('REFERRED', 'Reffered to Committee'),
        ('COMMITTEE', 'Committee Hearing'),
        ('SECOND_READING', 'Second Reading'),
        ('THIRD_READING', 'Third Reading'),
        ('APPROVED', 'Approved'),
    ]

    DOC_CHOICES = [('ORDINANCE', 'Ordinance'), ('RESOLUTION', 'Resolution')]
    PAGE_SIZE_CHOICES = [('A4', 'A4'), ('LETTER', 'Letter'), ('LEGAL', 'Legal')]
    PAGE_ORIENTATION_CHOICES = [('PORTRAIT', 'Portrait'), ('LANDSCAPE', 'Landscape')]
    public_participation = models.BooleanField(default=False)
    title = models.TextField()
    reference_no = models.CharField(max_length=100, blank=True, null=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='my_docs')
    content = models.TextField()
    # No default — a fresh draft shouldn't silently start as an Ordinance
    # before the councilor has actually chosen. create_draft's submit path
    # blocks filing while this is blank.
    doc_type = models.CharField(max_length=15, choices=DOC_CHOICES, default='', blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    referred_committee = models.ForeignKey(Committee, on_delete=models.SET_NULL, null=True, blank=True, related_name='committee')
    current_version = models.IntegerField(default=1)
    hearing_date = models.DateField(null=True, blank=True) 
    session_included = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    amended_content = models.TextField(blank=True, null=True)
    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS,null=True,blank=True)
    amendment_status = models.CharField(
        max_length=20,
        choices=[
            ('IN_PROGRESS', 'In Progress'),
            ('FINALIZED', 'Amendments Finalized'),
            ('NO_AMENDMENTS', 'No Amendments Made'),
        ],
        default='IN_PROGRESS',
        blank=True,
        null=True
    )
# In Document model
    source_barangay_doc = models.ForeignKey(
        BarangayFiles,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='derived_drafts'
    )
    # Councilors added as present/participating during committee hearing or
    # second-reading floor deliberation — used for the "Sponsored by" byline
    # and as the signature list on the printed sheet before Final Approval.
    participating_councilors = models.ManyToManyField(
        Councilor, blank=True, related_name='participated_documents'
    )
    # The physically-signed copy, scanned and uploaded at Final Approval —
    # required before a measure can move to APPROVED (see approve_measure).
    signed_pdf = models.FileField(upload_to='signed_documents/%Y/', null=True, blank=True)
    # Content hashes of paragraphs already flagged with a similarity comment
    # in the saved .docx (see _run_similarity_check_on_docx in views.py).
    # Lets repeated post-save checks add a comment only once per paragraph
    # text — python-docx can't enumerate/delete existing comments, so this
    # is what keeps the check from re-flagging the same unchanged paragraph
    # forever.
    flagged_similarity_hashes = models.JSONField(default=list, blank=True)
    # Layout-tab page setup (draft.html) — live in the editor preview and
    # carried through to the exported PDF's @page/@frame CSS (see
    # documents/views.py's _pdf_page_layout and document_pdf.html). 2.54cm
    # (1 inch) all around is the standard Word/Google-Docs default and also
    # unifies what was previously two different implicit margins — the
    # editor's own hardcoded ~2.5cm CSS padding vs. the PDF's xhtml2pdf
    # built-in 1cm fallback — into one value that's now actually the same
    # in both places by default, and user-adjustable in either.
    page_size = models.CharField(max_length=10, choices=PAGE_SIZE_CHOICES, default='A4')
    page_orientation = models.CharField(max_length=10, choices=PAGE_ORIENTATION_CHOICES, default='PORTRAIT')
    margin_top_cm = models.FloatField(default=2.54)
    margin_bottom_cm = models.FloatField(default=2.54)
    margin_left_cm = models.FloatField(default=2.54)
    margin_right_cm = models.FloatField(default=2.54)

    def sponsor_councilors(self):
        """Author first, then any councilors added during committee/second
        reading, deduplicated — the full signatory list for printing."""
        councilors = list(self.participating_councilors.all())
        author_councilor = getattr(self.author, 'councilor_profile', None)
        if author_councilor:
            councilors = [c for c in councilors if c.pk != author_councilor.pk]
            councilors.insert(0, author_councilor)
        return councilors

    def save(self, *args, **kwargs):
        self.content = sanitize_document_html(self.content)

        is_new = self.pk is None
        status_changed = False

        if not is_new:
            old_instance = Document.objects.get(pk=self.pk)
            if old_instance.status != self.status:
                status_changed = True
                self.current_version += 1

        super().save(*args, **kwargs)

  
        if self.status not in ['DRAFT', 'GHOST'] and (is_new or status_changed):
            Archives.objects.create(
                original_doc=self,
                title=self.title,
                content=self.content,
                status=self.status,
                version=self.current_version,
                reference_no=self.reference_no
            )

    def __str__(self):
        return f"{self.title} ({self.status})"
    




    
class AmendmentNote(models.Model):
    doc = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name='amendment_notes'
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True
    )
    note = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Amendment note on {self.doc} by {self.author}"


class ReturnReason(models.Model):
    document    = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='return_reasons')
    reason      = models.TextField()
    returned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    previous_status = models.CharField(max_length=20, default='FIRST_READING')
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Returned: {self.document.title[:50]}"


class LegacyDocument(models.Model):
    DOC_TYPE_CHOICES = [
        ('ORDINANCE','Ordinance'),
        ('RESOLUTION','Resolution'),
    ]

    title = models.CharField(max_length=1000)
    # unique=True: the upload view already required this to be non-empty,
    # but nothing stopped two uploads from silently sharing the same
    # reference number — both would then show up as valid citations in
    # Gazette search and the AI Legal Basis assistant's retrieval with no
    # way to tell them apart.
    reference_no = models.CharField(max_length=100, unique=True)
    doc_type = models.CharField(max_length=20, choices=DOC_TYPE_CHOICES)
    year = models.IntegerField(null=True, blank=True)
    pdf_file = models.FileField(upload_to='legacy_documents/%Y/', max_length = 500)
    extracted_text = models.TextField(blank=True)  # OCR result stored here
    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    ocr_processed = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-year', 'reference_no']

    def __str__(self):
        return f"{self.doc_type} {self.reference_no} ({self.year})"
    
class PublicComment(models.Model):
    """
    Read-only mirror of gazette_publiccomment.
    Unmanaged — Django will never create or migrate this table.
    LePMITS reads directly from the Gazette's existing table.
    """
    document    = models.ForeignKey(Document, on_delete=models.DO_NOTHING)
    name        = models.CharField(max_length=200)
    barangay    = models.CharField(max_length=100, blank=True)
    comment     = models.TextField()
    is_approved = models.BooleanField(default=False)
    created_at  = models.DateTimeField(auto_now_add=True)
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    tag         = models.CharField(max_length=10, default='comment')
    replyTo     = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.DO_NOTHING,
        related_name='replies'
    )

    class Meta:
        managed = False                      # ← never touch the real table
        db_table = 'gazette_publiccomment'   # ← must match exactly




class NationalLaw(models.Model):
    title       = models.CharField(max_length=500)
    law_number  = models.CharField(max_length=100, help_text="e.g. Republic Act No. 7160")
    year        = models.IntegerField(null=True, blank=True)
    description = models.TextField(blank=True, help_text="Brief description of what the law covers")
    pdf_file    = models.FileField(upload_to='national_laws/', null=True, blank=True,
                                   help_text="Upload PDF — text will be extracted automatically")
    content     = models.TextField(blank=True, help_text="Extracted or manually entered full text")
    ocr_processed = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['law_number']

    def __str__(self):
        return f"{self.law_number} — {self.title}"


class NationalLawChunk(models.Model):
    law        = models.ForeignKey(NationalLaw, on_delete=models.CASCADE, related_name='chunks')
    chunk_text = models.TextField()
    chunk_type = models.CharField(max_length=50, default='SECTION')
    chunk_index = models.IntegerField()
    embedding  = VectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)

    class Meta:
        ordering = ['chunk_index']

    def __str__(self):
        return f"{self.law.law_number} — chunk {self.chunk_index}"


class DocumentChunk(models.Model):
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="chunks",
    )
    legacy_document = models.ForeignKey(
        LegacyDocument,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="chunks",
    )
    chunk_type = models.CharField(max_length=50)
    chunk_text = models.TextField()
    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    chunk_index = models.IntegerField()

    class Meta:
        db_table = "document_chunk"
        ordering = ["chunk_index"]


class DocumentReferenceCounter(models.Model):
    """Persistent, monotonic per-(doc_type, year) counter backing
    Document.reference_no — replaces the old "count existing rows in these
    statuses" approach, which silently undercounted (and produced
    duplicate numbers) the moment a document moved into a status the
    hardcoded list didn't include, or moved backward (e.g. returned to
    draft) after already holding a number. See documents/views.py's
    assign_reference_number, the only place this should ever be
    incremented — always under _acquire_sequence_lock's advisory lock, so
    concurrent callers for the same (doc_type, year) still can't collide.

    doc_type, not origin, is the key: a councilor-authored Resolution and
    a barangay-originated Resolution share this same counter (both get a
    "DR-#-YYYY" number), since they're the same doc_type and must never
    collide with each other.
    """
    doc_type = models.CharField(max_length=15, choices=Document.DOC_CHOICES)
    year = models.IntegerField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['doc_type', 'year'], name='unique_doc_type_year_counter'),
        ]

    def __str__(self):
        return f"{self.doc_type} {self.year}: {self.count}"