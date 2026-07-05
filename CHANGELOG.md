# Changelog

All notable changes to `stapel-gdpr` are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
