"""GDPR-04: closing an account must not lock the subject out of its own grace period.

The stand found it on 0.4.2 and the path was unchanged through 0.5.4:
``POST user/account/close`` revokes every session inside the transaction that
records the closure — correct, and it stays — but the 202 it answers with says
``can_cancel: true`` and names a ``grace_ends_at`` 30 days out. The only
credential the caller held for ``GET .../close/status`` and
``POST .../cancel-close`` was the session that call had just destroyed, so both
answered 401 to the very token that closed the account. The promise in the body
was unreachable from the app that received it.

These tests hold the fix to the shape of the promise: the 202 carries a signed,
single-purpose ``closure_token``; it polls and cancels THIS closure and nothing
else; it dies with the grace period; and a plain revoked session is still
refused.
"""
from datetime import timedelta

import pytest
from django.core import signing
from django.utils import timezone

from stapel_gdpr.closure_token import (
    CLOSURE_TOKEN_HEADER,
    CLOSURE_TOKEN_SALT,
    ClosureTokenExpired,
    ClosureTokenInvalid,
    make_closure_token,
    read_closure_token,
)
from stapel_gdpr.models import AccountClosureRequest
from stapel_gdpr.orchestrator import gdpr_orchestrator

STATUS_URL = "/gdpr/api/v1/user/account/close/status"
CANCEL_URL = "/gdpr/api/v1/user/account/cancel-close"
CLOSE_URL = "/gdpr/api/v1/user/account/close"


def _header(token: str) -> dict:
    return {CLOSURE_TOKEN_HEADER: token}


@pytest.fixture
def anon():
    """A client with no credentials at all — a caller whose sessions are gone.

    Deliberately NOT the ``api_client`` fixture: ``authed_client`` force-
    authenticates that very instance, so a test reusing it would be quietly
    authenticated and would prove nothing about the token.
    """
    from rest_framework.test import APIClient

    return APIClient()


def _close(authed_client):
    """Close the account the way a client does, and return the 202's token."""
    resp = authed_client.post(CLOSE_URL)
    assert resp.status_code == 202, resp.content
    body = resp.json()
    assert body["can_cancel"] is True
    assert body["closure_token"], "the 202 promised a cancellable grace with no way to reach it"
    return body["closure_token"]


