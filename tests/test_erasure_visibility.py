"""Who may read an erasure, with guest sessions switched on.

A guest is not ``AnonymousUser``: it is a user row with ``is_anonymous=True``
and it passes ``IsAuthenticated``. On a deployment with ``AUTH_ANONYMOUS`` on
— which is the default — a view whose whole gate is a bare ``IsAuthenticated``
therefore admits half the sessions on the stand. These tests exercise the two
erasure read endpoints with a real guest session, and pin the check that will
notice the next view that forgets to say what it meant.
"""
import pytest

from stapel_gdpr.models import AccountClosureRequest, ErasureRequest
from stapel_gdpr.orchestrator import gdpr_orchestrator
from tests.support import gdpr_conf

STATUS_URL = "/gdpr/api/v1/erasures/{pk}"
MINE_URL = "/gdpr/api/v1/me/erasures"

OWNERS = {"recordings": ["account", "recording"]}


def refuse_everyone(request, subject_type, subject_key):
    """A host that authorizes nobody: only ownership can open a row."""
    return False


@pytest.fixture(autouse=True)
def _owners(settings):
    settings.STAPEL_GDPR = gdpr_conf(
        DATA_OWNERS=OWNERS, DATA_OWNERS_VERSION="vis-1",
        ERASURE_AUTHORIZER="tests.test_erasure_visibility.refuse_everyone",
    )


def _module_views(patterns):
    """Every view class this module routes to, each one once."""
    seen = {}
    for pattern in patterns:
        view_cls = getattr(getattr(pattern, "callback", None), "cls", None)
        if view_cls is None:
            continue
        if not view_cls.__module__.startswith("stapel_gdpr"):
            continue  # the generated swagger views are not ours to declare for
        seen.setdefault(view_cls.__name__, view_cls)
    return [seen[name] for name in sorted(seen)]


def _closing(user_id, status):
    from django.utils import timezone

    return AccountClosureRequest.objects.create(
        user_id=user_id,
        trigger=AccountClosureRequest.TRIGGER_MANUAL,
        status=status,
        grace_ends_at=timezone.now(),
    )


@pytest.mark.django_db
class TestAGuestSessionReachingTheErasureViews:
    def test_a_guest_reads_its_own_erasure(self, guest_client, guest):
        """The capability that must survive any gate we choose.

        A guest session can close its own account, and the erasure that
        closure opens carries the guest's pk — so the guest has to be able to
        watch it. Shutting guests out of this view would break that.
        """
        erasure = gdpr_orchestrator.request_erasure(
            "account", str(guest.pk), requested_by=guest.pk,
        )
        resp = guest_client.get(STATUS_URL.format(pk=erasure.pk))
        assert resp.status_code == 200
        assert resp.json()["subject_key"] == str(guest.pk)

    def test_a_guest_cannot_walk_the_id_space(self, guest_client, user):
        """The exposure that must not survive it."""
        theirs = gdpr_orchestrator.request_erasure(
            "recording", "rec-1", requested_by=user.pk,
        )
        resp = guest_client.get(STATUS_URL.format(pk=theirs.pk))
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == "error.404.gdpr.erasure_not_found"

    def test_one_account_cannot_read_anothers_erasure(self, api_client, user, db):
        """The same walk, by a full account rather than a guest.

        ``<int:request_id>`` is a dense id space, so "is logged in" cannot be
        the gate for either kind of session.
        """
        from django.contrib.auth import get_user_model

        theirs = gdpr_orchestrator.request_erasure(
            "recording", "rec-9", requested_by=user.pk,
        )
        neighbour = get_user_model().objects.create_user(
            username="neighbour", email="neighbour@example.com", password="pw-1234",
        )
        api_client.force_authenticate(user=neighbour)
        resp = api_client.get(STATUS_URL.format(pk=theirs.pk))
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == "error.404.gdpr.erasure_not_found"

    def test_a_platform_started_erasure_is_nobodys_own(self, guest_client):
        """``requested_by`` is null for those, and null belongs to no caller."""
        theirs = gdpr_orchestrator.request_erasure("recording", "rec-2")
        assert theirs.requested_by is None
        assert guest_client.get(STATUS_URL.format(pk=theirs.pk)).status_code == 404

    def test_a_guests_list_holds_only_its_own(self, guest_client, guest, user):
        gdpr_orchestrator.request_erasure("recording", "mine", requested_by=guest.pk)
        gdpr_orchestrator.request_erasure("recording", "theirs", requested_by=user.pk)
        gdpr_orchestrator.request_erasure("recording", "platform")

        resp = guest_client.get(MINE_URL)
        assert resp.status_code == 200
        assert [row["subject_key"] for row in resp.json()] == ["mine"]


