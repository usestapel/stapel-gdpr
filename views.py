import logging
import os

from django.http import FileResponse
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.views import APIView
from stapel_core.django.api.errors import (
    StapelErrorResponse,
    StapelResponse,
    error_500_internal,
)

from .dto import ClosureStatusDTO, ExportRequestDTO, ExportStatusDTO
from .errors import (
    ERR_404_EXPORT_NOT_FOUND,
    ERR_404_NO_ACTIVE_CLOSURE,
    ERR_409_CLOSURE_PENDING,
    ERR_409_EXPORT_COOLDOWN,
    ERR_409_LEGAL_HOLD,
    ERR_410_DOWNLOAD_EXPIRED,
    ERR_425_EXPORT_NOT_READY,
)
from .models import AccountClosureRequest, DataExportRequest
from .orchestrator import gdpr_orchestrator
from .serializers import (
    ClosureStatusSerializer,
    ExportRequestSerializer,
    ExportStatusSerializer,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Data export — GDPR Art. 15 / 20
# =============================================================================


class DataExportRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Request personal data export",
        description="Initiates an async export job. Archive ready within 48 h. Max once per 30 days.",
        responses={202: ExportRequestSerializer},
        tags=["GDPR"],
    )
    def post(self, request: Request):
        try:
            export_req = gdpr_orchestrator.request_export(request.user.pk)
        except ValueError as e:
            if str(e) == "export_cooldown":
                return StapelErrorResponse(409, ERR_409_EXPORT_COOLDOWN)
            return error_500_internal()

        # Enqueue async worker
        from .tasks import run_data_export

        run_data_export.delay(export_req.pk)

        dto = ExportRequestDTO(
            request_id=export_req.pk,
            status=export_req.status,
            message="Your archive will be ready within 48 hours. We will notify you when it is done.",
        )
        return StapelResponse(ExportRequestSerializer(dto), status=202)


class DataExportStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Get data export status",
        responses={200: ExportStatusSerializer},
        tags=["GDPR"],
    )
    def get(self, request: Request):
        export_req = (
            DataExportRequest.objects.filter(
                user_id=request.user.pk,
            )
            .exclude(status=DataExportRequest.STATUS_EXPIRED)
            .order_by("-created_at")
            .first()
        )

        if not export_req:
            return StapelErrorResponse(404, ERR_404_EXPORT_NOT_FOUND)

        parts_done = export_req.parts.filter(status="done").count()
        parts_total = export_req.parts.count()
        is_ready = export_req.status == DataExportRequest.STATUS_READY
        expires_at = (
            export_req.download_expires_at.isoformat()
            if export_req.download_expires_at
            else None
        )

        dto = ExportStatusDTO(
            request_id=export_req.pk,
            status=export_req.status,
            parts_done=parts_done,
            parts_total=parts_total,
            download_available=is_ready and bool(export_req.download_token),
            expires_at=expires_at,
        )
        return StapelResponse(ExportStatusSerializer(dto))


class DataExportDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Download data export archive",
        description="Returns the ZIP archive. Link is valid for 7 days after export is ready.",
        responses={200: None},
        tags=["GDPR"],
    )
    def get(self, request: Request):
        return self._serve(request, request.query_params.get("token", ""))

    @extend_schema(
        summary="Download data export archive (token in body)",
        description=(
            "Same as GET but the single-use token travels in the request body "
            "instead of the URL, so it never lands in access logs or referrers. "
            "Bound to the authenticated user."
        ),
        responses={200: None},
        tags=["GDPR"],
    )
    def post(self, request: Request):
        return self._serve(request, str(request.data.get("token", "")))

    def _serve(self, request: Request, token: str):
        if not token:
            return StapelErrorResponse(404, ERR_404_EXPORT_NOT_FOUND)

        # Token is always bound to the authenticated user — knowing the token
        # alone is not enough to fetch someone else's archive.
        export_req = DataExportRequest.objects.filter(
            user_id=request.user.pk,
            download_token=token,
        ).first()

        if not export_req:
            return StapelErrorResponse(404, ERR_404_EXPORT_NOT_FOUND)

        if export_req.status != DataExportRequest.STATUS_READY:
            return StapelErrorResponse(425, ERR_425_EXPORT_NOT_READY)

        if (
            export_req.download_expires_at
            and timezone.now() > export_req.download_expires_at
        ):
            export_req.status = DataExportRequest.STATUS_EXPIRED
            export_req.save(update_fields=["status"])
            return StapelErrorResponse(410, ERR_410_DOWNLOAD_EXPIRED)

        if not export_req.archive_path or not os.path.exists(export_req.archive_path):
            logger.error(
                "GDPR archive file missing: request=%s path=%s",
                export_req.pk,
                export_req.archive_path,
            )
            return error_500_internal()

        response = FileResponse(
            open(export_req.archive_path, "rb"),
            content_type="application/zip",
            as_attachment=True,
            filename=f"personal_data_export_{export_req.created_at.strftime('%Y-%m-%d')}.zip",
        )
        return response


