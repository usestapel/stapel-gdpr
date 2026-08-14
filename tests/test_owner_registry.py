"""GDPR-02: erasure completeness is proven, or it is not claimed.

The orchestrator used to mark a closure DELETED as soon as the in-process
providers returned — with the remote list empty, that check was vacuously
true. A deployment with one registered provider therefore reported completed
erasures while every other store kept the data. These tests pin the inverted
default: no inventory, an unreachable owner, a stale inventory, a silent owner
— each of them keeps the closure in DELETING and each of them is reported at
boot.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_gdpr.checks import check_data_owner_registry, check_reregistration_hashes
from stapel_gdpr.models import (
    AccountClosureRequest,
    AccountDeletionPart,
    DataExportRequest,
)
from stapel_gdpr.orchestrator import gdpr_orchestrator
from stapel_gdpr.owners import data_owner_report
from tests.support import gdpr_conf


@pytest.fixture
def wired(settings):
    """A deployment that declared exactly the one provider it registered."""
    settings.STAPEL_GDPR = gdpr_conf(
        DATA_OWNERS=["fake"], DATA_OWNERS_VERSION="test-registry-1",
    )
    return settings


@pytest.mark.django_db
class TestCompletenessBlocksDeleted:
    def test_unconfigured_registry_blocks_deleted(self, settings, user, fake_provider):
        """The shape the audit found: nothing declared, everything "erased"."""
        settings.STAPEL_GDPR = gdpr_conf()  # no DATA_OWNERS at all

        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING
        assert closure.local_erasure_done is True  # the local half really ran
        assert closure.deleted_at is None

    def test_declared_but_unreachable_owner_blocks_deleted(
        self, settings, user, fake_provider,
    ):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake", {"name": "recordings", "kind": "local"}],
            DATA_OWNERS_VERSION="test-registry-2",
        )
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING
        assert "recordings" in closure.unreceipted_owners

    def test_registered_but_undeclared_provider_blocks_deleted(
        self, settings, user, fake_provider,
    ):
        """A stale inventory cannot certify anything, even a superset one."""
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["profiles"], DATA_OWNERS_VERSION="test-registry-3",
        )
        report = data_owner_report()
        assert report.undeclared == ("fake",)

        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)
        gdpr_orchestrator.mark_section_erased(closure.correlation_id, "profiles", "r-1")

        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING

    def test_full_receipts_flip_deleted_and_record_them(self, settings, user, fake_provider):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake", {"name": "recordings", "kind": "remote"}],
            DATA_OWNERS_VERSION="test-registry-4",
        )
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING

        gdpr_orchestrator.mark_section_erased(
            closure.correlation_id, "recordings", "tombstone-42",
        )

        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETED
        assert closure.completeness_waived is False
        assert closure.registry_version == "test-registry-4"
        receipts = dict(closure.parts.values_list("service", "receipt_id"))
        assert receipts["recordings"] == "tombstone-42"
        assert receipts["fake"]  # local owners leave a receipt too

    def test_named_escape_hatch_finalizes_and_marks_the_waiver(
        self, settings, user, fake_provider, caplog,
    ):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake", {"name": "recordings", "kind": "remote"}],
            DATA_OWNERS_VERSION="test-registry-5",
            ALLOW_ERASURE_WITHOUT_RECEIPTS=True,
        )
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETED
        assert closure.completeness_waived is True
        assert any("without full receipts" in r.message for r in caplog.records)


@pytest.mark.django_db
class TestOwnerTimeout:
    def test_silent_owner_times_out_and_keeps_blocking(self, settings, user, fake_provider):
        from stapel_gdpr.tasks import sweep_deletion_deadlines

        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake", {"name": "recordings", "kind": "remote", "timeout_hours": 1}],
            DATA_OWNERS_VERSION="test-registry-6",
        )
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        part = closure.parts.get(service="recordings")
        assert part.deadline is not None
        AccountDeletionPart.objects.filter(pk=part.pk).update(
            deadline=timezone.now() - timedelta(minutes=1),
        )

        assert sweep_deletion_deadlines() == 1
        part.refresh_from_db()
        assert part.status == AccountDeletionPart.STATUS_TIMEOUT

        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING


@pytest.mark.django_db
class TestPartialExportIsExplicit:
    def test_missing_owner_marks_the_export_partial(self, settings, user, fake_provider):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake", {"name": "recordings", "kind": "remote"}],
            DATA_OWNERS_VERSION="test-registry-7",
        )
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)

        DataExportRequest.objects.filter(pk=req.pk).update(deadline=timezone.now())
        gdpr_orchestrator.sweep_deadlines()

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY
        assert req.is_partial is True
        assert req.missing_services == ["recordings"]

    def test_unconfigured_registry_makes_every_export_partial(
        self, settings, user, fake_provider,
    ):
        settings.STAPEL_GDPR = gdpr_conf()
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY
        assert req.is_partial is True

    def test_complete_export_is_not_marked_partial(self, wired, user, fake_provider):
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)

        req.refresh_from_db()
        assert req.is_partial is False
        assert req.missing_services == []


@pytest.mark.django_db
class TestBootChecks:
    def test_unconfigured_registry_is_a_boot_error(self, settings):
        settings.STAPEL_GDPR = gdpr_conf()
        ids = [m.id for m in check_data_owner_registry()]
        assert "gdpr.E001" in ids

    def test_missing_and_undeclared_owners_are_boot_errors(self, settings, fake_provider):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=[{"name": "recordings", "kind": "local"}],
            DATA_OWNERS_VERSION="v1",
        )
        messages = [str(m) for m in check_data_owner_registry()]
        assert any("recordings" in m for m in messages)
        assert any("fake" in m for m in messages)

    def test_open_escape_hatch_is_reported(self, settings, fake_provider):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake"],
            DATA_OWNERS_VERSION="v1",
            ALLOW_ERASURE_WITHOUT_RECEIPTS=True,
        )
        warnings = [m for m in check_data_owner_registry() if m.id == "gdpr.W003"]
        assert any("ALLOW_ERASURE_WITHOUT_RECEIPTS" in str(w) for w in warnings)

    def test_missing_revoker_is_reported(self, settings, fake_provider):
        settings.STAPEL_GDPR = {"DATA_OWNERS": ["fake"], "DATA_OWNERS_VERSION": "v1"}
        assert any(m.id == "gdpr.W005" for m in check_data_owner_registry())

    def test_wired_deployment_is_clean(self, wired, fake_provider):
        assert check_data_owner_registry() == []
        assert check_reregistration_hashes(databases=["default"]) == []


@pytest.fixture
def dummy_backend(monkeypatch):
    """Every query behaves like Django's dummy backend: no database at all.

    ``django.db.backends.dummy`` is what Django fills in when ``DATABASES``
    has no ENGINE — the shape of a boot smoke test that runs without a
    database — and every one of its API calls raises ``ImproperlyConfigured``
    (``django/db/backends/dummy/base.py``), which is NOT a ``DatabaseError``.
    Overriding ``DATABASES`` cannot express this in-process (Django lists it
    in ``COMPLEX_OVERRIDE_SETTINGS``: the connection handler keeps the
    connections it already built), so the refusal is injected where the
    check meets the ORM.
    """
    from django.core.exceptions import ImproperlyConfigured
    from django.db.models.query import QuerySet

    def complain(*args, **kwargs):
        raise ImproperlyConfigured(
            "settings.DATABASES is improperly configured. Please supply the "
            "ENGINE value. Check settings documentation for more details."
        )

    monkeypatch.setattr(QuerySet, "count", complain)


@pytest.fixture
def query_is_forbidden(monkeypatch):
    """Any query at all fails the test, with an error the check cannot catch.

    ``dummy_backend`` alone cannot prove "no database was touched": the
    widened ``except`` would swallow the evidence and the test would pass
    on a check that queried anyway.
    """
    from django.db.models.query import QuerySet

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "the check queried the database although it was offered none"
        )

    monkeypatch.setattr(QuerySet, "count", forbidden)


class TestReregistrationCheckDatabaseContract:
    """A check may only query the databases it was handed.

    ``django.core.checks.registry.run_checks`` calls every check with
    ``databases=`` — the aliases the caller opted into (``manage.py check
    --database default``, or ``migrate``). ``None`` means "touch no
    database", and that is exactly what a boot smoke test running without
    one passes. ``django.core.checks.database.check_database_backends`` is
    the canonical shape of the contract.

    Before 0.4.1 this check queried unconditionally and caught only
    ``DatabaseError``, so a deployment with no database did not get a
    finding — it got an ``ImproperlyConfigured`` traceback out of
    ``manage.py check``.
    """

    def test_no_databases_offered_means_no_query(self, query_is_forbidden):
        assert check_reregistration_hashes() == []

    def test_the_registry_runs_it_without_a_database(self, query_is_forbidden):
        """The path `manage.py check` takes: every check, ``databases=None``."""
        from django.core.checks.registry import registry

        findings = registry.run_checks(tags=["gdpr"], databases=None)
        assert not [f for f in findings if f.id == "gdpr.E004"]

    @pytest.mark.django_db
    def test_the_registry_reports_findings_when_a_database_is_offered(self, user):
        """``manage.py check --database default`` still sees the real rows."""
        import hashlib

        from django.core.checks.registry import registry

        from stapel_gdpr.models import ReRegistrationHash

        ReRegistrationHash.objects.create(
            hash_type="email",
            hash_value=hashlib.sha256(b"person@example.com").hexdigest(),
            user_id_was=str(user.pk),
            expires_at=timezone.now() + timedelta(days=30),
        )
        ids = [f.id for f in registry.run_checks(tags=["gdpr"], databases=["default"])]
        assert "gdpr.E004" in ids

    def test_an_unreachable_database_degrades_rather_than_explodes(
        self, dummy_backend
    ):
        assert check_reregistration_hashes(databases=["default"]) == []
