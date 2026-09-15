"""Waiting for a receipt is not failing to get one.

`_maybe_finalize` runs after EVERY receipt, so a healthy multi-owner erasure
passes through its blocker branch once per owner that has not answered yet.
That branch logged at WARNING, so every SUCCESSFUL erasure announced itself as
"not certifiable".

Measured on a client fleet, 2026-09-15: the warning at 23:03:02.747, the last
receipt at .939, the request DELETED at .944 — 197 milliseconds, reported as a
compliance problem, on all thirty-four requests in the database. An operator
who sees that on every erasure stops reading it, which is precisely when the
one that is genuinely stuck arrives.
"""
import logging

import pytest
from datetime import timedelta

from django.utils import timezone

from stapel_gdpr.models import ErasurePart, ErasureRequest
from stapel_gdpr.orchestrator import gdpr_orchestrator

from .support import gdpr_conf

pytestmark = pytest.mark.django_db

LOGGER = "stapel_gdpr.orchestrator"


@pytest.fixture(autouse=True)
def _two_owners(settings):
    settings.STAPEL_GDPR = gdpr_conf(
        DATA_OWNERS={"recordings": ["recording"], "media": ["recording"]},
        DATA_OWNERS_VERSION="in-flight-1",
    )


def _request_with_parts(done, pending, *, deadline=None):
    request = ErasureRequest.objects.create(
        subject_type="recording",
        subject_key="rec-in-flight",
        due_at=timezone.now() + timedelta(days=30),
        state=ErasureRequest.STATE_ERASING,
    )
    for owner in done:
        part = ErasurePart.objects.create(request=request, owner=owner, kind="local")
        part.record_receipt()
    for owner in pending:
        ErasurePart.objects.create(
            request=request,
            owner=owner,
            kind="local",
            deadline=deadline or (timezone.now() + timedelta(hours=24)),
        )
    return request


def test_an_erasure_still_collecting_receipts_does_not_warn(caplog):
    """The 197-millisecond case: one owner in, one still coming."""
    request = _request_with_parts(done=["media"], pending=["recordings"])

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        gdpr_orchestrator._maybe_finalize(request)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], [r.getMessage() for r in warnings]
    assert any(
        "awaiting receipts" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG
    ), [r.getMessage() for r in caplog.records]
    request.refresh_from_db()
    assert request.state == ErasureRequest.STATE_ERASING


def test_a_receipt_past_its_deadline_still_warns(caplog):
    """Nobody is coming. This is the case the line exists for."""
    request = _request_with_parts(
        done=["media"],
        pending=["recordings"],
        deadline=timezone.now() - timedelta(hours=1),
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        gdpr_orchestrator._maybe_finalize(request)

    assert any(
        "not certifiable" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ), [r.getMessage() for r in caplog.records]


def test_a_registry_problem_warns_however_fresh_the_deadline(caplog, settings):
    """An owner nobody declared is not fixed by waiting longer."""
    settings.STAPEL_GDPR = gdpr_conf(
        DATA_OWNERS={},
        DATA_OWNERS_VERSION="in-flight-2",
    )
    request = _request_with_parts(done=[], pending=["recordings"])

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        gdpr_orchestrator._maybe_finalize(request)

    assert any(
        "not certifiable" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ), [r.getMessage() for r in caplog.records]


def test_the_full_receipt_set_still_finalises(caplog):
    """The downgrade must not have bought quiet by not finishing."""
    request = _request_with_parts(done=["media", "recordings"], pending=[])

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        gdpr_orchestrator._maybe_finalize(request)

    request.refresh_from_db()
    assert request.state == ErasureRequest.STATE_DELETED
    assert request.completeness_waived is False
