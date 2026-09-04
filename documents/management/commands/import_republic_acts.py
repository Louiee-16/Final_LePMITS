"""
Bulk-imports the Republic Acts slice of the bettergovph/gov-library dataset
(https://huggingface.co/datasets/bettergovph/gov-library, CC BY-NC 4.0 via
Lawphil.net / Arellano Law Foundation) into the existing NationalLaw/
NationalLawChunk models — the same models the old one-by-one
PDF-upload-via-admin flow used (documents/admin.py's NationalLawAdmin),
just populated in bulk from a pre-parsed parquet file instead of manual
per-act uploads.

Embeds each law's pre-generated `summary` field, not its full text: the
dataset already ships a real per-law summary, so embedding that instead of
full act content is both far cheaper (~12,000 short embeddings instead of
~12,000 documents' worth of chunks) and generally sufficient for
relevance-matching purposes — this feature needs to find which laws are
topically relevant, not serve full-text search.

Usage:
    python manage.py import_republic_acts --file path/to/repacts_with_summary.parquet
    python manage.py import_republic_acts --file ... --limit 200      # quick test run
    python manage.py import_republic_acts --file ... --force          # re-embed already-done rows
    python manage.py import_republic_acts --file ... --workers 10
"""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand, CommandError

from documents.models import NationalLaw, NationalLawChunk

# The dataset's ra_bill_number column is inconsistently formatted — confirmed
# directly against real rows: sometimes a bare number ("112"), sometimes
# already prefixed ("RA 1394", "RA No. 611", "Republic Act No. 534").
# Naively prepending "RA " to all of them produces malformed double-prefixed
# values like "RA RA No. 22". Pulling out just the digits normalizes every
# variant to the same clean "RA {number}" form this codebase already uses
# elsewhere (confirmed against real generate_legal_basis() output: "RA
# 11898", "RA 9003").
def _normalize_ra_number(raw):
    match = re.search(r"\d+", raw or "")
    return match.group(0) if match else None


class Command(BaseCommand):
    help = "Bulk-import Republic Acts from the bettergovph/gov-library parquet dataset."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the repacts(_with_summary).parquet file.")
        parser.add_argument("--limit", type=int, default=None, help="Only import the first N rows (for a quick test run).")
        parser.add_argument("--force", action="store_true", help="Re-embed laws that already have a chunk.")
        parser.add_argument("--workers", type=int, default=6, help="Parallel embedding workers.")

    def handle(self, *args, **options):
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise CommandError("pyarrow is required: pip install pyarrow")

        path = options["file"]
        limit = options["limit"]
        force = options["force"]
        workers = options["workers"]

        try:
            table = pq.read_table(path)
        except FileNotFoundError:
            raise CommandError(f"File not found: {path}")

        rows = table.to_pylist()
        if limit:
            rows = rows[:limit]
        self.stdout.write(f"{len(rows)} row(s) to process.")

        # Phase 1: create/update NationalLaw rows — cheap, sequential, no
        # network calls, so no reason to parallelize this part.
        law_by_number = {}
        skipped_no_summary = skipped_no_ra_number = 0
        for row in rows:
            ra_num = _normalize_ra_number(row.get("ra_bill_number"))
            if not ra_num:
                skipped_no_ra_number += 1
                continue
            summary = (row.get("summary") or "").strip()
            if not summary:
                skipped_no_summary += 1
                continue

            law_number = f"RA {ra_num}"
            law, _ = NationalLaw.objects.update_or_create(
                law_number=law_number,
                defaults={
                    "title": (row.get("title") or "")[:500],
                    "year": row.get("year"),
                    "description": summary,
                    "content": row.get("content") or "",
                    # Not OCR-derived — the dataset already ships clean
                    # extracted text, this flag just tells the admin's
                    # save_model() not to try to re-OCR anything.
                    "ocr_processed": True,
                },
            )
            law_by_number[law_number] = (law, summary)

        self.stdout.write(
            f"NationalLaw rows created/updated: {len(law_by_number)} "
            f"(skipped {skipped_no_ra_number} with no RA number, "
            f"{skipped_no_summary} with no summary)."
        )

        # Phase 2: figure out which laws still need an embedded chunk.
        if not force:
            already_embedded = set(
                NationalLawChunk.objects.filter(law__law_number__in=law_by_number.keys())
                .exclude(embedding=None)
                .values_list("law__law_number", flat=True)
            )
            to_embed = {k: v for k, v in law_by_number.items() if k not in already_embedded}
            self.stdout.write(f"{len(to_embed)} law(s) need embedding ({len(already_embedded)} already done).")
        else:
            to_embed = law_by_number

        if not to_embed:
            self.stdout.write(self.style.SUCCESS("Nothing to embed. Done."))
            return

        # Phase 3: embed in parallel — embed_text() is a pure HTTP call to
        # Ollama with no DB access, so it's safe to run across threads;
        # every actual DB write happens back on the main thread as each
        # future completes, so there's no concurrent-write risk.
        from documents.rag.embedder import embed_text

        def _embed_one(law_number, law, summary):
            vec = embed_text(f"{law_number}. {law.title}. {summary}")
            return law, summary, vec

        done = failed = 0
        total = len(to_embed)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_embed_one, ln, law, summary): ln
                for ln, (law, summary) in to_embed.items()
            }
            for future in as_completed(futures):
                ln = futures[future]
                try:
                    law, summary, vec = future.result()
                    NationalLawChunk.objects.update_or_create(
                        law=law, chunk_index=0,
                        defaults={"chunk_text": summary, "chunk_type": "SUMMARY", "embedding": vec},
                    )
                    done += 1
                    if done % 50 == 0 or done == total:
                        self.stdout.write(f"  embedded {done}/{total}...")
                except Exception as e:
                    failed += 1
                    self.stdout.write(self.style.ERROR(f"  {ln} failed: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Done. {done} embedded, {failed} failed."))
