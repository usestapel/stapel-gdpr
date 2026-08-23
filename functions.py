"""comm Functions of the GDPR module — the synchronous half of the intake.

One Function today: ``gdpr.erasure.request``. Until 0.5.1 an erasure could
only be opened in-process, through ``gdpr_orchestrator.request_erasure``. In a
fleet the service that DETECTS the need is almost never this one — the
retention purge in stapel-recordings, a host's delete view in some other
container — and "import the orchestrator" has no remote form at all. So the
intake got the two shapes every other cross-service call in the fleet has:

- ``gdpr.erasure.open`` (Action, ``actions.py``) — fire-and-forget, for a
  caller that only needs the erasure to happen;
- ``gdpr.erasure.request`` (this Function) — the same payload, answering
  ``{request_id, due_at, state}``, for a caller that must record the id or
  show a deadline immediately.

Both go through :func:`open_from_payload`, so the two doors cannot drift into
two different sets of rules — the same subject-type check, the same origin
vocabulary, the same idempotency.

Idempotency is the whole reason ``idempotency_key`` exists on the request row.
Action delivery is at-least-once and a Function call that times out mid-flight
may well have been executed, so on both paths a caller's retry is
indistinguishable from a second decision. A repeat of the same key returns the
request that already exists, rather than a second erasure of one subject with
its own set of receipt slots that nobody will ever complete.
"""
import json
import logging
from pathlib import Path

from stapel_core.comm import function

logger = logging.getLogger(__name__)

_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

#: Name of the Function. Callers that do not want to hardcode a string import
#: this (or, better, call ``stapel_gdpr.client.request_erasure``).
ERASURE_REQUEST = "gdpr.erasure.request"

#: Name of the fire-and-forget Action handled in ``actions.py``. Lives here
#: beside its sibling so the pair is one declaration.
ERASURE_OPEN = "gdpr.erasure.open"


def _schema(kind: str, name: str) -> dict:
    return json.loads((_SCHEMAS_DIR / kind / f"{name}.json").read_text(encoding="utf-8"))


#: The consumed-Action contract, read from ``schemas/consumes/``.
ERASURE_OPEN_SCHEMA = _schema("consumes", ERASURE_OPEN)
#: The Function contract, read from ``schemas/functions/``.
ERASURE_REQUEST_SCHEMA = _schema("functions", ERASURE_REQUEST)


def open_from_payload(payload: dict):
    """``{subject_type, subject_key, ...}`` -> the :class:`ErasureRequest`.

    The one place the wire payload becomes a call, shared by the Action and
    the Function. Raises ``ValueError`` for a missing subject or a
    ``subject_type`` outside ``STAPEL_GDPR["SUBJECT_TYPES"]``; the Action
    handler logs that and drops the message (a redelivery cannot fix a typo),
    while the Function lets it reach the caller, which can.
    """
    from .orchestrator import gdpr_orchestrator

    subject_type = str(payload.get("subject_type") or "").strip()
    subject_key = str(payload.get("subject_key") or "").strip()
    if not subject_type or not subject_key:
        raise ValueError("subject_type and subject_key are required")

    return gdpr_orchestrator.request_erasure(
        subject_type,
        subject_key,
        workspace_id=(str(payload.get("workspace_id") or "").strip() or None),
        requested_by=(str(payload.get("requested_by") or "").strip() or None),
        origin=(str(payload.get("origin") or "").strip() or "user"),
        idempotency_key=str(payload.get("idempotency_key") or "").strip(),
    )


@function(ERASURE_REQUEST, schema=ERASURE_REQUEST_SCHEMA)
def erasure_request(payload: dict) -> dict:
    """Open an erasure and answer with its identity.

    Input: ``{"subject_type", "subject_key", "workspace_id"?,
    "requested_by"?, "origin"?, "idempotency_key"?}``.
    Output: ``{"request_id", "due_at", "state"}``.

    ``due_at`` is when OUR systems must be clean (``ERASURE_SLA_DAYS``), not
    when every subprocessor's contractual window closes — a caller that needs
    the later date reads ``fully_erased_by`` from ``GET /erasures/{id}``,
    because it depends on obligations written when the request completes.
    """
    request = open_from_payload(payload)
    return {
        "request_id": request.pk,
        "due_at": request.due_at.isoformat(),
        "state": request.state,
    }


__all__ = [
    "ERASURE_OPEN",
    "ERASURE_OPEN_SCHEMA",
    "ERASURE_REQUEST",
    "ERASURE_REQUEST_SCHEMA",
    "erasure_request",
    "open_from_payload",
]
