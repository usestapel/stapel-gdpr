# stapel-gdpr — MODULE.md

Agent-facing map of this module: what it provides, where it can be extended **without forking**, and what not to do. Use it to classify a desired change as *app-layer override via an extension point* vs *upstream contribution* (see `docs/stdlib-contribution-pipeline.md` and system-design §8.6 in the stapel workspace).

Stapel ground rules apply: modules never import each other; all cross-module communication goes through `stapel_core.comm` (Actions/Functions) or shared primitives in `stapel_core`; all customization must be possible from the host project layer.

## What this module provides

| Capability | Entry points | Notes |
|---|---|---|
| Data export (GDPR Art. 15/20) | `POST/GET user/data-export/{request,status,download}` (`urls.py`), `GDPROrchestrator.request_export/run_export` | Fan-out to all registered sections; 24 h assembly deadline (partial archive after that, swept hourly), download link valid 7 days, 30-day cooldown per user |
| Account closure & deletion (Art. 17) | `POST user/account/{close,cancel-close}`, `GET user/account/close/status`, `GDPROrchestrator.initiate_closure/cancel_closure/execute_deletion` | 30-day grace period, account deactivated immediately; deletion = local `GDPRProvider`s + `user.deleted` comm fan-out + per-service remote confirmations |
| Deletion completion tracking | `AccountClosureRequest` + `AccountDeletionPart` (`models.py`) | Closure flips to `deleted` only when `local_erasure_done` **and** every `AccountDeletionPart` is `done` (vacuously true with no remote services) |
| Legal hold (Art. 17(3)) | `LegalHold` model, `LegalHold.is_held(user_id)`; Django admin | Blocks `initiate_closure` and `execute_deletion`; held users skipped by grace-period worker and retention cleanup |
| Re-registration detection | `is_reregistration`, `store_hashes`, `compute_hash` (`reregistration.py`), `ReRegistrationHash` model | Salted SHA-256 of normalized email/phone, 24-month retention (`RETENTION = 730 days`); hashes captured automatically before erasure |
| Background workers | `tasks.py`: `run_data_export`, `sweep_pending_exports`, `process_expired_grace_periods`, `check_inactive_accounts`, `run_retention_cleanup`; `get_gdpr_beat_schedule()` | Celery tasks; host composes the beat schedule explicitly |
| Inactivity closure | `check_inactive_accounts` | 12-month inactivity → closure (`trigger='inactivity'`); warning notifications at 60 and 14 days before |
| Microservices completion consumer | `manage.py consume_gdpr_completions` | Consumes `gdpr.export.completed` / `gdpr.delete.completed` bus events |

Public API (`stapel_gdpr.__all__`, lazily imported): `LegalHold`, `gdpr_orchestrator`, `gdpr_settings`, `is_reregistration`, `store_hashes`. Everything else is internal.

## Extension points (fork-free)

### Settings — `STAPEL_GDPR` namespace (`conf.py`)

`gdpr_settings = AppSettings("STAPEL_GDPR", ...)` (`stapel_core.conf.AppSettings`). Resolution order per key: `settings.STAPEL_GDPR` dict → flat Django setting of the same name → environment variable → default. This module declares **no `import_strings`** keys — its dotted-path seams are the flat settings below.

| Key | Default | What it customizes |
|---|---|---|
| `REMOTE_DELETION_SERVICES` | `[]` | Service names that must confirm erasure via `gdpr.section.erased` before a closure is marked `deleted`; one `AccountDeletionPart` is created per entry in `execute_deletion` |
| `REREG_SALT` | `""` (falls back to `SECRET_KEY`) | Salt for re-registration hashes. Set it once, before any hashes exist |
| `STAGING_ROOT` | `""` (→ `MEDIA_ROOT/gdpr/staging`) | Filesystem root for per-request export staging dirs |
| `ARCHIVE_ROOT` | `""` (→ `MEDIA_ROOT/gdpr/exports`) | Filesystem root for final export ZIP archives |

