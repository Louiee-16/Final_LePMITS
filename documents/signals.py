"""
documents/signals.py
LePMITS — Step 6: Auto-embed Document when status transitions to APPROVED
"""

from __future__ import annotations
import logging
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from documents.models import Document

logger = logging.getLogger(__name__)

# We stash the pre-save status on the instance so post_save can compare.
# Using pre_save avoids a second DB hit inside post_save.

@receiver(pre_save, sender=Document)
def _stash_old_status(sender, instance, **kwargs):
    """
    Before saving, record the current DB status on the instance so that
    post_save can detect a transition to APPROVED without an extra query.
    New records (no pk yet) get old_status=None.
    """
    if instance.pk:
        try:
            instance._old_status = Document.objects.only("status").get(pk=instance.pk).status
        except Document.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


@receiver(post_save, sender=Document)
def auto_embed_on_approval(sender, instance, created, **kwargs):
    """
    After a Document is saved, embed it if its status has just transitioned
    TO 'APPROVED' (i.e. it was not APPROVED before this save).

    - Idempotent: if the document was already APPROVED and is re-saved as
      APPROVED (e.g. a field edit), we skip to avoid redundant work.
      Re-embedding in that case can be forced via the embed_documents --force
      management command.
    - Non-blocking: embedding errors are logged but never re-raised, so a
      sentence-transformers failure cannot crash a status save.
    """
    if instance.status != "APPROVED":
        return

    old_status = getattr(instance, "_old_status", None)

    # Only embed on the APPROVED transition, not on every APPROVED save.
    if old_status == "APPROVED":
        logger.debug(
            "Document pk=%s is still APPROVED (no transition) — skipping auto-embed.",
            instance.pk,
        )
        return

    logger.info(
        "Document pk=%s ('%s') transitioned %s → APPROVED — triggering embedding.",
        instance.pk,
        instance.title,
        old_status or "(new)",
    )

    try:
        # Lazy import keeps startup fast and avoids any import-cycle risk.
        from rag.embedder import embed_document  # noqa: PLC0415

        success = embed_document(instance)
        if success:
            logger.info("Auto-embed succeeded for Document pk=%s.", instance.pk)
        else:
            logger.warning(
                "Auto-embed returned False for Document pk=%s — "
                "document may have no extractable text.",
                instance.pk,
            )
    except Exception as exc:  # noqa: BLE001
        # Never let an embedding failure crash the legislative status save.
        logger.error(
            "Auto-embed raised an exception for Document pk=%s: %s — "
            "embedding skipped; run `embed_documents --force` to retry.",
            instance.pk,
            exc,
        )