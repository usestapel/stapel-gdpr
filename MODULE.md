# stapel-gdpr — MODULE.md

Agent-facing map of this module: what it provides, where it can be extended **without forking**, and what not to do. Use it to classify a desired change as *app-layer override via an extension point* vs *upstream contribution* (see `docs/stdlib-contribution-pipeline.md` and system-design §8.6 in the stapel workspace).

Stapel ground rules apply: modules never import each other; all cross-module communication goes through `stapel_core.comm` (Actions/Functions) or shared primitives in `stapel_core`; all customization must be possible from the host project layer.

## What this module provides

| Capability | Entry points | Notes |
|---|---|---|
| Data export (GDPR Art. 15/20) | `POST user/data-export/{request,status,download}` (`urls.py`), `GDPROrchestrator.request_export/run_export` | Fan-out to all declared data owners; 24 h assembly deadline (partial archive after that, swept hourly), single-use download token (`DOWNLOAD_TTL_HOURS`, POST body only, archive deleted on use), 30-day cooldown per user |
| Account closure & deletion (Art. 17) | `POST user/account/{close,cancel-close}`, `GET user/account/close/status`, `GDPROrchestrator.initiate_closure/cancel_closure/execute_deletion` | 30-day grace period; the account is deactivated **through the model** (observers fire) and all sessions are revoked, or the closure is refused; deletion = local `GDPRProvider`s + erasure of the primary `users.User` row + `user.deleted` comm fan-out + per-owner receipts |
| Server-side closed-account gate | `guards.AccountNotClosed` (on every view here), `guards.AccountClosureGuardMiddleware` (host-wired), `lifecycle.access_state` | Reads the closure row, never `is_active` — a token that syncs `is_active=true` back into the user table cannot reopen an erasing account |
| Deletion completeness | `owners.data_owner_report()`, `AccountDeletionPart.receipt_id/deadline`, `checks.py` | Closure flips to `deleted` only when every owner declared in `DATA_OWNERS` returned a durable receipt against a registry with no missing/undeclared owners, and the primary identity is gone. Silence times out and keeps blocking |
| Legal hold (Art. 17(3)) | `LegalHold` model, `LegalHold.is_held(user_id)`; Django admin | Blocks `initiate_closure` and `execute_deletion`; held users skipped by grace-period worker and retention cleanup |
| Re-registration detection | `is_reregistration`, `store_hashes`, `compute_hash` (`reregistration.py`), `ReRegistrationHash` model | One purpose-bound keyed HMAC-SHA256 of normalized email/phone, 24-month retention (`RETENTION = 730 days`); hashes captured automatically before erasure. Rows written around `store_hashes` are `scheme='unverified'`, never match, and are reported by `gdpr.E004` |
| Background workers | `tasks.py`: `run_data_export`, `sweep_pending_exports`, `process_expired_grace_periods`, `check_inactive_accounts`, `run_retention_cleanup`, `purge_expired_exports`, `sweep_deletion_deadlines`; `get_gdpr_beat_schedule()` | Celery tasks; host composes the beat schedule explicitly |
| Boot-time configuration checks | `checks.py` (`gdpr.E001/E002/W003/E004/W005/E006`), registered from `apps.ready()` | `manage.py check` fails on a missing/stale data-owner inventory or unverified hash rows, and warns on every open escape hatch |
| Inactivity closure | `check_inactive_accounts` | 12-month inactivity → closure (`trigger='inactivity'`); warning notifications at 60 and 14 days before |
| Microservices completion consumer | `manage.py consume_gdpr_completions` | Consumes `gdpr.export.completed` / `gdpr.delete.completed` bus events |

Public API (`stapel_gdpr.__all__`, lazily imported): `LegalHold`, `gdpr_orchestrator`, `gdpr_settings`, `is_reregistration`, `store_hashes`, `access_state`, `is_access_denied`, `data_owner_report`, `AccountClosureGuardMiddleware`. Everything else is internal.

## Extension points (fork-free)

### Settings — `STAPEL_GDPR` namespace (`conf.py`)

