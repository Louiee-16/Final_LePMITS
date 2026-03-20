from django.db import models
from django.conf import settings
from committees.models import Committee

class Document(models.Model):
    STATUS_CHOICES = [
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

    title = models.CharField(max_length=500)
    reference_no = models.CharField(max_length=100, blank=True, null=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='my_docs')
    content = models.TextField()
    doc_type = models.CharField(max_length=15, choices=DOC_CHOICES, default='ORDINANCE')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    referred_committee = models.ForeignKey(Committee, on_delete=models.SET_NULL, null=True, blank=True)
    current_version = models.IntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        status_changed = False
        
        if not is_new:
            old_instance = Document.objects.get(pk=self.pk)
            if old_instance.status != self.status:
                status_changed = True
                self.current_version += 1

        super().save(*args, **kwargs)

        # PAPER TRAIL LOGIC:
        # Create an archive if the status is NOT Draft and either it's new or status changed.
        if self.status != 'DRAFT' and (is_new or status_changed):
            from archives.models import Archives
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