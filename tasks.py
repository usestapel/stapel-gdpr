"""
CRON workers for GDPR processing.

Register in Django settings:
    CELERY_BEAT_SCHEDULE = {
        **get_gdpr_beat_schedule(),
        ...
    }
"""
import logging

from celery import shared_task
from celery.schedules import crontab
from django.utils import timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data export
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def run_data_export(self, request_id: int):
    """Generate export archive for a single request (monolith mode)."""
    from .orchestrator import gdpr_orchestrator
    try:
        gdpr_orchestrator.run_export(request_id)
    except Exception as e:
        logger.error('run_data_export failed [request=%s]: %s', request_id, e)
        raise self.retry(exc=e)


@shared_task
def sweep_pending_exports():
    """Assemble partial archives for export requests that have exceeded their 24h deadline.

    Runs hourly. Catches services that went down mid-export or simply never responded.
    """
    from .orchestrator import gdpr_orchestrator
    gdpr_orchestrator.sweep_deadlines()


def expire_export(req) -> None:
    """Mark one export expired and delete its archive from disk.

    Shared by the download view (a token spent too late) and the scheduled
    purge, so an expired export means "the ZIP is gone" in both paths.
    """
    import os

    from .models import DataExportRequest

    path = req.archive_path
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            logger.error('Failed to delete expired GDPR archive [request=%s path=%s]: %s',
                         req.pk, path, e)
    DataExportRequest.objects.filter(pk=req.pk).update(
        status=DataExportRequest.STATUS_EXPIRED,
        archive_path=None,
        download_token=None,
        download_token_hash=None,
    )


@shared_task
def purge_expired_exports() -> int:
    """Delete export archives whose download window closed. Returns the count.

    Archives used to be written to the local filesystem and left there: a
    complete personal-data dump per user, kept forever, with a token that
    stayed valid for a week. Retention has to be enforced by something that
    runs, so this is a scheduled task with a number in the log rather than a
    sentence in the docs.
    """
    from .models import DataExportRequest

    stale = DataExportRequest.objects.filter(
        download_expires_at__lte=timezone.now(),
    ).exclude(status=DataExportRequest.STATUS_EXPIRED)

    purged = 0
    for req in stale:
        expire_export(req)
        purged += 1

    # Consumed downloads keep no archive either — the row survives as a
    # record of the request, the ZIP does not.
    orphans = DataExportRequest.objects.filter(
        download_consumed_at__isnull=False,
    ).exclude(archive_path=None)
    for req in orphans:
        expire_export(req)
        purged += 1

    if purged:
        logger.info('GDPR export purge: %s archives removed', purged)
    return purged


@shared_task
def sweep_deletion_deadlines() -> int:
    """Time out erasure parts whose owner never confirmed. Returns the count.

    A timed-out part keeps blocking the DELETED status, flips its request to
    TIMEOUT and emits ``gdpr.erasure.timeout`` — this task is what makes an
    owner's silence visible instead of eternal. Subject-agnostic since
    0.5.0: it sweeps entity erasures exactly as it sweeps account ones.
    """
    from .orchestrator import gdpr_orchestrator

    return gdpr_orchestrator.sweep_deletion_deadlines()


@shared_task
def probe_data_owners() -> str:
    """Ask every declared data owner to prove its erasure path is consumed.

    Daily. Owners answer ``gdpr.owner.alive`` from the same subscriber that
    handles erasure, and the answers land in ``DataOwnerHealth`` — which
    ``gdpr.W006`` reads at boot. Unwired, an owner with no consumer process
    is discoverable only by waiting for an erasure to time out, which is how
    seven silent owners survived a fleet for months.
    """
    from .orchestrator import gdpr_orchestrator

    return gdpr_orchestrator.probe_data_owners()


# ---------------------------------------------------------------------------
# DSAR deadlines
# ---------------------------------------------------------------------------