Flat Django settings this module also reads:

| Setting | Read in | Purpose |
|---|---|---|
| `GDPR_PROVIDERS` | `apps.py ready()` | **The dotted-path seam.** List of `GDPRProvider` class paths (e.g. `'stapel_auth.gdpr.AuthGDPRProvider'`) loaded with `import_string` and registered into `stapel_core.gdpr.gdpr_registry` — no compile-time dependency on any service package |
| `GDPR_COLLECTING_SERVICES` | `orchestrator._collecting_services()` | Microservices mode: explicit list of services expected to contribute export parts; falls back to `gdpr_registry.sections` (monolith) |
| `GDPR_STAGING_ROOT` / `GDPR_ARCHIVE_ROOT` | `orchestrator.py` | Legacy flat equivalents of `STAGING_ROOT` / `ARCHIVE_ROOT` |
| `FRONTEND_URL` | `orchestrator._build_download_url` | Base of the download URL in the export-ready notification |

### Deletion parts — how a module/app participates in account deletion

This is the key extension point. Three ways to participate, all fork-free:

1. **In-process provider (monolith).** Implement `stapel_core.gdpr.GDPRProvider` in your app (`section`, `export(user_id)`, `delete(user_id)`, `anonymize(user_id)`; override `export_to_staging` for binary files) and list its dotted path in `GDPR_PROVIDERS`. The orchestrator runs `anonymize()` then `delete()` for every registered provider during `execute_deletion`, and `export_to_staging()` during exports. A closure is only marked complete if every provider succeeded — a raised exception keeps it in `deleting` for retry.
2. **Comm subscriber + confirmation (any transport).** Subscribe `@on_action("user.deleted")` in your module, erase your slice, then `emit("gdpr.section.erased", {"user_id", "correlation_id", "service"})` echoing the `correlation_id` from the `user.deleted` payload. Add your service name to `STAPEL_GDPR["REMOTE_DELETION_SERVICES"]` so the orchestrator creates an `AccountDeletionPart` and waits for your confirmation (`actions.handle_section_erased` → `gdpr_orchestrator.mark_section_erased`). Handlers must be idempotent — delivery is at-least-once.
3. **Remote service (microservices).** Subclass `stapel_core.gdpr.GDPRServiceConsumerCommand` (set `gdpr_service_name`, implement `get_gdpr_provider()`); it consumes `gdpr.export.requested` / `gdpr.delete.requested`, uploads exports to object storage, and publishes `gdpr.export.completed` / `gdpr.delete.completed`. List the service in `GDPR_COLLECTING_SERVICES`. Export parts can alternatively be reported over HTTP: `POST internal/export/<request_id>/part-ready` (service auth required).

`LegalHold` is app-layer usable as-is: `LegalHold.objects.create(user_id=..., reason=..., created_by=...)` blocks closure/deletion; setting `released_at` releases it. `ReRegistrationHash` is written automatically before erasure; auth flows call `is_reregistration(email=..., phone=...)` at signup.

### Events & functions (comm surface)

Comm **Actions emitted** (transactional outbox, at-least-once; schemas in `schemas/emits/`):

| Action | Payload | When |
|---|---|---|
| `user.deletion_initiated` | `user_id`, `trigger` (`manual`\|`inactivity`\|`platform`), `grace_ends_at` | `initiate_closure` — grace period starts, account deactivated |
| `user.deleted` | `user_id`, `correlation_id`, `trigger` | `execute_deletion` — every module storing user data must subscribe and erase |

Comm **Actions consumed** (`actions.py`):

| Action | Payload (schema-validated) | Handler |
|---|---|---|
| `gdpr.section.erased` | `user_id`, `correlation_id`, `service` | `handle_section_erased` → marks the matching `AccountDeletionPart` done, finalizes the closure when all parts are done |

Comm **Functions**: none provided, none called.

