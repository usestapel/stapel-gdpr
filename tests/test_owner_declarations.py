"""GDPR-09: the host's inventory is checked against the installed libraries.

The 2026-09-07 fleet incident, reproduced. A deployment listed ``"profiles"``
and ``"cdn"`` — app labels, not owner names; the libraries declare
``"profile"`` and ``"media"`` — and omitted ``video`` and ``agent``
altogether. Four stores were never asked to erase anything. The misspelled
two were inferred *remote* and timed out in silence; the omitted two got no
receipt slot, so the request had nothing left to wait for and reported itself
complete. `manage.py check` was green throughout, and an external audit found
it months later.

These tests pin the four findings that make that deployment red at boot, and
the report that refuses to say "complete" over a silent owner.
"""
import pytest
from django.utils import timezone

from stapel_gdpr.checks import check_data_owner_names
from stapel_gdpr.declarations import (
    SEAM_CONSTANT,
    SEAM_PROVIDER,
    SEAM_REGISTRATION,
    OwnerDeclaration,
    installed_owner_declarations,
    nearest_declared_name,
)
from tests.support import gdpr_conf


class _StubAppConfig:
    """The two attributes :func:`_from_modules` reads off an installed app."""

    def __init__(self, name, label):
        self.name = name
        self.label = label


@pytest.fixture
def blobs_app(monkeypatch):
    """Install ``tests.fake_media_lib`` as an app labelled ``blobs``.

    A stub rather than an INSTALLED_APPS override on purpose: the code under
    test reads ``name`` and ``label`` off an app config and imports
    ``<name>.erasure`` for real, so this exercises the actual import and the
    actual constant, without re-running every app's ``ready()``.
    """
    from django.apps import apps

    real = apps.get_app_configs

    def with_stub():
        return list(real()) + [
            _StubAppConfig("tests.fake_media_lib", "blobs"),
        ]

    monkeypatch.setattr(apps, "get_app_configs", with_stub)
    return "fakemedia"


@pytest.fixture
def canonical_owner():
    """A library on the canonical seam: ``register_gdpr_owner`` in ready()."""
    from stapel_core.gdpr import register_gdpr_owner
    from stapel_core.gdpr.owners import _reset_gdpr_owners

    def erase(subject_type, subject_key, workspace_id):
        return None

    # A subject type no other test can request, so the comm subscription this
    # leaves behind can never fire inside another test's erasure.
    register_gdpr_owner("fakeagent", ("fakething",), erase)
    yield "fakeagent"
    _reset_gdpr_owners()


# =============================================================================
# Reading the declarations
# =============================================================================


class TestInstalledOwnerDeclarations:
    def test_reads_the_canonical_registration(self, canonical_owner):
        found = installed_owner_declarations()

        assert found[canonical_owner].subject_types == ("fakething",)
        assert found[canonical_owner].seam == SEAM_REGISTRATION

    def test_reads_a_legacy_gdpr_provider(self, fake_provider):
        found = installed_owner_declarations()

        assert "fake" in found
        assert found["fake"].seam == SEAM_PROVIDER
        # A bare provider carries no subject types; empty must read as
        # "unknown", never as "this owner claims nothing".
        assert found["fake"].subject_types == ()

    def test_reads_module_constants_and_records_the_app_label_as_an_alias(
        self, blobs_app,
    ):
        found = installed_owner_declarations()

        declaration = found[blobs_app]
        assert declaration.seam == SEAM_CONSTANT
        assert declaration.source == "tests.fake_media_lib.erasure"
        assert declaration.subject_types == ("account", "workspace", "file")
        assert "blobs" in declaration.aliases
        assert blobs_app not in declaration.aliases

    def test_an_app_without_an_erasure_seam_declares_nothing(self):
        """django.contrib.auth is installed and is not a GDPR data owner."""
        assert "auth" not in installed_owner_declarations()


class TestNearestDeclaredName:
    def test_an_app_label_resolves_to_the_owner_it_owns(self):
        declared = {
            "media": OwnerDeclaration(name="media", aliases=("cdn", "stapel_cdn")),
        }

        assert nearest_declared_name("cdn", declared) == "media"

    def test_a_near_miss_resolves(self):
        declared = {"profile": OwnerDeclaration(name="profile")}

        assert nearest_declared_name("profiles", declared) == "profile"

    def test_an_unrelated_name_resolves_to_nothing(self):
        """A remote owner in another container is not a typo of a local one."""
        declared = {"profile": OwnerDeclaration(name="profile")}

        assert nearest_declared_name("billing", declared) is None

    def test_a_declared_name_is_not_a_near_miss_of_itself(self):
        declared = {"profile": OwnerDeclaration(name="profile")}

        assert nearest_declared_name("profile", declared) is None


# =============================================================================
# gdpr.E009 — a name no installed library declares
# =============================================================================


