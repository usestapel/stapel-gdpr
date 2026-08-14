"""Re-registration detection (GDPR-compatible).

When an account is erased we may not keep the email/phone in clear text, but
the platform is allowed to keep an irreversible keyed digest for a limited
period (24 months) to detect banned/deleted users re-registering.

**The hash format is defined here and nowhere else.** The store
(:class:`stapel_gdpr.models.ReRegistrationHash`) lives in this library, so
its format is this library's contract, and there is exactly one:

    HMAC-SHA256(key, "stapel-gdpr:rereg:v1:<type>:<normalized value>")

Keyed, so an attacker holding a dump cannot walk a dictionary of every email
address in the world back to a match; purpose-bound, so the same key used for
another purpose cannot produce a colliding digest; versioned in the string
itself, so rotating the scheme is a new version rather than a silent format
drift. A bare ``sha256(email.lower())`` — which is what a second writer was
found storing into this same table — has none of those properties: for the
identifiers this table exists to remember, it is reversible in practice.

Any writer that needs a row here calls :func:`store_hashes`. Rows arriving by
any other route are recorded with ``scheme='unverified'``, never matched by
:func:`is_reregistration`, and reported by the ``gdpr.E004`` system check.

Auth flows call :func:`is_reregistration` at signup time.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

#: How long re-registration hashes are retained.
RETENTION = timedelta(days=730)  # 24 months

#: Purpose string mixed into every digest. Changing it invalidates every
#: stored hash on purpose — that is what a scheme version is for.
PURPOSE = 'stapel-gdpr:rereg:v1'


def _key() -> bytes:
    from django.conf import settings

    from .conf import gdpr_settings

    return str(gdpr_settings.REREG_SALT or settings.SECRET_KEY).encode()


def _normalize(hash_type: str, value: str) -> str:
    value = (value or '').strip()
    if hash_type == 'email':
        return value.lower()
    # phone: keep digits and a leading '+' (users.User already stores E.164)
    plus = '+' if value.startswith('+') else ''
    return plus + ''.join(c for c in value if c.isdigit())


def compute_hash(hash_type: str, value: str) -> str:
    """Purpose-bound keyed HMAC-SHA256 of a normalized email/phone."""
    normalized = _normalize(hash_type, value)
    message = f'{PURPOSE}:{hash_type}:{normalized}'.encode()
    return hmac.new(_key(), message, hashlib.sha256).hexdigest()


def _legacy_digest(hash_type: str, value: str) -> str:
    """The pre-HMAC digest, for reading rows written before migration 0003.

    Read-only and read-once: nothing writes this format any more, and the
    rows carrying it age out with the 24-month retention. Kept so that
    switching schemes does not silently amnesty every account erased before
    the switch.
    """
    normalized = _normalize(hash_type, value)
    return hashlib.sha256(f'{_key().decode()}:{hash_type}:{normalized}'.encode()).hexdigest()


def store_hashes(user_id, email: str | None = None, phone: str | None = None) -> int:
    """Persist re-registration hashes for a user about to be erased.

    Returns the number of hashes written. Idempotent — re-running for the
    same identifier does not duplicate rows.
    """
    from .models import ReRegistrationHash

    written = 0
    expires_at = timezone.now() + RETENTION
    for hash_type, value in (('email', email), ('phone', phone)):
        if not value:
            continue
        _, created = ReRegistrationHash.objects.get_or_create(
            hash_type=hash_type,
            hash_value=compute_hash(hash_type, value),
            defaults={
                'scheme': ReRegistrationHash.SCHEME_HMAC_V1,
                'user_id_was': str(user_id),
                'expires_at': expires_at,
            },
        )
        written += int(created)
    return written


def is_reregistration(email: str | None = None, phone: str | None = None) -> bool:
    """True if the given email or phone belonged to a previously deleted
    account (unexpired hash on record). Intended for auth signup flows."""
    from .models import ReRegistrationHash

    from django.db.models import Q

    now = timezone.now()
    for hash_type, value in (('email', email), ('phone', phone)):
        if not value:
            continue
        match = (
            Q(hash_value=compute_hash(hash_type, value),
              scheme=ReRegistrationHash.SCHEME_HMAC_V1)
            | Q(hash_value=_legacy_digest(hash_type, value),
                scheme=ReRegistrationHash.SCHEME_LEGACY)
        )
        if ReRegistrationHash.objects.filter(
            match, hash_type=hash_type, expires_at__gt=now,
        ).exists():
            return True
    return False


def purge_unverified_hashes(include_legacy: bool = False) -> int:
    """Delete rows not written through :func:`store_hashes`. Returns the count.

    An unverified row is a digest of the same PII in a format this library
    never produced (a bare SHA-256 of an email is dictionary-recoverable),
    it never matches a lookup, and keeping it is retention with no purpose
    left. ``include_legacy`` additionally drops the pre-0003 rows, which is
    the right call for a deployment that knows another writer was hashing
    into this table — their format cannot be told apart after the fact.
    """
    from .models import ReRegistrationHash

    keep = [ReRegistrationHash.SCHEME_HMAC_V1]
    if not include_legacy:
        keep.append(ReRegistrationHash.SCHEME_LEGACY)
    qs = ReRegistrationHash.objects.exclude(scheme__in=keep)
    count = qs.count()
    qs.delete()
    if count:
        logger.warning(
            'Purged %s re-registration hashes not written through store_hashes '
            '(include_legacy=%s)', count, include_legacy,
        )
    return count


__all__ = [
    'compute_hash',
    'store_hashes',
    'is_reregistration',
    'purge_unverified_hashes',
    'RETENTION',
    'PURPOSE',
]
