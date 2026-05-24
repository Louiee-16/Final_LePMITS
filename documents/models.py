#documents/models.py
from django.db import models
from django.conf import settings
from committees.models import Committee
from archives.models import Archives
from barangay.models import BarangayFiles
from pgvector.django import VectorField



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
    public_participation = models.BooleanField(default=False)
    title = models.TextField()
    reference_no = models.CharField(max_length=100, blank=True, null=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='my_docs')
    content = models.TextField()
    doc_type = models.CharField(max_length=15, choices=DOC_CHOICES, default='ORDINANCE')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    referred_committee = models.ForeignKey(Committee, on_delete=models.SET_NULL, null=True, blank=True, related_name='committee')
    current_version = models.IntegerField(default=1)
    hearing_date = models.DateField(null=True, blank=True) 
    session_included = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    amended_content = models.TextField(blank=True, null=True)
    embedding = VectorField(dimensions=384,null=True,blank=True)
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
    def save(self, *args, **kwargs):
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


class LegacyDocument(models.Model):
    DOC_TYPE_CHOICES = [
        ('ORDINANCE','Ordinance'),
        ('RESOLUTION','Resolution'),
    ]

    title = models.CharField(max_length=1000)
    reference_no = models.CharField(max_length=100, blank=True)
    doc_type = models.CharField(max_length=20, choices=DOC_TYPE_CHOICES)
    year = models.IntegerField(null=True, blank=True)
    pdf_file = models.FileField(upload_to='legacy_documents/%Y/', max_length = 500)
    extracted_text = models.TextField(blank=True)  # OCR result stored here
    embedding = VectorField(dimensions=384, null=True, blank=True)
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