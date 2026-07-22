import pgvector.django.vector
from django.db import migrations


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
