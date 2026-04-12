#archives/models.py
from django.db import models

class Archives(models.Model):
    original_doc = models.ForeignKey('documents.Document', on_delete=models.CASCADE, related_name='history')
    
    title = models.CharField(max_length=500)
    content = models.TextField()
    status = models.CharField(max_length=50)
    version = models.IntegerField()
    reference_no = models.CharField(max_length=100, null=True, blank=True)
    
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-version'] 

    def __str__(self):
        return f"v{self.version} - {self.title} ({self.status})"
    

