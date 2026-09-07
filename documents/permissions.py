"""documents/permissions.py — shared role-check helper.

Split out of documents/views.py so both views.py itself and
documents/legacy_upload.py can import it without a circular import
(legacy_upload.py needs it; views.py re-exports legacy_upload.py's view
functions for backward-compatible `views.Upload_legacy`-style access).
"""


def _user_has_role(user, *roles):
    """True if user.role is one of `roles`. Centralizes the role-set
    comparison itself (request.user.role not in [...] / != ...) that was
    previously hand-typed identically at 12 separate call sites across
    documents/views.py — each site keeps its own message/redirect/
    compound-condition logic untouched; only the literal role-list
    comparison is shared, so this doesn't change behavior anywhere, just
    where the permitted-role sets are spelled out."""
    return user.role in roles
