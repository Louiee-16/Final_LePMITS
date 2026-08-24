"""
Usage:
    python manage.py embed_documents
    python manage.py embed_documents --force
    python manage.py embed_documents --legacy-only
    python manage.py embed_documents --docs-only
"""

from django.core.management.base import BaseCommand

from documents.models import Document, DocumentChunk, LegacyDocument
from documents.rag.embedder import embed_document


class Command(BaseCommand):
    help = "Chunk and embed APPROVED Documents and LegacyDocuments into DocumentChunk."

    def add_arguments(self, parser):
        parser.add_argument("--force",       action="store_true", help="Re-embed already-chunked docs.")
        parser.add_argument("--legacy-only", action="store_true", help="Only LegacyDocument records.")
        parser.add_argument("--docs-only",   action="store_true", help="Only Document records.")

    def handle(self, *args, **options):
        force       = options["force"]
        legacy_only = options["legacy_only"]
        docs_only   = options["docs_only"]

        if not legacy_only:
            self._process(
                qs=Document.objects.filter(status="APPROVED"),
                source_type="document",
                force=force,
                label="Document",
            )
        if not docs_only:
            self._process(
                qs=LegacyDocument.objects.all(),
                source_type="legacy_document",
                force=force,
                label="LegacyDocument",
            )

        self.stdout.write(self.style.SUCCESS("embed_documents complete."))

    def _process(self, qs, source_type, force, label):
        if not force:
            # Skip docs that already have chunks
            already_chunked_ids = DocumentChunk.objects.filter(
                **{"legacy_document__isnull": source_type == "document",
                   "document__isnull":        source_type == "legacy_document"}
            ).values_list(
                "document_id" if source_type == "document" else "legacy_document_id",
                flat=True
            ).distinct()
            qs = qs.exclude(pk__in=already_chunked_ids)

        total = qs.count()
        self.stdout.write(f"{label} — {total} record(s) to process.")
        ok = failed = 0

        for doc in qs.iterator():
            try:
                if embed_document(doc, source_type=source_type):
                    self.stdout.write(f"  #{doc.pk} — chunks saved.")
                    ok += 1
                else:
                    self.stdout.write(self.style.WARNING(f"  #{doc.pk} — no chunks, skipping."))
                    failed += 1

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  #{doc.pk} failed: {e}"))
                failed += 1

        self.stdout.write(self.style.SUCCESS(f"  {label}: {ok}/{total} OK."))
        if failed:
            self.stdout.write(self.style.WARNING(f"  {label}: {failed} failed."))
