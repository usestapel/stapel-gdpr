"""Celery beat sweep tests — inactivity, deadlines, retention, grace execution."""
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_gdpr.models import (
    AccountClosureRequest,
    DataExportRequest,
    LegalHold,
)
from stapel_gdpr.orchestrator import gdpr_orchestrator
from stapel_gdpr.tasks import (
    check_inactive_accounts,
    get_gdpr_beat_schedule,
    notify_llm_providers_of_deletion,
    process_expired_grace_periods,
    run_data_export,
    sweep_pending_exports,
)


def _make_user(last_login=None, is_active=True):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    u = User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password="testpass-1234",
    )
    User.objects.filter(pk=u.pk).update(last_login=last_login, is_active=is_active)
    u.refresh_from_db()
    return u


def _notification_events():
    from stapel_core.bus import get_bus

    return [
        e for e in get_bus().events
        if e.payload.get("notification_type", "").startswith("gdpr.inactivity")
    ]


@pytest.mark.django_db
class TestInactivityChecker:
    def test_closes_accounts_inactive_for_a_year(self, db):
        stale = _make_user(last_login=timezone.now() - timedelta(days=366))
        fresh = _make_user(last_login=timezone.now() - timedelta(days=10))

        check_inactive_accounts()

        closure = AccountClosureRequest.objects.get(user_id=stale.pk)
        assert closure.trigger == AccountClosureRequest.TRIGGER_INACTIVITY
        assert closure.status == AccountClosureRequest.STATUS_GRACE
        stale.refresh_from_db()
        assert stale.is_active is False

        assert not AccountClosureRequest.objects.filter(user_id=fresh.pk).exists()
        fresh.refresh_from_db()
        assert fresh.is_active is True

        closed_mails = [
            e for e in _notification_events()
            if e.payload["notification_type"] == "gdpr.inactivity_closed"
        ]
        assert any(e.payload["email"] == stale.email for e in closed_mails)

    def test_existing_closure_not_duplicated(self, db):
        stale = _make_user(last_login=timezone.now() - timedelta(days=366))
        gdpr_orchestrator.initiate_closure(stale.pk)

        check_inactive_accounts()

        assert AccountClosureRequest.objects.filter(user_id=stale.pk).count() == 1

    def test_legal_hold_blocks_inactivity_closure(self, db):
        held = _make_user(last_login=timezone.now() - timedelta(days=366))
        LegalHold.objects.create(user_id=held.pk, reason="litigation")

        check_inactive_accounts()  # must not raise

        assert not AccountClosureRequest.objects.filter(user_id=held.pk).exists()
        held.refresh_from_db()
        assert held.is_active is True

    def test_warning_windows_send_emails(self, db):
        warn60 = _make_user(last_login=timezone.now() - timedelta(days=365 - 60))
        warn14 = _make_user(last_login=timezone.now() - timedelta(days=365 - 14))
        outside = _make_user(last_login=timezone.now() - timedelta(days=365 - 100))

        check_inactive_accounts()

        warnings = [
            e for e in _notification_events()
            if e.payload["notification_type"] == "gdpr.inactivity_warning"
        ]
        by_email = {e.payload["email"]: e.payload["variables"] for e in warnings}
        assert by_email[warn60.email] == {"days_remaining": 60}
        assert by_email[warn14.email] == {"days_remaining": 14}
        assert outside.email not in by_email
        # warned users are NOT closed
        assert not AccountClosureRequest.objects.filter(
            user_id__in=[warn60.pk, warn14.pk],
        ).exists()

    def test_warning_email_failure_is_swallowed(self, db, monkeypatch):
        _make_user(last_login=timezone.now() - timedelta(days=365 - 14))

        def boom(**kwargs):
            raise RuntimeError("smtp down")

        monkeypatch.setattr("stapel_core.notifications.request_notification", boom)
        check_inactive_accounts()  # must not raise


@pytest.mark.django_db
class TestExportTasks:
    def test_run_data_export_retries_on_failure(self, monkeypatch):
        def boom(request_id):
            raise RuntimeError("db gone")

        monkeypatch.setattr(gdpr_orchestrator, "run_export", boom)
        with pytest.raises(Exception):  # Retry in worker mode, exc when eager
            run_data_export.delay(12345)

    def test_sweep_pending_exports_assembles_expired(self, settings, user):
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)
        DataExportRequest.objects.filter(pk=req.pk).update(
            status=DataExportRequest.STATUS_PROCESSING,
            deadline=timezone.now() - timedelta(minutes=1),
        )

        sweep_pending_exports()

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY
        assert req.download_token


@pytest.mark.django_db
class TestGraceWorker:
    def test_deletion_error_is_logged_not_raised(self, user, monkeypatch, caplog):
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        AccountClosureRequest.objects.filter(pk=closure.pk).update(
            grace_ends_at=timezone.now() - timedelta(hours=1),
        )

        def boom(c):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(gdpr_orchestrator, "execute_deletion", boom)
        process_expired_grace_periods()  # must not raise

        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_GRACE
        assert any("execute_deletion failed" in r.message for r in caplog.records)


class TestMiscTasks:
    def test_notify_llm_providers_logs_obligation(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="stapel_gdpr.tasks"):
            notify_llm_providers_of_deletion("user-1", ["openrouter", "anthropic"])
        assert any("LLM deletion obligation" in r.message for r in caplog.records)

    def test_beat_schedule_contains_all_sweeps(self):
        schedule = get_gdpr_beat_schedule()
        assert set(schedule) == {
            "gdpr-export-deadline-sweep",
            "gdpr-account-closure-worker",
            "gdpr-inactivity-checker",
            "gdpr-retention-cleanup",
        }
        for entry in schedule.values():
            assert entry["task"].startswith("stapel_gdpr.tasks.")
            assert "schedule" in entry


class DummyRegProvider:
    """Referenced by dotted path in test_app_ready_registers_providers."""

    section = "dummyreg"

    def export(self, user_id):
        return {}

    def delete(self, user_id):
        pass

    def anonymize(self, user_id):
        pass


def test_app_ready_registers_providers(settings):
    from django.apps import apps as django_apps

    from stapel_core.gdpr import gdpr_registry

    settings.GDPR_PROVIDERS = ["tests.test_tasks.DummyRegProvider"]
    cfg = django_apps.get_app_config("gdpr")
    try:
        cfg._register_gdpr_providers()
        assert "dummyreg" in gdpr_registry.sections
    finally:
        gdpr_registry._providers = [
            p for p in gdpr_registry._providers if p.section != "dummyreg"
        ]
