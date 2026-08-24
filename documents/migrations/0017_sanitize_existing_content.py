from django.db import migrations


def sanitize_existing_content(apps, schema_editor):
    """
    documents.models.Document.save() now sanitizes `content` on every write,
    but rows saved before that change may still hold unsanitized HTML.
    Clean them here so the fix covers documents already in the database,
    not only ones edited after this migration runs.
    """
    from documents.sanitize import sanitize_document_html

    Document = apps.get_model("documents", "Document")
    Archives = apps.get_model("archives", "Archives")

    for doc in Document.objects.exclude(content=""):
        clean = sanitize_document_html(doc.content)
        if clean != doc.content:
            Document.objects.filter(pk=doc.pk).update(content=clean)

    for archived in Archives.objects.exclude(content=""):
        clean = sanitize_document_html(archived.content)
        if clean != archived.content:
            Archives.objects.filter(pk=archived.pk).update(content=clean)


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0016_vector_4096"),
        ("archives", "0003_delete_barangayarchives"),
    ]

    operations = [
        migrations.RunPython(sanitize_existing_content, migrations.RunPython.noop),
    ]
