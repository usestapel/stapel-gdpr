"""Action subscriptions of the GDPR module. Consumed contracts live in
``schemas/consumes/``.

Owners confirm erasure of their slice by emitting ``gdpr.section.erased``
with the correlation_id they received in ``gdpr.erasure.requested`` (or, for
one more minor, in the deprecated ``user.deleted``), and prove their
subscriber is actually running by answering ``gdpr.owner.probe`` with
``gdpr.owner.alive`` from the same module. Handlers must be idempotent —
delivery is at-least-once.

``gdpr.erasure.open`` runs the other way: it is how a service OPENS an
erasure here, since ``gdpr_orchestrator.request_erasure`` is reachable only
in-process and the owner that detects the need usually runs somewhere else.
See :mod:`stapel_gdpr.functions` for its synchronous sibling and
:mod:`stapel_gdpr.client` for the seam an owner library points at.
"""
import logging

from stapel_core.comm import on_action

from .functions import ERASURE_OPEN, ERASURE_OPEN_SCHEMA, open_from_payload

logger = logging.getLogger(__name__)

SECTION_ERASED_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "gdpr.section.erased",
    "type": "object",
    "required": ["correlation_id"],
    "properties": {
        # The subject pair, as of 0.5.0: what the owner erased, in the
        # vocabulary the request used. Optional so an owner still on the
        # account-only shape keeps confirming; the correlation_id is what
        # actually routes the receipt.
        "subject_type":   {"type": "string"},
        "subject_key":    {"type": "string"},
        "user_id":        {"type": "string"},
        "correlation_id": {"type": "string"},
        # `owner` is the 0.5.0 name for what `service` called the same
        # thing. Both are accepted; an owner may send either.
        "owner":          {"type": "string"},
        "service":        {"type": "string"},
        # Opaque, owner-issued proof of erasure (job id, tombstone id, ...).
        # Stored on the erasure part so a completed erasure can be audited
        # back to the thing each owner actually did.
        "receipt_id":     {"type": "string"},
        # What the owner removed, by its own count — the difference between
        # "it says it ran" and "it says what it did".
        "counts":         {"type": "object"},
    },
    "additionalProperties": False,
}

OWNER_ALIVE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "gdpr.owner.alive",
    "type": "object",
    "required": ["owner"],
    "properties": {
        "owner":          {"type": "string"},
        "subject_types":  {"type": "array", "items": {"type": "string"}},
        "correlation_id": {"type": "string"},
    },
    "additionalProperties": False,
}


@on_action(ERASURE_OPEN, schema=ERASURE_OPEN_SCHEMA)
def handle_erasure_open(event):
    """Open an erasure asked for by another service.

    Idempotent on ``idempotency_key``: the same key returns the request that
    already exists, so a redelivery — or a purge job that asks again tomorrow
    — cannot mint a second erasure for one subject.

    A malformed or unknown-subject payload is logged and DROPPED rather than
    re-raised. Redelivery cannot fix a typo'd ``subject_type``, and an Action
    that fails forever occupies the consumer's retry budget while looking
    exactly like a downstream outage. The caller that needs to be told is the
    one that called the ``gdpr.erasure.request`` Function, where the error
    reaches it synchronously.
    """
    try:
        request = open_from_payload(event.payload)
    except ValueError as exc:
        logger.error(
            "Refused gdpr.erasure.open (%s): %s",
            exc, getattr(event, "event_id", "?"),
        )
        return

    logger.info(
        "GDPR erasure opened from %s [request=%s subject=%s:%s]",
        getattr(event, "service", "?") or "?", request.pk,
        request.subject_type, request.subject_key,
    )


@on_action("gdpr.section.erased", schema=SECTION_ERASED_SCHEMA)
def handle_section_erased(event):
    """Mark the owner's erasure part done; finalize the request when complete."""
    correlation_id = event.payload.get("correlation_id")
    owner = (
        event.payload.get("owner")
        or event.payload.get("service")
        or event.service
    )
    if not correlation_id or not owner:
        logger.error("Malformed gdpr.section.erased event: %s", getattr(event, "event_id", "?"))
        return

    from .orchestrator import gdpr_orchestrator

    gdpr_orchestrator.mark_section_erased(
        correlation_id,
        owner,
        receipt_id=str(event.payload.get("receipt_id") or ""),
        counts=event.payload.get("counts") or {},
    )


@on_action("gdpr.owner.alive", schema=OWNER_ALIVE_SCHEMA)
def handle_owner_alive(event):
    """Record that an owner's erasure subscriber answered a probe.

    The answer comes from the same subscriber that erases, so a row here is
    evidence the erasure path is *consumed*. A declared owner with no answer
    in ``OWNER_ALIVE_MAX_AGE_HOURS`` is reported by ``gdpr.W006`` at boot,
    which is the whole point: an owner whose consumer was never deployed
    used to be discoverable only by waiting for an erasure to time out.
    """
    owner = event.payload.get("owner") or event.service
    if not owner:
        logger.error("Malformed gdpr.owner.alive event: %s", getattr(event, "event_id", "?"))
        return

    from .orchestrator import gdpr_orchestrator

    gdpr_orchestrator.record_owner_alive(
        owner, list(event.payload.get("subject_types") or []),
    )
