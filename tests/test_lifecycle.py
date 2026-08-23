"""GDPR-01: closure must actually close the account.

The defect had three moving parts and each one gets a test here:

* deactivation went through ``QuerySet.update``, so no observer ever saw the
  transition and nothing downstream (stapel-auth's activation events among
  them) was told;
* nothing revoked the user's sessions, so every access token minted before
  the closure kept working;
* "closed" was read off ``is_active``, a flag a component syncing a user from
  a JWT claim can write straight back — which turned a stale token into an
  account-reopening tool.
"""
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db.models.signals import post_save

from stapel_gdpr.errors import SessionRevocationUnavailable
from stapel_gdpr.lifecycle import (
    ACCESS_ACTIVE,
    ACCESS_CLOSING,
    ACCESS_DELETING,
    SESSIONS_REVOKED_ACTION,
    access_state,
    is_access_denied,
)
from stapel_gdpr.models import AccountClosureRequest
from stapel_gdpr.orchestrator import gdpr_orchestrator
from tests import support
from tests.support import gdpr_conf


class _FakeRequest:
    """Minimal request: the middleware only ever looks at ``.user``."""

    def __init__(self, user):
        self.user = user


@pytest.fixture
def revocations():
    support.revoked_users.clear()
    yield support.revoked_users
    support.revoked_users.clear()


@pytest.fixture
def captured_revocation_events():
    """Subscribe to user.sessions_revoked and unsubscribe afterwards.

    stapel-core's action registry is process-global: a subscriber left behind
    is itself a revocation seam, so a leaked one would quietly disarm the
    fail-closed tests below.
    """
    from stapel_core.comm import action_registry, subscribe_action

    captured = []
    subscribe_action(SESSIONS_REVOKED_ACTION, captured.append)
    yield captured
    action_registry._subscribers.get(SESSIONS_REVOKED_ACTION, []).clear()


@pytest.mark.django_db
class TestDeactivationIsObservable:
    def test_closure_deactivation_fires_model_observers(self, user, revocations):
        """The whole point: a QuerySet.update() emits no signal at all.

        stapel-auth's activation observer is a pre_save/post_save pair on the
        user model, so a closure that writes with raw SQL announces nothing —
        no user.deactivated, no membership suspension, no revocation.
        """
        seen = []

        def observer(sender, instance, **kwargs):
            seen.append((str(instance.pk), instance.is_active))

        post_save.connect(observer, sender=get_user_model(),
                          dispatch_uid="test-activation-observer")
        try:
            gdpr_orchestrator.initiate_closure(user.pk)
        finally:
            post_save.disconnect(sender=get_user_model(),
                                 dispatch_uid="test-activation-observer")

        assert (str(user.pk), False) in seen
        user.refresh_from_db()
        assert user.is_active is False

    def test_cancel_reactivates_through_the_same_seam(self, user, revocations):
        seen = []

        def observer(sender, instance, **kwargs):
            seen.append(instance.is_active)

        gdpr_orchestrator.initiate_closure(user.pk)
        post_save.connect(observer, sender=get_user_model(),
                          dispatch_uid="test-reactivation-observer")
        try:
            gdpr_orchestrator.cancel_closure(user.pk)
        finally:
            post_save.disconnect(sender=get_user_model(),
                                 dispatch_uid="test-reactivation-observer")

        assert seen == [True]


@pytest.mark.django_db
class TestSessionRevocation:
    def test_closure_revokes_sessions_and_announces_it(
        self, user, revocations, captured_revocation_events,
    ):
        captured = captured_revocation_events
        gdpr_orchestrator.initiate_closure(user.pk)

        assert revocations == [str(user.pk)]
        assert [e.payload["user_id"] for e in captured] == [str(user.pk)]

    def test_event_is_schema_valid(self, user, revocations, captured_revocation_events):
        import json
        from pathlib import Path

        import jsonschema

        import stapel_gdpr

        captured = captured_revocation_events
        gdpr_orchestrator.initiate_closure(user.pk)

        schema = json.loads(
            (Path(stapel_gdpr.__file__).parent / "schemas" / "emits"
             / "user.sessions_revoked.json").read_text()
        )
        jsonschema.validate(captured[0].payload, schema)

    def test_closure_fails_closed_without_a_revoker(self, settings, user):
        """No seam to revoke with -> no closure. The transaction rolls back."""
        settings.STAPEL_GDPR = {}  # nothing wired: no revoker, no owners

        with pytest.raises(SessionRevocationUnavailable):
            gdpr_orchestrator.initiate_closure(user.pk)

        assert not AccountClosureRequest.objects.filter(user_id=user.pk).exists()
        user.refresh_from_db()
        assert user.is_active is True

    def test_named_escape_hatch_allows_an_unrevoked_closure(self, settings, user, caplog):
        settings.STAPEL_GDPR = {"ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION": True}

        closure = gdpr_orchestrator.initiate_closure(user.pk)

        assert closure.status == AccountClosureRequest.STATUS_GRACE
        assert any("revokes no sessions" in r.message for r in caplog.records)

    def test_close_endpoint_returns_503_when_revocation_is_unavailable(
        self, settings, authed_client, user,
    ):
        settings.STAPEL_GDPR = {}
        resp = authed_client.post("/gdpr/api/v1/user/account/close")
        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == "error.503.gdpr.closure_unavailable"


