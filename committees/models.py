from django.db import models
from councilors.models import Councilor

class Committee(models.Model):
    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True)

    chairman = models.ForeignKey(
        Councilor, 
        on_delete=models.SET_NULL,
        null=True,
        related_name='chaired_committees'
    )
    vice_chairman = models.ForeignKey(
        Councilor, 
        on_delete=models.SET_NULL, 
        null=True,
        related_name='vice_chaired_committees'
    )
    member = models.ForeignKey(
        Councilor, 
        on_delete=models.SET_NULL, 
        null=True,
        related_name='member_committees'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name