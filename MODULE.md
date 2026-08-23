# stapel-gdpr — MODULE.md

Agent-facing map of this module: what it provides, where it can be extended **without forking**, and what not to do. Use it to classify a desired change as *app-layer override via an extension point* vs *upstream contribution* (see `docs/stdlib-contribution-pipeline.md` and system-design §8.6 in the stapel workspace).

Stapel ground rules apply: modules never import each other; all cross-module communication goes through `stapel_core.comm` (Actions/Functions) or shared primitives in `stapel_core`; all customization must be possible from the host project layer.

## What this module provides

| Capability | Entry points | Notes |
|---|---|---|
| Data export (GDPR Art. 15/20) | `POST user/data-export/{request,status,download}` (`urls.py`), `GDPROrchestrator.request_export/run_export` | Fan-out to all declared data owners; 24 h assembly deadline (partial archive after that, swept hourly), single-use download token (`DOWNLOAD_TTL_HOURS`, POST body only, archive deleted on use), 30-day cooldown per user |
| Account closure & deletion (Art. 17) | `POST user/account/{close,cancel-close}`, `GET user/account/close/status`, `GDPROrchestrator.initiate_closure/cancel_closure/execute_deletion` | 30-day grace period; the account is deactivated **through the model** (observers fire) and all sessions are revoked, or the closure is refused; deletion = local `GDPRProvider`s + erasure of the primary `users.User` row + `user.deleted` comm fan-out + per-owner receipts |
| Server-side closed-account gate | `guards.AccountNotClosed` (on every view here), `guards.AccountClosureGuardMiddleware` (host-wired), `lifecycle.access_state` | Reads the closure row, never `is_active` — a token that syncs `is_active=true` back into the user table cannot reopen an erasing account |
| Subject-scoped erasure (Art. 17) | `POST erasures`, `GET erasures/{id}`, `GET me/erasures`, `GDPROrchestrator.request_erasure` | The account's machine, generalized: a `workspace`/`meeting`/`recording`/`document`/`file` gets the same purge SLA (`ERASURE_SLA_DAYS`), the same one-receipt-per-owner ledger and the same refusal to self-certify. No grace — the host already removed it from the UI. Authorization is the host's `ERASURE_AUTHORIZER` |
| Erasure intake from another service | `gdpr.erasure.open` (Action), `gdpr.erasure.request` (Function), `stapel_gdpr.client.request_erasure` / `CommErasureClient` | `request_erasure` is an in-process call, and the owner that detects the need (a retention purge, a delete view in another container) cannot reach it. The client helper picks the Function when a comm transport is configured and the orchestrator otherwise, so an owner library points one dotted path at it and works in both deployments. Idempotent on a caller-supplied `idempotency_key`: at-least-once delivery must not mint two erasures for one subject |
| Erasure completeness | `owners.data_owner_report()`, `ErasurePart.receipt_id/deadline`, `checks.py` | An erasure flips to `deleted` only when every owner **claiming that subject type** returned a durable receipt against a registry with no missing/undeclared owners (and, for an account, the primary identity is gone). Silence times out, keeps blocking, and emits `gdpr.erasure.timeout` |
| Data-owner liveness | `probe_data_owners` (daily), `DataOwnerHealth`, `GET owners/health`, `gdpr.W006` | Owners answer `gdpr.owner.alive` from the *same* subscriber that erases, so an answer proves the erasure path is consumed rather than that a container is deployed. A silent owner is named at boot instead of at the first missed deadline |
| DSAR intake (Art. 12) | `POST/GET dsar`, `PATCH dsar/{id}`, `dsar.create_dsar`, `sweep_dsar_deadlines`, `gdpr.W008` | Authenticated and anonymous-with-captcha intake, automated acknowledgement inside the request (`ack_sent_at` is proof, not an assumption), both statutory clocks, and a wiring step handing erasure to `initiate_closure` and access/portability to `request_export` |
| Subprocessor ledger | `STAPEL_GDPR["SUBPROCESSORS"]`, `SubprocessorObligation`, `subprocessors.record_subprocessor_obligations`, `ErasureRequest.fully_erased_by` | One row per processor per erasure with the date its contractual window closes, so "erased from our systems on X; from every processor by Y" is two queryable dates |
| Backup restore re-queue | `manage.py gdpr_requeue_after_restore --restored-from <iso>` | The one operation that silently undoes a completed erasure. Idempotent by construction: the clone FKs its source and `(origin, source_request)` is unique |
| Legal hold (Art. 17(3)) | `LegalHold` model, `LegalHold.is_held(user_id)`; Django admin | Blocks `initiate_closure` and `execute_deletion`; held users skipped by grace-period worker and retention cleanup |
| Re-registration detection | `is_reregistration`, `store_hashes`, `compute_hash` (`reregistration.py`), `ReRegistrationHash` model | One purpose-bound keyed HMAC-SHA256 of normalized email/phone, 24-month retention (`RETENTION = 730 days`); hashes captured automatically before erasure. Rows written around `store_hashes` are `scheme='unverified'`, never match, and are reported by `gdpr.E004` |
| Background workers | `tasks.py`: `run_data_export`, `sweep_pending_exports`, `process_expired_grace_periods`, `check_inactive_accounts`, `run_retention_cleanup`, `purge_expired_exports`, `sweep_deletion_deadlines`, `probe_data_owners`, `sweep_dsar_deadlines`; `get_gdpr_beat_schedule()` | Celery tasks; host composes the beat schedule explicitly |
| Boot-time configuration checks | `checks.py` (`gdpr.E001/E002/W003/E004/W005/E006/W006/W007/W008`), registered from `apps.ready()` | `manage.py check` fails on a missing/stale data-owner inventory or unverified hash rows, and warns on every open escape hatch, every owner that stopped answering probes (`W006`) and every unacknowledged data-subject request (`W008`) |
| Inactivity closure | `check_inactive_accounts` | 12-month inactivity → closure (`trigger='inactivity'`); warning notifications at 60 and 14 days before |
| Microservices completion consumer | `manage.py consume_gdpr_completions` | Consumes `gdpr.export.completed` / `gdpr.delete.completed` bus events |