@pytest.mark.django_db
class TestClosedAccountIsDeniedServerSide:
    def test_access_state_tracks_the_closure_row(self, user, fake_provider, revocations):
        assert access_state(user.pk) == ACCESS_ACTIVE
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        assert access_state(user.pk) == ACCESS_CLOSING
        assert is_access_denied(user.pk) is False  # grace is cancellable

        gdpr_orchestrator.execute_deletion(closure)
        closure.refresh_from_db()
        assert access_state(user.pk) in (ACCESS_DELETING, "deleted")
        assert is_access_denied(user.pk) is True

    def test_reactivated_flag_does_not_reopen_a_deleting_account(
        self, settings, authed_client, user, fake_provider, revocations,
    ):
        """The stale-token scenario, from this library's side.

        Something wrote is_active=True back onto the row (that is stapel-core's
        half of the finding). The account must still be refused, because the
        gate reads the closure row, which no token can write.
        """
        settings.STAPEL_GDPR = gdpr_conf(DATA_OWNERS=["fake", "profiles"])
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING

        get_user_model().objects.filter(pk=user.pk).update(is_active=True)

        resp = authed_client.post("/gdpr/api/v1/user/data-export/request")
        assert resp.status_code == 403
        assert "erased" in str(resp.json())

    def test_middleware_refuses_every_request_of_an_erasing_account(
        self, settings, user, fake_provider, revocations,
    ):
        from stapel_gdpr.guards import AccountClosureGuardMiddleware

        settings.STAPEL_GDPR = gdpr_conf(DATA_OWNERS=["fake", "profiles"])
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        called = []
        middleware = AccountClosureGuardMiddleware(lambda r: called.append(r) or "ok")

        response = middleware(_FakeRequest(user))
        assert response.status_code == 403
        assert called == []

    def test_middleware_lets_an_ordinary_user_through(self, user):
        from stapel_gdpr.guards import AccountClosureGuardMiddleware

        middleware = AccountClosureGuardMiddleware(lambda r: "downstream")
        assert middleware(_FakeRequest(user)) == "downstream"
        assert middleware(_FakeRequest(None)) == "downstream"


@pytest.mark.django_db
class TestPrimaryIdentityIsErased:
    """The other half of GDPR-01: the person on file after an 'erasure'.

    Providers deleted their own adjunct tables and every one of them left
    users.User — email, phone, username, password hash — exactly where it
    was, because that row belonged to none of them.
    """

    def test_execute_deletion_anonymizes_the_user_row(
        self, settings, user, fake_provider, revocations,
    ):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake"], DATA_OWNERS_VERSION="identity-1",
        )
        email, username = user.email, user.username

        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        user.refresh_from_db()
        assert user.email != email
        assert user.username != username
        assert user.email.endswith("@deleted.invalid")
        assert user.has_usable_password() is False
        assert user.is_active is False

        closure.refresh_from_db()
        assert closure.identity_erased_at is not None
        assert closure.status == AccountClosureRequest.STATUS_DELETED

    def test_delete_strategy_removes_the_row(
        self, settings, user, fake_provider, revocations,
    ):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake"], DATA_OWNERS_VERSION="identity-2",
            PRIMARY_IDENTITY_ERASURE="delete",
        )
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        assert not get_user_model().objects.filter(pk=user.pk).exists()

    def test_a_no_op_strategy_is_caught_and_blocks_deleted(
        self, settings, user, fake_provider, revocations, caplog,
    ):
        """The exact defect shape: an ``anonymize()`` that does nothing.

        A strategy is not trusted to have worked — the row is re-read, and a
        surviving email keeps the closure in DELETING instead of certifying
        an erasure that never happened.
        """
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake"], DATA_OWNERS_VERSION="identity-3",
            PRIMARY_IDENTITY_ERASURE="tests.support.no_op_erasure",
        )
        email = user.email

        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        user.refresh_from_db()
        assert user.email == email
        closure.refresh_from_db()
        assert closure.identity_erased_at is None
        assert closure.status == AccountClosureRequest.STATUS_DELETING
        assert any("identity erasure failed" in r.message for r in caplog.records)

    def test_deleted_is_refused_while_the_identity_survives(
        self, settings, user, fake_provider, revocations,
    ):
        """Receipts alone never certify an erasure the user row outlived."""
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake"], DATA_OWNERS_VERSION="identity-4",
        )
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        AccountClosureRequest.objects.filter(pk=closure.pk).update(
            status=AccountClosureRequest.STATUS_DELETING,
            identity_erased_at=None,
        )
        closure.refresh_from_db()
        gdpr_orchestrator._maybe_finalize(closure.erasure)

        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING

    def test_erasure_is_idempotent_when_the_user_is_already_gone(self, user):
        from stapel_gdpr.lifecycle import erase_identity

        get_user_model().objects.filter(pk=user.pk).delete()
        assert erase_identity(user.pk) == "already_erased"

    def test_bad_strategy_is_a_boot_error(self, settings, fake_provider):
        from stapel_gdpr.checks import check_data_owner_registry

        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake"], DATA_OWNERS_VERSION="identity-5",
            PRIMARY_IDENTITY_ERASURE="tests.support.does_not_exist",
        )
        assert any(m.id == "gdpr.E006" for m in check_data_owner_registry())


@pytest.mark.django_db
def test_set_active_is_idempotent(user):
    from stapel_gdpr.lifecycle import set_active

    assert set_active(user.pk, False) is True
    assert set_active(user.pk, False) is False  # already there: no event
    assert set_active(uuid.uuid4(), False) is False
