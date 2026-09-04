"""Local, headless LibreOffice document conversion.

Single source of truth for every docx/html/pdf conversion this app does —
replaces the Collabora Docker container's /cool/convert-to/{format} HTTP
endpoint entirely. Investigated directly (see documents/wopi.py's git
history / session notes): running Collabora correctly requires not just
several Linux capabilities but a custom seccomp profile that deliberately
re-allows syscalls (mount, unshare, chroot) Docker blocks by default — a
real container-escape-surface tradeoff for what's fundamentally just a
file format conversion. This module does the same conversion with a plain
local subprocess call: no Docker, no elevated container permissions, no
network service to keep running or reason about.

The actual subprocess invocation (--headless --convert-to, with a fresh
-env:UserInstallation profile dir per call to avoid LibreOffice's own
profile-lock collision when two conversions run at once) is the same
pattern documents/views.py's download_document_docx already used and
proved works — extracted here as one shared function instead of being
duplicated per call site.
"""
import logging
import os
import shutil
import subprocess
import tempfile

from django.conf import settings

logger = logging.getLogger(__name__)


class LibreOfficeUnavailable(Exception):
    """Raised when settings.LIBREOFFICE_PATH doesn't point at a real binary."""


# --convert-to's target-format argument accepts a bare extension ("pdf",
# "html") and lets LibreOffice auto-detect which registered filter to use
# for it — works fine for pdf/html (confirmed directly: real conversions
# through this exact code path), but not for docx. Confirmed directly
# against this real installation: bare "docx" as the target fails with
# "no export filter ... found, aborting" — LibreOffice apparently
# registers more than one candidate filter for that extension and can't
# pick one via auto-detection alone, at least for an HTML-sourced
# conversion (used to be assumed this only mattered when the input format
# itself resolves ambiguously, but the failure reproduced consistently
# across a fresh profile, a reused profile, and with/without inherited
# venv environment variables — none of those were the actual cause).
# Naming the filter explicitly ("MS Word 2007 XML", LibreOffice's own
# name for the standard .docx export filter) resolves it — verified with
# a real conversion. Only docx needs this; every other target format this
# app converts to keeps using the plain extension form.
_EXPLICIT_TARGET_FILTERS = {
    "docx": "docx:MS Word 2007 XML",
}


def convert(source_bytes: bytes, source_suffix: str, target_format: str, *, label: str = "convert") -> bytes:
    """
    Converts source_bytes (a file of type source_suffix, e.g. ".docx" or
    ".html") to target_format (e.g. "pdf", "docx", "html") via a local,
    headless LibreOffice subprocess.

    `label` is just a readable prefix for this call's temp directories —
    doesn't affect the conversion, only helps identify stray temp dirs if
    something ever needs debugging on disk.

    Raises:
        LibreOfficeUnavailable — LIBREOFFICE_PATH isn't installed here.
        RuntimeError — the conversion itself failed or timed out.
    """
    if not os.path.exists(settings.LIBREOFFICE_PATH):
        raise LibreOfficeUnavailable(
            f"LibreOffice isn't installed on this server (expected at {settings.LIBREOFFICE_PATH})."
        )

    # A fresh, unique -env:UserInstallation profile dir per call —
    # headless LibreOffice locks its user profile, so two conversions
    # running at the same time (e.g. two staff saving drafts
    # simultaneously) would otherwise collide and one would fail. This is
    # LibreOffice's own documented workaround for exactly that, not
    # something specific to this app.
    work_dir = tempfile.mkdtemp(prefix=f"lo_{label}_")
    profile_dir = tempfile.mkdtemp(prefix=f"lo_profile_{label}_")
    try:
        source_path = os.path.join(work_dir, f"input{source_suffix}")
        with open(source_path, "wb") as f:
            f.write(source_bytes)

        try:
            result = subprocess.run(
                [
                    settings.LIBREOFFICE_PATH,
                    "--headless", "--norestore",
                    f"-env:UserInstallation=file:///{profile_dir.replace(os.sep, '/')}",
                    "--convert-to", _EXPLICIT_TARGET_FILTERS.get(target_format, target_format),
                    "--outdir", work_dir,
                    source_path,
                ],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"LibreOffice conversion to {target_format} timed out.")

        output_path = os.path.join(work_dir, f"input.{target_format}")
        if result.returncode != 0 or not os.path.exists(output_path):
            logger.warning(
                "LibreOffice conversion (%s -> %s) failed: rc=%s stdout=%s stderr=%s",
                source_suffix, target_format, result.returncode, result.stdout, result.stderr,
            )
            raise RuntimeError(f"LibreOffice conversion to {target_format} failed.")

        with open(output_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(profile_dir, ignore_errors=True)