Public API (`stapel_gdpr.__all__`, lazily imported): `LegalHold`, `gdpr_orchestrator`, `gdpr_settings`, `is_reregistration`, `store_hashes`, `access_state`, `is_access_denied`, `data_owner_report`, `AccountClosureGuardMiddleware`. Everything else is internal.

## Extension points (fork-free)

### Settings — `STAPEL_GDPR` namespace (`conf.py`)

`gdpr_settings = AppSettings("STAPEL_GDPR", ...)` (`stapel_core.conf.AppSettings`). Resolution order per key: `settings.STAPEL_GDPR` dict → flat Django setting of the same name → environment variable → default. This module declares **no `import_strings`** keys — its dotted-path seams are the flat settings below.

| Key | Default | What it customizes |
|---|---|---|
| `DATA_OWNERS` | `[]` | **Mandatory inventory.** A map owner → subject types: `{"recordings": ["account", "workspace", "recording"]}`; a value may be a full spec (`{"subject_types", "kind": "local"\|"remote", "timeout_hours"}`). A plain list is still accepted and means `["account"]` for every entry. One `ErasurePart` per owner **claiming the subject**; an empty inventory is a `gdpr.E001` boot error and blocks every `deleted` state |
| `SUBJECT_TYPES` | `["account", "workspace", "meeting", "recording", "document", "file"]` | What an erasure may be opened for. `POST /erasures` refuses anything else, so a typo'd subject cannot become a request no owner can answer |
| `ERASURE_SLA_DAYS` | `30` | Purge SLA: `due_at = requested_at + this`. Accounts keep their cancellable grace on top; entities have none |
| `OWNER_ALIVE_MAX_AGE_HOURS` | `48` | How long a declared owner may go without answering `gdpr.owner.probe` before `gdpr.W006` names it at boot |
| `ERASURE_AUTHORIZER` | `""` (staff only) | Dotted path to `authorize(request, subject_type, subject_key) -> bool` for `POST /erasures`. Only the host knows whether this user owns that recording; an authorizer that cannot be imported, or that raises, refuses |
| `SUBPROCESSORS` | `[]` | Processors and their contractual deletion windows: `[{"name": "openai", "window_days": 30}]`. One `SubprocessorObligation` per entry when an erasure completes; `fully_erased_by` is the max of our `due_at` and theirs |
| `DSAR_STAFF_EMAILS` | `[]` | Where `gdpr.dsar.opened` goes. Empty means nobody is told a request arrived, which is how a 30-day statutory clock is missed |
| `DATA_OWNERS_VERSION` | `""` | Stamped onto each closure (`registry_version`) so an audit can tell which inventory certified it. Bump whenever `DATA_OWNERS` changes |
| `OWNER_TIMEOUT_HOURS` | `24` | Default per-owner receipt deadline; `sweep_deletion_deadlines` flips overdue parts to `timeout`, which keeps blocking `deleted` and emits `gdpr.erasure.timeout` |
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

