from django.db import models
from django.conf import settings

class Councilor(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='councilor_profile')
    name = models.CharField(max_length=50, blank=True)
    email = models.EmailField(max_length=50, blank=True)
    district = models.IntegerField(choices=[(1, 'District 1'), (2, 'District 2')])
    profile_picture = models.ImageField(upload_to='councilors/', null=True, blank=True)

    is_active = models.BooleanField(default=True, help_text="Uncheck if they are no longer in office")

    def __str__(self):
        return self.name