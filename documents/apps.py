"""
documents/apps.py
LePMITS — AppConfig that wires up documents/signals.py on startup.
"""

from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "documents"

    def ready(self):
        import documents.signals  # noqa: F401  — connects signal receivers