### Erasure parts — how a module/app participates in erasure

This is the key extension point, and since 0.5.0 it is scoped by **subject**,
not fixed to the account.

**Declare what you own, and about whom.** `STAPEL_GDPR["DATA_OWNERS"]` is a map
from owner name to the subject types that owner holds data about:

```python
STAPEL_GDPR = {
    "DATA_OWNERS": {
        "recordings": ["account", "workspace", "meeting", "recording"],
        "docs":       ["account", "workspace", "document"],
        "media":      {"subject_types": ["account", "workspace", "file", "recording"],
                       "kind": "remote", "timeout_hours": 6},
        "billing":    ["account"],
    },
    "DATA_OWNERS_VERSION": "2026-08-23.1",
}
```

An `ErasureRequest` creates one `ErasurePart` per owner that claims its
`subject_type`, so a recording erasure waits for `recordings` and `media` and
never blocks on `billing`. A plain list (`["auth", "profiles"]`) is still
accepted and means `["account"]` for every entry — nothing breaks on the bump.

Three ways to participate, all fork-free:

1. **In-process provider (monolith).** Implement `stapel_core.gdpr.GDPRProvider` in your app (`section`, `export(user_id)`, `delete(user_id)`, `anonymize(user_id)`; override `export_to_staging` for binary files) and list its dotted path in `GDPR_PROVIDERS`. The orchestrator runs `anonymize()` then `delete()` for every registered provider during an account erasure, and `export_to_staging()` during exports. An erasure is only marked complete if every provider succeeded — a raised exception keeps it in `erasing` for retry. Declare the provider's `section` in `DATA_OWNERS` too: a registered provider the inventory does not list is a stale inventory (`gdpr.E002`) and blocks completeness. In-process providers are account-scoped by construction; an owner that also holds entity data participates through (2).
2. **Comm subscriber + confirmation (any transport).** Subscribe `@on_action("gdpr.erasure.requested")` in your module, erase the slice named by `subject_type`/`subject_key` (plus `workspace_id` where you partition by it), then `emit("gdpr.section.erased", {"correlation_id", "owner", "subject_type", "subject_key", "receipt_id", "counts"})` echoing the `correlation_id` you received. `receipt_id` is your own durable proof (job id, tombstone id) and is stored on the part; `counts` is what you actually removed. **Answer `gdpr.owner.probe` with `gdpr.owner.alive {owner, subject_types}` from the same subscriber** — that is what makes "alive" evidence the erasure path is consumed rather than that a container is running, and it is what `gdpr.W006` and `GET /owners/health` read. Handlers must be idempotent — delivery is at-least-once. (`user.deleted` still fires for account erasures until 0.6.0; new subscribers should use `gdpr.erasure.requested`.)
3. **Remote service (microservices).** Subclass `stapel_core.gdpr.GDPRServiceConsumerCommand` (set `gdpr_service_name`, implement `get_gdpr_provider()`); it consumes `gdpr.export.requested` / `gdpr.delete.requested`, uploads exports to object storage, and publishes `gdpr.export.completed` / `gdpr.delete.completed`. List the service in `GDPR_COLLECTING_SERVICES`. Export parts can alternatively be reported over HTTP: `POST internal/export/<request_id>/part-ready` (service auth required).

