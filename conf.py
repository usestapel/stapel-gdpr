"""STAPEL_GDPR settings namespace.

Configure in Django settings::

    STAPEL_GDPR = {
        # -- Data-owner registry (mandatory, versioned) --------------------
        # Every store that holds personal data, and WHICH subjects it holds
        # data about. An erasure is only ever marked DELETED when every
        # owner claiming that subject type returned a receipt, so an owner
        # missing from this map is a store that silently keeps the data
        # forever. Preferred form — a map owner -> subject types:
        #   "DATA_OWNERS": {
        #       "recordings": ["account", "workspace", "meeting", "recording"],
        #       "billing":    ["account"],
        #       "media":      {"subject_types": ["account", "file"],
        #                      "kind": "remote", "timeout_hours": 6},
        #   }
        # A plain list is still accepted and means ["account"] for every
        # entry — nothing breaks on the bump. Entries there are either a
        # bare name (kind is inferred: 'local' when an in-process
        # GDPRProvider with that section is registered, 'remote' otherwise)
        # or a dict: {"name": "recordings", "kind": "remote",
        # "timeout_hours": 6, "subject_types": [...]}.
        #
        # The names are the LIBRARIES' own, not app labels — the `cdn` app
        # owns "media", the `profiles` app owns "profile". A name nothing
        # declares is inferred remote and times out in silence, so
        # gdpr.E009 refuses it at boot; an installed owner absent from this
        # map is a store no erasure ever waits for (gdpr.E010).
        "DATA_OWNERS": {"auth": ["account"], "profile": ["account"],
                        "media": {"subject_types": ["account", "file"],
                                  "kind": "remote"}},
        # Subjects an erasure can be requested for. The account is the
        # historical one; entities were added in 0.5.0 so a host can put a
        # deleted recording/document/file on the same receipts path.
        "SUBJECT_TYPES": ["account", "workspace", "meeting", "recording", "document", "file"],
        # Purge SLA: due_at = requested_at + this. The account keeps its own
        # cancellable 30-day grace on top (AccountClosureRequest); entities
        # have no grace — the UI removal already happened.
        "ERASURE_SLA_DAYS": 30,
        # Bump whenever DATA_OWNERS changes. Stamped onto every closure so
        # an audit can tell which inventory a given erasure was judged by.
        "DATA_OWNERS_VERSION": "2026-08-13.1",
        # Owner names an INSTALLED library declares that this deployment
        # deliberately does not ask to erase. Normally there are none: a
        # library that declares an erasure owner and is not in DATA_OWNERS is
        # a store that keeps the data forever, which is gdpr.E010. Naming one
        # here downgrades that to gdpr.W011 — still reported at every boot,
        # because "we chose this" has to stay visible in the same place the
        # accident would have been. The name has to be the one the library
        # declares; there is no wildcard.
        "DATA_OWNERS_OPT_OUT": [],
        # Grace given to an owner before its erasure part is marked timed
        # out (a timed-out part blocks DELETED just like a failed one).
        "OWNER_TIMEOUT_HOURS": 24,
        # How long an owner may stay silent after `probe_data_owners` before
        # gdpr.W006 reports it at boot. Silence is a finding, not a log line:
        # an owner that never answers is an owner whose erasures will time
        # out, and this says so before the first request does.
        "OWNER_ALIVE_MAX_AGE_HOURS": 48,
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

        # -- Entity erasure authorization ----------------------------------
        # Dotted path to ``authorize(request, subject_type, subject_key) ->
        # bool``, consulted by ``POST /erasures``. Empty means staff only:
        # the endpoint erases whatever the caller names, so the host's own
        # ownership predicate belongs here before any product wires it.
        "ERASURE_AUTHORIZER": "",

        # -- Subprocessor ledger -------------------------------------------
        # Processors that received a copy of the data and the contractual
        # window they have to delete it in. One SubprocessorObligation row
        # per entry is written when a request reaches DELETED, so
        # "erased from all processors by X" is a queryable date instead of
        # a sentence in a DPA.
        "SUBPROCESSORS": [
            {"name": "openai", "window_days": 30},
            {"name": "google", "window_days": 55},
        ],

        # -- DSAR intake ----------------------------------------------------
        # Where ``gdpr.dsar.opened`` goes. Empty means nobody is told a
        # request arrived, which is how a 30-day statutory clock is missed.
        "DSAR_STAFF_EMAILS": ["privacy@example.com"],
        # Rolling hourly budget per caller on the three doors that start
        # work or send mail on request: the public DSAR intake, account
        # closure and the data-export request (``stapel_gdpr.throttling``).
        # The intake is AllowAny by regulation and @captcha_protected is a
        # no-op without a configured captcha backend, so without this the
        # form is an unauthenticated mail trigger anyone can hold open.
        # Keyed on the account when there is one, else on
        # ``stapel_core.netintel.client_ip`` — never on a header the caller
        # writes. ``0`` disables every budget here.
        "INTAKE_RATE_LIMIT_PER_HOUR": 10,

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
        "DATA_OWNERS_OPT_OUT": [],
        "SUBJECT_TYPES": [
            "account", "workspace", "meeting", "recording", "document", "file",
        ],
        "ERASURE_SLA_DAYS": 30,
        "OWNER_TIMEOUT_HOURS": 24,
        "OWNER_ALIVE_MAX_AGE_HOURS": 48,
        "ERASURE_AUTHORIZER": "",
        "SUBPROCESSORS": [],
        "DSAR_STAFF_EMAILS": [],
        "INTAKE_RATE_LIMIT_PER_HOUR": 10,
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
