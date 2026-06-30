"""
GDPROrchestrator — coordinates export and deletion across all registered providers.

Monolith:  calls providers directly in-process.
Microservices: providers in remote services handle their own data via bus events;
               this orchestrator handles the local portion + assembly coordination.
"""
import logging
import zipfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from stapel_core.gdpr import gdpr_registry

from .models import AccountClosureRequest, DataExportPart, DataExportRequest

logger = logging.getLogger(__name__)


class GDPROrchestrator:

    # -------------------------------------------------------------------------
    # Export
    # -------------------------------------------------------------------------

    def request_export(self, user_id: int) -> DataExportRequest:
        """Create a new export request. Raises if one is already pending/processing."""
        from datetime import timedelta
        from django.utils import timezone

        recent_cutoff = timezone.now() - timedelta(days=30)
        if DataExportRequest.objects.filter(
            user_id=user_id,
            created_at__gte=recent_cutoff,
            status__in=[DataExportRequest.STATUS_PENDING, DataExportRequest.STATUS_PROCESSING, DataExportRequest.STATUS_READY],
        ).exists():
            raise ValueError('export_cooldown')

        expected = gdpr_registry.sections or getattr(settings, 'GDPR_EXPORT_SERVICES', [])
        req = DataExportRequest.objects.create(user_id=user_id, expected_services=expected)
        for section in expected:
            DataExportPart.objects.create(request=req, service=section)

        return req

    def run_export(self, request_id: int) -> None:
        """Execute export for all local providers. Called by data-export-worker."""
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

    def mark_part_ready(self, request_id: int, service: str) -> None:
        """Called by remote services (microservices mode) when their portion is ready."""
        part = DataExportPart.objects.get(request_id=request_id, service=service)
        part.status       = DataExportPart.STATUS_DONE
        part.completed_at = timezone.now()
        part.save(update_fields=['status', 'completed_at'])

        req = DataExportRequest.objects.get(pk=request_id)
        staging_dir = self._staging_dir(request_id)
        self._try_assemble(req, staging_dir)

    def _try_assemble(self, req: DataExportRequest, staging_dir: Path) -> None:
        if req.all_parts_done:
            self._assemble_zip(req, staging_dir)
        elif timezone.now() >= req.deadline:
            # Partial export: deadline passed, include what we have
            logger.warning('GDPR export deadline passed, assembling partial archive [%s]', req.pk)
            self._assemble_zip(req, staging_dir, partial=True)

    def _assemble_zip(self, req: DataExportRequest, staging_dir: Path, partial: bool = False) -> None:

        archive_root = Path(getattr(settings, 'GDPR_ARCHIVE_ROOT', '/tmp/gdpr_exports'))
        archive_root.mkdir(parents=True, exist_ok=True)
        zip_path = archive_root / f'export_{req.pk}.zip'

        date_str = req.created_at.strftime('%Y-%m-%d')
        zip_root = f'export_{date_str}'

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            readme = self._build_readme(req, partial)
            zf.writestr(f'{zip_root}/README.txt', readme)

            if staging_dir.exists():
                for file in staging_dir.rglob('*'):
                    if file.is_file():
                        zf.write(file, f'{zip_root}/{file.relative_to(staging_dir)}')

        req.archive_path = str(zip_path)
        req.status       = DataExportRequest.STATUS_READY
        req.save(update_fields=['archive_path', 'status'])
        req.generate_download_token()

        logger.info('GDPR export archive assembled [request=%s, partial=%s]', req.pk, partial)

    def _build_readme(self, req: DataExportRequest, partial: bool) -> str:
        lines = [
            'Your personal data export',
            f'Requested: {req.created_at.strftime("%Y-%m-%d %H:%M UTC")}',
            '',
        ]
        if partial:
            missing = [
                p.service for p in req.parts.exclude(status=DataExportPart.STATUS_DONE)
            ]
            lines += [
                'NOTE: This is a partial export. The following sections could not be',
                'included within the processing deadline:',
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
    # Account closure
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
        """Hard delete + anonymize. Called by account-closure-worker after grace period."""
        closure.status = AccountClosureRequest.STATUS_DELETING
        closure.save(update_fields=['status'])

        user_id = closure.user_id

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

        closure.status     = AccountClosureRequest.STATUS_DELETED
        closure.deleted_at = timezone.now()
        closure.save(update_fields=['status', 'deleted_at'])
        logger.info('GDPR account deletion complete [user_id=%s]', user_id)

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
