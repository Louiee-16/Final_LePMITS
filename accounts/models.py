from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from datetime import timedelta
import random

class User(AbstractUser):
    ROLES = (
        ('SECRETARIAT', 'Secretariat'),
        ('STAFF', 'Legislative Staff'),
        ('COUNCILOR', 'Councilor'),
        ('BARANGAY', 'Barangay'),
        ('ADMIN', 'Admin')
    )
    role = models.CharField(max_length=20, choices=ROLES, default='STAFF')
    office_or_district = models.CharField(max_length=100, blank=True) # e.g. "District 1" or "Brgy. San Jose"
    def get_councilor_name(self):
        try:
            return self.councilor_profile.name
        except:
            return self.username

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"


class TwoFactorCode(models.Model):
    user       = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='otp_codes')
    code       = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used    = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def is_expired(self):
        return timezone.now() > self.created_at + timedelta(minutes=10)

    @classmethod
    def generate_for(cls, user):
        cls.objects.filter(user=user, is_used=False).update(is_used=True)
        code = f"{random.randint(0, 999999):06d}"
        return cls.objects.create(user=user, code=code)

    def __str__(self):
        return f"OTP for {self.user.username} — {'used' if self.is_used else 'active'}"