from django.db import models

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