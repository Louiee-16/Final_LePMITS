from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    ROLES = (
        ('SECRETARIAT', 'Secretariat'),
        ('STAFF', 'Legislative Staff'),
        ('COUNCILOR', 'Councilor'),
        ('BRGY_SEC', 'Barangay Secretary'),
    )
    role = models.CharField(max_length=20, choices=ROLES, default='STAFF')
    office_or_district = models.CharField(max_length=100, blank=True) # e.g. "District 1" or "Brgy. San Jose"

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"