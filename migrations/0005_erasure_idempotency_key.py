"""``ErasureRequest.idempotency_key`` — the de-duplication key a caller in
ANOTHER service supplies.

Purely additive (expand-only): a nullable-equivalent column with a ``''``
default and a PARTIAL unique index that ignores it. Every existing row keeps
``''`` and is therefore outside the constraint, so no backfill and no window
in which an old writer violates the new rule.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('gdpr', '0004_erasure_requests_dsar_and_subprocessors'),
    ]

    operations = [
        migrations.AddField(
            model_name='erasurerequest',
            name='idempotency_key',
            field=models.CharField(blank=True, db_index=True, default='', max_length=128),
        ),
        migrations.AddConstraint(
            model_name='erasurerequest',
            constraint=models.UniqueConstraint(
                condition=models.Q(('idempotency_key', ''), _negated=True),
                fields=('idempotency_key',),
                name='gdpr_erasure_one_request_per_idempotency_key',
            ),
        ),
    ]
