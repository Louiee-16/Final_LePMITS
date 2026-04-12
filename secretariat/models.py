from django.db import models

class Session(models.Model):
    session_number = models.CharField(max_length = 20, null =True)
    council_number = models.CharField(max_length=10, null= True)
    session_time = models.CharField(max_length=10, null=True)
    previous_session_date = models.DateField(null=True, blank =True)
    invocation_by = models.CharField(max_length=50, null=True)
    session_date = models.DateField(null=True, blank=True)
    date_started = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    agenda_finalized_date = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    order_of_business = models.FileField(upload_to='session/%Y/', null=True, blank=True)
    participants = models.TextField()
    minutes = models.FileField(upload_to = 'session/minutes/%Y/', null=True, blank=True)