**Bus events** (microservices mode, constants in `stapel_core.gdpr`): publishes `gdpr.export.requested`; consumes `gdpr.export.completed` and `gdpr.delete.completed` via `manage.py consume_gdpr_completions`. (`_publish_delete_requested` for `gdpr.delete.requested` exists but is not on any current code path — deletion fan-out goes through the `user.deleted` comm action.)

**Notifications requested** (customize templates in the host's notifications setup, not here): `gdpr.export_ready`, `gdpr.inactivity_warning`, `gdpr.inactivity_closed` via `stapel_core.notifications.request_notification`.

### Swappable models

None. This module defines no swappable models and takes no FK to the user table — `user_id` is stored as a plain `UUIDField`, and the user is only touched through `django.contrib.auth.get_user_model()` (deactivate/reactivate `is_active`, read `email`/`phone`). Any `AUTH_USER_MODEL` with UUID primary keys works; no override hook is needed or provided.

### Serializer seams

All views subclass `GDPRAPIView` (`views.py`), which exposes `request_serializer_class` / `response_serializer_class` class attributes plus `get_request_serializer_class()` / `get_response_serializer_class()` getters. To change a response envelope: subclass the view, swap the class attribute (or override the getter) with your own `StapelDataclassSerializer` over an extended DTO, and mount your subclass in the host project's `urls.py` instead of including `stapel_gdpr.urls`. URL wiring is host-owned; `permission_classes` are ordinary DRF attributes overridable the same way. Serializers are `StapelDataclassSerializer`s over the dataclass DTOs in `dto.py` (`ExportRequestDTO`, `ExportStatusDTO`, `ClosureStatusDTO`).

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
- **Do not keep cleartext email/phone after erasure** and do not roll your own re-registration checks — use `store_hashes` / `is_reregistration`.
- **Do not change `REREG_SALT` once hashes exist** — it silently invalidates every stored re-registration hash.
- **Do not rely on a `user.deletion_cancelled` event — it does not exist** (see limitation below).

## Known limitation

There is **no `user.deletion_cancelled` comm action**. `cancel_closure()` reactivates the local user (`is_active=True`) and updates the closure row, but emits nothing — consumers that reacted to `user.deletion_initiated` (e.g. stapel-notifications deactivating a user's contacts) are not told about the cancellation and only recover on their next sync with the source of truth. Design consumer reactions to `user.deletion_initiated` to be self-healing. Adding the event is an upstream contribution. Related: `schemas/emits/user.export_ready.json` exists but no `user.export_ready` action is currently emitted — export readiness is delivered via the `gdpr.export_ready` notification instead.

## App-layer override vs upstream contribution — rule of thumb

**App-layer (no fork, do it in the host project):**
- Anything reachable via `STAPEL_GDPR` keys or the flat settings above (remote services list, salt, staging/archive roots, providers, collecting services).
- Adding your app's data to export/deletion → `GDPRProvider` + `GDPR_PROVIDERS`, or `user.deleted` subscriber + `gdpr.section.erased` + `REMOTE_DELETION_SERVICES`.
- Reacting to closures/deletions → `@on_action` subscribers in your own app.
- Changing API envelopes, permissions, or routes → subclass views (serializer seams), own `urls.py`.
- Re-scheduling workers → compose your own `CELERY_BEAT_SCHEDULE` instead of `get_gdpr_beat_schedule()`.
- Placing/releasing legal holds → `LegalHold` ORM/admin.

**Upstream contribution (change stapel-gdpr itself):**
- New emitted events (e.g. `user.deletion_cancelled`, an actual `user.export_ready` action) or payload/schema changes.
- Making hardcoded policy constants configurable: 30-day grace period (`models.py`), 30-day export cooldown and 24 h export deadline (`orchestrator.py`), 7-day download validity, 24-month hash retention (`reregistration.py`), 12-month/60-day/14-day inactivity thresholds (`tasks.py`).
- New orchestrator states, model fields, or migrations; new settings keys; new `import_strings` seams; Django signals.
- If the customization requires monkey-patching, editing this package's code, or touching its models' state machine — it is upstream, not app-layer.
