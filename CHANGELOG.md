# Changelog

All notable changes to `stapel-gdpr` are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.5.0] — 2026-08-23

Pre-1.0, so a minor is where breaking changes live. This one generalizes the
subject of an erasure, adds the three edges the machine never had (intake,
re-queue, the subprocessor ledger), and makes an owner's silence a finding
instead of a log line.

### Breaking

- **`AccountDeletionPart` is gone**, replaced by `ErasurePart` (FK to
  `ErasureRequest`, not to a closure; `status` → `state`, `service` → `owner`,
  `completed_at` → `receipt_at`, `error` → `note`). Migration 0004 is a
  **cutover**: the rows are carried into the new table and the old one is
  dropped in the same release, so there is no window in which both exist.
  Deploy stop-the-world. Code that imported `AccountDeletionPart`, or read
  `closure.parts`, moves to `closure.erasure.parts`.
- **`AccountClosureRequest.parts` no longer exists.** `all_remote_parts_done`
  and `unreceipted_owners` still answer the same questions, now through
  `closure.erasure`.
- **`tasks.notify_llm_providers_of_deletion` is removed** — deletion-driven,
  no deprecation window. It wrote a log line claiming to record a DPA
  obligation, which no audit could query. `subprocessors.record_subprocessor_obligations`
  writes a `SubprocessorObligation` row per processor instead, and the
  orchestrator calls it automatically when an erasure completes.
- **`GDPROrchestrator.mark_section_erased` takes `counts`** and matches on
  `ErasureRequest.correlation_id` rather than the closure's. Callers using the
  documented comm path are unaffected; a host calling the method directly with
  positional arguments still works.
- **`user.deleted` is deprecated**, not removed: it keeps firing for account
  erasures for one minor and disappears in 0.6.0. Subscribe to
  `gdpr.erasure.requested`, which carries the subject pair `user.deleted`
  cannot express.

Not breaking, deliberately: `STAPEL_GDPR["DATA_OWNERS"]` still accepts a plain
list of names, which now means `["account"]` for every entry. No host has to
touch its settings on this bump.

### Added — the subject is a parameter

`ErasureRequest(subject_type, subject_key, workspace_id, requested_by, origin,
requested_at, grace_ends_at, due_at, state, completed_at, correlation_id)` with
`queued → erasing → deleted | timeout`. A workspace, meeting, recording,
document or file now gets the account's machine: a purge SLA
(`ERASURE_SLA_DAYS`, default 30), one receipt per data owner, the same refusal
to self-certify on silence. Entities get no grace — the host already removed
them from the UI, so the clock is a purge deadline, not a waiting period.

`AccountClosureRequest` is unchanged as the user-facing grace/cancel object and
creates its `ErasureRequest(subject_type="account")` at grace end, carrying the
closure's own correlation id. **The 0.4.x HTTP surface is identical.**

`DATA_OWNERS` grows from a list of names into a map owner → subject types, so a
recording erasure waits for recordings and media and not for billing. Both
forms resolve to the same declaration; `kind`/`timeout_hours` remain available
per owner.

### Added — silence is a finding

- `probe_data_owners` (daily) emits `gdpr.owner.probe`; owners answer
  `gdpr.owner.alive {owner, subject_types}` **from the same subscriber that
  handles erasure**, so an answer proves the erasure path is consumed rather
  than that a container is deployed.
- `DataOwnerHealth` stores it; `GET /gdpr/api/v1/owners/health` (staff) is the
  table; `gdpr.W006` names every declared owner with no answer in
  `OWNER_ALIVE_MAX_AGE_HOURS` (48) at **boot**, rather than at the first
  erasure that times out thirty days later.
- `sweep_deletion_deadlines` is subject-agnostic and now flips the request too,
  emitting `gdpr.erasure.timeout {owners, ...}` so a host can alert.

### Added — DSAR intake

`DsarRequest` carries both statutory clocks (`ack_due_at` = three business
days, `resolve_due_at` = thirty calendar days). `POST /dsar` takes an
authenticated request and an anonymous one from a public /privacy form behind
stapel-core's tiered captcha policy. The acknowledgement (`gdpr.dsar.received`)
goes out inside intake and stamps `ack_sent_at` — an unmet deadline is a NULL
the sweep and the boot check can see, not an assumption that mail was sent.
Staff get `gdpr.dsar.opened` at `DSAR_STAFF_EMAILS`.

