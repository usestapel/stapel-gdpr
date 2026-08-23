"""Server-side refusal of a closed account — permission class and middleware.

``is_active`` is not a safe answer to "may this account still act?". It is a
plain boolean on the user row that any component syncing a user from a JWT
claim can write back (stapel-core's ``jwt/utils.py`` does exactly that), so a
token minted before the closure can resurrect the account it was closing.
The closure row can not be written by a token, so this is what the gate
reads: an account in ``deleting``/``deleted`` is refused whatever
``is_active`` currently says.

Two surfaces, one predicate (:func:`stapel_gdpr.lifecycle.is_access_denied`):

* :class:`AccountNotClosed` — DRF permission, already applied to every
  user-facing view in this module;
* :class:`AccountClosureGuardMiddleware` — the fleet-wide one. Add it to
  ``MIDDLEWARE`` after authentication and every request of an erasing
  account is refused, not just the GDPR endpoints.

Grace is deliberately allowed through both: cancelling a closure requires
logging in.
"""
from __future__ import annotations

from rest_framework import permissions

from .lifecycle import is_access_denied

__all__ = [
    "AccountClosureGuardMiddleware",
    "AccountNotClosed",
    "erasure_authorized",
]


def erasure_authorized(request, subject_type: str, subject_key: str) -> bool:
    """Whether *request* may open an erasure for this subject.

    ``POST /erasures`` erases whatever the caller names, so the default is
    the only safe one this library can pick: staff only. A host plugs its own
    ownership predicate in as
    ``STAPEL_GDPR["ERASURE_AUTHORIZER"] = "myapp.gdpr.owns_subject"``, a
    callable ``(request, subject_type, subject_key) -> bool`` — only the host
    knows whether this user owns that recording.

    An authorizer that cannot be imported or that raises refuses the
    request: an ownership check that fails open is worse than none, because
    it looks like one.
    """
    import logging

    from django.utils.module_loading import import_string

    from .conf import gdpr_settings

    logger = logging.getLogger(__name__)
    dotted = str(gdpr_settings.ERASURE_AUTHORIZER or "")
    if not dotted:
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.is_staff)
    try:
        return bool(import_string(dotted)(request, subject_type, subject_key))
    except ImportError as e:
        logger.error('STAPEL_GDPR["ERASURE_AUTHORIZER"]=%r cannot be imported: %s', dotted, e)
        return False
    except Exception as e:
        logger.error('STAPEL_GDPR["ERASURE_AUTHORIZER"]=%r raised: %s', dotted, e)
        return False


class AccountNotClosed(permissions.BasePermission):
    """Refuse a user whose account is being (or has been) erased."""

    message = "This account is being erased and can no longer be used."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return True  # authentication is a different permission's job
        return not is_access_denied(user.pk)


class AccountClosureGuardMiddleware:
    """403 every authenticated request of an erasing/erased account.

    Costs one indexed query per authenticated request. Wire it after the
    authentication middleware::

        MIDDLEWARE = [
            ...,
            "stapel_gdpr.guards.AccountClosureGuardMiddleware",
        ]
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            if is_access_denied(user.pk):
                return self._denied()
        return self.get_response(request)

    @staticmethod
    def _denied():
        """The same error envelope the API returns, rendered by hand.

        Middleware sits outside DRF's content negotiation, so the DRF
        Response has to be rendered here or Django hands back an
        un-iterable body.
        """
        from rest_framework.renderers import JSONRenderer
        from stapel_core.django.api.errors import StapelErrorResponse

        from .errors import ERR_403_ACCOUNT_CLOSED

        response = StapelErrorResponse(403, ERR_403_ACCOUNT_CLOSED)
        response.accepted_renderer = JSONRenderer()
        response.accepted_media_type = "application/json"
        response.renderer_context = {}
        return response.render()
