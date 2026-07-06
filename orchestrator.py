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

from .conf import gdpr_settings
from .models import (
    AccountClosureRequest,
    AccountDeletionPart,
    DataExportPart,
    DataExportRequest,
    LegalHold,
)

logger = logging.getLogger(__name__)

# Framework users have UUID primary keys; str is accepted for convenience.
UserId = Union[uuid_lib.UUID, str]


def _secure_mkdir(path: Path) -> Path:
    """mkdir -p with owner-only permissions (0700)."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _collecting_services() -> list[str]:
    """Return the list of services expected to contribute GDPR data.

    Monolith: derives from in-process registry.
    Microservices: explicitly configured via GDPR_COLLECTING_SERVICES setting.
    """
    from_settings = getattr(settings, 'GDPR_COLLECTING_SERVICES', [])
    return from_settings or gdpr_registry.sections


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

        archive_root = _secure_mkdir(self._archive_root())
        zip_path = archive_root / f'export_{req.pk}.zip'

        date_str = req.created_at.strftime('%Y-%m-%d')
        zip_root = f'export_{date_str}'

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f'{zip_root}/README.txt', self._build_readme(req, partial))
            if staging_dir.exists():
                for file in staging_dir.rglob('*'):
                    if file.is_file():
                        zf.write(file, f'{zip_root}/{file.relative_to(staging_dir)}')

        req.archive_path = str(zip_path)
        req.status       = DataExportRequest.STATUS_READY
        req.save(update_fields=['archive_path', 'status'])
        req.generate_download_token()

        # PII must not linger in the staging area once zipped.
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)

        self._send_ready_notification(req)
        logger.info('GDPR export archive assembled [request=%s partial=%s]', req.pk, partial)

    def _download_bucket_parts(self, req: DataExportRequest, staging_dir: Path) -> None:
        """Download parts uploaded to object storage into the local staging directory."""
        from django.core.files.storage import default_storage

        for part in req.parts.filter(status=DataExportPart.STATUS_DONE, bucket_path__isnull=False):
            dest_dir = staging_dir / part.service
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file = dest_dir / 'export.json'
            if dest_file.exists():
                continue
            try:
                with default_storage.open(part.bucket_path) as src:
                    dest_file.write_bytes(src.read())
            except Exception as e:
                logger.error('Failed to download GDPR part from bucket [service=%s path=%s]: %s',
                             part.service, part.bucket_path, e)

    def _send_ready_notification(self, req: DataExportRequest) -> None:
        try:
            from django.contrib.auth import get_user_model
            from stapel_core.notifications import request_notification
            user = get_user_model().objects.filter(pk=req.user_id).first()
            if user and getattr(user, 'email', None):
                request_notification(
                    email=user.email,
                    notification_type='gdpr.export_ready',
                    variables={
                        'download_url': self._build_download_url(req),
                        'expires_at': req.download_expires_at.isoformat() if req.download_expires_at else '',
                    },
                )
        except Exception as e:
            logger.error('Failed to send GDPR ready notification [request=%s]: %s', req.pk, e)

    def _build_download_url(self, req: DataExportRequest) -> str:
        frontend_url = getattr(settings, 'FRONTEND_URL', '').rstrip('/')
        return f'{frontend_url}/privacy/export/{req.download_token}/'

    def _build_readme(self, req: DataExportRequest, partial: bool) -> str:
        lines = [
            'Your personal data export',
            f'Requested: {req.created_at.strftime("%Y-%m-%d %H:%M UTC")}',
            '',
        ]
        if partial:
            missing = [p.service for p in req.parts.exclude(status=DataExportPart.STATUS_DONE)]
            lines += [
                'NOTE: This is a partial export. The following sections could not be',
                'included within the 24-hour processing window:',
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
        """Create the closure request, deactivate the user, and announce it.

        The row + deactivation + ``user.deletion_initiated`` emit are one
        outbox unit via ``mutate_and_emit()``: a failing emit rolls the
        mutation back and propagates (never swallowed) — a closure request
        that consumers were never told about (e.g. stapel-notifications
        deactivating contacts) must not silently exist. Callers that need
        best-effort semantics already wrap this call (``tasks.py``'s
        ``check_inactive_accounts``); the HTTP view surfaces it as a 500.
        """
        if LegalHold.is_held(user_id):
            raise ValueError('legal_hold')
        if AccountClosureRequest.objects.filter(
            user_id=user_id, status__in=[AccountClosureRequest.STATUS_GRACE, AccountClosureRequest.STATUS_DELETING]
        ).exists():
            raise ValueError('closure_already_pending')

        from stapel_core.comm import mutate_and_emit

        with mutate_and_emit() as emit:
            closure = AccountClosureRequest.objects.create(user_id=user_id, trigger=trigger)
            self._deactivate_user(user_id)
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

        closure.status       = AccountClosureRequest.STATUS_CANCELLED
        closure.cancelled_at = timezone.now()
        closure.save(update_fields=['status', 'cancelled_at'])
        self._reactivate_user(user_id)
        return closure

    def execute_deletion(self, closure: AccountClosureRequest) -> None:
        """Erase user data.

        Local (in-process) providers always run — in a monolith they are the
        whole deletion. Additionally a ``user.deleted`` Action is emitted so
        that comm subscribers / remote services erase their side (transport
        chosen by STAPEL_COMM: in-process, Kafka, ...). The closure is marked
        DELETED only when every local provider actually succeeded — a
        swallowed provider crash must not be recorded as a completed erasure.

        Remote services (STAPEL_GDPR["REMOTE_DELETION_SERVICES"]) each get an
        AccountDeletionPart and must confirm with a ``gdpr.section.erased``
        action carrying this closure's correlation_id. The closure flips to
        DELETED only when local providers succeeded AND all expected remote
        parts are done (immediately, when the list is empty).

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

        closure.status = AccountClosureRequest.STATUS_DELETING
        if not closure.correlation_id:
            closure.correlation_id = str(uuid_lib.uuid4())
        closure.save(update_fields=['status', 'correlation_id'])

        user_id        = closure.user_id
        correlation_id = closure.correlation_id

        # Expected remote confirmations — one part per configured service.
        for service in gdpr_settings.REMOTE_DELETION_SERVICES:
            AccountDeletionPart.objects.get_or_create(closure=closure, service=service)

        # Re-registration hashes must be captured BEFORE erasure destroys
        # the identifiers.
        self._store_reregistration_hashes(user_id)

        failed = self._run_deletion_inprocess(user_id)

        if failed:
            logger.error(
                'GDPR deletion incomplete [user=%s failed=%s] — left in DELETING for retry',
                user_id, failed,
            )
            return

        from stapel_core.comm import mutate_and_emit

        with mutate_and_emit() as emit:
            closure.local_erasure_done = True
            closure.save(update_fields=['local_erasure_done'])
            emit(
                'user.deleted',
                {
                    'user_id': str(user_id),
                    'correlation_id': correlation_id,
                    'trigger': closure.trigger,
                },
                key=str(user_id),
            )

        self._maybe_finalize(closure)

    def mark_section_erased(self, correlation_id: str, service: str) -> None:
        """Called when a remote service confirms erasure via gdpr.section.erased."""
        closure = AccountClosureRequest.objects.filter(correlation_id=correlation_id).first()
        if closure is None:
            logger.warning('gdpr.section.erased for unknown correlation_id=%s service=%s',
                           correlation_id, service)
            return

        updated = AccountDeletionPart.objects.filter(
            closure=closure, service=service,
        ).exclude(status=AccountDeletionPart.STATUS_DONE).update(
            status=AccountDeletionPart.STATUS_DONE,
            completed_at=timezone.now(),
        )
        if not updated:
            logger.debug('GDPR deletion part already done or unknown [correlation=%s service=%s]',
                         correlation_id, service)
            return

        closure.refresh_from_db()
        self._maybe_finalize(closure)

    def _maybe_finalize(self, closure: AccountClosureRequest) -> None:
        """Flip the closure to DELETED once local + all remote erasure is confirmed."""
        if closure.status != AccountClosureRequest.STATUS_DELETING:
            return
        if not closure.local_erasure_done or not closure.all_remote_parts_done:
            return
        closure.status     = AccountClosureRequest.STATUS_DELETED
        closure.deleted_at = timezone.now()
        closure.save(update_fields=['status', 'deleted_at'])
        logger.info('GDPR account deletion completed [user=%s correlation=%s]',
                    closure.user_id, closure.correlation_id)

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

    def _deactivate_user(self, user_id: UserId) -> None:
        try:
            from django.contrib.auth import get_user_model
            get_user_model().objects.filter(pk=user_id).update(is_active=False)
        except Exception as e:
            logger.error('Failed to deactivate user %s: %s', user_id, e)

    def _reactivate_user(self, user_id: UserId) -> None:
        try:
            from django.contrib.auth import get_user_model
            get_user_model().objects.filter(pk=user_id).update(is_active=True)
        except Exception as e:
            logger.error('Failed to reactivate user %s: %s', user_id, e)


gdpr_orchestrator = GDPROrchestrator()
