"""The subprocessor ledger — a DPA obligation you can query.

Third-party processors that received a copy of the data delete it on their
own contractual schedule, and none of the ones we use exposes a deletion
API. That fact used to be handled by writing a log line ("obligation
recorded") and moving on: unqueryable, unauditable, and impossible to
answer "when will this person be gone from everywhere?" with.

So the obligation becomes a row. One :class:`~stapel_gdpr.models.
SubprocessorObligation` per declared processor per erasure, written the
moment the erasure reaches DELETED, with the date that processor's window
closes. ``ErasureRequest.fully_erased_by`` is the max of those and our own
SLA, and the status endpoints publish it — so a product can say "erased
from our systems on X; from every processor by Y" and mean both halves.

No provider API is called here, because there is none to call. When one
appears, it confirms an existing row (``state='confirmed'``) rather than
replacing the ledger.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from .conf import gdpr_settings

logger = logging.getLogger(__name__)

__all__ = ["declared_subprocessors", "record_subprocessor_obligations"]


def declared_subprocessors(only: list[str] | None = None) -> list[tuple[str, int]]:
    """``STAPEL_GDPR["SUBPROCESSORS"]`` as ``(name, window_days)`` pairs.

    Accepts a bare name (window 0 — the processor holds nothing past the
    request) as well as the full ``{"name", "window_days"}`` dict, so a
    host that only needs to list names is not forced into dict syntax.
    """
    wanted = set(only) if only else None
    pairs: list[tuple[str, int]] = []
    seen: set[str] = set()
    for entry in gdpr_settings.SUBPROCESSORS or []:
        if isinstance(entry, str):
            name, window = entry.strip(), 0
        else:
            name = str(entry.get("name") or "").strip()
            window = int(entry.get("window_days") or 0)
        if not name or name in seen:
            continue
        if wanted is not None and name not in wanted:
            continue
        seen.add(name)
        pairs.append((name, window))
    return pairs


def record_subprocessor_obligations(request, providers: list[str] | None = None) -> int:
    """Write one obligation row per declared processor. Returns how many.

    Called by the orchestrator when an erasure reaches DELETED. An owner
    whose slice went to a subset of the processors may call it directly with
    *providers* — the rows are keyed ``(request, provider)``, so a second
    call for a processor already recorded is a no-op rather than a duplicate.
    """
    from .models import SubprocessorObligation

    anchor = request.completed_at or timezone.now()
    written = 0
    for name, window_days in declared_subprocessors(providers):
        _, created = SubprocessorObligation.objects.get_or_create(
            request=request,
            provider=name,
            defaults={
                "window_days": window_days,
                "recorded_at": timezone.now(),
                "due_at": anchor + timedelta(days=window_days),
            },
        )
        written += int(created)
    if written:
        logger.info(
            "GDPR subprocessor obligations recorded [correlation=%s count=%s]",
            request.correlation_id, written,
        )
    elif not declared_subprocessors(providers):
        # Not an error — a deployment may genuinely use no processor — but
        # it is the difference between "nothing left our systems" and
        # "nobody wrote down what did", and only the setting can say which.
        logger.debug(
            'GDPR erasure completed with STAPEL_GDPR["SUBPROCESSORS"] empty '
            "[correlation=%s]", request.correlation_id,
        )
    return written