# =============================================================================
# Account closure — GDPR Art. 17
# =============================================================================


class AccountCloseView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Initiate account closure",
        description="Starts a 30-day grace period. Account is deactivated immediately. Can be cancelled by logging in.",
        responses={202: ClosureStatusSerializer},
        tags=["GDPR"],
    )
    def post(self, request: Request):
        try:
            closure = gdpr_orchestrator.initiate_closure(request.user.pk)
        except ValueError as e:
            if str(e) == "closure_already_pending":
                return StapelErrorResponse(409, ERR_409_CLOSURE_PENDING)
            if str(e) == "legal_hold":
                return StapelErrorResponse(409, ERR_409_LEGAL_HOLD)
            return error_500_internal()

        dto = ClosureStatusDTO(
            status=closure.status,
            grace_ends_at=closure.grace_ends_at.isoformat(),
            can_cancel=True,
        )
        return StapelResponse(ClosureStatusSerializer(dto), status=202)


class AccountCancelCloseView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Cancel account closure during grace period",
        responses={200: ClosureStatusSerializer},
        tags=["GDPR"],
    )
    def post(self, request: Request):
        try:
            closure = gdpr_orchestrator.cancel_closure(request.user.pk)
        except ValueError:
            return StapelErrorResponse(404, ERR_404_NO_ACTIVE_CLOSURE)

        dto = ClosureStatusDTO(
            status=closure.status,
            grace_ends_at=closure.grace_ends_at.isoformat(),
            can_cancel=False,
        )
        return StapelResponse(ClosureStatusSerializer(dto))


class AccountCloseStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Get account closure status",
        responses={200: ClosureStatusSerializer},
        tags=["GDPR"],
    )
    def get(self, request: Request):
        closure = (
            AccountClosureRequest.objects.filter(
                user_id=request.user.pk,
            )
            .exclude(status=AccountClosureRequest.STATUS_CANCELLED)
            .order_by("-initiated_at")
            .first()
        )

        if not closure:
            return StapelErrorResponse(404, ERR_404_NO_ACTIVE_CLOSURE)

        dto = ClosureStatusDTO(
            status=closure.status,
            grace_ends_at=closure.grace_ends_at.isoformat(),
            can_cancel=closure.status == AccountClosureRequest.STATUS_GRACE,
        )
        return StapelResponse(ClosureStatusSerializer(dto))


# =============================================================================
# Internal — called by remote services in microservices mode
# =============================================================================


class ExportPartReadyView(APIView):
    """Remote service notifies us that its export portion is staged and ready."""

    permission_classes = [
        permissions.IsAuthenticated
    ]  # replaced by IsServiceRequest in production

    @extend_schema(exclude=True)
    def post(self, request: Request, request_id: int):
        from stapel_core.django.api.permissions import IsServiceRequest

        if not IsServiceRequest().has_permission(request, self):
            return StapelErrorResponse(403, "error.403.forbidden")

        service = request.data.get("service", "")
        if not service:
            return StapelErrorResponse(400, "error.400.bad_request")
        bucket_path = request.data.get("bucket_path", "")

        # mark_part_ready is keyed by correlation_id — resolve it from the
        # request row addressed by this URL.
        from .models import DataExportRequest

        req = DataExportRequest.objects.filter(pk=request_id).first()
        if req is None:
            return StapelErrorResponse(400, "error.400.bad_request")

        try:
            gdpr_orchestrator.mark_part_ready(req.correlation_id, service, bucket_path)
        except Exception as e:
            logger.error(
                "mark_part_ready failed: request=%s service=%s err=%s",
                request_id,
                service,
                e,
            )
            return error_500_internal()

        return StapelResponse(status=204)
