import re

from django.db import migrations

# Matches CO-001-2025 / CR-005-2021 etc. — only touches rows that actually
# follow this convention; anything else (a manually-typed reference number
# that never matched the auto-extraction pattern) is left as-is.
_PADDED_REF_RE = re.compile(r'^(CO|CR)-0*(\d+)-(\d{4})$')


def strip_zero_padding(apps, schema_editor):
    LegacyDocument = apps.get_model('documents', 'LegacyDocument')
    for doc in LegacyDocument.objects.all():
        match = _PADDED_REF_RE.match(doc.reference_no)
        if not match:
            continue
        prefix, number, year = match.groups()
        new_ref = f'{prefix}-{number}-{year}'
        if new_ref != doc.reference_no:
            doc.reference_no = new_ref
            doc.save(update_fields=['reference_no'])


def noop_reverse(apps, schema_editor):
    # Not reversible — the original zero-padding width isn't recoverable
    # once stripped (CO-001 and CO-1 both normalize to the same value).
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('documents', '0025_alter_legacydocument_reference_no'),
    ]

    operations = [
        migrations.RunPython(strip_zero_padding, noop_reverse),
    ]