**Asking for an entity erasure.** After your own soft-delete succeeds, call
`POST /gdpr/api/v1/erasures {subject_type, subject_key, workspace_id?}` (or
`gdpr_orchestrator.request_erasure(...)` in-process). The endpoint erases
whatever the caller names, so it consults `ERASURE_AUTHORIZER` — a dotted path
to `authorize(request, subject_type, subject_key) -> bool`. The default is
staff-only and a broken authorizer refuses: only the host knows whether this
user owns that recording, and an ownership check that fails open is worse than
none.

**After a backup restore**, run
`manage.py gdpr_requeue_after_restore --restored-from <the backup's ISO
timestamp>`. This is the one operation that silently undoes a completed
erasure, and the command is the whole runbook: it clones every erasure that
completed inside the restored window as `origin="restore_requeue"` and
re-dispatches it. Idempotent — a repeated or overlapping run writes nothing.

`LegalHold` is app-layer usable as-is: `LegalHold.objects.create(user_id=..., reason=..., created_by=...)` blocks closure/deletion; setting `released_at` releases it. `ReRegistrationHash` is written automatically before erasure; auth flows call `is_reregistration(email=..., phone=...)` at signup.

### Events & functions (comm surface)

Comm **Actions emitted** (transactional outbox, at-least-once; schemas in `schemas/emits/`):

| Action | Payload | When |
|---|---|---|
| `user.deletion_initiated` | `user_id`, `trigger` (`manual`\|`inactivity`\|`platform`), `grace_ends_at` | `initiate_closure` — grace period starts, account deactivated |
| `gdpr.erasure.requested` | `request_id`, `correlation_id`, `subject_type`, `subject_key`, `workspace_id`, `requested_by`, `origin`, `due_at` | Every new erasure, account included — the action owner libraries subscribe to |
| `gdpr.erasure.timeout` | `request_id`, `correlation_id`, `subject_type`, `subject_key`, `owners`, `due_at` | `sweep_deletion_deadlines` — at least one owner never confirmed, so the request cannot be certified. Subscribe to alert |
| `gdpr.owner.probe` | `correlation_id` | `probe_data_owners` (daily) — answer with `gdpr.owner.alive` from your erasure subscriber |
| `gdpr.dsar.overdue` | `dsar_id`, `kind`, `channel`, `state`, `deadline`, `due_at`, `received_at` | `sweep_dsar_deadlines` — a statutory clock was missed, once per deadline per request |
| `user.deleted` | `user_id`, `correlation_id`, `trigger` | `execute_deletion` — **deprecated since 0.5.0, removed in 0.6.0**: use `gdpr.erasure.requested`, which carries the subject pair this cannot express |
| `user.sessions_revoked` | `user_id`, `reason` | `initiate_closure` — a remote auth service must revoke its own sessions/access JTIs for this user |

Comm **Actions consumed** (`actions.py`):

| Action | Payload (schema-validated) | Handler |
|---|---|---|
| `gdpr.section.erased` | `correlation_id`, `owner` (or the older `service`), `subject_type`, `subject_key`, `receipt_id`, `counts` (all optional but `correlation_id`) | `handle_section_erased` → stores the receipt on the matching `ErasurePart`, finalizes the request when every claiming owner has one |
| `gdpr.owner.alive` | `owner`, `subject_types`, `correlation_id` (optional) | `handle_owner_alive` → stamps `DataOwnerHealth`, which `gdpr.W006` and `GET owners/health` read |
| `gdpr.erasure.open` | `subject_type`, `subject_key`, `workspace_id?`, `requested_by?`, `origin?`, `idempotency_key?` (schema in `schemas/consumes/`) | `handle_erasure_open` → `request_erasure`. Fire-and-forget intake for a service that cannot import the orchestrator. Idempotent on `idempotency_key`; a malformed or unknown-subject payload is logged and dropped, never retried forever |

