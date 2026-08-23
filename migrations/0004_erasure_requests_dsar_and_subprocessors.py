"""Subject-scoped erasure, DSAR intake, owner health, subprocessor ledger.

Deletion-driven, in one release: ``AccountDeletionPart`` rows are moved into
``ErasurePart`` under a freshly minted ``ErasureRequest`` per closure, and the
old table is dropped in the same migration. There is no release in which both
tables exist, so nothing can be written to the one nothing reads.

The account was never a second mechanism — it was the only subject the
receipts ledger knew. Each moved closure therefore becomes exactly what a
0.5.0 account closure produces: ``ErasureRequest(subject_type="account",
subject_key=<user id>)`` carrying the closure's own correlation_id, so an
owner's late ``gdpr.section.erased`` for an in-flight closure still lands on
the right part after the upgrade.

# stapel: cutover-phase
"""

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def move_deletion_parts_into_erasures(apps, schema_editor):
    """Carry every closure's receipts across, keyed by its correlation_id."""
    AccountClosureRequest = apps.get_model('gdpr', 'AccountClosureRequest')
    AccountDeletionPart = apps.get_model('gdpr', 'AccountDeletionPart')
    ErasureRequest = apps.get_model('gdpr', 'ErasureRequest')
    ErasurePart = apps.get_model('gdpr', 'ErasurePart')

    # The 0.4.x part statuses and the 0.5.0 part states are the same
    # vocabulary; the column was renamed, the values were not.
    for closure in AccountClosureRequest.objects.all().iterator():
        parts = list(AccountDeletionPart.objects.filter(closure_id=closure.pk))
        if not parts:
            continue
        completed_at = closure.deleted_at
        erasure = ErasureRequest.objects.create(
            subject_type='account',
            subject_key=str(closure.user_id),
            requested_by=closure.user_id,
            origin=('inactivity' if closure.trigger == 'inactivity' else 'user'),
            requested_at=closure.initiated_at,
            grace_ends_at=closure.grace_ends_at,
            # The purge SLA the 0.4.x machine ran on was the grace period
            # itself; using it keeps a migrated request's due_at truthful
            # instead of restarting a 30-day clock at upgrade time.
            due_at=closure.grace_ends_at,
            state={
                'grace': 'queued',
                'deleting': 'erasing',
                'deleted': 'deleted',
                'cancelled': 'queued',
            }.get(closure.status, 'queued'),
            completed_at=completed_at,
            correlation_id=closure.correlation_id,
            registry_version=closure.registry_version,
            completeness_waived=closure.completeness_waived,
            closure_id=closure.pk,
        )
        ErasurePart.objects.bulk_create([
            ErasurePart(
                request_id=erasure.pk,
                owner=part.service,
                state=part.status,
                kind=part.kind,
                receipt_id=part.receipt_id,
                receipt_at=part.completed_at,
                deadline=part.deadline,
                note=part.error or '',
            )
            for part in parts
        ])


