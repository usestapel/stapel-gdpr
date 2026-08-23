"""DSAR intake: the edge between a person asking and the machine acting.

The clocks are the point. A request that is recorded but not acknowledged
has started a statutory countdown nobody is watching, so these tests pin
that the acknowledgement is sent inside the intake call and that its
*absence* is what the sweep and the boot check see.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_gdpr.models import DsarRequest, business_days_from
from stapel_gdpr.orchestrator import gdpr_orchestrator
from tests.support import gdpr_conf


@pytest.fixture(autouse=True)
def staffed(settings):
    settings.STAPEL_GDPR = gdpr_conf(
        DATA_OWNERS=["fake"],
        DATA_OWNERS_VERSION="dsar-1",
        DSAR_STAFF_EMAILS=["privacy@example.com"],
    )


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------


class TestBusinessDays:
    def test_three_business_days_skip_the_weekend(self):
        from datetime import datetime, timezone as dt_timezone

        friday = datetime(2026, 8, 21, 10, 0, tzinfo=dt_timezone.utc)
        assert friday.weekday() == 4
        # Fri + 3 business days is Wednesday, not Monday: counting the
        # weekend is how an automated acknowledgement misses the deadline.
        assert business_days_from(friday, 3).day == 26

    def test_a_midweek_request_counts_plainly(self):
        from datetime import datetime, timezone as dt_timezone

        monday = datetime(2026, 8, 17, 10, 0, tzinfo=dt_timezone.utc)
        assert business_days_from(monday, 3).day == 20


@pytest.mark.django_db
class TestIntake:
    def test_both_clocks_are_set_at_creation(self, db):
        from stapel_gdpr.dsar import create_dsar

        dsar = create_dsar(kind="rectification", subject_email="a@example.com")
        assert dsar.ack_due_at > dsar.received_at
        assert dsar.resolve_due_at == dsar.received_at + timedelta(days=30)

    def test_the_acknowledgement_is_sent_and_recorded(self, db):
        from stapel_gdpr.dsar import create_dsar

        dsar = create_dsar(kind="rectification", subject_email="a@example.com")
        assert dsar.ack_sent_at is not None
        assert dsar.state == DsarRequest.STATE_ACKNOWLEDGED

    def test_a_failed_acknowledgement_leaves_the_deadline_visibly_unmet(self, db, monkeypatch):
        """The record is not best-effort even though the mail is."""
        import stapel_core.notifications as notifications

        from stapel_gdpr.dsar import create_dsar

        def boom(**kwargs):
            raise RuntimeError("notifications are down")

        monkeypatch.setattr(notifications, "request_notification", boom)
        dsar = create_dsar(kind="rectification", subject_email="a@example.com")
        assert dsar.ack_sent_at is None
        assert dsar.state == DsarRequest.STATE_RECEIVED

    def test_erasure_kind_starts_the_cancellable_closure(self, user):
        from stapel_gdpr.dsar import create_dsar
        from stapel_gdpr.models import AccountClosureRequest

        dsar = create_dsar(
            kind="erasure", subject_email=user.email, user_id=user.pk,
        )
        closure = AccountClosureRequest.objects.get(user_id=user.pk)
        assert closure.status == AccountClosureRequest.STATUS_GRACE
        assert dsar.state == DsarRequest.STATE_IN_PROGRESS
        assert f"closure={closure.pk}" in dsar.note

    def test_access_kind_starts_an_export(self, user):
        from stapel_gdpr.dsar import create_dsar

        dsar = create_dsar(kind="access", subject_email=user.email, user_id=user.pk)
        assert dsar.export_request is not None
        assert dsar.state == DsarRequest.STATE_IN_PROGRESS

    def test_an_anonymous_request_is_never_wired_automatically(self, db):
        """An unverified email must not become a deletion oracle."""
        from stapel_gdpr.dsar import create_dsar

        dsar = create_dsar(
            kind="erasure",
            subject_email="stranger@example.com",
            channel=DsarRequest.CHANNEL_FORM,
        )
        assert dsar.user_id is None
        assert dsar.erasure_request is None
        assert dsar.state == DsarRequest.STATE_ACKNOWLEDGED

    def test_a_refused_automation_is_recorded_not_swallowed(self, user):
        from stapel_gdpr.dsar import create_dsar
        from stapel_gdpr.models import LegalHold

        LegalHold.objects.create(user_id=user.pk, reason="litigation")
        dsar = create_dsar(kind="erasure", subject_email=user.email, user_id=user.pk)
        assert "legal_hold" in dsar.note


# ---------------------------------------------------------------------------
# Deadlines: the sweep and the boot check
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDsarDeadlines:
    def _overdue_ack(self):
        dsar = DsarRequest.objects.create(
            kind="access", subject_email="a@example.com",
        )
        DsarRequest.objects.filter(pk=dsar.pk).update(
            ack_due_at=timezone.now() - timedelta(hours=1),
        )
        dsar.refresh_from_db()
        return dsar

    def test_an_unacknowledged_request_is_reported_once(self):
        from stapel_core.comm import subscribe_action

        from stapel_gdpr.tasks import sweep_dsar_deadlines

        alerts = []
        subscribe_action("gdpr.dsar.overdue", lambda e: alerts.append(e.payload))

        self._overdue_ack()
        assert sweep_dsar_deadlines() == 1
        assert alerts[-1]["deadline"] == "acknowledgement"
        # A daily sweep must not turn one missed deadline into a daily alarm.
        assert sweep_dsar_deadlines() == 0

    def test_an_unresolved_request_is_reported(self):
        from stapel_gdpr.tasks import sweep_dsar_deadlines

        dsar = DsarRequest.objects.create(
            kind="access", subject_email="a@example.com", ack_sent_at=timezone.now(),
        )
        DsarRequest.objects.filter(pk=dsar.pk).update(
            resolve_due_at=timezone.now() - timedelta(hours=1),
        )
        assert sweep_dsar_deadlines() == 1

    def test_a_resolved_request_is_not_chased(self):
        from stapel_gdpr.tasks import sweep_dsar_deadlines

        dsar = self._overdue_ack()
        DsarRequest.objects.filter(pk=dsar.pk).update(state=DsarRequest.STATE_RESOLVED)
        assert sweep_dsar_deadlines() == 0

    def test_w008_reports_the_unacknowledged_queue(self):
        from stapel_gdpr.checks import check_dsar_deadlines

        self._overdue_ack()
        findings = check_dsar_deadlines(databases=["default"])
        assert len(findings) == 1
        assert findings[0].id == "gdpr.W008"

    def test_w008_is_silent_when_everything_was_acknowledged(self, db):
        from stapel_gdpr.checks import check_dsar_deadlines

        DsarRequest.objects.create(
            kind="access", subject_email="a@example.com", ack_sent_at=timezone.now(),
        )
        assert check_dsar_deadlines(databases=["default"]) == []

    def test_w008_stays_silent_without_a_database(self, db):
        from stapel_gdpr.checks import check_dsar_deadlines

        self._overdue_ack()
        assert check_dsar_deadlines(databases=None) == []


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDsarEndpoints:
    def test_an_authenticated_request_uses_the_account_email(self, authed_client, user):
        resp = authed_client.post(
            "/gdpr/api/v1/dsar", {"kind": "rectification", "note": "fix my name"},
            format="json",
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["channel"] == "app"
        assert body["subject_email"] == user.email
        assert body["ack_sent_at"]

    def test_an_anonymous_request_needs_an_email(self, api_client):
        resp = api_client.post(
            "/gdpr/api/v1/dsar", {"kind": "access"}, format="json",
        )
        assert resp.status_code == 400

    def test_an_anonymous_request_is_accepted_from_the_public_form(self, api_client):
        resp = api_client.post(
            "/gdpr/api/v1/dsar",
            {"kind": "access", "email": "stranger@example.com"},
            format="json",
        )
        assert resp.status_code == 201
        assert resp.json()["channel"] == "form"

    def test_an_unknown_kind_is_refused(self, api_client):
        resp = api_client.post(
            "/gdpr/api/v1/dsar",
            {"kind": "telepathy", "email": "a@example.com"},
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.gdpr.unknown_dsar_kind"

    def test_a_configured_captcha_is_enforced_on_the_public_form(self, settings, api_client):
        """The anonymous door goes through core's tiered challenge policy."""
        settings.STAPEL_CAPTCHA = {"BACKEND": "turnstile", "SECRET": "test-secret"}
        resp = api_client.post(
            "/gdpr/api/v1/dsar",
            {"kind": "access", "email": "stranger@example.com"},
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.captcha_required"

    def test_the_queue_is_staff_only(self, authed_client):
        assert authed_client.get("/gdpr/api/v1/dsar").status_code == 403

    def test_staff_read_the_queue(self, api_client, user):
        from stapel_gdpr.dsar import create_dsar

        create_dsar(kind="access", subject_email="a@example.com")
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        api_client.force_authenticate(user=user)

        resp = api_client.get("/gdpr/api/v1/dsar")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_matching_a_request_to_an_account_wires_it(self, api_client, user):
        from stapel_gdpr.dsar import create_dsar
        from stapel_gdpr.models import AccountClosureRequest

        dsar = create_dsar(
            kind="erasure",
            subject_email=user.email,
            channel=DsarRequest.CHANNEL_FORM,
        )
        assert not AccountClosureRequest.objects.filter(user_id=user.pk).exists()

        user.is_staff = True
        user.save(update_fields=["is_staff"])
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"/gdpr/api/v1/dsar/{dsar.pk}",
            {"user_id": str(user.pk), "note": "identity verified by passport"},
            format="json",
        )
        assert resp.status_code == 200
        assert AccountClosureRequest.objects.filter(user_id=user.pk).exists()

    def test_patch_of_an_unknown_id_is_404(self, api_client, user):
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        api_client.force_authenticate(user=user)
        resp = api_client.patch("/gdpr/api/v1/dsar/999999", {"state": "resolved"}, format="json")
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == "error.404.gdpr.dsar_not_found"

    def test_patch_refuses_an_unknown_state(self, api_client, user):
        from stapel_gdpr.dsar import create_dsar

        dsar = create_dsar(kind="access", subject_email="a@example.com")
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"/gdpr/api/v1/dsar/{dsar.pk}", {"state": "vanished"}, format="json",
        )
        assert resp.status_code == 400

    def test_patch_records_state_and_note(self, api_client, user):
        from stapel_gdpr.dsar import create_dsar

        dsar = create_dsar(kind="rectification", subject_email="a@example.com")
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"/gdpr/api/v1/dsar/{dsar.pk}",
            {"state": "resolved", "note": "name corrected"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "resolved"
        assert resp.json()["note"] == "name corrected"


# ---------------------------------------------------------------------------
# Account closure still produces an erasure — the HTTP surface is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAccountClosureIsOneSubject:
    def test_grace_end_creates_an_account_erasure(self, settings, user, fake_provider):
        from stapel_gdpr.models import AccountClosureRequest, ErasureRequest

        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake"], DATA_OWNERS_VERSION="closure-1",
        )
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        assert not ErasureRequest.objects.filter(closure=closure).exists()

        gdpr_orchestrator.execute_deletion(closure)

        erasure = closure.erasure
        assert erasure.subject_type == "account"
        assert erasure.subject_key == str(user.pk)
        assert erasure.correlation_id == closure.correlation_id
        assert erasure.state == ErasureRequest.STATE_DELETED
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETED

    def test_user_deleted_still_fires_for_one_more_minor(self, settings, user, fake_provider):
        from stapel_core.comm import subscribe_action

        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake"], DATA_OWNERS_VERSION="closure-2",
        )
        seen = []
        subscribe_action("user.deleted", lambda e: seen.append(e.payload))
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)
        assert seen and seen[-1]["correlation_id"] == closure.correlation_id

    def test_a_retried_execution_reuses_the_same_erasure(self, settings, user, fake_provider):
        from stapel_gdpr.models import ErasureRequest

        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake"], DATA_OWNERS_VERSION="closure-3",
        )
        fake_provider.fail_delete = True
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)
        fake_provider.fail_delete = False
        gdpr_orchestrator.execute_deletion(closure)

        assert ErasureRequest.objects.filter(closure=closure).count() == 1