Comm **Functions provided** (`functions.py`, schemas in `schemas/functions/`):

| Function | Payload | Answers |
|---|---|---|
| `gdpr.erasure.request` | Same as `gdpr.erasure.open` | `{request_id, due_at, state}` — the synchronous door, for a caller that must record the id or show a deadline right away. Same idempotency: a repeat of the key answers with the request that already exists |

Comm **Functions called**: none.

### Opening an erasure from another service

`gdpr_orchestrator.request_erasure(...)` is an **in-process** call. In a fleet
the owner that detects the need is almost never the service running this
module — stapel-recordings' `purge_soft_deleted_recordings`, a host's delete
view in another container — and "import the orchestrator" has no remote form:
it works in the monolith and fails the day the two are split, which is exactly
when an owner starts deleting its own rows outside the receipts ledger.

Since 0.5.1 there are three doors, and an owner library needs to know about
only the last one:

| Door | Reach for it when |
|---|---|
| `gdpr.erasure.open` (Action) | You only need the erasure to happen. `emit("gdpr.erasure.open", {...})` and carry on |
| `gdpr.erasure.request` (Function) | You need `request_id` / `due_at` back — to link your own row to the erasure, or to render "pending deletion until X" immediately |
| `stapel_gdpr.client.request_erasure(...)` | Always, from library code. It picks between the two by deployment so the calling module has no transport opinion at all |

```python
from stapel_gdpr.client import request_erasure

result = request_erasure(
    "recording", str(recording.id),
    workspace_id=str(recording.workspace_id),
    idempotency_key=f"purge:recording:{recording.id}",
)
result["request_id"], result["due_at"], result["state"]
```

The rule: **the Function when `STAPEL_COMM["FUNCTION_TRANSPORT"]` is
configured, the in-process orchestrator otherwise.** At the default
`"inprocess"` there is no RPC to make — either this process has the
orchestrator or nobody does.

**Always pass `idempotency_key` from anything that can ask twice** (a retry, a
daily sweep, an at-least-once redelivery). Same key = same request: the row
that already exists comes back, with no second set of receipt slots and no
second `gdpr.erasure.requested` restarting every owner's deadline clock. An
un-keyed retry is a second erasure of one subject, and one of the two will
never be completed by anybody. The key is unique across erasures, so scope it
to the caller (`"purge:recording:<id>"`, not `"<id>"`).

**Wiring an owner library's seam.** An owner that exposes a dotted-path
erasure-client seam — stapel-recordings' `STAPEL_RECORDINGS["ERASURE_CLIENT"]`
is the reference shape — points it at:

```python
STAPEL_RECORDINGS = {"ERASURE_CLIENT": "stapel_gdpr.client.CommErasureClient"}
```

`CommErasureClient` is duck-typed against that seam (`available()`,
`has_open_erasure()`, `request_erasure()`) rather than subclassing the owner's
ABC — modules never import each other, and this module must not become a
dependency of the modules that report to it. Unlike an owner's own default
client it works in **both** deployments. It derives the idempotency key from
the subject (`owner:<subject_type>:<subject_key>`), so a sweep that re-asks
daily still opens exactly one erasure. Over a transport `has_open_erasure`
answers `False` — there is no read Function for that question, and the key is
what actually prevents the duplicate; the visible difference is a counter, not
a second request. Override `idempotency_key()` or `key_prefix` to scope it
differently.

**Bus events** (microservices mode, constants in `stapel_core.gdpr`): publishes `gdpr.export.requested`; consumes `gdpr.export.completed` and `gdpr.delete.completed` via `manage.py consume_gdpr_completions`. (`_publish_delete_requested` for `gdpr.delete.requested` exists but is not on any current code path — deletion fan-out goes through the `user.deleted` comm action.)

