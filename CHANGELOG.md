# Changelog

All notable changes to `stapel-gdpr` are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
