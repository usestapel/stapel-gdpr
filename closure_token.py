"""The capability the 202 hands back, so a closed account can still be polled and cancelled.

Why this module exists. ``POST user/account/close`` revokes every session of
the subject *inside the same transaction that records the closure* — that is
deliberate and stays (a closure that leaves pre-closure access tokens alive is
the defect :mod:`stapel_gdpr.lifecycle` exists to refuse). But the 202 it
answers with promises ``can_cancel: true`` and a 30-day grace, and the only
credential the caller had for ``GET user/account/close/status`` and
``POST user/account/cancel-close`` was the session that call had just
destroyed. Both endpoints answered 401 to the very token that closed the
account, so the app that offered the grace period could neither show it nor
undo it. Logging back in is not the answer either: closure deactivates the
user, and Django's ``ModelBackend`` refuses an inactive account, so the
documented "cancel by logging in" needs a host auth backend that admits
inactive users.

The capability has to survive the revocation, so it cannot be a session. It is
a signed, single-purpose token:

* **Signed, not stored** — ``django.core.signing`` HMACs the payload with the
  project ``SECRET_KEY`` (``SECRET_KEY_FALLBACKS`` are honoured on read, so a
  key rotation does not strand a grace period). Nothing new is persisted, so
  there is no second credential table to leak or to forget to purge.
* **Single-purpose and closure-scoped** — the payload names one closure id and
  one subject. It authenticates nobody: it is accepted only by the closure
  status and cancel endpoints, and only for the closure it names. It is not a
  session, it grants no other endpoint, and presenting it for a closure that a
  newer one superseded is refused with 403.
* **Expiring with the thing it authorizes** — ``exp`` is the closure's own
  ``grace_ends_at``. The moment the cancellable window shuts the token is dead
  (401), which is also the moment the account stops being recoverable.

It travels in the ``X-Closure-Token`` request header and never in a query
string — the same rule the export download token was moved to in 0.4.0, for
the same reason: a URL lands in access logs, browser history, ``Referer`` and
every proxy in between.
"""
from __future__ import annotations

from datetime import datetime

from django.core import signing
from django.utils import timezone

#: Header the token is presented in. Never a query parameter.
CLOSURE_TOKEN_HEADER = "X-Closure-Token"

#: Namespace for the HMAC, so a signature minted here can never be replayed
#: against another ``django.core.signing`` consumer in the same project.
CLOSURE_TOKEN_SALT = "stapel_gdpr.closure_token"

__all__ = [
    "CLOSURE_TOKEN_HEADER",
    "CLOSURE_TOKEN_SALT",
    "ClosureTokenExpired",
    "ClosureTokenInvalid",
    "make_closure_token",
    "read_closure_token",
]


class ClosureTokenInvalid(Exception):
    """The token is missing, malformed, or not signed by this project."""


class ClosureTokenExpired(Exception):
    """The token is authentic but its grace period is over."""


def make_closure_token(closure) -> str:
    """Mint the single-purpose token for *closure*.

    Called once, when the closure is created; the 202 carries the result. A
    caller that loses it has no way to get another without a session — which
    is the point: the token is a capability, not a lookup.
    """
    return signing.dumps(
        {
            "cid": int(closure.pk),
            "sub": str(closure.user_id),
            "exp": closure.grace_ends_at.isoformat(),
        },
        salt=CLOSURE_TOKEN_SALT,
        compress=True,
    )


def read_closure_token(raw: str) -> dict:
    """Verify *raw* and return its ``{cid, sub, exp}`` payload.

    Raises :class:`ClosureTokenExpired` past the grace end it was signed for,
    and :class:`ClosureTokenInvalid` for anything else — an empty header, a
    tampered payload, a signature from another salt or another project's key.
    The two are separate because they mean different things to the caller: the
    first says the window closed, the second says the credential is not ours.

    The expiry is read from the *signed* payload rather than from the closure
    row: the row is what the token is about, the payload is what the token
    grants, and only the payload cannot be moved after the fact.
    """
    if not raw:
        raise ClosureTokenInvalid("no token presented")
    try:
        payload = signing.loads(raw, salt=CLOSURE_TOKEN_SALT)
    except signing.BadSignature as e:
        raise ClosureTokenInvalid(str(e)) from e

    if not isinstance(payload, dict) or not {"cid", "sub", "exp"} <= set(payload):
        raise ClosureTokenInvalid("payload is not a closure token")

    try:
        expires_at = datetime.fromisoformat(str(payload["exp"]))
    except ValueError as e:
        raise ClosureTokenInvalid(f"unparseable exp: {payload['exp']!r}") from e
    if timezone.is_naive(expires_at):
        expires_at = timezone.make_aware(expires_at, timezone.get_default_timezone())

    if timezone.now() > expires_at:
        raise ClosureTokenExpired(f"grace ended {expires_at.isoformat()}")

    return payload
