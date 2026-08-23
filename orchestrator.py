"""
GDPROrchestrator — coordinates export and deletion across all registered providers.

Monolith mode:   providers run in-process via gdpr_registry; staging_dir used for files.
Microservices:   orchestrator publishes bus events; each service handles its own data,
                 uploads to object storage, and publishes a completion event.
                 The orchestrator assembles the final archive by downloading from storage.
"""
import logging
import os
import shutil
import uuid as uuid_lib
import zipfile
from pathlib import Path
from typing import Union

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from stapel_core.gdpr import (
    GDPR_DELETE_REQUESTED,
    GDPR_EXPORT_REQUESTED,
    gdpr_registry,
)

from . import lifecycle
from .conf import gdpr_settings
from .models import (
    AccountClosureRequest,
    DataExportPart,
    DataExportRequest,
    ErasurePart,
    ErasureRequest,
    LegalHold,
)
from .owners import SUBJECT_ACCOUNT, data_owner_report, subject_types

logger = logging.getLogger(__name__)

# Framework users have UUID primary keys; str is accepted for convenience.
UserId = Union[uuid_lib.UUID, str]


#: Shape a remote-supplied ``bucket_path`` must have before this service will
#: open it (security audit 2026-08-11).
#:
#: The value arrives from a peer service — over HTTP (ExportPartReadyView) or
#: over the bus (consume_gdpr_completions) — and its contents are copied
#: verbatim into an archive a USER downloads. Nothing validated it, so a
#: compromised or merely buggy peer could name any key in the bucket and have
#: this service hand it to whoever requested the export. Django's
#: FileSystemStorage refuses traversal; an S3 backend has no such notion —
#: keys are opaque strings and "../" is just characters.
#:
#: Two rules, both applied at ingest (mark_part_ready) AND at open
#: (_download_bucket_parts), because rows written before this rule or by a
#: writer that bypassed the orchestrator must not be readable either.
BUCKET_PATH_PREFIX_TEMPLATE = "gdpr/{correlation_id}/"

#: Segments that are never a legitimate part of an export key: traversal,
#: absolutes, Windows separators/drives, and anything URL-shaped.
_BUCKET_PATH_REJECTED = ("..", "\\", "://", "\x00")


def export_bucket_prefix(correlation_id: str) -> str:
    """The prefix a part's ``bucket_path`` must start with, or "" if opted out.

    ``STAPEL_GDPR["EXPORT_BUCKET_PREFIX"]`` is a template over the request's
    own correlation id, so a peer can only ever name a key belonging to the
    export it was asked about — the property that stops one user's archive
    from absorbing another's. Set it to "" to accept any key (see MODULE.md;
    reported by ``manage.py check`` as gdpr.W007).
    """
    template = gdpr_settings.EXPORT_BUCKET_PREFIX
    if not template:
        return ""
    return str(template).format(correlation_id=correlation_id)


def is_safe_bucket_path(bucket_path: str, correlation_id: str) -> bool:
    """Whether *bucket_path* may be opened for the export *correlation_id*."""
    if not bucket_path or not isinstance(bucket_path, str):
        return False
    if bucket_path.startswith("/") or bucket_path.startswith("~"):
        return False
    if any(token in bucket_path for token in _BUCKET_PATH_REJECTED):
        return False
    if any(ch in bucket_path for ch in ("\r", "\n")):
        return False
    prefix = export_bucket_prefix(correlation_id)
    # The shape rules above hold even when a host opted out of the prefix:
    # traversal and absolute keys are never a legitimate export part.
    return not prefix or bucket_path.startswith(prefix)


