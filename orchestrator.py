"""
GDPROrchestrator — coordinates export and deletion across all registered providers.

Monolith mode:   providers run in-process via gdpr_registry; staging_dir used for files.
Microservices:   orchestrator publishes bus events; each service handles its own data,
                 uploads to object storage, and publishes a completion event.
                 The orchestrator assembles the final archive by downloading from storage.
"""
import logging
import zipfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from stapel_core.gdpr import (
    GDPR_DELETE_REQUESTED,
    GDPR_EXPORT_REQUESTED,
    gdpr_registry,
)

from .models import AccountClosureRequest, DataExportPart, DataExportRequest

logger = logging.getLogger(__name__)


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

    def request_export(self, user_id: int) -> DataExportRequest:
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
                    'user_id': req.user_id,
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
        req = DataExportRequest.objects.select_for_update().get(pk=request_id)
        if req.status not in (DataExportRequest.STATUS_PENDING, DataExportRequest.STATUS_PROCESSING):
            return

        req.status = DataExportRequest.STATUS_PROCESSING
        req.save(update_fields=['status'])

        staging_dir = self._staging_dir(request_id)
        staging_dir.mkdir(parents=True, exist_ok=True)

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
        if req.all_parts_done or force or timezone.now() >= req.deadline:
            self._assemble_zip(req, staging_dir, partial=not req.all_parts_done)

    def _assemble_zip(self, req: DataExportRequest, staging_dir: Path, partial: bool = False) -> None:
        self._download_bucket_parts(req, staging_dir)

        archive_root = Path(getattr(settings, 'GDPR_ARCHIVE_ROOT', '/tmp/gdpr_exports'))
        archive_root.mkdir(parents=True, exist_ok=True)
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
                    params={
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
        base = Path(getattr(settings, 'GDPR_STAGING_ROOT', '/tmp/gdpr_staging'))
        return base / str(request_id)

    # -------------------------------------------------------------------------
    # Account closure / deletion
    # -------------------------------------------------------------------------

    def initiate_closure(self, user_id: int, trigger: str = AccountClosureRequest.TRIGGER_MANUAL) -> AccountClosureRequest:
        if AccountClosureRequest.objects.filter(
            user_id=user_id, status__in=[AccountClosureRequest.STATUS_GRACE, AccountClosureRequest.STATUS_DELETING]
        ).exists():
            raise ValueError('closure_already_pending')

        closure = AccountClosureRequest.objects.create(user_id=user_id, trigger=trigger)
        self._deactivate_user(user_id)
        return closure

    def cancel_closure(self, user_id: int) -> AccountClosureRequest:
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
        """Dispatch deletion to all services via the bus (microservices) or in-process (monolith)."""
        import uuid
        closure.status = AccountClosureRequest.STATUS_DELETING
        closure.save(update_fields=['status'])

        user_id = closure.user_id
        collecting = _collecting_services()

        if collecting:
            self._publish_delete_requested(user_id, str(uuid.uuid4()), collecting)
        else:
            self._run_deletion_inprocess(user_id)
            closure.status     = AccountClosureRequest.STATUS_DELETED
            closure.deleted_at = timezone.now()
            closure.save(update_fields=['status', 'deleted_at'])

    def _publish_delete_requested(self, user_id: int, correlation_id: str, services: list[str]) -> None:
        try:
            from stapel_core.bus.event import Event
            from stapel_core.bus.router import get_bus
            get_bus().publish(GDPR_DELETE_REQUESTED, Event(
                event_type=GDPR_DELETE_REQUESTED,
                service='gdpr',
                payload={
                    'correlation_id': correlation_id,
                    'user_id': user_id,
                    'services': services,
                },
                key=str(user_id),
            ))
            logger.info('GDPR deletion dispatched [user_id=%s correlation=%s services=%s]',
                        user_id, correlation_id, services)
        except Exception as e:
            logger.error('Failed to publish GDPR delete event: %s', e)
            raise

    def _run_deletion_inprocess(self, user_id: int) -> None:
        for provider in gdpr_registry.providers:
            try:
                provider.anonymize(user_id)
            except Exception as e:
                logger.error('GDPR anonymize failed [%s / %s]: %s', user_id, provider.section, e)
        for provider in gdpr_registry.providers:
            try:
                provider.delete(user_id)
            except Exception as e:
                logger.error('GDPR delete failed [%s / %s]: %s', user_id, provider.section, e)

    def _deactivate_user(self, user_id: int) -> None:
        try:
            from django.contrib.auth import get_user_model
            get_user_model().objects.filter(pk=user_id).update(is_active=False)
        except Exception as e:
            logger.error('Failed to deactivate user %s: %s', user_id, e)

    def _reactivate_user(self, user_id: int) -> None:
        try:
            from django.contrib.auth import get_user_model
            get_user_model().objects.filter(pk=user_id).update(is_active=True)
        except Exception as e:
            logger.error('Failed to reactivate user %s: %s', user_id, e)


gdpr_orchestrator = GDPROrchestrator()