@shared_task
def sweep_dsar_deadlines() -> int:
    """Emit ``gdpr.dsar.overdue`` for every missed DSAR clock. Returns the count.

    Daily. Two deadlines, both statutory: the acknowledgement (three
    business days, normally met by the automated one at intake) and the
    resolution (thirty days). Emitted once per deadline per request —
    ``overdue_notified_at`` is the idempotency mark, so a daily sweep does
    not turn one missed deadline into a daily alarm nobody reads.
    """
    from stapel_core.comm import mutate_and_emit

    from .models import DsarRequest

    now = timezone.now()
    open_states = [
        DsarRequest.STATE_RECEIVED,
        DsarRequest.STATE_ACKNOWLEDGED,
        DsarRequest.STATE_IN_PROGRESS,
    ]

    def _emit(dsar, deadline: str, due_at) -> None:
        with mutate_and_emit() as emit:
            DsarRequest.objects.filter(pk=dsar.pk).update(overdue_notified_at=now)
            emit(
                'gdpr.dsar.overdue',
                {
                    'dsar_id': dsar.pk,
                    'kind': dsar.kind,
                    'channel': dsar.channel,
                    'state': dsar.state,
                    'deadline': deadline,
                    'due_at': due_at.isoformat(),
                    'received_at': dsar.received_at.isoformat(),
                },
                key=str(dsar.pk),
            )

    count = 0
    unacknowledged = DsarRequest.objects.filter(
        ack_sent_at__isnull=True,
        ack_due_at__lte=now,
        overdue_notified_at__isnull=True,
        state__in=open_states,
    )
    for dsar in unacknowledged:
        _emit(dsar, 'acknowledgement', dsar.ack_due_at)
        logger.error('GDPR DSAR acknowledgement overdue [dsar=%s due=%s]',
                     dsar.pk, dsar.ack_due_at.isoformat())
        count += 1

    unresolved = DsarRequest.objects.filter(
        resolve_due_at__lte=now,
        overdue_notified_at__isnull=True,
        state__in=open_states,
    )
    for dsar in unresolved:
        _emit(dsar, 'resolution', dsar.resolve_due_at)
        logger.error('GDPR DSAR resolution overdue [dsar=%s due=%s]',
                     dsar.pk, dsar.resolve_due_at.isoformat())
        count += 1

    return count


# ---------------------------------------------------------------------------
# Account closure worker
# ---------------------------------------------------------------------------

@shared_task
def process_expired_grace_periods():
    """Execute deletion for accounts whose 30-day grace period has elapsed.

    Users under an unreleased legal hold are skipped — their closure stays
    in GRACE until the hold is released (GDPR Art. 17(3)).
    """
    from .models import AccountClosureRequest, LegalHold
    from .orchestrator import gdpr_orchestrator

    held_user_ids = LegalHold.objects.filter(
        released_at__isnull=True,
    ).values_list('user_id', flat=True)

    expired = AccountClosureRequest.objects.filter(
        status=AccountClosureRequest.STATUS_GRACE,
        grace_ends_at__lte=timezone.now(),
    ).exclude(user_id__in=held_user_ids)
    for closure in expired:
        try:
            gdpr_orchestrator.execute_deletion(closure)
        except Exception as e:
            logger.error('execute_deletion failed [user=%s]: %s', closure.user_id, e)


# ---------------------------------------------------------------------------
# Inactivity checker
# ---------------------------------------------------------------------------

@shared_task
def check_inactive_accounts():
    """
    Detect accounts inactive for 12 months.
    Sends warning emails at 60 days and 14 days before; initiates closure at 12 months.
    """
    from datetime import timedelta
    from django.contrib.auth import get_user_model

    User = get_user_model()
    now  = timezone.now()

    cutoff_close = now - timedelta(days=365)

    # Users to close now
    inactive_to_close = User.objects.filter(
        is_active=True,
        last_login__lte=cutoff_close,
    )
    for user in inactive_to_close:
        try:
            from .orchestrator import gdpr_orchestrator
            from .models import AccountClosureRequest
            if not AccountClosureRequest.objects.filter(
                user_id=user.pk,
                status__in=[AccountClosureRequest.STATUS_GRACE, AccountClosureRequest.STATUS_DELETING],
            ).exists():
                gdpr_orchestrator.initiate_closure(user.pk, trigger=AccountClosureRequest.TRIGGER_INACTIVITY)
                _send_inactivity_closed_email(user)
        except Exception as e:
            logger.error('inactivity closure failed [user=%s]: %s', user.pk, e)

    # 60-day and 14-day warnings (approximate — check within a 1-day window)
    for days_before, send_fn in [(60, _send_inactivity_warn_60), (14, _send_inactivity_warn_14)]:
        cutoff = now - timedelta(days=365 - days_before)
        window_start = cutoff - timedelta(hours=12)
        window_end   = cutoff + timedelta(hours=12)
        users = User.objects.filter(is_active=True, last_login__range=(window_start, window_end))
        for user in users:
            try:
                send_fn(user, days_before)
            except Exception as e:
                logger.error('inactivity warning email failed [user=%s days=%s]: %s', user.pk, days_before, e)