`gdpr_settings = AppSettings("STAPEL_GDPR", ...)` (`stapel_core.conf.AppSettings`). Resolution order per key: `settings.STAPEL_GDPR` dict → flat Django setting of the same name → environment variable → default. This module declares **no `import_strings`** keys — its dotted-path seams are the flat settings below.

| Key | Default | What it customizes |
|---|---|---|
| `DATA_OWNERS` | `[]` | **Mandatory inventory.** Every store holding personal data, as `"name"` or `{"name", "kind": "local"\|"remote", "timeout_hours"}`. One `AccountDeletionPart` per entry; an empty list is a `gdpr.E001` boot error and blocks every `deleted` status |
| `DATA_OWNERS_VERSION` | `""` | Stamped onto each closure (`registry_version`) so an audit can tell which inventory certified it. Bump whenever `DATA_OWNERS` changes |
| `OWNER_TIMEOUT_HOURS` | `24` | Default per-owner receipt deadline; `sweep_deletion_deadlines` flips overdue parts to `timeout`, which keeps blocking `deleted` |
| `SESSION_REVOKER` | `""` (auto-detect) | Dotted path to `revoke(user) -> None`. Unset: stapel-auth's `SessionService.revoke_all`, then a `user.sessions_revoked` subscriber, then a broker transport; with none of those, closure raises `SessionRevocationUnavailable` (HTTP 503) |
| `PRIMARY_IDENTITY_ERASURE` | `"anonymize"` | What happens to the `users.User` row itself: `anonymize` (scrub in place, keep the pk), `delete`, or a dotted path to `erase(user) -> None`. The result is verified — a strategy that leaves email/phone/username intact raises and the closure stays `deleting` |
| `DOWNLOAD_TTL_HOURS` | `24` | Lifetime of the single-use export download token; the archive is deleted on consume or expiry |
| `DOWNLOAD_URL_TEMPLATE` | `"{frontend_url}/privacy/export/#token={token}"` | Where the ready-notification points. Keep the token in the URL **fragment** — a query string lands in access logs, history and `Referer` |
| `REMOTE_DELETION_SERVICES` | `[]` | Legacy pre-registry list; folded into `DATA_OWNERS` as `kind='remote'` entries |
| `REREG_SALT` | `""` (falls back to `SECRET_KEY`) | Key for the purpose-bound re-registration HMAC. Set it once, before any hashes exist |
| `STAGING_ROOT` | `""` (→ `MEDIA_ROOT/gdpr/staging`) | Filesystem root for per-request export staging dirs |
| `ARCHIVE_ROOT` | `""` (→ `MEDIA_ROOT/gdpr/exports`) | Filesystem root for final export ZIP archives |
| `EXPORT_BUCKET_PREFIX` | `"gdpr/{correlation_id}/"` | Prefix a peer service's `bucket_path` must start with before this service opens the object and copies its bytes into a user's download. Templated over the export's own correlation id, so a peer can only name a key belonging to the export it was asked about. `""` accepts any key (`gdpr.W007`); traversal, absolute and URL-shaped keys are refused either way. Checked at ingest (`mark_part_ready`) **and** at open (`_download_bucket_parts`), so a row written around the orchestrator is not readable either |
| `ALLOW_ERASURE_WITHOUT_RECEIPTS` | `False` | Escape hatch: mark a closure `deleted` without a full set of receipts (recorded as `completeness_waived`, warned about by `gdpr.W003`) |
| `ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION` | `False` | Escape hatch: close accounts with no revocation seam, leaving pre-closure access tokens valid until they expire |

Flat Django settings this module also reads:

| Setting | Read in | Purpose |
|---|---|---|
| `GDPR_PROVIDERS` | `apps.py ready()` | **The dotted-path seam.** List of `GDPRProvider` class paths (e.g. `'stapel_auth.gdpr.AuthGDPRProvider'`) loaded with `import_string` and registered into `stapel_core.gdpr.gdpr_registry` — no compile-time dependency on any service package |
| `GDPR_COLLECTING_SERVICES` | `orchestrator._collecting_services()` | Microservices mode: explicit list of services expected to contribute export parts; falls back to `gdpr_registry.sections` (monolith) |
| `GDPR_STAGING_ROOT` / `GDPR_ARCHIVE_ROOT` | `orchestrator.py` | Legacy flat equivalents of `STAGING_ROOT` / `ARCHIVE_ROOT` |
| `FRONTEND_URL` | `orchestrator._build_download_url` | Base of the download URL in the export-ready notification |