def _ids(problems):
    return sorted(p.id for p in problems)


class TestUnknownOwnerName:
    def test_the_app_label_is_reported_with_the_name_it_should_have_been(
        self, settings, blobs_app,
    ):
        """darom's ``"cdn"`` where the library declares ``"media"``."""
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"blobs": ["account", "workspace", "file"]},
            DATA_OWNERS_VERSION="incident-1",
        )

        problems = check_data_owner_names()

        e009 = [p for p in problems if p.id == "gdpr.E009"]
        assert len(e009) == 1
        assert '"blobs" -> "fakemedia"' in e009[0].msg
        assert "tests.fake_media_lib.erasure" in e009[0].msg

    def test_a_misspelling_is_reported(self, settings, blobs_app):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={
                "fakemedias": ["account", "workspace", "file"],
            },
            DATA_OWNERS_VERSION="incident-2",
        )

        problems = check_data_owner_names()

        assert "gdpr.E009" in _ids(problems)

    def test_a_remote_owner_this_process_cannot_see_is_not_a_finding(
        self, settings, blobs_app,
    ):
        """The microservices shape: most owners live in other containers."""
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={
                "fakemedia": ["account", "workspace", "file"],
                "billing": {"subject_types": ["account"], "kind": "remote"},
            },
            DATA_OWNERS_VERSION="remote-1",
        )

        assert check_data_owner_names() == []

    def test_the_correct_name_produces_nothing(self, settings, blobs_app):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"fakemedia": ["account", "workspace", "file"]},
            DATA_OWNERS_VERSION="correct-1",
        )

        assert check_data_owner_names() == []


# =============================================================================
# gdpr.E010 / gdpr.W011 — an installed store the inventory never asks
# =============================================================================


class TestUndeclaredInstalledOwner:
    def test_an_omitted_library_is_an_error(self, settings, blobs_app):
        """darom's missing ``video`` and ``agent``: erased by nobody, ever."""
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"auth": ["account"]},
            DATA_OWNERS_VERSION="incident-3",
        )

        problems = check_data_owner_names()

        e010 = [p for p in problems if p.id == "gdpr.E010"]
        assert len(e010) == 1
        assert '"fakemedia"' in e010[0].msg
        assert "account, workspace, file" in e010[0].msg

    def test_a_misspelled_name_is_not_also_reported_as_a_missing_store(
        self, settings, blobs_app,
    ):
        """One edit fixes both; two findings read as two stores to go find."""
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"blobs": ["account", "workspace", "file"]},
            DATA_OWNERS_VERSION="incident-4",
        )

        assert _ids(check_data_owner_names()) == ["gdpr.E009"]

    def test_a_deliberate_opt_out_is_a_warning_not_silence(
        self, settings, blobs_app,
    ):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"auth": ["account"]},
            DATA_OWNERS_OPT_OUT=["fakemedia"],
            DATA_OWNERS_VERSION="optout-1",
        )

        problems = check_data_owner_names()

        assert _ids(problems) == ["gdpr.W011"]
        assert '"fakemedia"' in problems[0].msg

    def test_an_opt_out_for_a_name_that_is_declared_does_not_silence_it(
        self, settings, blobs_app,
    ):
        """The hatch excuses an ABSENCE; it cannot excuse a wrong name."""
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"blobs": ["account"]},
            DATA_OWNERS_OPT_OUT=["blobs"],
            DATA_OWNERS_VERSION="optout-2",
        )

        assert "gdpr.E009" in _ids(check_data_owner_names())


# =============================================================================
# gdpr.W012 — a subject the owner erases and the host never asks about
# =============================================================================


class TestUnaskedSubjectTypes:
    def test_subjects_the_entry_does_not_list_are_reported(
        self, settings, blobs_app,
    ):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fakemedia"],  # bare name: claims `account` only
            DATA_OWNERS_VERSION="subjects-1",
        )

        problems = check_data_owner_names()

        w012 = [p for p in problems if p.id == "gdpr.W012"]
        assert len(w012) == 1
        assert "'workspace'" in w012[0].msg
        assert "'file'" in w012[0].msg
        assert "'account'" not in w012[0].msg

    def test_a_subject_missing_from_subject_types_is_reported(
        self, settings, blobs_app,
    ):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"fakemedia": ["account", "workspace", "file"]},
            SUBJECT_TYPES=["account", "workspace"],  # `file` cannot be requested
            DATA_OWNERS_VERSION="subjects-2",
        )

        problems = check_data_owner_names()

        w012 = [p for p in problems if p.id == "gdpr.W012"]
        assert len(w012) == 1
        assert 'SUBJECT_TYPES' in w012[0].msg


# =============================================================================
# The check stays quiet where it has nothing to say
# =============================================================================


