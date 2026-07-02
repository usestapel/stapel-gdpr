"""STAPEL_GDPR settings namespace.

Configure in Django settings:

    STAPEL_GDPR = {
        # Remote services (comm consumers) that must confirm erasure with a
        # gdpr.section.erased action before a closure is marked DELETED.
        "REMOTE_DELETION_SERVICES": ["profiles", "cdn"],
        # Salt for re-registration hashes. Defaults to SECRET_KEY.
        "REREG_SALT": "",
        # Filesystem roots for export staging / final archives.
        # Default: MEDIA_ROOT/gdpr/staging and MEDIA_ROOT/gdpr/exports.
        "STAGING_ROOT": "",
        "ARCHIVE_ROOT": "",
    }
"""
from stapel_core.conf import AppSettings

gdpr_settings = AppSettings(
    "STAPEL_GDPR",
    defaults={
        "REMOTE_DELETION_SERVICES": [],
        "REREG_SALT": "",
        "STAGING_ROOT": "",
        "ARCHIVE_ROOT": "",
    },
)

__all__ = ["gdpr_settings"]