def _secure_mkdir(path: Path) -> Path:
    """mkdir -p with owner-only permissions (0700)."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _collecting_services() -> list[str]:
    """Services expected to contribute to an export.

    Every declared data owner, plus whatever a host listed the old way
    (GDPR_COLLECTING_SERVICES) and whatever is registered in-process — the
    union, because an owner that is not asked cannot be reported missing.
    """
    expected = list(data_owner_report().names)
    for name in list(getattr(settings, 'GDPR_COLLECTING_SERVICES', []) or []) + gdpr_registry.sections:
        if name not in expected:
            expected.append(name)
    return expected


class GDPROrchestrator:

    # -------------------------------------------------------------------------
    # Export
    # -------------------------------------------------------------------------

    def request_export(self, user_id: UserId) -> DataExportRequest:
        """Create a new export request and dispatch it to all services via the bus.

        Raises ValueError('export_cooldown') if a recent request already exists.
        """
        import uuid
        from datetime import timedelta

        recent_cutoff = timezone.now() - timedelta(days=30)
        if DataExportRequest.objects.filter(
            user_id=user_id,
            created_at__gte=recent_cutoff,
            status__in=[
                DataExportRequest.STATUS_PENDING,
                DataExportRequest.STATUS_PROCESSING,
                DataExportRequest.STATUS_READY,
            ],
        ).exists():
            raise ValueError('export_cooldown')

        expected = _collecting_services()
        correlation_id = str(uuid.uuid4())

        req = DataExportRequest.objects.create(
            user_id=user_id,
            expected_services=expected,
            correlation_id=correlation_id,
            deadline=timezone.now() + timedelta(hours=24),
        )
        for section in expected:
            DataExportPart.objects.create(request=req, service=section)

        self._publish_export_requested(req)
        return req

    def _publish_export_requested(self, req: DataExportRequest) -> None:
        try:
            from stapel_core.bus.event import Event
            from stapel_core.bus.router import get_bus
            get_bus().publish(GDPR_EXPORT_REQUESTED, Event(
                event_type=GDPR_EXPORT_REQUESTED,
                service='gdpr',
                payload={
                    'correlation_id': req.correlation_id,
                    'user_id': str(req.user_id),
                    'request_id': req.pk,
                },
                key=str(req.user_id),
            ))
            logger.info('GDPR export requested [correlation=%s user=%s services=%s]',
                        req.correlation_id, req.user_id, req.expected_services)
        except Exception as e:
            logger.error('Failed to publish GDPR export event: %s', e)
            raise

    def run_export(self, request_id: int) -> None:
        """Execute export for all local (in-process) providers. Used in monolith mode."""
        # select_for_update() requires an open transaction — without one
        # Django raises TransactionManagementError on every call.
        with transaction.atomic():
            req = DataExportRequest.objects.select_for_update().get(pk=request_id)
            if req.status not in (DataExportRequest.STATUS_PENDING, DataExportRequest.STATUS_PROCESSING):
                return

            req.status = DataExportRequest.STATUS_PROCESSING
            req.save(update_fields=['status'])

        staging_dir = _secure_mkdir(self._staging_dir(request_id))

        for provider in gdpr_registry.providers:
            part = req.parts.filter(service=provider.section).first()
            if not part or part.status == DataExportPart.STATUS_DONE:
                continue
            try:
                provider_dir = staging_dir / provider.section
                provider_dir.mkdir(exist_ok=True)
                provider.export_to_staging(req.user_id, provider_dir)
                part.status       = DataExportPart.STATUS_DONE
                part.completed_at = timezone.now()
            except Exception as e:
                logger.error('GDPR export failed [%s / %s]: %s', request_id, provider.section, e)
                part.status = DataExportPart.STATUS_FAILED
                part.error  = str(e)
            part.save(update_fields=['status', 'completed_at', 'error'])

        self._try_assemble(req, staging_dir)

    def mark_part_ready(self, correlation_id: str, service: str, bucket_path: str) -> None:
        """Called when a remote service publishes gdpr.export.completed."""
        try:
            req = DataExportRequest.objects.get(correlation_id=correlation_id)
        except DataExportRequest.DoesNotExist:
            logger.warning('GDPR export completed for unknown correlation_id=%s service=%s',
                           correlation_id, service)
            return

        if bucket_path and not is_safe_bucket_path(bucket_path, req.correlation_id):
            # Refuse the part rather than store a key we will not open later:
            # the export then reports this service as missing (an honestly
            # partial archive) instead of carrying somebody else's object.
            logger.error(
                'GDPR part refused: bucket_path outside %r [correlation=%s service=%s path=%s]',
                export_bucket_prefix(req.correlation_id) or '<shape rules only>',
                correlation_id, service, bucket_path,
            )
            return

        updated = DataExportPart.objects.filter(
            request=req, service=service,
        ).exclude(status=DataExportPart.STATUS_DONE).update(
            status=DataExportPart.STATUS_DONE,
            bucket_path=bucket_path,
            completed_at=timezone.now(),
        )
        if not updated:
            logger.debug('GDPR part already done or unknown [correlation=%s service=%s]',
                         correlation_id, service)
            return

        req.refresh_from_db()
        self._try_assemble(req, self._staging_dir(req.pk))

    def sweep_deadlines(self) -> None:
        """Force-assemble partial archives for requests past their 24h deadline."""
        expired = DataExportRequest.objects.filter(
            status=DataExportRequest.STATUS_PROCESSING,
            deadline__lte=timezone.now(),
        )
        for req in expired:
            logger.warning('GDPR export deadline reached, assembling partial [request=%s]', req.pk)
            self._try_assemble(req, self._staging_dir(req.pk), force=True)

    def _try_assemble(self, req: DataExportRequest, staging_dir: Path, force: bool = False) -> None:
        """Assemble the archive exactly once.

        Guarded with SELECT ... FOR UPDATE and an ASSEMBLING status flip so
        concurrent part completions (bus consumer + HTTP callback + sweep)
        cannot build the zip twice.
        """
        with transaction.atomic():
            locked = DataExportRequest.objects.select_for_update().get(pk=req.pk)
            if locked.status not in (
                DataExportRequest.STATUS_PENDING,
                DataExportRequest.STATUS_PROCESSING,
            ):
                return  # already assembling / ready / failed / expired
            if not (locked.all_parts_done or force or timezone.now() >= locked.deadline):
                return
            locked.status = DataExportRequest.STATUS_ASSEMBLING
            locked.save(update_fields=['status'])

        try:
            self._assemble_zip(locked, staging_dir, partial=not locked.all_parts_done)
        except Exception as e:
            logger.error('GDPR archive assembly failed [request=%s]: %s', locked.pk, e)
            # Return the request to PROCESSING so the deadline sweep retries.
            locked.status = DataExportRequest.STATUS_PROCESSING
            locked.error  = str(e)
            locked.save(update_fields=['status', 'error'])
            raise

    def _assemble_zip(self, req: DataExportRequest, staging_dir: Path, partial: bool = False) -> None:
        self._download_bucket_parts(req, staging_dir)

        # Completeness is judged against the declared registry, not against
        # whatever happened to answer: an owner nobody declared cannot be
        # reported missing, so an unconfigured registry makes every export
        # partial by construction.
        registry = data_owner_report()
        missing = self._missing_services(req, registry)
        partial = partial or bool(missing) or bool(registry.problems)

        archive_root = _secure_mkdir(self._archive_root())
        zip_path = archive_root / f'export_{req.pk}.zip'

        date_str = req.created_at.strftime('%Y-%m-%d')
        zip_root = f'export_{date_str}'

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f'{zip_root}/README.txt', self._build_readme(req, partial, missing))
            if staging_dir.exists():
                for file in staging_dir.rglob('*'):
                    if file.is_file():
                        zf.write(file, f'{zip_root}/{file.relative_to(staging_dir)}')

        from stapel_core.comm import mutate_and_emit

        # READY flip + download token + ``user.export_ready`` are one outbox
        # unit (schemas/emits/user.export_ready.json): a failing emit rolls
        # the READY state back and propagates — an export consumers were
        # never told about must not silently exist (same discipline as
        # ``initiate_closure``). The email in ``_send_ready_notification``
        # below stays best-effort; the *event* is the contract.
        req.archive_path     = str(zip_path)
        req.status           = DataExportRequest.STATUS_READY
        req.is_partial       = partial
        req.missing_services = missing
        with mutate_and_emit() as emit:
            req.save(update_fields=[
                'archive_path', 'status', 'is_partial', 'missing_services',
            ])
            token = req.generate_download_token()
            emit(
                'user.export_ready',
                {
                    'user_id': str(req.user_id),
                    'request_id': req.pk,
                    'download_expires_at': req.download_expires_at.isoformat(),
                    'is_partial': partial,
                    'missing_services': missing,
                },
                key=str(req.user_id),
            )

        # PII must not linger in the staging area once zipped.
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)

        self._send_ready_notification(req, token)
        logger.info('GDPR export archive assembled [request=%s partial=%s missing=%s]',
                    req.pk, partial, missing)

    def _missing_services(self, req: DataExportRequest, registry) -> list[str]:
        """Expected sections with nothing in the archive, declared owners first."""
        delivered = set(
            req.parts.filter(status=DataExportPart.STATUS_DONE).values_list('service', flat=True)
        )
        expected = list(registry.names) + [
            s for s in (req.expected_services or []) if s not in registry.names
        ]
        return [s for s in expected if s not in delivered]

    def _download_bucket_parts(self, req: DataExportRequest, staging_dir: Path) -> None:
        """Download parts uploaded to object storage into the local staging directory."""
        from django.core.files.storage import default_storage

        for part in req.parts.filter(status=DataExportPart.STATUS_DONE, bucket_path__isnull=False):
            dest_dir = staging_dir / part.service
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file = dest_dir / 'export.json'
            if dest_file.exists():
                continue
            # Checked again here, not only at ingest: this row may predate the
            # rule or have been written by something other than the
            # orchestrator, and the bytes go straight into a user's download.
            if not is_safe_bucket_path(part.bucket_path, req.correlation_id):
                logger.error(
                    'GDPR part not downloaded: bucket_path fails the export key '
                    'contract [request=%s service=%s path=%s]',
                    req.pk, part.service, part.bucket_path,
                )
                continue
            try:
                with default_storage.open(part.bucket_path) as src:
                    dest_file.write_bytes(src.read())
            except Exception as e:
                logger.error('Failed to download GDPR part from bucket [service=%s path=%s]: %s',
                             part.service, part.bucket_path, e)

    def _send_ready_notification(self, req: DataExportRequest, token: str) -> None:
        try:
            from django.contrib.auth import get_user_model
            from stapel_core.notifications import request_notification
            user = get_user_model().objects.filter(pk=req.user_id).first()
            if user and getattr(user, 'email', None):
                request_notification(
                    email=user.email,
                    notification_type='gdpr.export_ready',
                    variables={
                        'download_url': self._build_download_url(token),
                        'expires_at': req.download_expires_at.isoformat() if req.download_expires_at else '',
                        'is_partial': req.is_partial,
                        'missing_services': req.missing_services,
                    },
                )
        except Exception as e:
            logger.error('Failed to send GDPR ready notification [request=%s]: %s', req.pk, e)

    def _build_download_url(self, token: str) -> str:
        """Where the user goes to spend the token.

        The default template parks the token in the URL *fragment*: browsers
        never send a fragment to a server, so it stays out of access logs,
        Referer headers and proxy traces — the leak paths the query-string
        form had.
        """
        frontend_url = getattr(settings, 'FRONTEND_URL', '').rstrip('/')
        template = gdpr_settings.DOWNLOAD_URL_TEMPLATE
        return template.format(frontend_url=frontend_url, token=token)

    def _build_readme(self, req: DataExportRequest, partial: bool, missing: list[str] | None = None) -> str:
        lines = [
            'Your personal data export',
            f'Requested: {req.created_at.strftime("%Y-%m-%d %H:%M UTC")}',
            '',
        ]
        if partial:
            if missing is None:
                missing = [p.service for p in req.parts.exclude(status=DataExportPart.STATUS_DONE)]
            lines += [
                'NOTE: This is a PARTIAL export. The following sections could not be',
                'included within the processing window, or hold data this deployment',
                'cannot currently account for:',
                *[f'  - {s}' for s in missing],
                'Please contact privacy@yourdomain.com to request the missing data.',
                '',
            ]
        done = [p.service for p in req.parts.filter(status=DataExportPart.STATUS_DONE)]
        lines += ['Included sections:', *[f'  - {s}' for s in done]]
        return '\n'.join(lines)

    def _staging_dir(self, request_id: int) -> Path:
        return self._staging_root() / str(request_id)

    def _staging_root(self) -> Path:
        configured = gdpr_settings.STAGING_ROOT or getattr(settings, 'GDPR_STAGING_ROOT', '')
        if configured:
            return Path(configured)
        return Path(settings.MEDIA_ROOT) / 'gdpr' / 'staging'

    def _archive_root(self) -> Path:
        configured = gdpr_settings.ARCHIVE_ROOT or getattr(settings, 'GDPR_ARCHIVE_ROOT', '')
        if configured:
            return Path(configured)
        return Path(settings.MEDIA_ROOT) / 'gdpr' / 'exports'

    # -------------------------------------------------------------------------
    # Account closure / deletion
    # -------------------------------------------------------------------------

    def initiate_closure(self, user_id: UserId, trigger: str = AccountClosureRequest.TRIGGER_MANUAL) -> AccountClosureRequest:
        """Create the closure request, deactivate the user, revoke sessions, announce.

        The row + deactivation + session revocation + ``user.deletion_initiated``
        emit are one outbox unit via ``mutate_and_emit()``: a failing emit
        rolls the mutation back and propagates (never swallowed) — a closure
        request that consumers were never told about (e.g.
        stapel-notifications deactivating contacts) must not silently exist.
        Callers that need best-effort semantics already wrap this call
        (``tasks.py``'s ``check_inactive_accounts``); the HTTP view surfaces
        it as a 500.

        Deactivation goes through :func:`stapel_gdpr.lifecycle.set_active`,
        never ``QuerySet.update``: the update path issues raw SQL, so the
        host's activation observers never fire and the closure propagates
        nowhere. Revocation raises
        :class:`~stapel_gdpr.errors.SessionRevocationUnavailable` when no
        seam resolves, which aborts the whole transaction — a closure that
        leaves every pre-closure access token alive is the defect this
        refuses to record.
        """
        if LegalHold.is_held(user_id):
            raise ValueError('legal_hold')
        if AccountClosureRequest.objects.filter(
            user_id=user_id, status__in=[AccountClosureRequest.STATUS_GRACE, AccountClosureRequest.STATUS_DELETING]
        ).exists():
            raise ValueError('closure_already_pending')

        from stapel_core.comm import mutate_and_emit

        with mutate_and_emit() as emit:
            closure = AccountClosureRequest.objects.create(
                user_id=user_id,
                trigger=trigger,
                registry_version=data_owner_report().version,
            )
            lifecycle.set_active(user_id, False, reason='gdpr_closure')
            lifecycle.revoke_sessions(user_id, emit=emit)
            emit(
                'user.deletion_initiated',
                {
                    'user_id': str(user_id),
                    'trigger': trigger,
                    'grace_ends_at': closure.grace_ends_at.isoformat(),
                },
                key=str(user_id),
            )
        return closure

    def cancel_closure(self, user_id: UserId) -> AccountClosureRequest:
        closure = AccountClosureRequest.objects.filter(
            user_id=user_id, status=AccountClosureRequest.STATUS_GRACE
        ).first()
        if not closure:
            raise ValueError('no_active_closure')

        with transaction.atomic():
            closure.status       = AccountClosureRequest.STATUS_CANCELLED
            closure.cancelled_at = timezone.now()
            closure.save(update_fields=['status', 'cancelled_at'])
            # Same seam as the deactivation, so ``user.reactivated`` fires and
            # consumers that suspended memberships lift them again.
            lifecycle.set_active(user_id, True)
        return closure

    def execute_deletion(self, closure: AccountClosureRequest) -> None:
        """Erase user data.

        Local (in-process) providers always run — in a monolith they are the
        whole deletion. Additionally a ``user.deleted`` Action is emitted so
        that comm subscribers / remote services erase their side (transport
        chosen by STAPEL_COMM: in-process, Kafka, ...). The closure is marked
        DELETED only when every local provider actually succeeded — a
        swallowed provider crash must not be recorded as a completed erasure.

        Every declared data owner claiming the ``account`` subject
        (STAPEL_GDPR["DATA_OWNERS"], plus the legacy
        REMOTE_DELETION_SERVICES) gets an ErasurePart and must produce a
        durable receipt: local owners when their provider returns, remote
        owners by confirming with a ``gdpr.section.erased`` action carrying
        this closure's correlation_id. The closure flips to DELETED only when
        the registry itself is trustworthy AND every part carries a receipt
        AND the primary user row itself was erased — never "immediately,
        because nothing was configured".

        ``local_erasure_done`` and the ``user.deleted`` emit are one outbox
        unit via ``mutate_and_emit()``: a failing emit rolls the flag back
        and propagates (never swallowed) — remote services rely on this
        event to erase their own section, so a closure must never be
        recorded as locally-erased without it having gone out. The caller
        (``tasks.py``'s ``process_expired_grace_periods``) already retries
        by re-invoking ``execute_deletion`` on the next sweep; local erasure
        is idempotent so the retry is safe.
        """
        if LegalHold.is_held(closure.user_id):
            raise ValueError('legal_hold')

        registry = data_owner_report()

        closure.status = AccountClosureRequest.STATUS_DELETING
        if not closure.correlation_id:
            closure.correlation_id = str(uuid_lib.uuid4())
        closure.registry_version = registry.version
        closure.save(update_fields=['status', 'correlation_id', 'registry_version'])

        user_id        = closure.user_id
        correlation_id = closure.correlation_id

        # The account is one subject among several. Its erasure request is
        # created here, at grace end — the closure keeps owning the
        # cancellable grace, the ErasureRequest owns the receipts ledger the
        # DELETED flip is checked against, exactly as it does for an entity.
        # execute_deletion is re-entrant by design (the grace sweep retries a
        # closure left in DELETING), so an existing request for this closure
        # is reused rather than duplicated — its correlation_id is unique and
        # its parts are the receipts already collected.
        erasure = ErasureRequest.objects.filter(correlation_id=correlation_id).first()
        if erasure is None:
            erasure = self.request_erasure(
                SUBJECT_ACCOUNT,
                str(user_id),
                requested_by=user_id,
                origin=(ErasureRequest.ORIGIN_INACTIVITY
                        if closure.trigger == AccountClosureRequest.TRIGGER_INACTIVITY
                        else ErasureRequest.ORIGIN_USER),
                correlation_id=correlation_id,
                closure=closure,
                grace_ends_at=closure.grace_ends_at,
            )

        # Re-registration hashes must be captured BEFORE erasure destroys
        # the identifiers.
        self._store_reregistration_hashes(user_id)

        failed = self._run_deletion_inprocess(user_id)
        self._record_local_receipts(erasure, registry, failed)

        if failed:
            logger.error(
                'GDPR deletion incomplete [user=%s failed=%s] — left in DELETING for retry',
                user_id, failed,
            )
            return

        # The primary user row is erased last among the local work: the
        # providers above look the user up by email/phone to find their own
        # rows, and re-registration hashes were already taken. It is erased
        # here rather than at finalization so a remote owner's silence can
        # never leave the person on file indefinitely.
        try:
            lifecycle.erase_identity(user_id)
        except Exception as e:
            logger.error(
                'GDPR primary identity erasure failed [user=%s]: %s — left in '
                'DELETING for retry', user_id, e,
            )
            return
        closure.identity_erased_at = timezone.now()
        closure.save(update_fields=['identity_erased_at'])

        from stapel_core.comm import mutate_and_emit

        with mutate_and_emit() as emit:
            closure.local_erasure_done = True
            closure.save(update_fields=['local_erasure_done'])
            # DEPRECATED, removed in 0.6.0: `gdpr.erasure.requested` already
            # went out when the erasure was created and carries the subject
            # pair this event cannot express. It keeps firing for one minor
            # so a fleet mid-upgrade never has an owner listening to nothing.
            emit(
                'user.deleted',
                {
                    'user_id': str(user_id),
                    'correlation_id': correlation_id,
                    'trigger': closure.trigger,
                },
                key=str(user_id),
            )

        self._maybe_finalize(erasure)

    # -------------------------------------------------------------------------
    # Subject-scoped erasure
    # -------------------------------------------------------------------------

    def request_erasure(
        self,
        subject_type: str,
        subject_key: str,
        *,
        workspace_id: str | None = None,
        requested_by: UserId | None = None,
        origin: str = ErasureRequest.ORIGIN_USER,
        correlation_id: str | None = None,
        closure: AccountClosureRequest | None = None,
        grace_ends_at=None,
        source_request: ErasureRequest | None = None,
        restored_from=None,
        note: str = '',
        idempotency_key: str = '',
    ) -> ErasureRequest:
        """Open an erasure for one subject and dispatch it to its owners.

        The same call for an account, a workspace, a meeting, a recording, a
        document or a file: the row, one receipt slot per owner that claims
        this subject type, and one ``gdpr.erasure.requested`` action — as one
        outbox unit, so an erasure consumers were never told about cannot
        exist. Owners that do not claim the type get no part and therefore
        never block it (a recording waits for recordings and media, not for
        billing).

        Raises ``ValueError('unknown_subject_type')`` for a type outside
        ``STAPEL_GDPR["SUBJECT_TYPES"]`` — a typo'd subject would otherwise
        produce a request no owner can ever answer, which looks exactly like
        an owner that went silent.

        ``idempotency_key`` is for callers that reach this from ANOTHER
        service (``gdpr.erasure.open`` / ``gdpr.erasure.request``): a repeat
        of the same key returns the request that already exists — same row,
        no second set of parts, no second announcement — because at-least-once
        delivery makes a redelivery indistinguishable from a second decision.
        Empty (the default) opts out, which is right for every in-process
        caller and for the restore re-queue, whose idempotency is the
        ``(origin, source_request)`` constraint instead.
        """
        if subject_type not in subject_types():
            raise ValueError('unknown_subject_type')

        from django.db import IntegrityError
        from stapel_core.comm import mutate_and_emit

        idempotency_key = str(idempotency_key or '')
        if idempotency_key:
            existing = ErasureRequest.objects.filter(
                idempotency_key=idempotency_key,
            ).first()
            if existing is not None:
                logger.info(
                    'GDPR erasure already open for idempotency key %s '
                    '[correlation=%s subject=%s:%s]',
                    idempotency_key, existing.correlation_id,
                    existing.subject_type, existing.subject_key,
                )
                return existing

        registry = data_owner_report()
        claiming = registry.owners_for(subject_type)
        now = timezone.now()

        try:
            with mutate_and_emit() as emit:
                request = ErasureRequest.objects.create(
                    subject_type=subject_type,
                    subject_key=str(subject_key),
                    workspace_id=str(workspace_id) if workspace_id else None,
                    requested_by=requested_by,
                    origin=origin,
                    requested_at=now,
                    grace_ends_at=grace_ends_at,
                    correlation_id=correlation_id or str(uuid_lib.uuid4()),
                    registry_version=registry.version,
                    state=ErasureRequest.STATE_ERASING,
                    closure=closure,
                    source_request=source_request,
                    restored_from=restored_from,
                    note=note,
                    idempotency_key=idempotency_key,
                )
                ErasurePart.objects.bulk_create([
                    ErasurePart(
                        request=request,
                        owner=owner.name,
                        kind=(ErasurePart.KIND_LOCAL if owner.is_local
                              else ErasurePart.KIND_REMOTE),
                        deadline=now + owner.timeout,
                    )
                    for owner in claiming
                ])
                emit(
                    'gdpr.erasure.requested',
                    {
                        'request_id': request.pk,
                        'correlation_id': request.correlation_id,
                        'subject_type': request.subject_type,
                        'subject_key': request.subject_key,
                        'workspace_id': request.workspace_id or '',
                        'requested_by': str(requested_by) if requested_by else '',
                        'origin': request.origin,
                        'due_at': request.due_at.isoformat(),
                    },
                    key=request.correlation_id,
                )
        except IntegrityError:
            # Two deliveries of the same key raced past the pre-check. The
            # loser's whole transaction rolled back — row, parts and outbox
            # event together — so returning the winner is the same answer it
            # would have got a millisecond later.
            if idempotency_key:
                winner = ErasureRequest.objects.filter(
                    idempotency_key=idempotency_key,
                ).first()
                if winner is not None:
                    return winner
            raise
        logger.info(
            'GDPR erasure requested [correlation=%s subject=%s:%s owners=%s]',
            request.correlation_id, subject_type, subject_key,
            [o.name for o in claiming],
        )
        return request

    def mark_section_erased(
        self,
        correlation_id: str,
        service: str,
        receipt_id: str = '',
        counts: dict | None = None,
    ) -> None:
        """Called when an owner confirms erasure via gdpr.section.erased.

        An unknown owner is NOT accepted as a receipt: a confirmation from a
        name nobody declared proves nothing about the owners that were.
        """
        request = ErasureRequest.objects.filter(correlation_id=correlation_id).first()
        if request is None:
            logger.warning('gdpr.section.erased for unknown correlation_id=%s owner=%s',
                           correlation_id, service)
            return

        updated = ErasurePart.objects.filter(
            request=request, owner=service,
        ).exclude(state=ErasurePart.STATE_DONE).update(
            state=ErasurePart.STATE_DONE,
            receipt_id=receipt_id or f'{service}:{correlation_id}',
            receipt_at=timezone.now(),
            counts=counts or {},
        )
        if not updated:
            logger.debug('GDPR erasure part already done or unknown [correlation=%s owner=%s]',
                         correlation_id, service)
            return

        request.refresh_from_db()
        self._maybe_finalize(request)

    def _record_local_receipts(self, request: ErasureRequest, registry,
                               failed: list[str]) -> None:
        """Write a receipt for every local owner whose provider actually ran.

        An owner declared local but never registered has no provider to run,
        so it gets no receipt — silence must not be mistaken for success.
        """
        for owner in registry.owners_for(request.subject_type):
            if not owner.is_local or owner.name in failed or owner.name in registry.missing:
                continue
            part = request.parts.filter(owner=owner.name).first()
            if part and part.state != ErasurePart.STATE_DONE:
                part.record_receipt()

    def _maybe_finalize(self, request: ErasureRequest) -> None:
        """Flip the erasure to DELETED only against a full set of receipts.

        Fails CLOSED, and that is the point: the pre-registry version
        finalized as soon as the local providers returned and the (empty)
        remote list was vacuously satisfied, so a deployment with one
        registered provider marked accounts DELETED while every other store
        kept the data. The request stays ERASING — visible, retryable, and
        honest — until every claiming owner produced a receipt against a
        registry that is itself sound. The named override is
        ``ALLOW_ERASURE_WITHOUT_RECEIPTS``.
        """
        if request.state not in (ErasureRequest.STATE_QUEUED, ErasureRequest.STATE_ERASING):
            return

        closure = request.closure
        if closure is not None:
            if closure.status != AccountClosureRequest.STATUS_DELETING:
                return
            if not closure.local_erasure_done:
                return
            if not closure.identity_erased_at:
                # Not waivable by ALLOW_ERASURE_WITHOUT_RECEIPTS: that hatch
                # is about owners this deployment cannot reach, whereas the
                # primary user row is always reachable — a surviving one
                # means the erasure did not run, not that it could not.
                logger.warning(
                    'GDPR closure has no erased primary identity, staying DELETING '
                    '[user=%s correlation=%s]', closure.user_id, closure.correlation_id,
                )
                return

        registry = data_owner_report()
        blockers = list(registry.problems)
        if not registry.owners_for(request.subject_type):
            # An erasure nobody was asked to perform must not report itself
            # complete; the inventory is what is wrong, and it says so here.
            blockers.append(
                f'no declared data owner claims subject_type={request.subject_type!r}'
            )
        unreceipted = request.unreceipted_owners
        if unreceipted:
            blockers.append(f'owners without an erasure receipt: {", ".join(unreceipted)}')

        waived = False
        if blockers:
            if not gdpr_settings.ALLOW_ERASURE_WITHOUT_RECEIPTS:
                logger.warning(
                    'GDPR erasure not certifiable, request stays %s '
                    '[subject=%s:%s correlation=%s]: %s',
                    request.state, request.subject_type, request.subject_key,
                    request.correlation_id, '; '.join(blockers),
                )
                return
            waived = True
            logger.error(
                'GDPR erasure marked DELETED without full receipts '
                '(ALLOW_ERASURE_WITHOUT_RECEIPTS is on) [subject=%s:%s correlation=%s]: %s',
                request.subject_type, request.subject_key, request.correlation_id,
                '; '.join(blockers),
            )

        completed_at = timezone.now()
        request.state               = ErasureRequest.STATE_DELETED
        request.completed_at        = completed_at
        request.completeness_waived = waived
        request.save(update_fields=['state', 'completed_at', 'completeness_waived'])

        # The processors' windows open the moment our own systems are clean:
        # an obligation recorded here is what `fully_erased_by` is computed
        # from, so the product can name both dates instead of one.
        from .subprocessors import record_subprocessor_obligations

        record_subprocessor_obligations(request)

        if closure is not None:
            closure.status              = AccountClosureRequest.STATUS_DELETED
            closure.deleted_at          = completed_at
            closure.completeness_waived = waived
            closure.save(update_fields=['status', 'deleted_at', 'completeness_waived'])
            logger.info(
                'GDPR account deletion completed [user=%s correlation=%s registry=%s waived=%s]',
                closure.user_id, closure.correlation_id, closure.registry_version, waived,
            )
        else:
            logger.info(
                'GDPR erasure completed [subject=%s:%s correlation=%s registry=%s waived=%s]',
                request.subject_type, request.subject_key, request.correlation_id,
                request.registry_version, waived,
            )

    def _store_reregistration_hashes(self, user_id: UserId) -> None:
        """Persist salted hashes of the user's identifiers before erasure."""
        try:
            from django.contrib.auth import get_user_model

            from .reregistration import store_hashes
            user = get_user_model().objects.filter(pk=user_id).first()
            if user is None:
                return
            store_hashes(
                user_id,
                email=getattr(user, 'email', None),
                phone=getattr(user, 'phone', None),
            )
        except Exception as e:
            logger.error('Failed to store re-registration hashes [%s]: %s', user_id, e)

    def _publish_delete_requested(self, user_id: UserId, correlation_id: str, services: list[str]) -> None:
        try:
            from stapel_core.bus.event import Event
            from stapel_core.bus.router import get_bus
            get_bus().publish(GDPR_DELETE_REQUESTED, Event(
                event_type=GDPR_DELETE_REQUESTED,
                service='gdpr',
                payload={
                    'correlation_id': correlation_id,
                    'user_id': str(user_id),
                    'services': services,
                },
                key=str(user_id),
            ))
            logger.info('GDPR deletion dispatched [user_id=%s correlation=%s services=%s]',
                        user_id, correlation_id, services)
        except Exception as e:
            logger.error('Failed to publish GDPR delete event: %s', e)
            raise

    def _run_deletion_inprocess(self, user_id: UserId) -> list[str]:
        """Run local providers; return the sections that failed."""
        failed: list[str] = []
        for provider in gdpr_registry.providers:
            try:
                provider.anonymize(user_id)
            except Exception as e:
                logger.error('GDPR anonymize failed [%s / %s]: %s', user_id, provider.section, e)
                failed.append(provider.section)
        for provider in gdpr_registry.providers:
            try:
                provider.delete(user_id)
            except Exception as e:
                logger.error('GDPR delete failed [%s / %s]: %s', user_id, provider.section, e)
                if provider.section not in failed:
                    failed.append(provider.section)
        return failed

    def sweep_deletion_deadlines(self) -> int:
        """Mark overdue erasure parts TIMED OUT. Returns how many.

        A silent owner is not a finished owner: the part flips to TIMEOUT,
        which keeps blocking DELETED — and the request it belongs to flips
        too, with a ``gdpr.erasure.timeout`` action, so a host can alert.
        Silence used to die in a log line nobody read.
        """
        from stapel_core.comm import mutate_and_emit

        overdue = ErasurePart.objects.filter(
            state=ErasurePart.STATE_PENDING,
            deadline__lte=timezone.now(),
        )
        affected = list(overdue.values_list('request_id', 'owner'))
        count = overdue.update(
            state=ErasurePart.STATE_TIMEOUT,
            note='owner did not confirm erasure before its deadline',
        )
        if not count:
            return 0

        for request_id, owner in affected:
            logger.error('GDPR erasure part timed out [request=%s owner=%s]', request_id, owner)

        for request in ErasureRequest.objects.filter(
            pk__in={request_id for request_id, _ in affected},
        ).exclude(state__in=[ErasureRequest.STATE_DELETED, ErasureRequest.STATE_TIMEOUT]):
            silent = sorted(
                owner for request_id, owner in affected if request_id == request.pk
            )
            with mutate_and_emit() as emit:
                request.state = ErasureRequest.STATE_TIMEOUT
                request.save(update_fields=['state'])
                emit(
                    'gdpr.erasure.timeout',
                    {
                        'request_id': request.pk,
                        'correlation_id': request.correlation_id,
                        'subject_type': request.subject_type,
                        'subject_key': request.subject_key,
                        'owners': silent,
                        'due_at': request.due_at.isoformat(),
                    },
                    key=request.correlation_id,
                )
        return count

    # -------------------------------------------------------------------------
    # Owner liveness
    # -------------------------------------------------------------------------

    def probe_data_owners(self) -> str:
        """Ask every declared owner to prove its erasure path is consumed.

        Returns the probe's correlation id. Owners answer
        ``gdpr.owner.alive`` from the *same* subscriber that handles
        erasure, so an answer is evidence the consumer runs — not that a
        container is deployed.
        """
        from stapel_core.comm import mutate_and_emit

        from .models import DataOwnerHealth

        registry = data_owner_report()
        correlation_id = str(uuid_lib.uuid4())
        now = timezone.now()

        with mutate_and_emit() as emit:
            for owner in registry.owners:
                DataOwnerHealth.objects.update_or_create(
                    owner=owner.name,
                    defaults={
                        'last_probe_at': now,
                        'declared_subject_types': list(owner.subjects),
                    },
                )
            emit(
                'gdpr.owner.probe',
                {'correlation_id': correlation_id},
                key=correlation_id,
            )
        logger.info('GDPR owner probe sent [correlation=%s owners=%s]',
                    correlation_id, list(registry.names))
        return correlation_id

    def record_owner_alive(self, owner: str, subject_types_answered: list[str]) -> None:
        """Store an owner's ``gdpr.owner.alive`` answer."""
        from .models import DataOwnerHealth

        declared = data_owner_report().owner(owner)
        DataOwnerHealth.objects.update_or_create(
            owner=owner,
            defaults={
                'last_alive_at': timezone.now(),
                'answered_subject_types': list(subject_types_answered or []),
                **(
                    {'declared_subject_types': list(declared.subjects)}
                    if declared is not None else {}
                ),
            },
        )


gdpr_orchestrator = GDPROrchestrator()