**Notifications requested** (customize templates in the host's notifications setup, not here): `gdpr.export_ready`, `gdpr.inactivity_warning`, `gdpr.inactivity_closed`, `gdpr.dsar.received` (the acknowledgement that satisfies the three-business-day clock — `ack_sent_at` is only stamped when this was actually requested), `gdpr.dsar.opened` (to `DSAR_STAFF_EMAILS`) via `stapel_core.notifications.request_notification`.

### Swappable models

None. This module defines no swappable models and takes no FK to the user table — `user_id` is stored as a plain `UUIDField`, and the user is only touched through `django.contrib.auth.get_user_model()` (`lifecycle.set_active`, reading `email`/`phone`, and `lifecycle.erase_identity` at deletion time). Any `AUTH_USER_MODEL` with UUID primary keys works. A host whose user model carries extra personal fields erases them through `PRIMARY_IDENTITY_ERASURE` (dotted path to `erase(user)`) or its own provider — `anonymize` scrubs the framework fields only.

### Serializer seams

All views subclass `GDPRAPIView` (`views.py`), which exposes `request_serializer_class` / `response_serializer_class` class attributes plus `get_request_serializer_class()` / `get_response_serializer_class()` getters. To change a response envelope: subclass the view, swap the class attribute (or override the getter) with your own `StapelDataclassSerializer` over an extended DTO, and mount your subclass in the host project's `urls.py` instead of including `stapel_gdpr.urls`. URL wiring is host-owned; `permission_classes` are ordinary DRF attributes overridable the same way. Serializers are `StapelDataclassSerializer`s over the dataclass DTOs in `dto.py` (`ExportRequestDTO`, `ExportStatusDTO`, `ClosureStatusDTO`).

### Middleware (host-wired)

`stapel_gdpr.guards.AccountClosureGuardMiddleware` refuses every authenticated request of an account in `deleting`/`deleted` with `error.403.gdpr.account_closed`, fleet-wide rather than on this module's endpoints only. Add it to `MIDDLEWARE` **after** the authentication middleware; it costs one indexed query per authenticated request. Grace is deliberately allowed through — cancelling a closure requires logging in. Views that only need the same rule locally use the DRF permission `guards.AccountNotClosed`, which every view in this module already carries.

### Signals

This module defines and sends **no Django signals**. Business milestones travel as comm Actions (table above). In-process hooks for the host project belong to `stapel_core.signals` (none of which are GDPR-specific today); adding a GDPR signal is an upstream contribution.

### Admin categories (`stapel_core.access`)

`@access.ops` (admin-suite AS-5): `DataExportRequest`, `DataExportPart`, `AccountClosureRequest`, `ErasureRequest`, `ErasurePart`, `DataOwnerHealth`, `SubprocessorObligation`, `ReRegistrationHash`. Every one of these is a state machine mutated exclusively by `GDPROrchestrator` (or, for `ReRegistrationHash`, `reregistration.store_hashes` / the retention-cleanup task) — there is no staff-facing review/approve/override action anywhere in `views.py` or `admin.py`. Closure cancellation is user-initiated only (`AccountCancelCloseView`, keyed off the authenticated requester, not a staff action). MODULE.md already documented the anti-pattern above: "Do not flip `AccountClosureRequest.status` or `ErasurePart` rows directly" — `@access.ops` now enforces that at the admin layer (read-only, including for a superuser) instead of only in prose. `ReRegistrationHash` is a dedup/TTL-expiring record (24-month retention, cleaned up by `run_retention_cleanup`), not a credential — `ops`, not `secret` — but `hash_value` is still a hash of PII, so `ReRegistrationHashAdmin.secret_fields = ('hash_value',)` masks it explicitly regardless of category (the same pattern `stapel-core` uses for `session_key`/`session_data` on the `ops`-categorized `Session` admin).

`LegalHold` and `DsarRequest` are left undecorated (implicit `business`). Placing a hold and releasing it (`released_at`) is a real, expected staff/compliance workflow through `LegalHoldAdmin` — see "Placing/releasing legal holds → `LegalHold` ORM/admin" above. `DsarRequest` is the same shape: triaging a data-subject request (state, note, matching it to an account) **is** the staff workflow, and the module ships an authenticated `PATCH dsar/{id}` for exactly that, so the admin is that workflow by another door rather than a hand-edit of a machine's state.

## Anti-patterns (tailored)

- **Do not fork to add a data section to export/deletion.** Implement a `GDPRProvider` in *your* app and list it in `GDPR_PROVIDERS`, or subscribe to `user.deleted` + confirm with `gdpr.section.erased`.
- **Do not import `stapel_gdpr` from another stapel module** (models, orchestrator, anything). Modules never import each other — participate via comm actions and the `stapel_core.gdpr` primitives. (Host *projects* may import the public API: `gdpr_orchestrator`, `LegalHold`, `is_reregistration`, `store_hashes`, `gdpr_settings`.)
- **Do not flip `AccountClosureRequest.status`, `ErasureRequest.state` or `ErasurePart` rows directly.** Completion is confirmed only by emitting `gdpr.section.erased` with the request's `correlation_id`; finalization logic (`_maybe_finalize`) owns the state machine.
- **Do not answer `gdpr.owner.probe` from a separate health subscriber.** Answering from anywhere but the subscriber that handles `gdpr.erasure.requested` turns `alive` back into "a container is running", which is the exact signal that was already available and already useless.
- **Do not hard-delete an entity and skip the erasure request.** A delete nobody receipted cannot be shown as "pending deletion until X", cannot be re-queued after a restore, and leaves every other owner's copy in place.
- **Do not wire a DSAR from an unverified email.** Intake deliberately refuses to act on an anonymous form submission; matching it to an account is a staff decision (`PATCH dsar/{id}` with `user_id`). Automating it makes the endpoint a deletion oracle.
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
- Anything reachable via `STAPEL_GDPR` keys or the flat settings above (data-owner inventory and its subject map, subject types, purge SLA, erasure authorizer, subprocessor windows, DSAR staff recipients, revocation and identity-erasure seams, download TTL/URL template, key, staging/archive roots, providers, collecting services). Every key has a row in `CONFIG.MD`.
- Deciding who may erase an entity → `ERASURE_AUTHORIZER`, not a fork of `ErasureRequestView`.
- Putting your own entity on the deletion clock → add it to `SUBJECT_TYPES`, claim it in `DATA_OWNERS`, call `POST /erasures` after your soft-delete.
- Adding your app's data to export/deletion → `GDPRProvider` + `GDPR_PROVIDERS`, or `user.deleted` subscriber + `gdpr.section.erased`; either way the owner belongs in `DATA_OWNERS`.
- Refusing closed accounts outside this module's endpoints → add `guards.AccountClosureGuardMiddleware` to `MIDDLEWARE`.
- Reacting to closures/deletions → `@on_action` subscribers in your own app.
- Changing API envelopes, permissions, or routes → subclass views (serializer seams), own `urls.py`.
- Re-scheduling workers → compose your own `CELERY_BEAT_SCHEDULE` instead of `get_gdpr_beat_schedule()`.
- Placing/releasing legal holds → `LegalHold` ORM/admin.

**Upstream contribution (change stapel-gdpr itself):**
- New emitted events (e.g. `user.deletion_cancelled`, an actual `user.export_ready` action) or payload/schema changes.
- Making hardcoded policy constants configurable: 30-day grace period (`models.py`), 30-day export cooldown and 24 h export deadline (`orchestrator.py`), 24-month hash retention (`reregistration.py`), 12-month/60-day/14-day inactivity thresholds (`tasks.py`), the DSAR acknowledgement/resolution windows (`DsarRequest.ACK_BUSINESS_DAYS` / `RESOLVE_DAYS`) and the one-day slack `gdpr_requeue_after_restore` applies to a backup timestamp.
- New orchestrator states, model fields, or migrations; new settings keys; new `import_strings` seams; Django signals.
- If the customization requires monkey-patching, editing this package's code, or touching its models' state machine — it is upstream, not app-layer.
