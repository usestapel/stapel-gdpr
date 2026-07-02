"""Re-registration detection (GDPR-compatible).

When an account is erased we may not keep the email/phone in clear text,
but the platform is allowed to keep an irreversible salted hash for a
limited period (24 months) to detect banned/deleted users re-registering.

Auth flows call :func:`is_reregistration` at signup time.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

#: How long re-registration hashes are retained.
RETENTION = timedelta(days=730)  # 24 months


def _salt() -> str:
    from django.conf import settings

    from .conf import gdpr_settings

    return str(gdpr_settings.REREG_SALT or settings.SECRET_KEY)


def _normalize(hash_type: str, value: str) -> str:
    value = (value or '').strip()
    if hash_type == 'email':
        return value.lower()
    # phone: keep digits and a leading '+' (users.User already stores E.164)
    plus = '+' if value.startswith('+') else ''
    return plus + ''.join(c for c in value if c.isdigit())


def compute_hash(hash_type: str, value: str) -> str:
    """Salted SHA-256 of a normalized email/phone."""
    normalized = _normalize(hash_type, value)
    return hashlib.sha256(f'{_salt()}:{hash_type}:{normalized}'.encode()).hexdigest()


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

    now = timezone.now()
    for hash_type, value in (('email', email), ('phone', phone)):
        if not value:
            continue
        if ReRegistrationHash.objects.filter(
            hash_type=hash_type,
            hash_value=compute_hash(hash_type, value),
            expires_at__gt=now,
        ).exists():
            return True
    return False


__all__ = ['compute_hash', 'store_hashes', 'is_reregistration', 'RETENTION']