A request matched to an account is handed to the mechanism that answers it:
`erasure` to `initiate_closure` with its cancellable grace intact,
`access`/`portability` to `request_export`. An anonymous one waits for staff to
match it (`PATCH /dsar/{id}` with `user_id`) — turning an unverified email into
an erasure is a deletion oracle. `sweep_dsar_deadlines` (daily) emits
`gdpr.dsar.overdue` once per deadline; `gdpr.W008` reports the unacknowledged
queue at boot.

`gdpr.W008` rather than the `W007` the spec asked for: that id has been the
`EXPORT_BUCKET_PREFIX` warning since 0.4.x, and silently reusing a published
check id would break every deployment that silenced it.

### Added — the restore that undoes an erasure

`manage.py gdpr_requeue_after_restore --restored-from <iso>` (plus `--dry-run`)
re-arms the clock for every erasure that completed inside the restored window
(with a day of slack, since a backup's timestamp is when the snapshot started).
Idempotent by construction rather than by a flag file: the clone FKs the
request it re-runs and `(origin, source_request)` is unique, so a repeated or
overlapping run writes nothing. One line for each product's deploy README:
*after any restore, run this with the backup's timestamp.*

### Added — the subprocessor ledger

`STAPEL_GDPR["SUBPROCESSORS"] = [{"name": "openai", "window_days": 30}, ...]`.
One `SubprocessorObligation` row per processor per erasure, written when the
erasure reaches `deleted`, with the date that processor's window closes.
`ErasureRequest.fully_erased_by` is the max of our own `due_at` and every
obligation's, and the status endpoints publish it — so a product can say
"erased from our systems on X; from every processor by Y" and mean both halves.
No provider API is called: none of the ones we use has one.

### Added — HTTP

- `POST /gdpr/api/v1/erasures` — the host's hook after its own soft-delete,
  behind the `ERASURE_AUTHORIZER` callable setting (default staff-only; an
  authorizer that cannot be imported, or that raises, refuses — an ownership
  check that fails open is worse than none).
- `GET /gdpr/api/v1/erasures/{id}` — state, per-owner receipts with counts,
  subprocessor obligations, `fully_erased_by`.
- `GET /gdpr/api/v1/me/erasures` — the caller's own pending deletions.
- `POST/GET /gdpr/api/v1/dsar`, `PATCH /gdpr/api/v1/dsar/{id}`,
  `GET /gdpr/api/v1/owners/health`.

Five new error codes, declared with remediations and translated in every
language the corpus ships: `error.400.gdpr.unknown_subject_type`,
`error.400.gdpr.unknown_dsar_kind`, `error.403.gdpr.erasure_forbidden`,
`error.404.gdpr.erasure_not_found`, `error.404.gdpr.dsar_not_found`.

### Added — settings, and a registry for them

`SUBJECT_TYPES`, `ERASURE_SLA_DAYS`, `OWNER_ALIVE_MAX_AGE_HOURS`,
`ERASURE_AUTHORIZER`, `SUBPROCESSORS`, `DSAR_STAFF_EMAILS`. This module also
gains its first **`CONFIG.MD`** — every key it reads, namespaced and flat, with
a purpose, a default and the check that fires when it is unset. The registry
did not exist before, which meant `stapel-config-lint` went green by having
nothing to lint.

`get_gdpr_beat_schedule()` gains `gdpr-data-owner-probe` and
`gdpr-dsar-deadline-sweep`.

### Tests

189 → 261. Every state transition of the new machine, both `DATA_OWNERS`
shapes, `W006` (never answered / stale / silent) and `W008` (with and without a
database), requeue idempotency across repeated and overlapping windows, and the
authorizer in all five of its states — default, custom, refusing, raising and
unimportable — each failing closed.


### Added — `required_settings` in `docs/capabilities.json`

`gdpr.E001` is boot-fatal when `STAPEL_GDPR["DATA_OWNERS"]` is empty, so
installing this app without that setting produces a service that cannot start.
Nothing said so in a form a generator could read: both stapel example apps
install `stapel_gdpr` and emit no `STAPEL_GDPR` block anywhere, and the
scaffold's validator would in fact have *rejected* `DATA_OWNERS`, because it is
not a capability axis.

The artifact now declares it. `DATA_OWNERS` and `DATA_OWNERS_VERSION` each
carry a `kind` and an `example` (shape enough for a generator to emit a correct
placeholder), a `why` and the `unset_check` they prevent (prose enough for a
human). `stapel-tools` reads the section and refuses to generate a project that
installs this app with no value supplied — at generation time, not at first
boot in production.

## [0.4.2] — 2026-08-15

### Changed — `stapel-core` floor raised to 0.26.0

`docs/errors.json` carries an `owner` per entry, and only stapel-core 0.26.0
emits it. The floor stayed at `>=0.24.0`, so a consumer resolving an older
core regenerated an artifact without `owner` and the drift gate went red —
the field was declared but never required. The floor now matches the
artifact that is committed.

## [0.4.1] — 2026-08-15

### Fixed — `check_reregistration_hashes` crashed a boot smoke test

The check queried the database unconditionally and caught only
`DatabaseError`. Django's dummy backend — what it fills in when `DATABASES`
carries no ENGINE, i.e. exactly a boot smoke test run without a database —
raises `ImproperlyConfigured`, which is not a `DatabaseError`, so
`manage.py check` did not report a finding: it exited with a traceback out
of `stapel_gdpr.checks`.

Two changes, both of them Django's own convention for a database-backed
check (`django.core.checks.database.check_database_backends` is the
canonical shape):

- the check now takes `databases=None` and returns `[]` without touching
  the database when it is offered none. `migrate` and
  `manage.py check --database <alias>` pass the aliases; everything else
  passes `None`. **This changes when `gdpr.E004` is reported**: a plain
  `manage.py check` no longer runs the query (the check has carried the
  `database` tag since it was introduced, which is what that tag means).
  Deploy pipelines that want it either pass `--database default` or get it
  from `migrate`;
- when a database *is* offered, it is queried per alias — routed with
  `router.allow_migrate_model`, so an alias that does not hold the model is
  skipped — and an unreachable or unconfigured one degrades to silence
  instead of an exception. `ImproperlyConfigured` joins `DatabaseError` in
  the handled set.

`gdpr.E004` itself is unchanged, and still reports every
`ReRegistrationHash` row written outside `store_hashes`; its message now
names the alias it counted them in.

## [0.4.0] — 2026-08-14

### Added — this module ships its own localized error catalogs

`translations/errors.{ru,es}.json` plus the `translations/.state.json`
provenance sidecar, generated and gated by `tests/test_error_i18n.py` through
the `stapel_core.i18n` contour (regenerate with
`STAPEL_REGEN_ERROR_I18N=1 pytest tests/test_error_i18n.py::test_regen`).

Since stapel-core 0.22.0 a package may only translate the keys it **owns**, and
since 0.23.1 a reader resolves a key it does not own from the **owner's**
catalog. This module owns ten `error.*.gdpr.*` keys and shipped no catalog at
all, so every consumer's localized error reference fell back to English for
them — no consumer could fix that on its own side, because the writer that
would have to place the text is scoped out of those keys. Concretely,
stapel-auth's `docs/errors.{ru,es}.md` rendered `_(en)_` rows for the three
keys the 2026-08-11 wave added (`error.403.gdpr.account_closed`,
`error.410.gdpr.download_consumed`, `error.503.gdpr.closure_unavailable`), and
the seven older ones only rendered in Russian because stapel-auth still carried
a pre-ownership-scoping copy of them, deleted in its 0.21.0 line.

All twenty values are seeded from stapel-translate's curated builtin corpus
(`origin: seed:stapel-builtin`; 0.6.1 is the release that added the three
missing strings), so the ten strings keep one home and nothing here is
unreviewed LLM output. `translations/*.json` joins the wheel's package data —
the catalog only resolves for a consumer if it is actually installed.

No behavior change: no code, no schema, no migration.

### Changed — requires stapel-core >= 0.24.0

The floor moved from `0.10` to `0.24.0`. This module imports no symbol that
core added in the 2026-08-11 wave — the floor moves because a guarantee made
here is only half-made without core's half of the same finding:

- **GDPR-01 is a two-repo fix.** `get_or_create_user_from_jwt` wrote the
  `is_active` claim into the local user row, so any token minted before a
  closure undid that closure when it was replayed — core's own changelog
  files that change under "a bearer token can no longer write account
  lifecycle (audit GDPR-01, P0)". What lands here defends the *read* side
  (`lifecycle.access_state` answers from the closure row, `guards` refuse a
  deleting account whatever `is_active` says); core 0.24.0 is what stops the
  *write*. On an older core the guards still hold, but the user row keeps
  being flipped back underneath them, which is not the state this release
  claims.
- `JWT_CREATE_USERS_FROM_TOKEN` now defaults to `False`, so an unknown
  `user_id` in a token no longer materialises the very row a closure just
  erased.
- `STAPEL_COMM["VALIDATE_SCHEMAS"]` is on by default instead of following
  `settings.DEBUG`, so the payloads this module emits (`user.export_ready`,
  the new `user.sessions_revoked`, `gdpr.section.erased`) are checked against
  the schemas in `schemas/emits/` in production and not only in development.

The suite passes against both cores — the difference the floor expresses is a
deployment property, not a test failure, which is exactly why it is stated as
a floor rather than left to a reader to discover.

### Security

Closes the three GDPR findings of the 2026-08-11 audit.

- **GDPR-01 — closure was reversible and never erased the person.** Closure
  deactivates through `lifecycle.set_active` (model save, so activation
  observers fire) instead of `QuerySet.update`, revokes every session through
  a resolvable seam (`SESSION_REVOKER`, auto-detecting stapel-auth) or refuses
  to close at all, and erases the primary `users.User` row itself
  (`PRIMARY_IDENTITY_ERASURE`: anonymize in place, delete, or a host callable)
  with the result verified rather than trusted. "Is this account closed?" is
  answered from the closure row by `lifecycle.access_state` — never from
  `is_active`, which a JWT-to-DB user sync can write back — and enforced by
  `guards.AccountNotClosed` on every view here plus the fleet-wide
  `guards.AccountClosureGuardMiddleware`.
- **GDPR-02 — erasure completeness was assumed.** `STAPEL_GDPR["DATA_OWNERS"]`
  is now a mandatory, versioned inventory; every owner gets an
  `AccountDeletionPart` with a deadline and must return a durable
  `receipt_id`. A closure reaches `DELETED` only against a full receipt set,
  a sound registry and an erased identity; missing, undeclared, failed or
  timed-out owners keep it in `DELETING`. Exports that cannot account for an
  owner are reported partial to the user (`is_partial`/`missing_services`),
  not only in a README inside the ZIP.
- **GDPR-03 — the download token was a week-long reusable bearer credential.**
  Only a SHA-256 digest of the token is stored, it is spent by one atomic
  conditional update, it travels in the POST body (URL fragment in the
  notification link, never a query string), the response is `no-store`, and
  the archive is deleted the moment it is served. `purge_expired_exports`
  enforces retention on a schedule. Re-registration hashes moved to one
  purpose-bound keyed HMAC with a per-row `scheme`; rows written around
  `store_hashes` never match, are reported by `gdpr.E004`, and are removable
  with `manage.py gdpr_purge_unverified_hashes`.

- **A remote `bucket_path` is validated before it is opened (UPGRADE NOTE).**
  A peer service's export part arrives as a storage key — over HTTP
  (`ExportPartReadyView`) or over the bus — and whatever it names was copied
  verbatim into an archive a USER downloads, with no shape check. Django's
  `FileSystemStorage` refuses traversal; an S3 backend has no such notion, so
  a compromised or merely buggy peer could pull an arbitrary key into another
  user's export. The key must now start with
  `STAPEL_GDPR["EXPORT_BUCKET_PREFIX"]`, which defaults to
  `"gdpr/{correlation_id}/"` — templated over the export's own correlation
  id, so a peer can only name a key belonging to the export it was asked
  about. Traversal, absolute and URL-shaped keys are refused regardless.
  Enforced at ingest (`mark_part_ready` refuses the part, and the export is
  honestly reported partial) **and** at open (`_download_bucket_parts`), so a
  row written before this rule or around the orchestrator is not readable
  either.
  *Upgrade:* peers that stage somewhere else must move to the prefix, or the
  deployment must state where they stage.
  *Opt-out restoring the old behaviour:*
  `STAPEL_GDPR = {"EXPORT_BUCKET_PREFIX": ""}` — accepts any key, and says so
  at `manage.py check` as `gdpr.W007`.
- **The internal callback declares the permission it enforces.**
  `ExportPartReadyView` declared `IsAuthenticated` and checked
  `IsServiceRequest` inside `post()`. The in-body check did close the
  endpoint, but the declaration is what a subclass overriding `post()`, and
  every permission introspection or audit, actually sees — "any logged-in
  user" on the endpoint that marks another service's GDPR export part
  complete. `permission_classes` is now
  `[IsServiceRequest, IsAuthenticated]`: the same requirement that was
  already effective, stated where the rest of the module states it.

### Added

- Boot-time system checks (`gdpr.E001/E002/W003/E004/W005/E006/W007`): a missing or
  stale data-owner inventory, unverified hash rows, an unusable revocation or
  identity-erasure seam, an opened export-bucket prefix, and every open
  escape hatch are reported by `manage.py check`.
- `user.sessions_revoked` comm action; `receipt_id` on `gdpr.section.erased`.
- Celery tasks `purge_expired_exports` and `sweep_deletion_deadlines`, both
  wired into `get_gdpr_beat_schedule()`.
- Named escape hatches, off by default: `ALLOW_ERASURE_WITHOUT_RECEIPTS`,
  `ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION`.

### Changed

- **Breaking:** `GET user/data-export/download` is gone; the token is spent
  through `POST` only.
- **Breaking:** a deployment must declare `DATA_OWNERS` and provide a session
  revoker, or closures fail loudly (HTTP 503) instead of silently leaving data
  and live tokens behind.

## [0.3.12] — 2026-08-02

### Added
- `docs/llms.txt` — the fifth contract artifact, an agent-sized slice of the
  hand-authored `docs/capabilities.json`, wired into `make contract` /
  `make contract-check` (badge-canon §3). `docs/capabilities.json`'s
  `version` field resynced to `pyproject.toml` (it had drifted to 0.3.10
  across the 0.3.11 release).
- Badge canon in README (CI/coverage/pypi/downloads/python/license),
  `migration-lint` uncommented in CI now that stapel-tools is on PyPI,
  classifier 3.14.
- CI matrix now tests Python 3.14 (the version actually in production),
  alongside the existing 3.11-3.13.

### Fixed
- `docs/capabilities.json`, `docs/flows.json`, `docs/errors.json`,
  `docs/llms.txt` and `CONFIG.MD` now ship in the wheel via `package-data`
  (#184); previously repo-only, invisible to `--from-installed` tooling.

### Tests
- `test_consumer.py` passes `--allow-in-process` explicitly where the
  publisher and consumer really are the same process, documenting the
  legitimate exception to core 0.14.2's in-process-bus consumer guard.

## [0.3.10] — 2026-07-17

Fleet follow-up to stapel-core 0.12.0 (legacy shim sweep — dropped
`django.{utils,jwt_provider,authentication}` shims, IRON_HOST, flat CAPTCHA,
JWTStatusView flat user block). No source changes needed: stapel-gdpr does
not touch any of the removed surfaces. Full suite green (107 passed) against
core 0.12.0.

### Changed
- `stapel-core` dependency ceiling `<0.12` → `<0.13`.

## [0.3.9] — 2026-07-17

### Removed
- Legacy sweep: two dead unassigned expressions in
  `tasks.check_inactive_accounts` (`now - timedelta(days=365 - 60)` /
  `... - 14)`) — leftovers of a refactor; the warning cutoffs are computed
  inside the loop. No behavior change, no public surface touched.

## [0.3.8] — 2026-07-17

### Changed
- `stapel-core` ceiling raised `>=0.10,<0.11` → `>=0.10,<0.12` (core 0.11
  fleet re-pin: default bus, nav, config-checks, error params/language —
  additive for modules). Suite green against core 0.11.2 (incl. the `s3`
  extra), no code changes needed. Unblocks `stapel-tools` v0.11.0's publish
  (its resolver conflict was this repo's `<0.11` ceiling).

## [0.3.7] — 2026-07-16

### Changed
- **v1 canon sweep §60** (api-versioning.md §2, §6): `urls.py` renamed to
  `urls_v1.py` (paths inside unchanged); the new root `urls.py` mounts it
  under `v1/`, so hosts including `stapel_gdpr.urls` under `.../api/` now
  serve `/<mount>/api/v1/...`. Bare `/<mount>/api/...` paths no longer exist
  (no live external consumers; sweep happens before the §3 gates are on).
- Lint hygiene to a clean `stapel-verify`: `ERR_400_BAD_REQUEST` /
  `ERR_403_FORBIDDEN` constants instead of raw strings (R005), explicit
  `# noqa: R007` on the documented endpoints not yet attached to flows.

## [0.3.6] — 2026-07-16

### Fixed
- **`user.export_ready` is now actually emitted.** The emit schema
  (`schemas/emits/user.export_ready.json`) existed but the code only sent
  the `gdpr.export_ready` email notification — no comm event ever left
  (2026-07-16 "silent contract lie" audit). Archive assembly now emits
  `user.export_ready` (`user_id`, `request_id`, `download_expires_at`) in
  one `mutate_and_emit()` outbox unit with the READY flip + download-token
  write: a failing emit rolls READY back, so consumers are told about
  exactly the exports that exist. The email stays best-effort.
- **EMIT002 (outbox atomicity):** `initiate_closure()` and `execute_deletion()`
  swallowed a failing `emit()` behind a broad `except Exception: logger.error(...)`
  — the closure row (+ user deactivation) or the `local_erasure_done` flip
  could commit while the `user.deletion_initiated` / `user.deleted` action
  silently never went out (the categories C1 bug, on the GDPR erasure path —
  remote services rely on `user.deleted` to erase their own section). Both
  sites now join their mutation and the emit into one `stapel_core.comm.mutate_and_emit()`
  unit: a failing emit rolls the mutation back and propagates instead of being
  swallowed. `tasks.py`'s callers (`check_inactive_accounts`,
  `process_expired_grace_periods`) already wrap these calls in their own
  `try/except` and retry on the next scheduled sweep — local erasure is
  idempotent, so re-running `execute_deletion()` is safe.
  `emit_check` (`stapel_core.lint.emit_check`) is now clean on this module.

### Changed
- Admin-suite AS-5: `@access.ops` on `DataExportRequest`, `DataExportPart`,
  `AccountClosureRequest`, `AccountDeletionPart`, and `ReRegistrationHash` —
  their state machines are owned entirely by `GDPROrchestrator` / scheduled
  tasks and were never meant to be hand-edited through the admin (MODULE.md
  already said so in prose: "Do not flip `AccountClosureRequest.status` or
  `AccountDeletionPart` rows directly"). `LegalHold` stays undecorated
  (`business`) — placing/releasing a hold through `LegalHoldAdmin` is a real
  staff workflow. `AccountClosureRequestAdmin`, `DataExportRequestAdmin`, and
  `ReRegistrationHashAdmin` now subclass `stapel_core.django.admin.base.StapelModelAdmin`;
  `ReRegistrationHashAdmin` additionally pins `secret_fields = ('hash_value',)`
  to mask the PII hash. Class attribute only — no migration.

## 0.3.4 — 2026-07-06

### Changed
- Pinned `stapel-core` to the `>=0.8,<0.9` window (library-standard §7.1: one
  minor window; floor `0.8.0` is published on PyPI — no pin into the void).
- CI: added the release-track job (library-standard §7.4) — installs the package
  the way an end user does (`pip install .`, dependencies resolved from PyPI
  strictly by the declared pins, no git-main core, no editable siblings), asserts
  `stapel-core` resolves inside the `0.8` window, and runs an import smoke.
  Advisory (continue-on-error) until the whole stapel graph is on PyPI; becomes
  the blocking precondition for a `vX.Y.Z` tag once it is.


## 0.3.3 — 2026-07-06

### Packaging
- `[project.urls]` added, trove classifiers completed (MIT/OSI, Python 3.13,
  `Typing :: Typed`, OS Independent, `3 :: Only`, Development Status) and a
  `[tool.ruff]` lint section added (single source shared with the git
  hooks/CI). Tests were already excluded from the wheel/sdist `packages`.


## 0.3.2 — 2026-07-05

### Changed
- OpenAPI: `@extend_schema` annotations for the AccountClose, AccountCancelClose,
  DataExportRequest, and DataExportDownload views now reflect the real contract —
  truthful error responses (`StapelErrorSerializer` for 404/409/410/425),
  `request=None` on body-less POSTs, an explicit `token` request body / query
  parameter for the download endpoints, and a binary (`application/zip`) 200
  response for the archive download. Resolves the drf-spectacular "unable to
  guess serializer" errors. No runtime behavior change.

## 0.3.1 — 2026-07-04

### Added
- `MODULE.md` — agent-facing extension-point map (part of the July 2026
  framework-wide documentation sweep). No functional changes.

## 0.3.0 — 2026-07-03

No functional changes — version alignment with the Stapel 0.3
release train; stapel-core dependency now `>=0.3.0,<0.4`.


## [0.2.0] - 2026-07-02

First functional release.

### Added
- Data export (GDPR Art. 15/20): request, async assembly from local providers
  and remote services, per-section `DataExportPart` tracking, 24h deadline
  sweep with partial archives, 7-day download links.
- Account closure (GDPR Art. 17): 30-day grace period, cancellation,
  orchestrated erasure across local `GDPRProvider`s, `user.deleted` /
  `user.deletion_initiated` comm actions.
- `AccountDeletionPart` — per-remote-service deletion confirmation tracking,
  mirroring `DataExportPart`. Expected services come from
  `STAPEL_GDPR["REMOTE_DELETION_SERVICES"]`; services confirm by emitting
  `gdpr.section.erased` `{user_id, correlation_id, service}`. A closure flips
  to `DELETED` only when local providers succeeded and every expected remote
  part is confirmed.
- `LegalHold` model + admin: closure initiation, deletion execution and
  retention cleanup refuse to touch data of users under an unreleased hold
  (`error.409.gdpr.legal_hold` on the API).
- Re-registration detection: on deletion execution, salted SHA-256 hashes
  (`STAPEL_GDPR["REREG_SALT"]`, defaults to `SECRET_KEY`) of the user's email
  and phone are stored in `ReRegistrationHash` before erasure;
  `stapel_gdpr.reregistration.is_reregistration(email=..., phone=...)` is
  exported for auth signup flows. Hashes are retained 24 months.
- `stapel_gdpr.conf.gdpr_settings` (`STAPEL_GDPR` AppSettings namespace):
  `REMOTE_DELETION_SERVICES`, `REREG_SALT`, `STAGING_ROOT`, `ARCHIVE_ROOT`.
- Download endpoint additionally accepts an Authorization-bound `POST` with
  the token in the body (keeps it out of access logs).
- Test suite (pytest + pytest-django, in-memory bus, in-process comm).
- `py.typed` marker.

### Changed
- **Breaking:** `DataExportRequest.user_id`, `AccountClosureRequest.user_id`
  are now `UUIDField` (framework users have UUID primary keys); event schemas
  declare `user_id` as `string`/uuid. Initial migration regenerated — no
  installed base at 0.1.0.
- `AccountClosureRequest.user_id` is no longer unique — a user may close,
  cancel, and close again; the orchestrator guards active closures instead.
- Export staging/archive directories default under `MEDIA_ROOT/gdpr/`
  (previously `/tmp`) and are created with `0700` permissions; the staging
  directory is removed after the archive is assembled.
- Archive assembly is serialized with `SELECT ... FOR UPDATE` and a new
  `ASSEMBLING` status so concurrent part completions cannot build the zip
  twice.
