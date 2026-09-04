from django.db import models
from django.conf import settings
from committees.models import Committee
class Barangay(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name = 'barangay')
    barangay_name = models.CharField(max_length=100, unique=True)
    captain = models.CharField(max_length=255)
    email = models.EmailField(blank=False, null=False)
    is_active = models.BooleanField(default=True)
    def __str__(self):
        return self.barangay_name

class BarangayFiles(models.Model):
    origin_barangay = models.ForeignKey(Barangay, on_delete=models.CASCADE)
    # The barangay's actual draft resolution, uploaded as a real .docx —
    # not a scan. Once referred to a committee, this file becomes the
    # Document's working docx directly (see barangay/views.py's
    # barangay_to_referral), so committee-level review edits the real
    # thing rather than a councilor re-typing it from a PDF.
    uploaded_docx = models.FileField(upload_to='barangay_uploads/%Y/', null=True, blank=True)
    date_submitted = models.DateTimeField(auto_now_add=True)
    remarks = models.TextField(max_length=600, null=True, blank=True )
    title = models.CharField(max_length=100, blank=True, null=True)
    subject = models.CharField(max_length=200, blank=True, null= True)
    status = models.CharField(max_length = 50)
    referred_committee = models.ForeignKey(Committee, on_delete=models.SET_NULL, null=True, blank=True)
