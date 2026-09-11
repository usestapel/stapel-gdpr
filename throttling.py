"""A budget on the doors a stranger can knock on.

Why this module exists (security audit 2026-09-11, L-6)
-------------------------------------------------------
``POST /gdpr/api/v1/dsar`` is ``AllowAny`` and has to be: a public privacy
form is the channel a regulator expects to find, and it cannot require a
login. Its only stated protection was ``@captcha_protected``, which is a
**no-op when no captcha backend is configured** — the state of most
deployments, and of every one the audit looked at. So the intake was an
unauthenticated mail trigger: anybody could make the instance send an
acknowledgement to any address and grow ``DsarRequest``, at request speed,
for as long as they liked. The edge can cap it (nginx now does on one
deployment), but a ceiling that only exists in one operator's config is not
a property of the library.

The two authenticated self-service doors beside it — closing an account and
asking for a data export — are cheaper to hold open because they need a
session, and a session costs one unauthenticated POST where guests are on.
Each one still starts a job and sends mail, so they share the mechanism.

Shape
-----
A rolling hourly budget in the shared cache, the same mechanic stapel-auth
uses for the guest-mint faucet: a counter per (scope, caller) with an hour's
TTL, spent BEFORE the work happens, answering ``error.429.rate_limit`` with a
retry-after when it is empty. Not DRF's ``ScopedRateThrottle``, for one
reason that decides it: that class keys on ``X-Forwarded-For`` in full unless
``NUM_PROXIES`` is set, i.e. on a header the client writes first, i.e. on
something the caller can rotate. This keys on
:func:`stapel_core.netintel.client_ip`, which is the deployment's own answer
to "who is calling" and is the value its other IP-keyed controls already use.

An authenticated caller is keyed on the account, not the address: a shared
office NAT is one address and many people, and the thing being limited there
is what one account may ask of the machine.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: One hour, the window every budget here rolls over.
WINDOW_SECONDS = 60 * 60

#: Budgeted doors, by the name their counter is kept under.
SCOPE_DSAR = "dsar"
SCOPE_ACCOUNT_CLOSE = "account_close"
SCOPE_DATA_EXPORT = "data_export"


def _caller_key(request) -> str:
    """Who is being budgeted: the account when there is one, else the address."""
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return f"user:{user.pk}"
    try:
        from stapel_core.netintel import client_ip

        address = client_ip(request)
    except Exception:  # pragma: no cover - netintel unavailable
        address = request.META.get("REMOTE_ADDR")
    return f"ip:{address or 'unknown'}"


def rate_limit() -> int:
    """``INTAKE_RATE_LIMIT_PER_HOUR``; ``0`` disables every budget here."""
    from .conf import gdpr_settings

    try:
        return int(gdpr_settings.INTAKE_RATE_LIMIT_PER_HOUR or 0)
    except (TypeError, ValueError):
        return 0


def spend(request, scope: str) -> int:
    """Spend one slot of *scope*'s budget for this caller.

    Returns ``0`` while budget remains — the slot is consumed — otherwise the
    number of seconds until the window rolls over (at least 1, so a caller is
    never told to retry immediately).

    Fails OPEN on a cache failure. A privacy intake that a broken cache turns
    into a refusal is a statutory channel closed by an infrastructure fault,
    which is worse than the abuse this caps; the counter is a budget, not a
    gate on correctness.
    """
    limit = rate_limit()
    if limit <= 0:
        return 0

    from django.core.cache import cache

    key = f"gdpr_intake_rate:{scope}:{_caller_key(request)}"
    try:
        count = cache.get(key) or 0
        if count >= limit:
            # cache.ttl is a django-redis extension; locmem has none to read.
            ttl = cache.ttl(key) if hasattr(cache, "ttl") else 0
            return max(int(ttl or 0), 1)
        cache.set(key, count + 1, WINDOW_SECONDS)
    except Exception:
        logger.warning(
            "stapel-gdpr: intake budget unavailable for scope %r — admitting "
            "the request rather than closing a statutory channel", scope,
        )
        return 0
    return 0


def refusal(retry_after: int):
    """The fleet's 429 envelope, with the retry-after this budget computed."""
    from stapel_core.django.api.errors import error_429_rate_limit

    return error_429_rate_limit(retry_after)


__all__ = [
    "SCOPE_ACCOUNT_CLOSE",
    "SCOPE_DATA_EXPORT",
    "SCOPE_DSAR",
    "WINDOW_SECONDS",
    "rate_limit",
    "refusal",
    "spend",
]
