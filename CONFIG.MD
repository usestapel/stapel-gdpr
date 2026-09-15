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
| DATA_OWNERS | env | **Mandatory inventory.** Every store holding personal data, mapped to the subject types it holds data about: `{"recordings": ["account", "workspace", "recording"]}`. An entry's value may be a full spec (`{"subject_types", "kind": "local"\|"remote", "timeout_hours"}`). A plain list of names is still accepted and means `["account"]` for every entry. One `ErasurePart` per owner that claims the subject; empty is `gdpr.E001` and blocks every `deleted` state. **Names must be the ones the libraries declare, not app labels** — the `cdn` app owns `media`, the `profiles` app owns `profile`; a name no installed library declares is `gdpr.E009` and an installed owner this omits is `gdpr.E010`. | yes | `[]` |
| DATA_OWNERS_VERSION | env | Stamped onto every erasure (`registry_version`) so an audit can tell which inventory certified it. Bump whenever `DATA_OWNERS` changes. Unset is `gdpr.W003`. | yes | `""` |
| DATA_OWNERS_OPT_OUT | env | Owner names an **installed** library declares that this deployment deliberately does not ask to erase. Downgrades `gdpr.E010` to `gdpr.W011` for the names listed — a chosen exemption stays reported at every boot, in the same place the accident would have been. No wildcard; the name has to be the one the library declares. | no | `[]` |
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
| EXPORT_ROOT | env | **Private root for personal-data exports — never a served root.** Holds the per-request staging directories and the finished ZIPs. A value inside `MEDIA_ROOT`, `STATIC_ROOT` or `STATICFILES_DIRS` is refused at boot (`gdpr.E013`); leaving it unset is `gdpr.W013`, because the default is local to whichever process wrote it. See "Where an export lives" below. | no | `""` (→ `BASE_DIR/private/gdpr`, or `<tmpdir>/stapel-gdpr` with no `BASE_DIR`) |
| STAGING_ROOT | env | Per-directory override of `EXPORT_ROOT` for staging. Checked by `gdpr.E013` too. | no | `""` (→ `EXPORT_ROOT/staging`) |
| ARCHIVE_ROOT | env | Per-directory override of `EXPORT_ROOT` for the finished ZIPs. Checked by `gdpr.E013` too. | no | `""` (→ `EXPORT_ROOT/exports`) |
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
| GDPR_EXPORT_ROOT | env | Flat equivalent of `EXPORT_ROOT`. | no | `""` |
| GDPR_STAGING_ROOT | env | Legacy flat equivalent of `STAGING_ROOT`. | no | `""` |
| GDPR_ARCHIVE_ROOT | env | Legacy flat equivalent of `ARCHIVE_ROOT`. | no | `""` |
| FRONTEND_URL | env | Base of the download URL in the export-ready notification (`DOWNLOAD_URL_TEMPLATE`). | no | `""` |
| STORAGES["stapel_gdpr_exports"] | env | Django storage alias for the export store. Configure it when the celery worker and the web process do not share a filesystem; otherwise the module uses a private `FileSystemStorage` rooted at `EXPORT_ROOT`. See below. | no | unset |

## Where an export lives

A data export is a ZIP holding *everything the system knows about a person*.
Until 0.7.0 it defaulted to `MEDIA_ROOT/gdpr/exports`, and the ordinary
Django/nginx shape serves `MEDIA_ROOT` as static files:

```nginx
location /media { alias /media; add_header Cache-Control "public, max-age=2592000"; }
```

So on a deployment that followed the ordinary shape — one that read nothing
and configured nothing — that archive sat at a guessable URL, served without
authentication and cacheable by every intermediary between the origin and the
reader. The library's own default was the whole vulnerability, which is why
the rules below are mechanisms rather than advice.

**1. The root is explicit and off every served root.** `EXPORT_ROOT` is its
own setting; it is not derived from `MEDIA_ROOT` and never falls back to it.
The default (`BASE_DIR/private/gdpr`) is a literal shape no ordinary
`location /media` or `location /static` block can reach. A deployment that
points `EXPORT_ROOT`, `STAGING_ROOT`, `ARCHIVE_ROOT` — or their flat
`GDPR_*` twins — inside `MEDIA_ROOT`, `STATIC_ROOT` or `STATICFILES_DIRS` is
refused at boot with **`gdpr.E013`**, an `Error` in the same spirit as
`stapel_core.storage.E001`. It is not a warning on purpose: a deployment
publishing personal-data archives is not degraded, and a warning is exactly
what let the old default run.

**2. The download is never a path and never a URL.** The archive is streamed
by `POST /gdpr/api/v1/user/data-export/download`, which authenticates the
subject, spends a single-use token carried in the request *body*, deletes the
object as it serves and answers `Cache-Control: no-store, private`. The store
the module writes through (`stapel_gdpr.export_store.PrivateFileSystemStorage`)
**raises on `url()`** — there is no address to guess and none to leak. Note
that plain `FileSystemStorage(base_url=None)` is *not* private: `base_url`
falls back to `settings.MEDIA_URL`, so the obvious way to build a private
store still hands out `/media/<key>`.

**3. The row stores a store key, not a filesystem path.**
`DataExportRequest.archive_path` holds a key inside the export store. The
download view runs in a different process from the celery task that writes
the archive; an absolute path written in the worker's container resolves to
nothing in the web container, which is how a correctly-generated export
answered a 500 to someone exercising data portability after being told it was
READY. When the two processes do not share a filesystem, give them a shared
store:

```python
STORAGES = {
    ...,
    "stapel_gdpr_exports": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {"bucket_name": "acme-private", "location": "gdpr",
                    "default_acl": "private", "querystring_auth": True},
    },
}
```

Anything else — a shared volume named in `EXPORT_ROOT`, a GCS backend — works
the same way: both processes resolve the same key.

**Signed object-storage URLs: no.** The library never hands one to a subject.
A signed URL is a bearer credential to the complete dump: it cannot be made
single-use, it cannot be revoked once issued, and it lands in browser history,
`Referer` and proxy logs — the leak paths the body-carried token exists to
avoid. If a deployment must use one anyway (archives too large to stream
through the app), keep the signature lifetime at **60 seconds or less** — it
covers the gap between the click and the first byte, not the gap between the
email and the click — and treat the download endpoint, not the URL, as the
thing that authenticates.

**Peer-uploaded slices.** In microservices mode a remote data owner writes its
slice to `default_storage` under `EXPORT_BUCKET_PREFIX`
(`gdpr/{correlation_id}/`). Since 0.7.0 each slice is deleted as soon as its
bytes are inside the archive. When `default_storage` is a filesystem store
inside a served root and at least one remote owner is declared, **`gdpr.W014`**
says so at boot: the window is minutes rather than forever, but it exists.
Give `default_storage` a private backend, or serve `MEDIA_ROOT` through an
authenticated handler.
