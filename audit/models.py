
from django.db import models
from django.conf import settings

class AuditLog(models.Model):
    ACTION_CHOICES = [
        ('CREATE', 'Created'),
        ('UPDATE', 'Modified'),
        ('DELETE', 'Deleted'),
        ('LOGIN',  'Login'),
        ('LOGOUT', 'Logout'),
        ('FILE',   'Filed'),
        ('MOVE',   'Status Change'),
    ]

    SEVERITY_CHOICES = [
        ('NORMAL', 'Normal'),
        ('HIGH',   'High'),
    ]

    user       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    action     = models.CharField(max_length=20, choices=ACTION_CHOICES)
    severity   = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default='NORMAL')
    target     = models.CharField(max_length=500, blank=True)  # e.g. "Draft Ordinance No. 17"
    detail     = models.TextField(blank=True)                  # extra context
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.user} — {self.action} — {self.timestamp}"