### Deletion parts — how a module/app participates in account deletion

This is the key extension point. Three ways to participate, all fork-free:

1. **In-process provider (monolith).** Implement `stapel_core.gdpr.GDPRProvider` in your app (`section`, `export(user_id)`, `delete(user_id)`, `anonymize(user_id)`; override `export_to_staging` for binary files) and list its dotted path in `GDPR_PROVIDERS`. The orchestrator runs `anonymize()` then `delete()` for every registered provider during `execute_deletion`, and `export_to_staging()` during exports. A closure is only marked complete if every provider succeeded — a raised exception keeps it in `deleting` for retry. Declare the provider's `section` in `DATA_OWNERS` too: a registered provider the inventory does not list is a stale inventory (`gdpr.E002`) and blocks completeness.
2. **Comm subscriber + confirmation (any transport).** Subscribe `@on_action("user.deleted")` in your module, erase your slice, then `emit("gdpr.section.erased", {"user_id", "correlation_id", "service", "receipt_id"})` echoing the `correlation_id` from the `user.deleted` payload; `receipt_id` is your own durable proof (job id, tombstone id) and is stored on the part. Add your service name to `STAPEL_GDPR["DATA_OWNERS"]` (`kind='remote'`) so the orchestrator creates an `AccountDeletionPart` and waits for your confirmation (`actions.handle_section_erased` → `gdpr_orchestrator.mark_section_erased`). Handlers must be idempotent — delivery is at-least-once.
3. **Remote service (microservices).** Subclass `stapel_core.gdpr.GDPRServiceConsumerCommand` (set `gdpr_service_name`, implement `get_gdpr_provider()`); it consumes `gdpr.export.requested` / `gdpr.delete.requested`, uploads exports to object storage, and publishes `gdpr.export.completed` / `gdpr.delete.completed`. List the service in `GDPR_COLLECTING_SERVICES`. Export parts can alternatively be reported over HTTP: `POST internal/export/<request_id>/part-ready` (service auth required).

`LegalHold` is app-layer usable as-is: `LegalHold.objects.create(user_id=..., reason=..., created_by=...)` blocks closure/deletion; setting `released_at` releases it. `ReRegistrationHash` is written automatically before erasure; auth flows call `is_reregistration(email=..., phone=...)` at signup.

### Events & functions (comm surface)

Comm **Actions emitted** (transactional outbox, at-least-once; schemas in `schemas/emits/`):

| Action | Payload | When |
|---|---|---|
| `user.deletion_initiated` | `user_id`, `trigger` (`manual`\|`inactivity`\|`platform`), `grace_ends_at` | `initiate_closure` — grace period starts, account deactivated |
| `user.deleted` | `user_id`, `correlation_id`, `trigger` | `execute_deletion` — every module storing user data must subscribe and erase |
| `user.sessions_revoked` | `user_id`, `reason` | `initiate_closure` — a remote auth service must revoke its own sessions/access JTIs for this user |

Comm **Actions consumed** (`actions.py`):

| Action | Payload (schema-validated) | Handler |
|---|---|---|
| `gdpr.section.erased` | `user_id`, `correlation_id`, `service`, `receipt_id` (optional) | `handle_section_erased` → stores the receipt on the matching `AccountDeletionPart`, finalizes the closure when every declared owner has one |

Comm **Functions**: none provided, none called.

**Bus events** (microservices mode, constants in `stapel_core.gdpr`): publishes `gdpr.export.requested`; consumes `gdpr.export.completed` and `gdpr.delete.completed` via `manage.py consume_gdpr_completions`. (`_publish_delete_requested` for `gdpr.delete.requested` exists but is not on any current code path — deletion fan-out goes through the `user.deleted` comm action.)

