from django.db import models
from documents.models import Document


class CommitteeReport(models.Model):

    STATUS_CHOICES = [
        ('PENDING',  'Pending Hearing'),
        ('APPROVED', 'Approved on Committee Level'),
        ('FAILED',   'Did not pass Committee Level'),
    ]

    draft   = models.OneToOneField(Document, on_delete=models.CASCADE, related_name='committee_report')
    content = models.TextField(blank=True, default='')  # Quill HTML, accumulated across hearings
    status  = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')

    final_pdf  = models.FileField(upload_to='final_reports/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True,null=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Report: {self.draft.title}"

    @property
    def latest_outcome(self):
        """Returns the outcome of the most recent hearing, or None."""
        latest = self.hearings.first()  # ordered by -hearing_date
        return latest.outcome if latest else None


class HearingLog(models.Model):

    OUTCOME_CHOICES = [
        ('PENDING',  'No decision yet'),
        ('APPROVED', 'Approved on committee level'),
        ('RESET',    'Reset — another hearing needed'),
        ('FAILED',   'Did not pass committee level'),
    ]

    report           = models.ForeignKey(CommitteeReport, on_delete=models.CASCADE, related_name='hearings')
    hearing_date     = models.DateField()
    attendance_notes = models.TextField(blank=True, default='')
    outcome          = models.CharField(max_length=20, choices=OUTCOME_CHOICES, default='PENDING')

    version_discussed = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-hearing_date']

    def save(self, *args, **kwargs):
        if not self.pk:
            count = HearingLog.objects.filter(report=self.report).count()
            self.version_discussed = count + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Hearing {self.version_discussed} — {self.hearing_date}"