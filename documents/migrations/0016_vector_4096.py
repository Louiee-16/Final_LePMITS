import pgvector.django.vector
from django.db import migrations

# NOTE: this migration changes the embedding vector dimension (768 -> 4096),
# same as 0012_vector_768_nomic before it. Postgres/pgvector cannot resize an
# existing vector column in place, so the RunSQL below wipes every stored
# embedding with no reverse path. If a change like this is ever needed again,
# back up documents_document.embedding / documents_legacydocument.embedding /
# documents_nationallawchunk / document_chunk first, then re-run
# `python manage.py embed_documents --force` afterwards to rebuild them —
# do not ship another silent wipe.


class Migration(migrations.Migration):

    dependencies = [
        ('documents', '0015_return_reason'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                DELETE FROM document_chunk;
                UPDATE documents_document       SET embedding = NULL;
                UPDATE documents_legacydocument SET embedding = NULL;
                DELETE FROM documents_nationallawchunk;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name='document',
            name='embedding',
            field=pgvector.django.vector.VectorField(blank=True, dimensions=4096, null=True),
        ),
        migrations.AlterField(
            model_name='documentchunk',
            name='embedding',
            field=pgvector.django.vector.VectorField(dimensions=4096, null=True, blank=True),
        ),
        migrations.AlterField(
            model_name='legacydocument',
            name='embedding',
            field=pgvector.django.vector.VectorField(blank=True, dimensions=4096, null=True),
        ),
        migrations.AlterField(
            model_name='nationallawchunk',
            name='embedding',
            field=pgvector.django.vector.VectorField(dimensions=4096, null=True, blank=True),
        ),
    ]