**Notifications requested** (customize templates in the host's notifications setup, not here): `gdpr.export_ready`, `gdpr.inactivity_warning`, `gdpr.inactivity_closed` via `stapel_core.notifications.request_notification`.

### Swappable models

None. This module defines no swappable models and takes no FK to the user table — `user_id` is stored as a plain `UUIDField`, and the user is only touched through `django.contrib.auth.get_user_model()` (`lifecycle.set_active`, reading `email`/`phone`, and `lifecycle.erase_identity` at deletion time). Any `AUTH_USER_MODEL` with UUID primary keys works. A host whose user model carries extra personal fields erases them through `PRIMARY_IDENTITY_ERASURE` (dotted path to `erase(user)`) or its own provider — `anonymize` scrubs the framework fields only.

### Serializer seams

All views subclass `GDPRAPIView` (`views.py`), which exposes `request_serializer_class` / `response_serializer_class` class attributes plus `get_request_serializer_class()` / `get_response_serializer_class()` getters. To change a response envelope: subclass the view, swap the class attribute (or override the getter) with your own `StapelDataclassSerializer` over an extended DTO, and mount your subclass in the host project's `urls.py` instead of including `stapel_gdpr.urls`. URL wiring is host-owned; `permission_classes` are ordinary DRF attributes overridable the same way. Serializers are `StapelDataclassSerializer`s over the dataclass DTOs in `dto.py` (`ExportRequestDTO`, `ExportStatusDTO`, `ClosureStatusDTO`).

### Middleware (host-wired)

`stapel_gdpr.guards.AccountClosureGuardMiddleware` refuses every authenticated request of an account in `deleting`/`deleted` with `error.403.gdpr.account_closed`, fleet-wide rather than on this module's endpoints only. Add it to `MIDDLEWARE` **after** the authentication middleware; it costs one indexed query per authenticated request. Grace is deliberately allowed through — cancelling a closure requires logging in. Views that only need the same rule locally use the DRF permission `guards.AccountNotClosed`, which every view in this module already carries.

### Signals

This module defines and sends **no Django signals**. Business milestones travel as comm Actions (table above). In-process hooks for the host project belong to `stapel_core.signals` (none of which are GDPR-specific today); adding a GDPR signal is an upstream contribution.

### Admin categories (`stapel_core.access`)

`@access.ops` (admin-suite AS-5): `DataExportRequest`, `DataExportPart`, `AccountClosureRequest`, `AccountDeletionPart`, `ReRegistrationHash`. Every one of these is a state machine mutated exclusively by `GDPROrchestrator` (or, for `ReRegistrationHash`, `reregistration.store_hashes` / the retention-cleanup task) — there is no staff-facing review/approve/override action anywhere in `views.py` or `admin.py`. Closure cancellation is user-initiated only (`AccountCancelCloseView`, keyed off the authenticated requester, not a staff action). MODULE.md already documented the anti-pattern above: "Do not flip `AccountClosureRequest.status` or `AccountDeletionPart` rows directly" — `@access.ops` now enforces that at the admin layer (read-only, including for a superuser) instead of only in prose. `ReRegistrationHash` is a dedup/TTL-expiring record (24-month retention, cleaned up by `run_retention_cleanup`), not a credential — `ops`, not `secret` — but `hash_value` is still a hash of PII, so `ReRegistrationHashAdmin.secret_fields = ('hash_value',)` masks it explicitly regardless of category (the same pattern `stapel-core` uses for `session_key`/`session_data` on the `ops`-categorized `Session` admin).

`LegalHold` is left undecorated (implicit `business`): placing a hold and releasing it (`released_at`) is a real, expected staff/compliance workflow through `LegalHoldAdmin` — see "Placing/releasing legal holds → `LegalHold` ORM/admin" above.

## Anti-patterns (tailored)

- **Do not fork to add a data section to export/deletion.** Implement a `GDPRProvider` in *your* app and list it in `GDPR_PROVIDERS`, or subscribe to `user.deleted` + confirm with `gdpr.section.erased`.
- **Do not import `stapel_gdpr` from another stapel module** (models, orchestrator, anything). Modules never import each other — participate via comm actions and the `stapel_core.gdpr` primitives. (Host *projects* may import the public API: `gdpr_orchestrator`, `LegalHold`, `is_reregistration`, `store_hashes`, `gdpr_settings`.)
- **Do not flip `AccountClosureRequest.status` or `AccountDeletionPart` rows directly.** Remote completion is confirmed only by emitting `gdpr.section.erased` with the closure's `correlation_id`; finalization logic (`_maybe_finalize`) owns the state machine.
- **Do not erase data on `user.deletion_initiated`.** The grace period can be cancelled; hard-delete only on `user.deleted`. `deletion_initiated` is for reversible reactions (suppress notifications, hide content).
- **Do not write non-idempotent action handlers.** Delivery is at-least-once; every handler must tolerate redelivery.
- **Do not bypass `LegalHold`.** Both `initiate_closure` and `execute_deletion` raise `ValueError('legal_hold')`; scripted deletions must go through the orchestrator, not raw model deletes.
- **Do not keep cleartext email/phone after erasure** and do not roll your own re-registration checks — use `store_hashes` / `is_reregistration`. Writing rows into `ReRegistrationHash` yourself (a bare `sha256(email)` is dictionary-recoverable) lands them as `scheme='unverified'`: never matched, reported by `gdpr.E004`, and purged by `manage.py gdpr_purge_unverified_hashes`.
- **Do not change `REREG_SALT` once hashes exist** — it silently invalidates every stored re-registration hash.
- **Do not treat `is_active` as "is this account closed?"** — it is a plain boolean any JWT-to-DB user sync can write back. Ask `stapel_gdpr.is_access_denied(user_id)` / `access_state(user_id)`, which read the closure row.
- **Do not deactivate a user with `QuerySet.update()`** — raw SQL fires no `pre_save`/`post_save`, so activation observers never run and the deactivation propagates nowhere. `lifecycle.set_active` writes through the instance.
- **Do not put the export download token in a URL** (query string or path): it lands in access logs, browser history and `Referer`. It travels in the POST body, is spent once, and dies with the archive.
- **Do not rely on a `user.deletion_cancelled` event — it does not exist** (see limitation below).

## Known limitation

There is **no `user.deletion_cancelled` comm action**. `cancel_closure()` reactivates the local user (`is_active=True`) and updates the closure row, but emits nothing — consumers that reacted to `user.deletion_initiated` (e.g. stapel-notifications deactivating a user's contacts) are not told about the cancellation and only recover on their next sync with the source of truth. Design consumer reactions to `user.deletion_initiated` to be self-healing. Adding the event is an upstream contribution.

## App-layer override vs upstream contribution — rule of thumb

**App-layer (no fork, do it in the host project):**
- Anything reachable via `STAPEL_GDPR` keys or the flat settings above (data-owner inventory, revocation and identity-erasure seams, download TTL/URL template, key, staging/archive roots, providers, collecting services).
- Adding your app's data to export/deletion → `GDPRProvider` + `GDPR_PROVIDERS`, or `user.deleted` subscriber + `gdpr.section.erased`; either way the owner belongs in `DATA_OWNERS`.
- Refusing closed accounts outside this module's endpoints → add `guards.AccountClosureGuardMiddleware` to `MIDDLEWARE`.
- Reacting to closures/deletions → `@on_action` subscribers in your own app.
- Changing API envelopes, permissions, or routes → subclass views (serializer seams), own `urls.py`.
- Re-scheduling workers → compose your own `CELERY_BEAT_SCHEDULE` instead of `get_gdpr_beat_schedule()`.
- Placing/releasing legal holds → `LegalHold` ORM/admin.

**Upstream contribution (change stapel-gdpr itself):**
- New emitted events (e.g. `user.deletion_cancelled`, an actual `user.export_ready` action) or payload/schema changes.
- Making hardcoded policy constants configurable: 30-day grace period (`models.py`), 30-day export cooldown and 24 h export deadline (`orchestrator.py`), 24-month hash retention (`reregistration.py`), 12-month/60-day/14-day inactivity thresholds (`tasks.py`).
- New orchestrator states, model fields, or migrations; new settings keys; new `import_strings` seams; Django signals.
- If the customization requires monkey-patching, editing this package's code, or touching its models' state machine — it is upstream, not app-layer.
