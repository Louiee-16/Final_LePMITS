from django.db import models
from documents.models import Document


class CommitteeReport(models.Model):

    STATUS_CHOICES = [
        ('PENDING',  'Hearing Scheduled'),
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
        """Returns the outcome of the most recent (non-deleted) hearing, or None."""
        latest = self.hearings.filter(deleted_at__isnull=True).first()  # ordered by -hearing_date
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
    # Soft delete — set by delete_hearing_log (same-day undo window, see
    # its docstring), cleared by restore_hearing_log. Actually removed
    # from the DB only once the day it was deleted on has passed (see
    # committee_level/views.py's _purge_expired_deleted_hearings) — kept
    # around meanwhile so Restore has something to restore.
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        # -id as a tiebreaker: -hearing_date alone leaves same-day entries
        # in an undefined order (confirmed — Postgres doesn't guarantee
        # insertion order without an explicit secondary sort key).
        ordering = ['-hearing_date', '-id']

    def save(self, *args, **kwargs):
        if not self.pk:
            count = HearingLog.objects.filter(report=self.report).count()
            self.version_discussed = count + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Hearing {self.version_discussed} — {self.hearing_date}"