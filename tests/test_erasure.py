"""Subject-scoped erasure: every state transition, and what refuses to move.

The account was never a second mechanism — it was the only subject the
receipts ledger knew. These tests pin the generalization: an entity gets the
same clock, the same per-owner receipts and the same refusal to call itself
DELETED on silence, and an owner is only ever asked about the subjects it
claims.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_core.comm import emit

from stapel_gdpr.models import (
    DataOwnerHealth,
    ErasurePart,
    ErasureRequest,
    SubprocessorObligation,
)
from stapel_gdpr.orchestrator import gdpr_orchestrator
from stapel_gdpr.owners import data_owner_report
from tests.support import gdpr_conf

#: A fleet-shaped inventory: owners that hold different subjects.
FLEET_OWNERS = {
    "recordings": ["account", "workspace", "meeting", "recording"],
    "media": ["account", "workspace", "file", "recording"],
    "billing": ["account"],
}


@pytest.fixture
def fleet(settings):
    settings.STAPEL_GDPR = gdpr_conf(
        DATA_OWNERS=FLEET_OWNERS,
        DATA_OWNERS_VERSION="fleet-1",
    )
    return FLEET_OWNERS


# ---------------------------------------------------------------------------
# The registry: a map of subjects, and the list that still means "account"
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOwnerRegistryShapes:
    def test_plain_list_still_means_account(self, settings):
        """The pre-0.5.0 setting keeps meaning exactly what it meant."""
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["auth", {"name": "cdn", "kind": "remote"}],
            DATA_OWNERS_VERSION="legacy-1",
        )
        report = data_owner_report()
        assert {o.name for o in report.owners_for("account")} == {"auth", "cdn"}
        assert report.owners_for("recording") == ()

    def test_map_scopes_owners_to_the_subjects_they_claim(self, fleet):
        report = data_owner_report()
        assert {o.name for o in report.owners_for("recording")} == {"recordings", "media"}
        assert {o.name for o in report.owners_for("meeting")} == {"recordings"}
        assert {o.name for o in report.owners_for("account")} == set(FLEET_OWNERS)

    def test_map_entry_may_still_carry_kind_and_timeout(self, settings):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={
                "recordings": {
                    "subject_types": ["recording"],
                    "kind": "remote",
                    "timeout_hours": 6,
                },
            },
            DATA_OWNERS_VERSION="mixed-1",
        )
        owner = data_owner_report().owner("recordings")
        assert owner.kind == "remote"
        assert owner.timeout == timedelta(hours=6)
        assert owner.subjects == ("recording",)


# ---------------------------------------------------------------------------
# QUEUED -> ERASING -> DELETED
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestErasureStateMachine:
    def test_request_creates_parts_only_for_claiming_owners(self, fleet):
        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
        assert erasure.state == ErasureRequest.STATE_ERASING
        assert set(erasure.parts.values_list("owner", flat=True)) == {"recordings", "media"}

    def test_due_at_is_the_purge_sla_and_no_grace_for_an_entity(self, fleet):
        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
        assert erasure.grace_ends_at is None
        assert erasure.due_at > timezone.now() + timedelta(days=29)

    def test_sla_days_is_configurable(self, settings, fleet):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=FLEET_OWNERS, DATA_OWNERS_VERSION="fleet-1",
            ERASURE_SLA_DAYS=7,
        )
        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
        assert erasure.due_at < timezone.now() + timedelta(days=8)

    def test_every_claiming_owner_receipt_flips_deleted(self, fleet):
        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")

        gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "recordings", "job-1")
        erasure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_ERASING

        gdpr_orchestrator.mark_section_erased(
            erasure.correlation_id, "media", "job-2", counts={"files": 4},
        )
        erasure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_DELETED
        assert erasure.completed_at is not None
        assert erasure.parts.get(owner="media").counts == {"files": 4}
        assert all(p.receipt_id for p in erasure.parts.all())

    def test_a_receipt_from_an_undeclared_owner_proves_nothing(self, fleet):
        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
        gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "billing", "nope")
        erasure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_ERASING
        assert erasure.unreceipted_owners == ["media", "recordings"]

    def test_redelivery_of_the_same_receipt_is_idempotent(self, fleet):
        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
        for _ in range(3):
            gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "recordings", "job-1")
        assert erasure.parts.filter(state=ErasurePart.STATE_DONE).count() == 1

    def test_an_unknown_subject_type_is_refused(self, fleet):
        with pytest.raises(ValueError, match="unknown_subject_type"):
            gdpr_orchestrator.request_erasure("spaceship", "x-1")

    def test_a_subject_no_owner_claims_never_reports_itself_erased(self, settings):
        """An erasure nobody was asked to perform is not a completed one."""
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"billing": ["account"]}, DATA_OWNERS_VERSION="thin-1",
        )
        erasure = gdpr_orchestrator.request_erasure("document", "doc-1")
        assert erasure.parts.count() == 0
        gdpr_orchestrator._maybe_finalize(erasure)
        erasure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_ERASING

    def test_the_request_is_announced_on_the_bus(self, fleet):
        seen = []
        from stapel_core.comm import subscribe_action

        subscribe_action("gdpr.erasure.requested", lambda e: seen.append(e.payload))
        erasure = gdpr_orchestrator.request_erasure(
            "recording", "rec-1", workspace_id="ws-9",
        )
        assert seen and seen[-1]["subject_type"] == "recording"
        assert seen[-1]["subject_key"] == "rec-1"
        assert seen[-1]["workspace_id"] == "ws-9"
        assert seen[-1]["correlation_id"] == erasure.correlation_id


# ---------------------------------------------------------------------------
# ERASING -> TIMEOUT
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestErasureTimeout:
    def test_a_silent_owner_times_out_the_whole_request_and_says_so(self, fleet):
        from stapel_core.comm import subscribe_action

        from stapel_gdpr.tasks import sweep_deletion_deadlines

        alerts = []
        subscribe_action("gdpr.erasure.timeout", lambda e: alerts.append(e.payload))

        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
        gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "recordings", "job-1")
        ErasurePart.objects.filter(request=erasure, owner="media").update(
            deadline=timezone.now() - timedelta(minutes=1),
        )

        assert sweep_deletion_deadlines() == 1

        erasure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_TIMEOUT
        assert erasure.parts.get(owner="media").state == ErasurePart.STATE_TIMEOUT
        assert alerts and alerts[-1]["owners"] == ["media"]

    def test_a_timed_out_request_is_not_swept_twice(self, fleet):
        from stapel_gdpr.tasks import sweep_deletion_deadlines

        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
        erasure.parts.all().update(deadline=timezone.now() - timedelta(minutes=1))
        assert sweep_deletion_deadlines() == 2
        assert sweep_deletion_deadlines() == 0


# ---------------------------------------------------------------------------
# Subprocessor ledger
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSubprocessorLedger:
    def test_obligations_are_written_when_the_erasure_completes(self, settings):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"recordings": ["recording"]},
            DATA_OWNERS_VERSION="proc-1",
            SUBPROCESSORS=[
                {"name": "openai", "window_days": 30},
                {"name": "netcup", "window_days": 0},
            ],
        )
        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
        gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "recordings", "job-1")
        erasure.refresh_from_db()

        assert set(erasure.obligations.values_list("provider", flat=True)) == {"openai", "netcup"}
        # "erased from our systems on X; from all processors by Y" is a date,
        # not a sentence in a DPA.
        assert erasure.fully_erased_by >= erasure.due_at

    def test_a_longer_window_pushes_fully_erased_by_out(self, settings):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"recordings": ["recording"]},
            DATA_OWNERS_VERSION="proc-2",
            ERASURE_SLA_DAYS=1,
            SUBPROCESSORS=[{"name": "google", "window_days": 55}],
        )
        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
        gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "recordings", "job-1")
        erasure.refresh_from_db()
        assert erasure.fully_erased_by > erasure.due_at

    def test_recording_a_subset_twice_writes_one_row(self, settings):
        from stapel_gdpr.subprocessors import record_subprocessor_obligations

        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"recordings": ["recording"]},
            DATA_OWNERS_VERSION="proc-3",
            SUBPROCESSORS=[
                {"name": "openai", "window_days": 30},
                {"name": "xai", "window_days": 30},
            ],
        )
        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
        assert record_subprocessor_obligations(erasure, ["openai"]) == 1
        assert record_subprocessor_obligations(erasure, ["openai"]) == 0
        assert SubprocessorObligation.objects.filter(request=erasure).count() == 1


# ---------------------------------------------------------------------------
# Owner liveness — silence is a finding
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOwnerLiveness:
    def test_probe_records_every_declared_owner(self, fleet):
        gdpr_orchestrator.probe_data_owners()
        rows = {r.owner: r for r in DataOwnerHealth.objects.all()}
        assert set(rows) == set(FLEET_OWNERS)
        assert all(r.last_probe_at is not None for r in rows.values())
        assert all(r.last_alive_at is None for r in rows.values())

    def test_an_alive_answer_is_recorded_from_the_bus(self, fleet):
        gdpr_orchestrator.probe_data_owners()
        emit("gdpr.owner.alive", {"owner": "media", "subject_types": ["file"]})
        row = DataOwnerHealth.objects.get(owner="media")
        assert row.last_alive_at is not None
        assert row.answered_subject_types == ["file"]
        # The inventory's own claim is kept beside the answer, so a drift
        # between what a store says it holds and what it is declared to hold
        # is readable off one row.
        assert row.declared_subject_types == FLEET_OWNERS["media"]

    def test_w006_names_owners_that_never_answered(self, fleet):
        from stapel_gdpr.checks import check_data_owner_liveness

        gdpr_orchestrator.probe_data_owners()
        emit("gdpr.owner.alive", {"owner": "media", "subject_types": ["file"]})

        findings = check_data_owner_liveness(databases=["default"])
        assert len(findings) == 1
        assert findings[0].id == "gdpr.W006"
        assert "recordings" in findings[0].msg and "billing" in findings[0].msg
        assert "media" not in findings[0].msg

    def test_w006_is_silent_when_every_owner_answered(self, fleet):
        from stapel_gdpr.checks import check_data_owner_liveness

        gdpr_orchestrator.probe_data_owners()
        for owner in FLEET_OWNERS:
            emit("gdpr.owner.alive", {"owner": owner, "subject_types": FLEET_OWNERS[owner]})
        assert check_data_owner_liveness(databases=["default"]) == []

    def test_w006_fires_again_once_an_answer_goes_stale(self, settings, fleet):
        from stapel_gdpr.checks import check_data_owner_liveness

        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=FLEET_OWNERS, DATA_OWNERS_VERSION="fleet-1",
            OWNER_ALIVE_MAX_AGE_HOURS=48,
        )
        for owner in FLEET_OWNERS:
            emit("gdpr.owner.alive", {"owner": owner, "subject_types": []})
        DataOwnerHealth.objects.filter(owner="billing").update(
            last_alive_at=timezone.now() - timedelta(hours=49),
        )
        findings = check_data_owner_liveness(databases=["default"])
        assert len(findings) == 1 and "billing" in findings[0].msg

    def test_the_check_stays_silent_without_a_database(self, fleet):
        from stapel_gdpr.checks import check_data_owner_liveness

        assert check_data_owner_liveness(databases=None) == []


# ---------------------------------------------------------------------------
# Backup restore re-arms the clock
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRequeueAfterRestore:
    def _completed(self, subject_key: str, completed_at=None):
        erasure = gdpr_orchestrator.request_erasure("recording", subject_key)
        gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "recordings", "job")
        erasure.refresh_from_db()
        if completed_at:
            ErasureRequest.objects.filter(pk=erasure.pk).update(completed_at=completed_at)
            erasure.refresh_from_db()
        return erasure

    @pytest.fixture(autouse=True)
    def _one_owner(self, settings):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"recordings": ["recording"]},
            DATA_OWNERS_VERSION="restore-1",
        )

    def test_a_completed_erasure_inside_the_window_is_requeued(self):
        from io import StringIO

        from django.core.management import call_command

        source = self._completed("rec-1")
        out = StringIO()
        call_command(
            "gdpr_requeue_after_restore",
            restored_from=timezone.now().isoformat(),
            stdout=out,
        )
        clone = ErasureRequest.objects.get(source_request=source)
        assert clone.origin == ErasureRequest.ORIGIN_RESTORE_REQUEUE
        assert clone.subject_key == "rec-1"
        assert clone.state == ErasureRequest.STATE_ERASING
        assert clone.parts.count() == 1
        assert "re-queued 1 erasure" in out.getvalue()

    def test_a_second_run_for_the_same_window_writes_nothing(self):
        from io import StringIO

        from django.core.management import call_command

        self._completed("rec-1")
        stamp = timezone.now().isoformat()
        for _ in range(2):
            call_command("gdpr_requeue_after_restore", restored_from=stamp, stdout=StringIO())
        assert ErasureRequest.objects.filter(
            origin=ErasureRequest.ORIGIN_RESTORE_REQUEUE,
        ).count() == 1

    def test_an_overlapping_later_run_writes_nothing_either(self):
        from io import StringIO

        from django.core.management import call_command

        self._completed("rec-1")
        call_command(
            "gdpr_requeue_after_restore",
            restored_from=timezone.now().isoformat(), stdout=StringIO(),
        )
        call_command(
            "gdpr_requeue_after_restore",
            restored_from=(timezone.now() + timedelta(hours=2)).isoformat(),
            stdout=StringIO(),
        )
        assert ErasureRequest.objects.filter(
            origin=ErasureRequest.ORIGIN_RESTORE_REQUEUE,
        ).count() == 1

    def test_an_erasure_completed_before_the_window_is_left_alone(self):
        from io import StringIO

        from django.core.management import call_command

        self._completed("rec-old", completed_at=timezone.now() - timedelta(days=10))
        call_command(
            "gdpr_requeue_after_restore",
            restored_from=timezone.now().isoformat(), stdout=StringIO(),
        )
        assert not ErasureRequest.objects.filter(
            origin=ErasureRequest.ORIGIN_RESTORE_REQUEUE,
        ).exists()

    def test_a_bad_timestamp_is_refused(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command("gdpr_requeue_after_restore", restored_from="last tuesday")

    def test_dry_run_writes_nothing(self):
        from io import StringIO

        from django.core.management import call_command

        self._completed("rec-1")
        out = StringIO()
        call_command(
            "gdpr_requeue_after_restore",
            restored_from=timezone.now().isoformat(), dry_run=True, stdout=out,
        )
        assert "would re-queue" in out.getvalue()
        assert not ErasureRequest.objects.filter(
            origin=ErasureRequest.ORIGIN_RESTORE_REQUEUE,
        ).exists()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def allow_everyone(request, subject_type, subject_key):
    """Host authorizer stand-in: this deployment lets any caller ask."""
    return True


def refuse_everyone(request, subject_type, subject_key):
    return False


def explode(request, subject_type, subject_key):
    raise RuntimeError("the host's ownership lookup is down")


@pytest.mark.django_db
class TestErasureEndpoints:
    @pytest.fixture(autouse=True)
    def _owners(self, settings):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=FLEET_OWNERS, DATA_OWNERS_VERSION="http-1",
            ERASURE_AUTHORIZER="tests.test_erasure.allow_everyone",
        )

    def test_post_creates_the_request(self, authed_client):
        resp = authed_client.post(
            "/gdpr/api/v1/erasures",
            {"subject_type": "recording", "subject_key": "rec-1"},
            format="json",
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["subject_type"] == "recording"
        assert body["state"] == "erasing"
        assert sorted(body["unreceipted_owners"]) == ["media", "recordings"]
        assert body["fully_erased_by"]

    def test_post_refuses_an_unknown_subject_type(self, authed_client):
        resp = authed_client.post(
            "/gdpr/api/v1/erasures",
            {"subject_type": "spaceship", "subject_key": "x"},
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.gdpr.unknown_subject_type"

    def test_post_refuses_a_missing_field(self, authed_client):
        resp = authed_client.post(
            "/gdpr/api/v1/erasures", {"subject_type": "recording"}, format="json",
        )
        assert resp.status_code == 400

    def test_the_default_authorizer_is_staff_only(self, settings, authed_client):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=FLEET_OWNERS, DATA_OWNERS_VERSION="http-1",
        )
        resp = authed_client.post(
            "/gdpr/api/v1/erasures",
            {"subject_type": "recording", "subject_key": "rec-1"},
            format="json",
        )
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == "error.403.gdpr.erasure_forbidden"

    def test_a_staff_caller_passes_the_default_authorizer(self, settings, api_client, user):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=FLEET_OWNERS, DATA_OWNERS_VERSION="http-1",
        )
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        api_client.force_authenticate(user=user)
        resp = api_client.post(
            "/gdpr/api/v1/erasures",
            {"subject_type": "recording", "subject_key": "rec-1"},
            format="json",
        )
        assert resp.status_code == 202

    def test_a_refusing_authorizer_refuses(self, settings, authed_client):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=FLEET_OWNERS, DATA_OWNERS_VERSION="http-1",
            ERASURE_AUTHORIZER="tests.test_erasure.refuse_everyone",
        )
        resp = authed_client.post(
            "/gdpr/api/v1/erasures",
            {"subject_type": "recording", "subject_key": "rec-1"},
            format="json",
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "dotted", ["tests.test_erasure.explode", "nope.does.not.exist"],
    )
    def test_a_broken_authorizer_fails_closed(self, settings, authed_client, dotted):
        """An ownership check that fails open is worse than none."""
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=FLEET_OWNERS, DATA_OWNERS_VERSION="http-1",
            ERASURE_AUTHORIZER=dotted,
        )
        resp = authed_client.post(
            "/gdpr/api/v1/erasures",
            {"subject_type": "recording", "subject_key": "rec-1"},
            format="json",
        )
        assert resp.status_code == 403

    def test_get_returns_parts_and_obligations(self, settings, authed_client, user):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=FLEET_OWNERS, DATA_OWNERS_VERSION="http-1",
            ERASURE_AUTHORIZER="tests.test_erasure.allow_everyone",
            SUBPROCESSORS=[{"name": "openai", "window_days": 30}],
        )
        erasure = gdpr_orchestrator.request_erasure(
            "recording", "rec-1", requested_by=user.pk,
        )
        for owner in ("recordings", "media"):
            gdpr_orchestrator.mark_section_erased(erasure.correlation_id, owner, f"{owner}-1")

        resp = authed_client.get(f"/gdpr/api/v1/erasures/{erasure.pk}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "deleted"
        assert len(body["parts"]) == 2
        assert body["obligations"][0]["provider"] == "openai"

    def test_get_of_an_unknown_id_is_404(self, authed_client):
        resp = authed_client.get("/gdpr/api/v1/erasures/999999")
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == "error.404.gdpr.erasure_not_found"

    def test_someone_elses_erasure_is_not_enumerable(self, settings, authed_client):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=FLEET_OWNERS, DATA_OWNERS_VERSION="http-1",
            ERASURE_AUTHORIZER="tests.test_erasure.refuse_everyone",
        )
        erasure = gdpr_orchestrator.request_erasure("recording", "rec-1")
        assert authed_client.get(f"/gdpr/api/v1/erasures/{erasure.pk}").status_code == 404

    def test_me_erasures_lists_only_mine(self, authed_client, user):
        gdpr_orchestrator.request_erasure("recording", "mine", requested_by=user.pk)
        gdpr_orchestrator.request_erasure("recording", "theirs")

        resp = authed_client.get("/gdpr/api/v1/me/erasures")
        assert resp.status_code == 200
        rows = resp.json()
        assert [r["subject_key"] for r in rows] == ["mine"]

    def test_owners_health_is_staff_only(self, authed_client):
        assert authed_client.get("/gdpr/api/v1/owners/health").status_code == 403

    def test_owners_health_reports_the_table(self, api_client, user):
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        api_client.force_authenticate(user=user)

        gdpr_orchestrator.probe_data_owners()
        emit("gdpr.owner.alive", {"owner": "media", "subject_types": ["file"]})

        resp = api_client.get("/gdpr/api/v1/owners/health")
        assert resp.status_code == 200
        rows = {r["owner"]: r for r in resp.json()}
        assert set(rows) == set(FLEET_OWNERS)
        assert rows["media"]["alive"] is True
        assert rows["billing"]["alive"] is False