def _send_inactivity_warn_60(user, days: int):
    from stapel_core.notifications import request_notification
    request_notification(
        email=user.email,
        notification_type='gdpr.inactivity_warning',
        variables={'days_remaining': days},
    )


def _send_inactivity_warn_14(user, days: int):
    _send_inactivity_warn_60(user, days)


def _send_inactivity_closed_email(user):
    from stapel_core.notifications import request_notification
    request_notification(
        email=user.email,
        notification_type='gdpr.inactivity_closed',
        variables={},
    )


# ---------------------------------------------------------------------------
# Retention cleanup
# ---------------------------------------------------------------------------

@shared_task
def run_retention_cleanup():
    """Delete data that has exceeded its legal retention period.

    Data belonging to users under an unreleased legal hold is preserved —
    it must remain available for litigation/investigation.
    """
    from .models import LegalHold, ReRegistrationHash

    held_user_ids = [
        str(uid)
        for uid in LegalHold.objects.filter(
            released_at__isnull=True,
        ).values_list('user_id', flat=True)
    ]

    expired_hashes = ReRegistrationHash.objects.filter(
        expires_at__lte=timezone.now(),
    ).exclude(user_id_was__in=held_user_ids)
    count = expired_hashes.count()
    expired_hashes.delete()
    if count:
        logger.info('Retention cleanup: deleted %s expired re-registration hashes', count)


# ---------------------------------------------------------------------------
# Beat schedule helper
# ---------------------------------------------------------------------------

def get_gdpr_beat_schedule() -> dict:
    """Add to CELERY_BEAT_SCHEDULE in your Django settings."""
    return {
        'gdpr-export-deadline-sweep': {
            'task': 'stapel_gdpr.tasks.sweep_pending_exports',
            'schedule': crontab(minute=0),          # every hour
        },
        'gdpr-account-closure-worker': {
            'task': 'stapel_gdpr.tasks.process_expired_grace_periods',
            'schedule': crontab(minute=30),         # every hour at :30
        },
        'gdpr-inactivity-checker': {
            'task': 'stapel_gdpr.tasks.check_inactive_accounts',
            'schedule': crontab(hour=3, minute=0),  # daily at 03:00 UTC
        },
        'gdpr-retention-cleanup': {
            'task': 'stapel_gdpr.tasks.run_retention_cleanup',
            'schedule': crontab(hour=4, minute=0),  # daily at 04:00 UTC
        },
        'gdpr-export-archive-purge': {
            'task': 'stapel_gdpr.tasks.purge_expired_exports',
            'schedule': crontab(minute=15),         # every hour at :15
        },
        'gdpr-deletion-deadline-sweep': {
            'task': 'stapel_gdpr.tasks.sweep_deletion_deadlines',
            'schedule': crontab(minute=45),         # every hour at :45
        },
        'gdpr-data-owner-probe': {
            'task': 'stapel_gdpr.tasks.probe_data_owners',
            'schedule': crontab(hour=5, minute=0),  # daily at 05:00 UTC
        },
        'gdpr-dsar-deadline-sweep': {
            'task': 'stapel_gdpr.tasks.sweep_dsar_deadlines',
            'schedule': crontab(hour=6, minute=0),  # daily at 06:00 UTC
        },
    }