@pytest.mark.django_db
class TestTheClosedSubjectCanStillReachItsClosure:
    """The defect itself: a closed account polling and cancelling with no session."""

    def test_status_with_the_token_and_no_session_at_all(self, authed_client, anon, user):
        token = _close(authed_client)

        # anon is unauthenticated — the state the caller is really in
        # once its sessions are revoked.
        resp = anon.get(STATUS_URL, headers=_header(token))
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["status"] == "grace"
        assert body["can_cancel"] is True

    def test_cancel_with_the_token_reopens_the_account(self, authed_client, anon, user):
        token = _close(authed_client)

        resp = anon.post(CANCEL_URL, headers=_header(token))
        assert resp.status_code == 200, resp.content
        assert resp.json()["can_cancel"] is False

        closure = AccountClosureRequest.objects.get(user_id=user.pk)
        assert closure.status == AccountClosureRequest.STATUS_CANCELLED
        user.refresh_from_db()
        assert user.is_active is True

    def test_status_keeps_answering_while_the_erasure_runs(
        self, authed_client, anon, user, fake_provider,
    ):
        """A subject watching its own erasure is exactly who this is for.

        Every other view here refuses a ``deleting`` account. Refusing this one
        would restore the blindness the token exists to remove — so it answers,
        with ``can_cancel`` false, which is the honest state.
        """
        token = _close(authed_client)
        closure = AccountClosureRequest.objects.get(user_id=user.pk)
        closure.status = AccountClosureRequest.STATUS_DELETING
        closure.save(update_fields=["status"])

        resp = anon.get(STATUS_URL, headers=_header(token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleting"
        assert resp.json()["can_cancel"] is False

    def test_cancel_is_refused_once_the_grace_is_no_longer_cancellable(
        self, authed_client, anon, user,
    ):
        token = _close(authed_client)
        AccountClosureRequest.objects.filter(user_id=user.pk).update(
            status=AccountClosureRequest.STATUS_DELETING,
        )

        resp = anon.post(CANCEL_URL, headers=_header(token))
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == "error.404.gdpr.no_active_closure"


@pytest.mark.django_db
class TestTheTokenIsScopedToOneClosure:
    def test_a_superseded_token_is_403_not_silently_retargeted(
        self, authed_client, anon, user,
    ):
        """Close, cancel, close again: the first round's token acts on nothing.

        The dangerous version of this endpoint would resolve "the subject's
        current closure" from a token minted for a different one and cancel the
        second closure with the first one's credential.
        """
        first = _close(authed_client)
        assert anon.post(CANCEL_URL, headers=_header(first)).status_code == 200
        second = _close(authed_client)
        assert second != first

        resp = anon.get(STATUS_URL, headers=_header(first))
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == "error.403.gdpr.closure_token_scope"

        resp = anon.post(CANCEL_URL, headers=_header(first))
        assert resp.status_code == 403
        assert AccountClosureRequest.objects.get(pk=_closure_id(second)).status == (
            AccountClosureRequest.STATUS_GRACE
        )

    def test_a_token_naming_a_closure_that_is_not_the_subjects_is_403(self, user, anon):
        """Forgeable only with the SECRET_KEY — so it is minted here by hand."""
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        forged = signing.dumps(
            {
                "cid": closure.pk + 1000,
                "sub": str(closure.user_id),
                "exp": closure.grace_ends_at.isoformat(),
            },
            salt=CLOSURE_TOKEN_SALT,
        )
        resp = anon.get(STATUS_URL, headers=_header(forged))
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == "error.403.gdpr.closure_token_scope"


def _closure_id(token: str) -> int:
    return read_closure_token(token)["cid"]


@pytest.mark.django_db
class TestTheTokenDiesWithTheGracePeriod:
    def test_expired_after_grace_is_401(self, authed_client, anon, user):
        _close(authed_client)
        # Move the closure's grace into the past and re-mint: the token's own
        # `exp` is what expires, and it is the grace end it was signed for, so
        # this is the token the client holds thirty days later.
        closure = AccountClosureRequest.objects.get(user_id=user.pk)
        closure.grace_ends_at = timezone.now() - timedelta(seconds=1)
        closure.save(update_fields=["grace_ends_at"])
        stale = make_closure_token(closure)

        for url, call in ((STATUS_URL, anon.get), (CANCEL_URL, anon.post)):
            resp = call(url, headers=_header(stale))
            assert resp.status_code == 401, url
            assert resp.json()["localizable_error"] == "error.401.gdpr.closure_token_expired"

    def test_read_raises_expired_rather_than_invalid(self, user):
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        closure.grace_ends_at = timezone.now() - timedelta(days=1)
        with pytest.raises(ClosureTokenExpired):
            read_closure_token(make_closure_token(closure))


@pytest.mark.django_db
class TestNothingElseGetsIn:
    def test_a_plain_revoked_session_is_still_401(self, anon, user):
        """No token, no session — the state the defect report is written from."""
        gdpr_orchestrator.initiate_closure(user.pk)

        for url, call in ((STATUS_URL, anon.get), (CANCEL_URL, anon.post)):
            resp = call(url)
            assert resp.status_code == 401, url
            assert resp.json()["localizable_error"] == "error.401.unauthorized"

    def test_a_tampered_or_foreign_token_is_401(self, authed_client, anon, user):
        token = _close(authed_client)

        for bad in (
            token[:-1] + ("a" if token[-1] != "a" else "b"),   # broken signature
            signing.dumps({"cid": 1, "sub": "x", "exp": "2999-01-01T00:00:00+00:00"}),  # wrong salt
            "not-a-token",
        ):
            resp = anon.get(STATUS_URL, headers=_header(bad))
            assert resp.status_code == 401, bad
            assert resp.json()["localizable_error"] == "error.401.gdpr.closure_token_invalid"

    def test_the_token_opens_nothing_but_these_two_endpoints(
        self, authed_client, anon, user,
    ):
        """It is a capability for one closure, not a session for the account."""
        token = _close(authed_client)

        assert anon.post(
            "/gdpr/api/v1/user/data-export/request", headers=_header(token),
        ).status_code in (401, 403)
        assert anon.get(
            "/gdpr/api/v1/me/erasures", headers=_header(token),
        ).status_code in (401, 403)

    def test_a_still_valid_session_keeps_working_without_any_token(
        self, authed_client, user,
    ):
        """The authenticated path is untouched — a host whose auth backend
        admits deactivated users can still log back in and cancel."""
        _close(authed_client)

        resp = authed_client.get(STATUS_URL)
        assert resp.status_code == 200
        assert resp.json()["status"] == "grace"

        resp = authed_client.post(CANCEL_URL)
        assert resp.status_code == 200
        assert AccountClosureRequest.objects.get(user_id=user.pk).status == (
            AccountClosureRequest.STATUS_CANCELLED
        )


@pytest.mark.django_db
class TestTokenMechanics:
    def test_payload_carries_closure_subject_and_the_grace_end(self, user):
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        payload = read_closure_token(make_closure_token(closure))

        assert payload["cid"] == closure.pk
        assert payload["sub"] == str(closure.user_id)
        assert payload["exp"] == closure.grace_ends_at.isoformat()

    def test_it_is_signed_not_stored(self, user):
        """No second credential table to leak, purge or forget."""
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        token = make_closure_token(closure)

        closure.refresh_from_db()
        assert token not in str(closure.__dict__)

    def test_an_empty_header_is_invalid_not_a_crash(self):
        with pytest.raises(ClosureTokenInvalid):
            read_closure_token("")

    def test_a_signed_payload_of_the_wrong_shape_is_invalid(self):
        with pytest.raises(ClosureTokenInvalid):
            read_closure_token(signing.dumps(["not", "a", "dict"], salt=CLOSURE_TOKEN_SALT))
        with pytest.raises(ClosureTokenInvalid):
            read_closure_token(signing.dumps({"cid": 1}, salt=CLOSURE_TOKEN_SALT))
        with pytest.raises(ClosureTokenInvalid):
            read_closure_token(
                signing.dumps({"cid": 1, "sub": "x", "exp": "whenever"}, salt=CLOSURE_TOKEN_SALT),
            )
