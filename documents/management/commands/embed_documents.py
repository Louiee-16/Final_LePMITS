# documents/management/commands/embed_documents.py
"""
LePMITS — Step 3: Embedding Management Command

Usage:
    python manage.py embed_documents
    python manage.py embed_documents --force        # re-embed already-embedded docs
    python manage.py embed_documents --legacy-only  # skip Document, only LegacyDocument
    python manage.py embed_documents --docs-only    # skip LegacyDocument, only Document
"""

from django.core.management.base import BaseCommand

from documents.models import Document, LegacyDocument
from documents.rag.embedder import embed_document, embed_legacy_document


class Command(BaseCommand):
    help = "Embed all APPROVED Documents and all LegacyDocuments into pgvector."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-embed documents that already have an embedding.",
        )
        parser.add_argument(
            "--legacy-only",
            action="store_true",
            help="Only process LegacyDocument records (skip Document).",
        )
        parser.add_argument(
            "--docs-only",
            action="store_true",
            help="Only process Document records (skip LegacyDocument).",
        )

    def handle(self, *args, **options):
        force = options["force"]
        legacy_only = options["legacy_only"]
        docs_only = options["docs_only"]

        if not legacy_only:
            self._embed_documents(force)

        if not docs_only:
            self._embed_legacy_documents(force)

        self.stdout.write(self.style.SUCCESS("embed_documents complete."))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _embed_documents(self, force: bool) -> None:
        qs = Document.objects.filter(status="APPROVED")

        if not force:
            qs = qs.filter(embedding__isnull=True)

        total = qs.count()
        self.stdout.write(
            f"Documents — {total} APPROVED record(s) to embed "
            f"({'including already-embedded' if force else 'skipping already-embedded'})."
        )

        ok = skipped = failed = 0

        for doc in qs.iterator():
            success = embed_document(doc)
            if success:
                ok += 1
            else:
                failed += 1

            # Progress tick every 25 records so the terminal isn't silent
            # during a large initial index run.
            processed = ok + failed
            if processed % 25 == 0:
                self.stdout.write(f"  … {processed}/{total} processed.")

        self._print_summary("Document", total, ok, failed)

    def _embed_legacy_documents(self, force: bool) -> None:
        qs = LegacyDocument.objects.all()

        if not force:
            qs = qs.filter(embedding__isnull=True)

        total = qs.count()
        self.stdout.write(
            f"LegacyDocuments — {total} record(s) to embed "
            f"({'including already-embedded' if force else 'skipping already-embedded'})."
        )

        ok = failed = 0

        for legacy_doc in qs.iterator():
            success = embed_legacy_document(legacy_doc)
            if success:
                ok += 1
            else:
                failed += 1

            processed = ok + failed
            if processed % 25 == 0:
                self.stdout.write(f"  … {processed}/{total} processed.")

        self._print_summary("LegacyDocument", total, ok, failed)

    def _print_summary(self, label: str, total: int, ok: int, failed: int) -> None:
        self.stdout.write(
            self.style.SUCCESS(f"  {label}: {ok}/{total} embedded successfully.")
        )
        if failed:
            self.stdout.write(
                self.style.WARNING(f"  {label}: {failed} failed — check logs above.")
            )