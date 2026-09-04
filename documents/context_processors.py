from .models import Document
from barangay.models import BarangayFiles


def incoming_docs_count(request):
    """Badge count for the Incoming tab — SECRETARIAT/STAFF only, matches
    the same 'status=FILED' query incoming_docs() uses."""
    user = request.user
    if not user.is_authenticated or user.role not in ('SECRETARIAT', 'STAFF'):
        return {}
    count = (
        Document.objects.filter(status='FILED').count()
        + BarangayFiles.objects.filter(status='FILED').count()
    )
    return {'incoming_docs_count': count}
