"""STAPEL_GDPR settings namespace.

Configure in Django settings::

    STAPEL_GDPR = {
        # -- Data-owner registry (mandatory, versioned) --------------------
        # Every store that holds personal data. An erasure is only ever
        # marked DELETED when every owner listed here returned a receipt,
        # so an owner missing from this list is a store that silently keeps
        # the user's data forever. Entries are either a bare name (kind is
        # inferred: 'local' when an in-process GDPRProvider with that
        # section is registered, 'remote' otherwise) or a dict:
        #   {"name": "recordings", "kind": "remote", "timeout_hours": 6}
        "DATA_OWNERS": ["auth", "profiles", {"name": "cdn", "kind": "remote"}],
        # Bump whenever DATA_OWNERS changes. Stamped onto every closure so
        # an audit can tell which inventory a given erasure was judged by.
        "DATA_OWNERS_VERSION": "2026-08-13.1",
        # Grace given to an owner before its deletion part is marked timed
        # out (a timed-out part blocks DELETED just like a failed one).
        "OWNER_TIMEOUT_HOURS": 24,
        # Legacy: remote services that must confirm erasure. Folded into
        # DATA_OWNERS as kind='remote' entries; kept for compatibility.
        "REMOTE_DELETION_SERVICES": [],

        # -- Session revocation on closure --------------------------------
        # Dotted path to ``revoke(user) -> None``. Left empty, the seam
        # auto-detects stapel-auth's SessionService.revoke_all, then an
        # in-process subscriber of ``user.sessions_revoked``, then a broker
        # transport. With none of those, closure FAILS instead of leaving
        # live sessions behind.
        "SESSION_REVOKER": "",

        # -- Erasure of the primary user row -------------------------------
        # What happens to users.User itself when the closure executes:
        # "anonymize" (default — scrub every identity field in place, keep
        # the pk so foreign keys stay resolvable), "delete" (drop the row),
        # or a dotted path to erase(user) -> None. Whatever runs, the result
        # is verified; a closure whose identity survived never reaches
        # DELETED.
        "PRIMARY_IDENTITY_ERASURE": "anonymize",

        # -- Export download ----------------------------------------------
        # Single-use token TTL. The archive is deleted on consume or expiry.
        "DOWNLOAD_TTL_HOURS": 24,
        # Where the ready-notification points. The token rides in the URL
        # fragment by default: fragments are not sent to servers, so the
        # token stays out of access logs, Referer headers and proxies.
        "DOWNLOAD_URL_TEMPLATE": "{frontend_url}/privacy/export/#token={token}",

        # -- Storage roots --------------------------------------------------
        # Default: MEDIA_ROOT/gdpr/staging and MEDIA_ROOT/gdpr/exports.
        "STAGING_ROOT": "",
        "ARCHIVE_ROOT": "",
        # Prefix a peer service's `bucket_path` must start with before this
        # service will open it and copy the bytes into a user's download.
        # Templated over the export's own correlation id, so a peer can only
        # name a key belonging to the export it was asked about: an S3
        # backend has no traversal notion, so without this a compromised or
        # buggy peer names any key in the bucket and it lands in somebody's
        # archive. "" accepts any key (reported as gdpr.W007); traversal,
        # absolute and URL-shaped keys are refused either way.
        "EXPORT_BUCKET_PREFIX": "gdpr/{correlation_id}/",

        # -- Re-registration hashes ----------------------------------------
        # Key for the purpose-bound HMAC (see reregistration.py).
        # Defaults to SECRET_KEY.
        "REREG_SALT": "",

        # -- Escape hatches (named, loud, off by default) -------------------
        # Mark a closure DELETED without a receipt from every declared
        # owner. Turns a proven erasure back into a hopeful one.
        "ALLOW_ERASURE_WITHOUT_RECEIPTS": False,
        # Start a closure even when no session-revocation seam resolves.
        # The user's live access tokens keep working until they expire.
        "ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION": False,
    }
"""
from stapel_core.conf import AppSettings

gdpr_settings = AppSettings(
    "STAPEL_GDPR",
    defaults={
        "DATA_OWNERS": [],
        "DATA_OWNERS_VERSION": "",
        "OWNER_TIMEOUT_HOURS": 24,
        "REMOTE_DELETION_SERVICES": [],
        "SESSION_REVOKER": "",
        "PRIMARY_IDENTITY_ERASURE": "anonymize",
        "DOWNLOAD_TTL_HOURS": 24,
        "DOWNLOAD_URL_TEMPLATE": "{frontend_url}/privacy/export/#token={token}",
        "STAGING_ROOT": "",
        "ARCHIVE_ROOT": "",
        "EXPORT_BUCKET_PREFIX": "gdpr/{correlation_id}/",
        "REREG_SALT": "",
        "ALLOW_ERASURE_WITHOUT_RECEIPTS": False,
        "ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION": False,
    },
)

__all__ = ["gdpr_settings"]
