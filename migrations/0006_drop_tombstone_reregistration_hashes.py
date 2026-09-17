"""Remove the re-registration hashes that describe a tombstone.

One erasure reached ``store_hashes`` from both sides of the primary-identity
erasure, so every completed account erasure left two rows for one person: the
digest of the address the memory exists for, and the digest of the
``deleted-<hex>@deleted.invalid`` placeholder written over it. The writer now
refuses the second (``reregistration._is_tombstone``); this removes the ones
already on file.

Only a row that can be PROVED to be a tombstone digest is deleted: the
placeholder is still on the erased user's row, so the digest is recomputed
from it and must also match that row's ``user_id_was``. A row whose subject is
gone (``PRIMARY_IDENTITY_ERASURE="delete"``), or one written under a different
key, cannot be proved and is left alone — it expires with the normal
retention. Deleting by "the later row per subject" would not need the key and
would also delete a legitimately remembered second address.
"""
from django.conf import settings
from django.db import migrations


def drop_tombstone_hashes(apps, schema_editor):
    from stapel_core.gdpr.identity import TOMBSTONE_EMAIL_SUFFIX

    from stapel_gdpr.reregistration import compute_hash

    Hash = apps.get_model('gdpr', 'ReRegistrationHash')
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    if not any(f.name == 'email' for f in User._meta.get_fields()):
        # A host identity model with no email column has no tombstone address
        # and therefore none of these rows.
        return

    removed = 0
    tombstoned = User.objects.filter(email__endswith=TOMBSTONE_EMAIL_SUFFIX)
    for user in tombstoned.only('pk', 'email').iterator(chunk_size=500):
        deleted, _ = Hash.objects.filter(
            hash_type='email',
            hash_value=compute_hash('email', user.email),
            user_id_was=str(user.pk),
        ).delete()
        removed += deleted
    if removed:
        print(f'  gdpr: removed {removed} re-registration hash(es) of a tombstone address')


def noop_reverse(apps, schema_editor):
    """No-op: the rows carried no information a lookup could use."""


class Migration(migrations.Migration):

    dependencies = [
        ('gdpr', '0005_erasure_idempotency_key'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(drop_tombstone_hashes, noop_reverse),
    ]
