"""Re-driving TIMEOUT erasures (``gdpr_redrive_timeouts``).

The deadline sweep parks silent parts in TIMEOUT and nothing moved them on:
``_maybe_finalize`` only acted on QUEUED/ERASING and the restore re-queue only
looks at DELETED. These tests pin that a re-drive asks the silent owners
again under the same correlation id, keeps the receipts already collected,
finalizes on the last receipt, and does nothing on a second run.
"""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from stapel_core.comm import emit, subscribe_action

from stapel_gdpr.models import AccountClosureRequest, ErasurePart, ErasureRequest
from stapel_gdpr.orchestrator import gdpr_orchestrator
from stapel_gdpr.tasks import sweep_deletion_deadlines
from tests.support import gdpr_conf

#: Owners that answer a ``gdpr.erasure.requested`` in this test, and every
#: request they were sent. One subscriber for the whole module (the comm
#: registry has no unsubscribe); tests switch owners on and off here.
ANSWERING: set[str] = set()
SEEN: list[dict] = []


def _owner_side(event):
    payload = dict(event.payload)
    SEEN.append(payload)
    for owner in sorted(ANSWERING):
        emit("gdpr.section.erased", {
            "service": owner,
            "correlation_id": payload["correlation_id"],
            "receipt_id": f"{owner}:{payload['subject_key']}:{payload['correlation_id']}",
        })


subscribe_action("gdpr.erasure.requested", _owner_side)


@pytest.fixture(autouse=True)
def _owners(settings):
    settings.STAPEL_GDPR = gdpr_conf(
        DATA_OWNERS={
            "recordings": ["account", "recording"],
            "identity-mirror": ["account"],
            "billing": ["account"],
            "media": ["recording"],
        },
        DATA_OWNERS_VERSION="redrive-1",
    )
    ANSWERING.clear()
    SEEN.clear()
    yield
    ANSWERING.clear()
    SEEN.clear()


def _timed_out_recording(answered=("recordings",)):
    """A recording erasure where only *answered* confirmed before the sweep."""
    ANSWERING.update(answered)
    erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
    ANSWERING.clear()
    erasure.parts.exclude(state=ErasurePart.STATE_DONE).update(
        deadline=timezone.now() - timedelta(minutes=1),
    )
    assert sweep_deletion_deadlines() == 1
    erasure.refresh_from_db()
    return erasure


def _timed_out_closure(user):
    """An account closure: identity erased, billing answered, two owners silent."""
    ANSWERING.add("billing")
    closure = gdpr_orchestrator.initiate_closure(user.pk)
    gdpr_orchestrator.execute_deletion(closure)
    ANSWERING.clear()
    closure.refresh_from_db()
    erasure = closure.erasure
    erasure.parts.exclude(state=ErasurePart.STATE_DONE).update(
        deadline=timezone.now() - timedelta(minutes=1),
    )
    assert sweep_deletion_deadlines() == 2
    erasure.refresh_from_db()
    return closure, erasure


def _run(**kwargs) -> str:
    out = StringIO()
    call_command("gdpr_redrive_timeouts", stdout=out, **kwargs)
    return out.getvalue()


@pytest.mark.django_db
class TestRedrive:
    def test_a_stuck_closure_is_re_driven_and_finalized(self, user):
        closure, erasure = _timed_out_closure(user)
        assert closure.status == AccountClosureRequest.STATUS_DELETING
        assert closure.identity_erased_at is not None
        assert erasure.state == ErasureRequest.STATE_TIMEOUT
        assert erasure.unanswered_owners == ["identity-mirror", "recordings"]
        billing_receipt = erasure.parts.get(owner="billing").receipt_id

        ANSWERING.update({"identity-mirror", "recordings"})
        out = _run()

        erasure.refresh_from_db()
        closure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_DELETED
        assert closure.status == AccountClosureRequest.STATUS_DELETED
        assert erasure.parts.filter(state=ErasurePart.STATE_DONE).count() == 3
        # The receipt collected before the timeout is the record; untouched.
        assert erasure.parts.get(owner="billing").receipt_id == billing_receipt
        # Asked again under the SAME correlation id.
        assert SEEN and SEEN[-1]["correlation_id"] == erasure.correlation_id
        assert SEEN[-1]["subject_type"] == "account"
        assert "re-drove 1 timed-out erasure request(s)" in out
        assert "unanswered parts reset: 2" in out
        assert "finalized now: 1" in out

    def test_owners_still_silent_leave_it_erasing_with_a_fresh_deadline(self):
        erasure = _timed_out_recording()
        before = timezone.now()
        _run()
        erasure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_ERASING
        part = erasure.parts.get(owner="media")
        assert part.state == ErasurePart.STATE_PENDING
        assert part.deadline > before + timedelta(hours=23)
        # It can time out again, and be re-driven again.
        erasure.parts.exclude(state=ErasurePart.STATE_DONE).update(
            deadline=timezone.now() - timedelta(minutes=1),
        )
        assert sweep_deletion_deadlines() == 1

    def test_a_second_run_does_nothing(self, user):
        _timed_out_closure(user)
        _run()  # owners still silent: request back in ERASING
        sent = len(SEEN)
        out = _run()
        assert len(SEEN) == sent
        assert "re-drove 0 timed-out erasure request(s)" in out

    def test_dry_run_prints_counts_and_writes_nothing(self, user):
        closure, erasure = _timed_out_closure(user)
        sent = len(SEEN)
        out = _run(dry_run=True)
        erasure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_TIMEOUT
        assert erasure.parts.filter(state=ErasurePart.STATE_TIMEOUT).count() == 2
        assert len(SEEN) == sent
        assert "would re-drive 1 timed-out erasure request(s)" in out
        assert "unanswered parts reset: 2" in out
        # Counts only: no subject keys or user ids on the operator's screen.
        assert str(user.pk) not in out
        assert erasure.correlation_id not in out

    def test_subject_type_filter(self, user):
        _timed_out_closure(user)
        _timed_out_recording()
        out = _run(dry_run=True, subject_type="recording")
        assert "would re-drive 1 timed-out" in out
        out = _run(dry_run=True)
        assert "would re-drive 2 timed-out" in out

    def test_a_late_receipt_finalizes_a_timed_out_request(self):
        # The receipt arriving after the sweep used to land on a DONE part of
        # a request nothing would ever finalize.
        erasure = _timed_out_recording()
        assert erasure.state == ErasureRequest.STATE_TIMEOUT
        silent = erasure.unanswered_owners
        for owner in silent:
            gdpr_orchestrator.mark_section_erased(erasure.correlation_id, owner, "late")
        erasure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_DELETED
        assert erasure.completeness_waived is False

    def test_a_partial_late_receipt_does_not_finalize_or_waive(self, settings):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={
                "recordings": ["recording"],
                "media": ["recording"],
                "search": ["recording"],
            },
            DATA_OWNERS_VERSION="redrive-2",
            ALLOW_ERASURE_WITHOUT_RECEIPTS=True,
        )
        erasure = gdpr_orchestrator.request_erasure("recording", "rec-2")
        erasure.parts.update(deadline=timezone.now() - timedelta(minutes=1))
        sweep_deletion_deadlines()
        gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "media", "late")
        erasure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_TIMEOUT
