"""Action subscriptions of the GDPR module.

Remote services confirm erasure of their data slice by emitting
``gdpr.section.erased`` with the correlation_id they received in
``user.deleted``. Handlers must be idempotent — delivery is at-least-once.
"""
import logging

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)

SECTION_ERASED_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "gdpr.section.erased",
    "type": "object",
    "required": ["user_id", "correlation_id", "service"],
    "properties": {
        "user_id":        {"type": "string"},
        "correlation_id": {"type": "string"},
        "service":        {"type": "string"},
    },
    "additionalProperties": False,
}


@on_action("gdpr.section.erased", schema=SECTION_ERASED_SCHEMA)
def handle_section_erased(event):
    """Mark the remote deletion part done; finalize the closure when complete."""
    correlation_id = event.payload.get("correlation_id")
    service = event.payload.get("service") or event.service
    if not correlation_id or not service:
        logger.error("Malformed gdpr.section.erased event: %s", getattr(event, "event_id", "?"))
        return

    from .orchestrator import gdpr_orchestrator

    gdpr_orchestrator.mark_section_erased(correlation_id, service)
