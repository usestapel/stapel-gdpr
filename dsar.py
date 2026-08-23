"""DSAR intake — the edge between a person asking and the machine acting.

Everything downstream of a data-subject request already existed: export,
erasure, receipts, deadlines. What did not exist was a way to *ask*. A
request that arrives by email or a web form has a statutory acknowledgement
clock (three business days in practice) and a resolution clock (thirty
days), and both were being met — when they were met — by somebody
remembering.

So intake is a row with both clocks on it, an acknowledgement that is sent
by the same transaction that records the request (``ack_sent_at`` is proof,
not an assumption), and a wiring step that hands the request to the
mechanism that answers it: erasure goes to ``initiate_closure`` with its
cancellable grace intact, access and portability go to ``request_export``.
Rectification has no automated answer and stays a staff task — which is why
the queue and its overdue sweep exist.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from .conf import gdpr_settings
from .models import DsarRequest

logger = logging.getLogger(__name__)

__all__ = ["create_dsar", "wire_dsar"]


def create_dsar(
    *,
    kind: str,
    subject_email: str,
    channel: str = DsarRequest.CHANNEL_APP,
    user_id=None,
    note: str = "",
) -> DsarRequest:
    """Record a request, acknowledge it, and set the machine going.

    The acknowledgement is the whole reason this is one function: a request
    stored without one starts a clock nobody is watching.
    """
    dsar = DsarRequest.objects.create(
        kind=kind,
        channel=channel,
        subject_email=subject_email,
        user_id=user_id,
        note=note,
    )
    _acknowledge(dsar)
    _notify_staff(dsar)
    wire_dsar(dsar)
    return dsar


def _acknowledge(dsar: DsarRequest) -> None:
    """Send the acknowledgement and record that it went.

    Best-effort like every other notification in this module — but the
    *record* is not: ``ack_sent_at`` stays unset when the notification could
    not be requested, so ``gdpr.W008`` and ``sweep_dsar_deadlines`` see an
    unacknowledged request instead of a silently satisfied deadline.
    """
    from stapel_core.notifications import request_notification

    try:
        request_notification(
            email=dsar.subject_email,
            notification_type="gdpr.dsar.received",
            variables={
                "kind": dsar.kind,
                "received_at": dsar.received_at.isoformat(),
                "resolve_due_at": dsar.resolve_due_at.isoformat(),
                "reference": str(dsar.pk),
            },
        )
    except Exception as e:
        logger.error("GDPR DSAR acknowledgement failed [dsar=%s]: %s", dsar.pk, e)
        return

    dsar.ack_sent_at = timezone.now()
    dsar.state = DsarRequest.STATE_ACKNOWLEDGED
    dsar.save(update_fields=["ack_sent_at", "state"])


def _notify_staff(dsar: DsarRequest) -> None:
    """Tell whoever owns the privacy queue that a request arrived."""
    from stapel_core.notifications import request_notification

    recipients = list(gdpr_settings.DSAR_STAFF_EMAILS or [])
    if not recipients:
        logger.warning(
            'GDPR DSAR received with STAPEL_GDPR["DSAR_STAFF_EMAILS"] empty: '
            "nobody was told [dsar=%s kind=%s]", dsar.pk, dsar.kind,
        )
        return
    for email in recipients:
        try:
            request_notification(
                email=email,
                notification_type="gdpr.dsar.opened",
                variables={
                    "kind": dsar.kind,
                    "channel": dsar.channel,
                    "subject_email": dsar.subject_email,
                    "resolve_due_at": dsar.resolve_due_at.isoformat(),
                    "reference": str(dsar.pk),
                },
            )
        except Exception as e:
            logger.error("GDPR DSAR staff notification failed [dsar=%s]: %s", dsar.pk, e)


def wire_dsar(dsar: DsarRequest) -> DsarRequest:
    """Hand a request to the mechanism that answers it, when there is one.

    Only possible for a request matched to an account: an anonymous form
    submission names an email, and turning an email into an erasure without
    verifying who sent it is a deletion oracle. Those wait for staff to set
    ``user_id`` and are wired on the next PATCH.
    """
    if dsar.user_id is None or dsar.erasure_request_id or dsar.export_request_id:
        return dsar

    from .orchestrator import gdpr_orchestrator

    try:
        if dsar.kind == DsarRequest.KIND_ERASURE:
            closure = gdpr_orchestrator.initiate_closure(dsar.user_id)
            # The closure owns the grace; its ErasureRequest does not exist
            # until grace end, so the link is made then (execute_deletion
            # passes the closure). What the DSAR records now is the closure
            # it started, through the erasure it will produce.
            dsar.note = (dsar.note + f"\nclosure={closure.pk}").strip()
            dsar.state = DsarRequest.STATE_IN_PROGRESS
            dsar.save(update_fields=["note", "state"])
        elif dsar.kind in (DsarRequest.KIND_ACCESS, DsarRequest.KIND_PORTABILITY):
            export = gdpr_orchestrator.request_export(dsar.user_id)
            from .tasks import run_data_export

            run_data_export.delay(export.pk)
            dsar.export_request = export
            dsar.state = DsarRequest.STATE_IN_PROGRESS
            dsar.save(update_fields=["export_request", "state"])
    except ValueError as e:
        # A cooldown, a pending closure or a legal hold is a real answer to
        # the request, not a failure of intake: the row stays open with the
        # reason on it so staff resolve it by hand.
        dsar.note = (dsar.note + f"\nnot automated: {e}").strip()
        dsar.save(update_fields=["note"])
    except Exception as e:
        logger.error("GDPR DSAR wiring failed [dsar=%s kind=%s]: %s", dsar.pk, dsar.kind, e)
        dsar.note = (dsar.note + f"\nnot automated: {e}").strip()
        dsar.save(update_fields=["note"])
    return dsar
