"""Account lifecycle seam — deactivation, revocation, and the closed-account gate.

Why this module exists. Closure used to deactivate through
``User.objects.filter(pk=...).update(is_active=False)``. ``QuerySet.update``
issues raw SQL: no ``pre_save``/``post_save``, therefore none of the
observers a host wires onto the user row ever fire — including stapel-auth's
activation observer, the single place that announces ``user.deactivated``.
The consequence was not cosmetic: nothing revoked the user's sessions, so
every access token minted before the closure kept working for its full
lifetime, and a service that syncs the user row from a JWT claim could write
``is_active=True`` straight back over the closure.

Three things close that, and they are separate on purpose:

1. :func:`set_active` writes through the model instance, so every registered
   observer sees the transition (stapel-auth's included) and the events it
   announces actually leave.
2. :func:`revoke_sessions` revokes live sessions and access JTIs through a
   resolvable seam, and **fails closed** when no seam resolves — a closure
   that cannot revoke is a closure that does not happen. Escape hatch:
   ``ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION``.
3. :func:`access_state` is the server-side answer to "is this account
   closed?", read from the closure row rather than from ``is_active``.
   ``is_active`` is a claim a stale token can flip back; the closure row is
   not, so the gate in :mod:`stapel_gdpr.guards` keeps refusing a deleting
   account even if something re-activates the user behind our back.
4. :func:`erase_identity` erases the primary user row itself. Providers
   deleted their own adjunct tables and every one of them left ``users.User``
   — email, phone, password hash — untouched, because the row belonged to
   none of them. It belongs to the closure, so the closure erases it, and
   verifies the result instead of trusting a provider's ``anonymize()``.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model

from .conf import gdpr_settings
from .errors import SessionRevocationUnavailable

logger = logging.getLogger(__name__)

#: Action announcing that a closure revoked every session of a user. Emitted
#: on every closure so that a remote auth service can revoke its own rows.
SESSIONS_REVOKED_ACTION = "user.sessions_revoked"

#: Account is usable.
ACCESS_ACTIVE = "active"
#: Closure requested, grace period running — reversible by the user.
ACCESS_CLOSING = "closing"
#: Erasure under way. Server-side access is refused from here on.
ACCESS_DELETING = "deleting"
#: Erasure finished.
ACCESS_DELETED = "deleted"

#: States that must be refused server-side, whatever a token claims.
DENIED_STATES = (ACCESS_DELETING, ACCESS_DELETED)

#: Scrub the identity in place, keeping the primary key so foreign keys across
#: the fleet stay resolvable and the closure row can still be found.
IDENTITY_ANONYMIZE = "anonymize"
#: Delete the row outright. Correct only where nothing else references it.
IDENTITY_DELETE = "delete"

__all__ = [
    "ACCESS_ACTIVE",
    "ACCESS_CLOSING",
    "ACCESS_DELETED",
    "ACCESS_DELETING",
    "DENIED_STATES",
    "IDENTITY_ANONYMIZE",
    "IDENTITY_DELETE",
    "SESSIONS_REVOKED_ACTION",
    "access_state",
    "erase_identity",
    "is_access_denied",
    "revoke_sessions",
    "set_active",
]


def set_active(user_id, active: bool, *, reason: str | None = None) -> bool:
    """Flip ``is_active`` through the model so observers fire. True on change.

    Never use ``QuerySet.update()`` for this: it bypasses the signal pair
    stapel-auth's activation observer is built on, and a deactivation nobody
    is told about propagates nowhere.
    """
    User = get_user_model()
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        logger.warning("GDPR lifecycle: user %s not found for is_active=%s", user_id, active)
        return False
    if bool(user.is_active) == bool(active):
        return False
    # stapel-auth's observer reads these off the instance; a plain field flip
    # carries no context and the schema marks both optional.
    if reason:
        setattr(user, "_stapel_auth_deactivation_reason", reason)
    try:
        user.is_active = bool(active)
        user.save(update_fields=["is_active"])
    finally:
        if hasattr(user, "_stapel_auth_deactivation_reason"):
            delattr(user, "_stapel_auth_deactivation_reason")
    return True


def _resolve_revoker():
    """Return ``(callable|None, strategy)`` for session revocation."""
    from django.utils.module_loading import import_string

    configured = str(gdpr_settings.SESSION_REVOKER or "")
    if configured:
        return import_string(configured), "configured"

    from django.apps import apps

    # Importability is not availability: the package can sit on sys.path in a
    # workspace checkout while its models have no app registry to live in.
    # Only an *installed* stapel-auth owns session rows to revoke.
    if apps.is_installed("stapel_auth"):
        try:
            from stapel_auth.sessions.services import SessionService

            return SessionService.revoke_all, "stapel_auth"
        except Exception as e:  # pragma: no cover - depends on the host's installs
            logger.error("stapel-auth is installed but its session service is unusable: %s", e)

    from stapel_core.comm import action_registry, comm_setting

    if action_registry.handlers(SESSIONS_REVOKED_ACTION):
        return None, "comm_inprocess"
    if comm_setting("ACTION_TRANSPORT", "inprocess") != "inprocess":
        # A broker transport: the emit below is durable, so revocation is
        # the consumer's job and its absence is not observable from here.
        return None, "comm_broker"
    return None, "none"


def revoke_sessions(user_id, *, emit=None) -> str:
    """Revoke every session/access JTI of *user_id*. Returns the strategy used.

    Raises :class:`SessionRevocationUnavailable` when nothing can revoke and
    the ``ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION`` hatch is closed — the
    caller is expected to let that abort the closure transaction rather than
    record a closure whose sessions stayed alive.

    *emit* is the emitter of the surrounding ``mutate_and_emit()`` block when
    there is one, so the announcement is part of the same outbox unit.
    """
    revoker, strategy = _resolve_revoker()

    if strategy == "none":
        if not gdpr_settings.ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION:
            raise SessionRevocationUnavailable(
                "no session-revocation seam resolved: set "
                'STAPEL_GDPR["SESSION_REVOKER"], subscribe to '
                f"{SESSIONS_REVOKED_ACTION}, or accept live tokens after "
                'closure with STAPEL_GDPR["ALLOW_CLOSURE_WITHOUT_SESSION_'
                'REVOCATION"] = True'
            )
        logger.warning(
            "GDPR closure revokes no sessions for user %s "
            "(ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION is on): every access "
            "token minted before the closure stays valid until it expires",
            user_id,
        )

    if revoker is not None:
        User = get_user_model()
        user = User.objects.filter(pk=user_id).first()
        if user is not None:
            revoker(user)

    payload = {"user_id": str(user_id), "reason": "gdpr_closure"}
    if emit is not None:
        emit(SESSIONS_REVOKED_ACTION, payload, key=str(user_id))
    else:
        from stapel_core.comm import emit as emit_action

        emit_action(SESSIONS_REVOKED_ACTION, payload, key=str(user_id))
    return strategy


#: Identity-bearing fields the anonymize strategy scrubs when the deployment's
#: user model has them. A host that stores personal data in fields of its own
#: erases those through a ``PRIMARY_IDENTITY_ERASURE`` callable or a provider —
#: this list is what the framework user model carries.
_IDENTITY_FIELDS = (
    "email", "phone", "first_name", "last_name", "bio", "avatar",
    "oauth_provider", "oauth_id", "last_login_ip", "username",
)

#: Fields whose surviving value proves the erasure did not happen. Checked
#: after every strategy, including a host-supplied one — the defect this
#: guards against is an ``anonymize()`` that quietly does nothing.
_IDENTIFYING_FIELDS = ("email", "phone", "username")


def _identity_snapshot(user) -> dict:
    return {
        name: getattr(user, name, None)
        for name in _IDENTIFYING_FIELDS
        if getattr(user, name, None)
    }


def _anonymize_identity(user) -> None:
    """Overwrite every identity-bearing field with a tombstone, in place.

    Irreversible on purpose: the values are overwritten rather than moved,
    so nothing is left to restore the person from. The row itself survives
    because references to it do — an account whose primary key vanishes
    takes unrelated rows with it (or breaks them), which is why deletion is
    the opt-in strategy and not the default.
    """
    import uuid

    tombstone = f"deleted-{uuid.uuid4().hex}"
    updates = {
        "username": tombstone,
        # RFC 2606 reserves .invalid: the address cannot be routed anywhere.
        "email": f"{tombstone}@deleted.invalid",
    }
    fields = {f.name for f in user._meta.get_fields() if getattr(f, "concrete", False)}
    changed = []
    for name in _IDENTITY_FIELDS:
        if name not in fields:
            continue
        if name in updates:
            value = updates[name]
        else:
            field = user._meta.get_field(name)
            value = None if field.null else ""
        setattr(user, name, value)
        changed.append(name)

    for name, value in (("is_active", False), ("is_staff", False), ("is_superuser", False)):
        if name in fields:
            setattr(user, name, value)
            changed.append(name)
    if "staff_roles" in fields:
        user.staff_roles = []
        changed.append("staff_roles")

    # No password left to try: not a hash of an unknown string, an
    # unusable marker that can never validate.
    user.set_unusable_password()
    changed.append("password")

    # A plain save(), never QuerySet.update(): the same observer discipline
    # as set_active() — consumers are told the row changed.
    user.save(update_fields=sorted(set(changed)))


def erase_identity(user_id) -> str:
    """Erase the primary user row. Returns the strategy that ran.

    The other half of the closure defect: providers deleted their adjunct
    tables (sessions, devices, audit rows) while the ``users.User`` row —
    email, phone, username, password hash — stayed exactly where it was, so
    an "erased" account was still a person on file. Nothing in the fleet
    owned that row, so nobody erased it; this module does now.

    Strategies (``STAPEL_GDPR["PRIMARY_IDENTITY_ERASURE"]``):

    * ``anonymize`` (default) — scrub every identity field in place, keep
      the primary key so foreign keys elsewhere stay resolvable;
    * ``delete`` — drop the row, for deployments where nothing references it;
    * a dotted path to ``erase(user) -> None`` for a host with its own
      identity model.

    Whatever ran, the result is verified: if an identifying field survives,
    this raises. A no-op ``anonymize()`` reporting success is precisely the
    defect being closed, so success here is measured, not trusted.
    """
    User = get_user_model()
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        # Already gone (a previous run, or a host that deletes users itself):
        # the post-condition holds, so this counts as erased.
        return "already_erased"

    strategy = str(gdpr_settings.PRIMARY_IDENTITY_ERASURE or IDENTITY_ANONYMIZE)
    before = _identity_snapshot(user)

    if strategy == IDENTITY_ANONYMIZE:
        _anonymize_identity(user)
    elif strategy == IDENTITY_DELETE:
        user.delete()
    else:
        from django.utils.module_loading import import_string

        import_string(strategy)(user)

    after = User.objects.filter(pk=user_id).first()
    if after is not None:
        survived = [
            name for name, value in before.items()
            if getattr(after, name, None) == value
        ]
        if survived:
            raise RuntimeError(
                f"primary identity of user {user_id} still carries "
                f"{', '.join(survived)} after PRIMARY_IDENTITY_ERASURE="
                f"{strategy!r}: the account was not erased"
            )
    logger.info("GDPR primary identity erased [user=%s strategy=%s]", user_id, strategy)
    return strategy


def access_state(user_id) -> str:
    """Lifecycle state of *user_id* as the server sees it (never a token)."""
    from .models import AccountClosureRequest

    statuses = set(
        AccountClosureRequest.objects.filter(user_id=user_id)
        .exclude(status=AccountClosureRequest.STATUS_CANCELLED)
        .values_list("status", flat=True)
    )
    if AccountClosureRequest.STATUS_DELETED in statuses:
        return ACCESS_DELETED
    if AccountClosureRequest.STATUS_DELETING in statuses:
        return ACCESS_DELETING
    if AccountClosureRequest.STATUS_GRACE in statuses:
        return ACCESS_CLOSING
    return ACCESS_ACTIVE


def is_access_denied(user_id) -> bool:
    """True when the account must be refused regardless of ``is_active``.

    Grace is deliberately *not* denied: the whole point of the grace period
    is that the user can log in and cancel.
    """
    return access_state(user_id) in DENIED_STATES