def drop_migrated_erasures(apps, schema_editor):
    """Reverse: the moved rows go away with the table they came from."""
    ErasureRequest = apps.get_model('gdpr', 'ErasureRequest')
    ErasureRequest.objects.filter(closure_id__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('gdpr', '0003_single_use_tokens_and_owner_receipts'),
    ]

    operations = [
        migrations.CreateModel(
            name='DataOwnerHealth',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('owner', models.CharField(max_length=50, unique=True)),
                ('last_alive_at', models.DateTimeField(blank=True, null=True)),
                ('last_probe_at', models.DateTimeField(blank=True, null=True)),
                ('declared_subject_types', models.JSONField(blank=True, default=list)),
                ('answered_subject_types', models.JSONField(blank=True, default=list)),
            ],
            options={
                'ordering': ['owner'],
            },
        ),
        migrations.CreateModel(
            name='ErasureRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('subject_type', models.CharField(db_index=True, max_length=32)),
                ('subject_key', models.CharField(db_index=True, max_length=128)),
                ('workspace_id', models.CharField(blank=True, db_index=True, max_length=64, null=True)),
                ('requested_by', models.UUIDField(blank=True, db_index=True, null=True)),
                ('origin', models.CharField(choices=[('user', 'User action'), ('dsar', 'Data subject access request'), ('inactivity', 'Inactivity'), ('restore_requeue', 'Re-queued after a backup restore'), ('admin', 'Administrator')], default='user', max_length=20)),
                ('requested_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('grace_ends_at', models.DateTimeField(blank=True, null=True)),
                ('due_at', models.DateTimeField()),
                ('state', models.CharField(choices=[('queued', 'Queued'), ('erasing', 'Erasing'), ('deleted', 'Deleted'), ('timeout', 'Timed out')], db_index=True, default='queued', max_length=20)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('correlation_id', models.CharField(blank=True, db_index=True, max_length=36, null=True, unique=True)),
                ('registry_version', models.CharField(blank=True, default='', max_length=64)),
                ('completeness_waived', models.BooleanField(default=False)),
                ('restored_from', models.DateTimeField(blank=True, null=True)),
                ('note', models.TextField(blank=True, default='')),
                ('closure', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='erasures', to='gdpr.accountclosurerequest')),
                ('source_request', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='requeues', to='gdpr.erasurerequest')),
            ],
            options={
                'ordering': ['-requested_at'],
            },
        ),
        migrations.CreateModel(
            name='ErasurePart',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('owner', models.CharField(max_length=50)),
                ('state', models.CharField(choices=[('pending', 'Pending'), ('done', 'Done'), ('failed', 'Failed'), ('timeout', 'Timed out')], default='pending', max_length=20)),
                ('kind', models.CharField(choices=[('local', 'In-process provider'), ('remote', 'Remote service')], default='remote', max_length=10)),
                ('receipt_id', models.CharField(blank=True, default='', max_length=128)),
                ('receipt_at', models.DateTimeField(blank=True, null=True)),
                ('counts', models.JSONField(blank=True, default=dict)),
                ('deadline', models.DateTimeField(blank=True, null=True)),
                ('note', models.TextField(blank=True, default='')),
                ('request', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='parts', to='gdpr.erasurerequest')),
            ],
        ),
        migrations.CreateModel(
            name='DsarRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('access', 'Access (Art. 15)'), ('erasure', 'Erasure (Art. 17)'), ('rectification', 'Rectification (Art. 16)'), ('portability', 'Portability (Art. 20)')], max_length=20)),
                ('channel', models.CharField(choices=[('app', 'In-app (authenticated)'), ('form', 'Public form (anonymous)'), ('email', 'Email, transcribed by staff')], default='app', max_length=10)),
                ('subject_email', models.EmailField(max_length=254)),
                ('user_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('received_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('ack_due_at', models.DateTimeField()),
                ('ack_sent_at', models.DateTimeField(blank=True, null=True)),
                ('resolve_due_at', models.DateTimeField()),
                ('state', models.CharField(choices=[('received', 'Received'), ('acknowledged', 'Acknowledged'), ('in_progress', 'In progress'), ('resolved', 'Resolved'), ('rejected', 'Rejected')], db_index=True, default='received', max_length=20)),
                ('note', models.TextField(blank=True, default='')),
                ('overdue_notified_at', models.DateTimeField(blank=True, null=True)),
                ('export_request', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='dsar_requests', to='gdpr.dataexportrequest')),
                ('erasure_request', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='dsar_requests', to='gdpr.erasurerequest')),
            ],
            options={
                'ordering': ['-received_at'],
            },
        ),
        migrations.CreateModel(
            name='SubprocessorObligation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('provider', models.CharField(max_length=64)),
                ('window_days', models.PositiveIntegerField(default=0)),
                ('recorded_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('due_at', models.DateTimeField()),
                ('state', models.CharField(choices=[('pending', 'Window open'), ('confirmed', 'Confirmed deleted'), ('overdue', 'Window closed without confirmation')], default='pending', max_length=20)),
                ('note', models.TextField(blank=True, default='')),
                ('request', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='obligations', to='gdpr.erasurerequest')),
            ],
            options={
                'ordering': ['provider'],
            },
        ),
        migrations.RunPython(
            move_deletion_parts_into_erasures, drop_migrated_erasures,
        ),
        migrations.DeleteModel(
            name='AccountDeletionPart',
        ),
        migrations.AddIndex(
            model_name='erasurerequest',
            index=models.Index(fields=['subject_type', 'subject_key'], name='gdpr_erasur_subject_45bd40_idx'),
        ),
        migrations.AddConstraint(
            model_name='erasurerequest',
            constraint=models.UniqueConstraint(condition=models.Q(('source_request__isnull', False)), fields=('origin', 'source_request'), name='gdpr_erasure_one_requeue_per_source'),
        ),
        migrations.AlterUniqueTogether(
            name='erasurepart',
            unique_together={('request', 'owner')},
        ),
        migrations.AlterUniqueTogether(
            name='subprocessorobligation',
            unique_together={('request', 'provider')},
        ),
    ]
