# CONFIG.MD — stapel-gdpr

Config registry for **stapel-gdpr** (`static-scaffold-and-config.md` §2). One
row per configuration key the module reads, its **source** (`env` = the
process environment / the `STAPEL_GDPR` settings dict), what it is for,
whether it is required, and its default.

Every namespaced key is read through `gdpr_settings`
(`stapel_gdpr.conf.AppSettings`, namespace `STAPEL_GDPR`). Resolution order
per key: `settings.STAPEL_GDPR` dict → a flat Django setting of the same name
→ environment variable → the default below.

The anonymous `POST /dsar` is guarded by `stapel_core.django.captcha`, whose
`STAPEL_CAPTCHA` keys are stapel-core's own and documented in its CONFIG.MD —
this library reads none of them. With no backend configured the public form
is open, which is a host's decision to make.

Two keys are genuinely mandatory and fail the boot when unset: `DATA_OWNERS`
(`gdpr.E001` — with no inventory of the stores holding personal data, no
erasure can be certified and every export is reported to the user as partial)
and, right behind it, `DATA_OWNERS_VERSION` (`gdpr.W003` — an erasure nobody
can attribute to an inventory version is not auditable). Everything else has
a working default.

## stapel-gdpr

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| DATA_OWNERS | env | **Mandatory inventory.** Every store holding personal data, mapped to the subject types it holds data about: `{"recordings": ["account", "workspace", "recording"]}`. An entry's value may be a full spec (`{"subject_types", "kind": "local"\|"remote", "timeout_hours"}`). A plain list of names is still accepted and means `["account"]` for every entry. One `ErasurePart` per owner that claims the subject; empty is `gdpr.E001` and blocks every `deleted` state. | yes | `[]` |
| DATA_OWNERS_VERSION | env | Stamped onto every erasure (`registry_version`) so an audit can tell which inventory certified it. Bump whenever `DATA_OWNERS` changes. Unset is `gdpr.W003`. | yes | `""` |
| SUBJECT_TYPES | env | axis: what an erasure may be opened for. `POST /erasures` refuses a type outside this list (`error.400.gdpr.unknown_subject_type`), so a typo'd subject cannot become a request no owner can answer. | no | `["account", "workspace", "meeting", "recording", "document", "file"]` |
| ERASURE_SLA_DAYS | env | Purge SLA: `due_at = requested_at + this`. The account keeps its own cancellable 30-day grace on top; entities have no grace, since the UI removal already happened. | no | `30` |
| OWNER_TIMEOUT_HOURS | env | Default per-owner receipt deadline. `sweep_deletion_deadlines` flips an overdue part to `timeout`, which keeps blocking `deleted` and emits `gdpr.erasure.timeout`. Overridable per owner with `timeout_hours`. | no | `24` |
| OWNER_ALIVE_MAX_AGE_HOURS | env | How long a declared owner may go without answering `gdpr.owner.probe` before `gdpr.W006` names it at boot. Silence is a finding: an owner that never answers is an owner whose erasures will time out. | no | `48` |
| ERASURE_AUTHORIZER | env | Dotted path to `authorize(request, subject_type, subject_key) -> bool` for `POST /erasures`. Empty means staff only — only the host knows whether this user owns that recording. An authorizer that cannot be imported, or that raises, refuses the request. | no | `""` (staff only) |
| SUBPROCESSORS | env | Processors that received a copy and their contractual deletion windows: `[{"name": "openai", "window_days": 30}]`. One `SubprocessorObligation` per entry is written when an erasure reaches `deleted`; `fully_erased_by` is the max of our own `due_at` and theirs. A bare name means a zero-day window. | no | `[]` |
| DSAR_STAFF_EMAILS | env | Where `gdpr.dsar.opened` goes when a data-subject request arrives. Empty means nobody is told, which is how a 30-day statutory clock is missed (logged as a warning at intake). | no | `[]` |
| SESSION_REVOKER | env | Dotted path to `revoke(user) -> None`. Unset, the seam auto-detects stapel-auth's `SessionService.revoke_all`, then a `user.sessions_revoked` subscriber, then a broker transport; with none of those, closure raises `SessionRevocationUnavailable` (HTTP 503) rather than leaving live tokens behind. `gdpr.W005` when nothing resolves. | no | `""` (auto-detect) |
| PRIMARY_IDENTITY_ERASURE | env | What happens to the `users.User` row itself: `anonymize` (scrub in place, keep the pk so FKs stay resolvable), `delete`, or a dotted path to `erase(user) -> None`. The result is verified — a strategy that leaves email/phone/username intact keeps the closure in `deleting`. `gdpr.E006` when unimportable. | no | `"anonymize"` |
| DOWNLOAD_TTL_HOURS | env | Lifetime of the single-use export download token; the archive is deleted on consume or expiry. | no | `24` |
| DOWNLOAD_URL_TEMPLATE | env | Where the export-ready notification points. Keep the token in the URL **fragment** — a query string lands in access logs, browser history and `Referer`. | no | `"{frontend_url}/privacy/export/#token={token}"` |
| EXPORT_BUCKET_PREFIX | env | Prefix a peer service's `bucket_path` must start with before this service opens the object and copies its bytes into a user's download; templated over the export's own correlation id. `""` accepts any key (`gdpr.W007`); traversal, absolute and URL-shaped keys are refused either way. | no | `"gdpr/{correlation_id}/"` |
| STAGING_ROOT | env | Filesystem root for per-request export staging directories. | no | `""` (→ `MEDIA_ROOT/gdpr/staging`) |
| ARCHIVE_ROOT | env | Filesystem root for final export ZIP archives. | no | `""` (→ `MEDIA_ROOT/gdpr/exports`) |
| REREG_SALT | env | Key for the purpose-bound re-registration HMAC. Set it once, before any hashes exist — changing it silently invalidates every stored hash. | no | `""` (falls back to `SECRET_KEY`) |
| REMOTE_DELETION_SERVICES | env | Legacy pre-registry list of services that must confirm erasure; folded into `DATA_OWNERS` as `kind='remote'` entries claiming `account`. | no | `[]` |
| ALLOW_ERASURE_WITHOUT_RECEIPTS | env | Escape hatch: mark an erasure `deleted` without a full set of receipts, recorded as `completeness_waived` and warned about by `gdpr.W003`. Turns a proven erasure back into a hopeful one. | no | `False` |
| ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION | env | Escape hatch: close accounts with no revocation seam, leaving every pre-closure access token valid until it expires. `gdpr.W003`. | no | `False` |

