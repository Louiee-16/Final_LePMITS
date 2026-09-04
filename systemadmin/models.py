from django.conf import settings
from django.db import models


class SystemError(models.Model):
    """One row per unhandled exception raised anywhere in the app,
    captured by SystemErrorLoggingMiddleware so admins can see crashes
    without digging through server console logs."""

    timestamp      = models.DateTimeField(auto_now_add=True)
    method         = models.CharField(max_length=10, blank=True)
    path           = models.CharField(max_length=500, blank=True)
    exception_type = models.CharField(max_length=255, blank=True)
    message        = models.TextField(blank=True)
    traceback      = models.TextField(blank=True)
    user           = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    ip_address     = models.GenericIPAddressField(null=True, blank=True)
    resolved       = models.BooleanField(default=False)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.exception_type} at {self.path} ({self.timestamp})"


class SystemSetting(models.Model):

    republic_name = models.CharField(max_length=255, default="Republic of the Philippines")
    city_name = models.CharField(max_length=255, default="City of San Juan, Metro Manila")
    office_name = models.CharField(max_length=255, default="Office of the Sangguniang Panlungsod")
    system_logo = models.ImageField(upload_to='branding/', null=True, blank=True)
    

    current_council_number = models.IntegerField(default=8)
    default_venue = models.TextField(default="Session Hall, Room 214 of City of San Juan Government Center")
    

    maintenance_mode = models.BooleanField(default=False)

    def __str__(self):
        return "System Configuration"

    class Meta:
        verbose_name = "System Setting"