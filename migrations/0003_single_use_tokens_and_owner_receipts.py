"""Single-use download tokens, per-owner deletion receipts, hash schemes.

Expand phase. ``download_token`` (the plaintext column) keeps existing but is
nulled here and never written again — every token it held was a reusable
seven-day bearer credential for a full personal-data archive, so invalidating
them is the point rather than a side effect. The column itself is dropped in
the contract phase one release later (release-management.md §3).

Pre-existing ``ReRegistrationHash`` rows are labelled ``legacy-pre-hmac``:
they are either this library's old salted SHA-256 or another writer's
unsalted one, and after the fact the two are indistinguishable. Lookups still
match them, so no erased account is forgotten; nothing writes that format
again.
"""
from django.db import migrations, models


def label_pre_scheme_hashes(apps, schema_editor):
    ReRegistrationHash = apps.get_model('gdpr', 'ReRegistrationHash')
    ReRegistrationHash.objects.update(scheme='legacy-pre-hmac')


def unlabel_pre_scheme_hashes(apps, schema_editor):
    ReRegistrationHash = apps.get_model('gdpr', 'ReRegistrationHash')
    ReRegistrationHash.objects.filter(scheme='legacy-pre-hmac').update(scheme='unverified')


def invalidate_plaintext_download_tokens(apps, schema_editor):
    DataExportRequest = apps.get_model('gdpr', 'DataExportRequest')
    DataExportRequest.objects.exclude(download_token=None).update(
        download_token=None,
        status='expired',
    )


def noop(apps, schema_editor):
    """Deliberately not reversible in effect: a revoked token stays revoked."""


class Migration(migrations.Migration):

    dependencies = [
        ('gdpr', '0002_correlation_id_backfill'),
    ]

    operations = [
        migrations.AddField(
            model_name='accountclosurerequest',
            name='completeness_waived',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='accountclosurerequest',
            name='identity_erased_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='accountclosurerequest',
            name='registry_version',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='accountdeletionpart',
            name='deadline',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='accountdeletionpart',
            name='kind',
            field=models.CharField(choices=[('local', 'In-process provider'), ('remote', 'Remote service')], default='remote', max_length=10),
        ),
        migrations.AddField(
            model_name='accountdeletionpart',
            name='receipt_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='dataexportrequest',
            name='download_consumed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='dataexportrequest',
            name='download_token_hash',
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='dataexportrequest',
            name='is_partial',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='dataexportrequest',
            name='missing_services',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='reregistrationhash',
            name='scheme',
            field=models.CharField(choices=[('hmac-sha256-v1', 'HMAC-SHA256 v1 (purpose-bound, keyed)'), ('legacy-pre-hmac', 'Legacy (pre-scheme row, format unattributable)'), ('unverified', 'Unverified (written outside store_hashes)')], default='unverified', max_length=32),
        ),
        migrations.AlterField(
            model_name='accountdeletionpart',
            name='status',
            field=models.CharField(choices=[('pending', 'Pending'), ('done', 'Done'), ('failed', 'Failed'), ('timeout', 'Timed out')], default='pending', max_length=20),
        ),
        migrations.RunPython(label_pre_scheme_hashes, unlabel_pre_scheme_hashes),
        migrations.RunPython(invalidate_plaintext_download_tokens, noop),
    ]