### Flat Django settings this module also reads

Still rows of the `stapel-gdpr` section above — the library reads them
itself, they simply live outside the `STAPEL_GDPR` namespace: `GDPR_PROVIDERS`
is loaded in `apps.ready()` before any namespace exists for it to live in,
and the rest predate the namespace.

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| GDPR_PROVIDERS | env | **The dotted-path seam.** List of `GDPRProvider` class paths (e.g. `'stapel_auth.gdpr.AuthGDPRProvider'`) loaded with `import_string` in `apps.ready()` and registered into `stapel_core.gdpr.gdpr_registry` — no compile-time dependency on any service package. Every registered section must also appear in `DATA_OWNERS`, or the inventory is stale (`gdpr.E002`). | no | `[]` |
| GDPR_COLLECTING_SERVICES | env | Microservices mode: explicit list of services expected to contribute export parts. Unioned with `DATA_OWNERS` and the in-process registry — an owner that is not asked cannot be reported missing. | no | `[]` |
| GDPR_STAGING_ROOT | env | Legacy flat equivalent of `STAGING_ROOT`. | no | `""` |
| GDPR_ARCHIVE_ROOT | env | Legacy flat equivalent of `ARCHIVE_ROOT`. | no | `""` |
| FRONTEND_URL | env | Base of the download URL in the export-ready notification (`DOWNLOAD_URL_TEMPLATE`). | no | `""` |
