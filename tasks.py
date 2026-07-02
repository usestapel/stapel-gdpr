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

    cutoff_close   = now - timedelta(days=365)
    now - timedelta(days=365 - 60)
    now - timedelta(days=365 - 14)

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
# LLM provider deletion
# ---------------------------------------------------------------------------

@shared_task
def notify_llm_providers_of_deletion(user_id: str, providers_used: list[str]):
    """
    Log deletion request for LLM providers.
    In practice this is a manual process via DPA — we log the obligation here.
    """
    from django.utils.timezone import now
    logger.info(
        'GDPR LLM deletion obligation recorded [user=%s providers=%s time=%s]',
        user_id, providers_used, now().isoformat(),
    )
    # TODO: if providers expose a deletion API, call it here


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
    }