@pytest.mark.django_db
class TestAnErasingAccountReadingTheErasureViews:
    """``AccountNotClosed`` guards every user-facing view in this module.

    These two were the exceptions: an account already past its grace and in
    DELETING could still list and read erasures, while its export and closure
    siblings refused it. The middleware hid that on any deployment that
    installed it; the module now holds the invariant on its own.
    """

    def test_a_deleting_account_cannot_read_one(self, authed_client, user):
        erasure = gdpr_orchestrator.request_erasure(
            "recording", "rec-1", requested_by=user.pk,
        )
        _closing(user.pk, AccountClosureRequest.STATUS_DELETING)
        assert authed_client.get(STATUS_URL.format(pk=erasure.pk)).status_code == 403

    def test_a_deleting_account_cannot_list_them(self, authed_client, user):
        gdpr_orchestrator.request_erasure("recording", "rec-1", requested_by=user.pk)
        _closing(user.pk, AccountClosureRequest.STATUS_DELETING)
        assert authed_client.get(MINE_URL).status_code == 403

    def test_grace_still_reads(self, authed_client, user):
        """Grace is deliberately let through: cancelling requires logging in."""
        erasure = gdpr_orchestrator.request_erasure(
            "recording", "rec-1", requested_by=user.pk,
        )
        _closing(user.pk, AccountClosureRequest.STATUS_GRACE)
        assert authed_client.get(STATUS_URL.format(pk=erasure.pk)).status_code == 200


@pytest.mark.django_db
class TestTheModuleHasNoUndeclaredView:
    """The sweep, as a gate rather than as a one-off grep.

    ``stapel_core.adoption`` E001/W002 report a view whose whole gate is a
    bare ``IsAuthenticated`` while guest sessions exist. Running it here, with
    the axis forced on, means the next stapel-gdpr view that forgets to take a
    position fails this suite instead of surfacing as a warning in somebody's
    production ``manage.py check``.
    """

    def test_every_gdpr_view_has_taken_a_position_on_guests(self, monkeypatch):
        from stapel_core.django import adoption_checks

        monkeypatch.setattr(
            adoption_checks, "anonymous_axis_enabled", lambda: True,
        )
        findings = [
            f for f in adoption_checks.check_anonymous_stance_declared()
            if "stapel_gdpr" in f.msg
        ]
        assert findings == [], "\n".join(f.msg for f in findings)

    def test_every_view_a_guest_passes_says_so(self, rf, guest):
        """The same question asked of the gate stack instead of its spelling.

        The check above reads a view green as soon as *any* second permission
        class stands beside ``IsAuthenticated``, on the argument that a second
        class is a more specific statement. ``AccountNotClosed`` is not: it
        asks whether an account is being erased, an orthogonal question that a
        guest passes. So every user-facing view in this module is invisible to
        that check and still admits guests.

        This one runs the gate: a view that refuses an unauthenticated caller
        and admits a guest session must carry a declaration. A view that
        admits both is simply public, and the guest axis says nothing about
        it.
        """
        from django.contrib.auth.models import AnonymousUser
        from stapel_core.django.api.permissions import (
            ANONYMOUS_DECLARATION_ATTR,
            ANONYMOUS_DECLARATIONS,
        )

        from stapel_gdpr import urls_v1

        def admits(view_cls, principal):
            request = rf.get("/")
            request.user = principal
            view = view_cls()
            return all(
                gate().has_permission(request, view)
                for gate in view_cls.permission_classes
            )

        undeclared = []
        for view_cls in _module_views(urls_v1.urlpatterns):
            if admits(view_cls, AnonymousUser()) or not admits(view_cls, guest):
                continue
            declaration = getattr(view_cls, ANONYMOUS_DECLARATION_ATTR, None)
            if declaration not in ANONYMOUS_DECLARATIONS:
                undeclared.append(view_cls.__name__)
        assert undeclared == [], (
            "a guest session passes the gate of these views and nothing in "
            f"their source says whether that was meant: {undeclared}"
        )


@pytest.mark.django_db
class TestThePredicateItself:
    """``erasure_visible`` is the one place the answer lives — tested there."""

    def test_a_string_pk_and_a_uuid_row_still_match(self, rf, user):
        erasure = gdpr_orchestrator.request_erasure(
            "recording", "rec-1", requested_by=user.pk,
        )
        from stapel_gdpr.guards import erasure_visible

        request = rf.get("/")
        request.user = user
        assert erasure_visible(request, erasure) is True

    def test_an_unauthenticated_request_sees_nothing(self, rf):
        from django.contrib.auth.models import AnonymousUser
        from django.utils import timezone

        from stapel_gdpr.guards import erasure_visible

        erasure = ErasureRequest.objects.create(
            subject_type="recording", subject_key="rec-1", due_at=timezone.now(),
        )
        request = rf.get("/")
        request.user = AnonymousUser()
        assert erasure_visible(request, erasure) is False