class TestSilence:
    def test_an_empty_inventory_is_left_to_e001(self, settings, blobs_app):
        settings.STAPEL_GDPR = gdpr_conf()

        assert check_data_owner_names() == []

    def test_no_installed_owner_library_is_not_a_finding(self, settings):
        """An erasure service that holds no stores of its own is normal."""
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["recordings", "billing"],
            DATA_OWNERS_VERSION="microservice-1",
        )

        assert check_data_owner_names() == []

    def test_the_check_is_registered_with_django(self):
        from django.core.checks import registry

        assert check_data_owner_names in registry.registry.registered_checks


# =============================================================================
# The report must not say "complete" over silence
# =============================================================================


@pytest.mark.django_db
class TestIncompleteOutcome:
    @pytest.fixture
    def erasure(self, settings, fake_provider):
        from stapel_gdpr.orchestrator import gdpr_orchestrator

        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"fake": ["account"], "silent": ["account"]},
            DATA_OWNERS_VERSION="outcome-1",
        )
        return gdpr_orchestrator.request_erasure("account", "u-1")

    def test_a_pending_owner_makes_the_request_pending(self, erasure):
        from stapel_gdpr.models import ErasureRequest

        assert erasure.outcome == ErasureRequest.OUTCOME_PENDING
        assert erasure.unanswered_owners == ["fake", "silent"]

    def test_a_timed_out_owner_makes_the_request_incomplete(self, erasure):
        from stapel_gdpr.models import ErasureRequest
        from stapel_gdpr.orchestrator import gdpr_orchestrator

        gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "fake")
        erasure.parts.filter(owner="silent").update(deadline=timezone.now())
        assert gdpr_orchestrator.sweep_deletion_deadlines() == 1

        erasure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_TIMEOUT
        assert erasure.outcome == ErasureRequest.OUTCOME_INCOMPLETE
        assert erasure.unanswered_owners == ["silent"]
        assert erasure.parts.get(owner="silent").unanswered is True
        assert erasure.parts.get(owner="fake").unanswered is False

    def test_every_owner_answering_is_complete(self, erasure):
        from stapel_gdpr.models import ErasureRequest
        from stapel_gdpr.orchestrator import gdpr_orchestrator

        gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "fake")
        gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "silent")

        erasure.refresh_from_db()
        assert erasure.state == ErasureRequest.STATE_DELETED
        assert erasure.outcome == ErasureRequest.OUTCOME_COMPLETE
        assert erasure.unanswered_owners == []

    def test_a_waived_completion_is_not_complete(self, settings, fake_provider):
        """ALLOW_ERASURE_WITHOUT_RECEIPTS reaches DELETED; it proves nothing."""
        from stapel_gdpr.models import ErasureRequest
        from stapel_gdpr.orchestrator import gdpr_orchestrator

        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS={"fake": ["account"], "silent": ["account"]},
            DATA_OWNERS_VERSION="outcome-2",
            ALLOW_ERASURE_WITHOUT_RECEIPTS=True,
        )
        request = gdpr_orchestrator.request_erasure("account", "u-2")
        gdpr_orchestrator.mark_section_erased(request.correlation_id, "fake")

        request.refresh_from_db()
        assert request.state == ErasureRequest.STATE_DELETED
        assert request.completeness_waived is True
        assert request.outcome == ErasureRequest.OUTCOME_INCOMPLETE
        assert request.unanswered_owners == ["silent"]

    def test_the_status_dto_carries_the_outcome_and_the_silence(self, erasure):
        from stapel_gdpr.models import ErasureRequest
        from stapel_gdpr.orchestrator import gdpr_orchestrator
        from stapel_gdpr.views import _erasure_dto

        gdpr_orchestrator.mark_section_erased(erasure.correlation_id, "fake")
        erasure.parts.filter(owner="silent").update(deadline=timezone.now())
        gdpr_orchestrator.sweep_deletion_deadlines()
        erasure.refresh_from_db()

        dto = _erasure_dto(erasure)

        assert dto.outcome == ErasureRequest.OUTCOME_INCOMPLETE
        assert dto.unanswered_owners == ["silent"]
        assert {p.owner: p.unanswered for p in dto.parts} == {
            "fake": False, "silent": True,
        }

    def test_the_admin_shows_the_outcome_and_who_never_answered(self, erasure):
        from django.contrib import admin as django_admin

        from stapel_gdpr.models import ErasureRequest

        erasure.parts.update(deadline=timezone.now())
        from stapel_gdpr.orchestrator import gdpr_orchestrator
        gdpr_orchestrator.sweep_deletion_deadlines()
        erasure.refresh_from_db()

        model_admin = django_admin.site._registry[ErasureRequest]
        assert "outcome_label" in model_admin.list_display
        assert model_admin.outcome_label(erasure) == "incomplete"
        assert model_admin.unanswered_label(erasure) == "fake, silent"
