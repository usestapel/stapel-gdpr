import logging
import os

from django.http import FileResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    extend_schema,
    inline_serializer,
)
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.views import APIView
from stapel_core.django.api.errors import (
    ERR_400_BAD_REQUEST,
    ERR_403_FORBIDDEN,
    StapelErrorResponse,
    StapelResponse,
    error_500_internal,
)
from stapel_core.django.api.permissions import IsServiceRequest
from stapel_core.django.openapi.schemas import StapelErrorSerializer

from .dto import ClosureStatusDTO, ExportRequestDTO, ExportStatusDTO
from .errors import (
    ERR_404_EXPORT_NOT_FOUND,
    ERR_404_NO_ACTIVE_CLOSURE,
    ERR_409_CLOSURE_PENDING,
    ERR_409_EXPORT_COOLDOWN,
    ERR_409_LEGAL_HOLD,
    ERR_410_DOWNLOAD_CONSUMED,
    ERR_410_DOWNLOAD_EXPIRED,
    ERR_425_EXPORT_NOT_READY,
    ERR_503_CLOSURE_UNAVAILABLE,
    SessionRevocationUnavailable,
)
from .guards import AccountNotClosed
from .models import AccountClosureRequest, DataExportRequest, hash_download_token
from .orchestrator import gdpr_orchestrator
from .serializers import (
    ClosureStatusSerializer,
    ExportRequestSerializer,
    ExportStatusSerializer,
)

logger = logging.getLogger(__name__)


class GDPRAPIView(APIView):
    """Base view exposing serializer seams.

    Every concrete view declares ``request_serializer_class`` /
    ``response_serializer_class`` (``None`` when that direction carries no
    serialized payload). Subclasses may swap either class attribute — or
    override the getters — to customize the request/response envelopes
    without rewriting the method bodies.
    """

    request_serializer_class = None
    response_serializer_class = None

    def get_request_serializer_class(self):
        return self.request_serializer_class

    def get_response_serializer_class(self):
        return self.response_serializer_class


# =============================================================================
# Data export — GDPR Art. 15 / 20
# =============================================================================


class DataExportRequestView(GDPRAPIView):
    permission_classes = [permissions.IsAuthenticated, AccountNotClosed]
    request_serializer_class = None
    response_serializer_class = ExportRequestSerializer

    @extend_schema(
        summary="Request personal data export",
        description="Initiates an async export job. Archive ready within 48 h. Max once per 30 days.",
        request=None,
        responses={
            202: ExportRequestSerializer,
            409: StapelErrorSerializer,
        },
        tags=["GDPR"],
    )
    def post(self, request: Request):  # noqa: R007
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
        return StapelResponse(self.get_response_serializer_class()(dto), status=202)


class DataExportStatusView(GDPRAPIView):
    permission_classes = [permissions.IsAuthenticated, AccountNotClosed]
    request_serializer_class = None
    response_serializer_class = ExportStatusSerializer

    @extend_schema(
        summary="Get data export status",
        responses={200: ExportStatusSerializer},
        tags=["GDPR"],
    )
    def get(self, request: Request):  # noqa: R007
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
            download_available=(
                is_ready
                and bool(export_req.download_token_hash)
                and export_req.download_consumed_at is None
            ),
            expires_at=expires_at,
            # An export missing sections must say so where the user looks,
            # not only inside a README they may never open.
            is_partial=export_req.is_partial,
            missing_services=list(export_req.missing_services or []),
        )
        return StapelResponse(self.get_response_serializer_class()(dto))


class DataExportDownloadView(GDPRAPIView):
    """Spend the single-use download token. POST only, token in the body.

    The GET variant took the token in the query string, which put a live
    credential to a full personal-data archive into access logs, browser
    history, Referer headers and every proxy in between — and it was never
    consumed, so the "single-use" token in the docs was in fact a reusable
    bearer credential for seven days. Now: body only, consumed atomically on
    first success, archive deleted the moment it is served, ``no-store`` on
    the response.
    """

    permission_classes = [permissions.IsAuthenticated, AccountNotClosed]
    # Token is a raw body field; the payload is a file.
    request_serializer_class = None
    response_serializer_class = None

    @extend_schema(
        summary="Download data export archive",
        description=(
            "Spends the single-use token and streams the ZIP archive. The "
            "token travels in the request body — never in the URL — and is "
            "consumed on the first successful download; the archive is "
            "deleted at the same moment. Bound to the authenticated user."
        ),
        request=inline_serializer(
            name="GDPRDownloadTokenRequest",
            fields={
                "token": serializers.CharField(
                    help_text=(
                        "Single-use download token bound to the authenticated user."
                    )
                )
            },
        ),
        responses={
            (200, "application/zip"): OpenApiTypes.BINARY,
            404: StapelErrorSerializer,
            410: StapelErrorSerializer,
            425: StapelErrorSerializer,
        },
        tags=["GDPR"],
    )
    def post(self, request: Request):  # noqa: R007
        return self._serve(request, str(request.data.get("token", "")))

    def _serve(self, request: Request, token: str):
        if not token:
            return StapelErrorResponse(404, ERR_404_EXPORT_NOT_FOUND)

        # Token is always bound to the authenticated user — knowing the token
        # alone is not enough to fetch someone else's archive. Only the digest
        # is stored, so the lookup hashes first.
        export_req = DataExportRequest.objects.filter(
            user_id=request.user.pk,
            download_token_hash=hash_download_token(token),
        ).first()

        if not export_req:
            return StapelErrorResponse(404, ERR_404_EXPORT_NOT_FOUND)

        if export_req.download_consumed_at is not None:
            return StapelErrorResponse(410, ERR_410_DOWNLOAD_CONSUMED)

        if export_req.status != DataExportRequest.STATUS_READY:
            return StapelErrorResponse(425, ERR_425_EXPORT_NOT_READY)

        if (
            export_req.download_expires_at
            and timezone.now() > export_req.download_expires_at
        ):
            from .tasks import expire_export

            expire_export(export_req)
            return StapelErrorResponse(410, ERR_410_DOWNLOAD_EXPIRED)

        archive_path = export_req.archive_path
        if not archive_path or not os.path.exists(archive_path):
            logger.error(
                "GDPR archive file missing: request=%s path=%s",
                export_req.pk,
                archive_path,
            )
            return error_500_internal()

        # Consume BEFORE serving: the loser of a concurrent race must get 410
        # rather than a second copy of the archive.
        if not export_req.consume_download_token(token):
            return StapelErrorResponse(410, ERR_410_DOWNLOAD_CONSUMED)

        # The file is opened before it is unlinked: POSIX keeps the inode
        # alive for this handle, so the response still streams while the
        # archive stops existing for everyone else.
        handle = open(archive_path, "rb")
        try:
            os.remove(archive_path)
        except OSError as e:  # pragma: no cover - filesystem-dependent
            logger.error("GDPR archive removal failed: request=%s path=%s err=%s",
                         export_req.pk, archive_path, e)
        DataExportRequest.objects.filter(pk=export_req.pk).update(archive_path=None)

        response = FileResponse(
            handle,
            content_type="application/zip",
            as_attachment=True,
            filename=f"personal_data_export_{export_req.created_at.strftime('%Y-%m-%d')}.zip",
        )
        # An archive of everything we know about a person must not sit in a
        # shared cache or on disk in the browser's cache directory.
        response["Cache-Control"] = "no-store, private"
        response["Pragma"] = "no-cache"
        return response


# =============================================================================
# Account closure — GDPR Art. 17
# =============================================================================


class AccountCloseView(GDPRAPIView):
    permission_classes = [permissions.IsAuthenticated, AccountNotClosed]
    request_serializer_class = None
    response_serializer_class = ClosureStatusSerializer

    @extend_schema(
        summary="Initiate account closure",
        description=(
            "Starts a 30-day grace period. The account is deactivated and all "
            "of its sessions are revoked immediately. Can be cancelled by "
            "logging in during the grace period."
        ),
        request=None,
        responses={
            202: ClosureStatusSerializer,
            409: StapelErrorSerializer,
            503: StapelErrorSerializer,
        },
        tags=["GDPR"],
    )
    def post(self, request: Request):  # noqa: R007
        try:
            closure = gdpr_orchestrator.initiate_closure(request.user.pk)
        except SessionRevocationUnavailable as e:
            # Deliberately a 503, not a 500: the deployment is misconfigured,
            # the request was fine, and retrying after it is fixed is the
            # right client behavior. Closing an account while its live tokens
            # keep working is not an acceptable degraded mode.
            logger.error("GDPR closure refused, sessions cannot be revoked: %s", e)
            return StapelErrorResponse(503, ERR_503_CLOSURE_UNAVAILABLE)
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
        return StapelResponse(self.get_response_serializer_class()(dto), status=202)


class AccountCancelCloseView(GDPRAPIView):
    permission_classes = [permissions.IsAuthenticated, AccountNotClosed]
    request_serializer_class = None
    response_serializer_class = ClosureStatusSerializer

    @extend_schema(
        summary="Cancel account closure during grace period",
        request=None,
        responses={
            200: ClosureStatusSerializer,
            404: StapelErrorSerializer,
        },
        tags=["GDPR"],
    )
    def post(self, request: Request):  # noqa: R007
        try:
            closure = gdpr_orchestrator.cancel_closure(request.user.pk)
        except ValueError:
            return StapelErrorResponse(404, ERR_404_NO_ACTIVE_CLOSURE)

        dto = ClosureStatusDTO(
            status=closure.status,
            grace_ends_at=closure.grace_ends_at.isoformat(),
            can_cancel=False,
        )
        return StapelResponse(self.get_response_serializer_class()(dto))


class AccountCloseStatusView(GDPRAPIView):
    permission_classes = [permissions.IsAuthenticated, AccountNotClosed]
    request_serializer_class = None
    response_serializer_class = ClosureStatusSerializer

    @extend_schema(
        summary="Get account closure status",
        responses={200: ClosureStatusSerializer},
        tags=["GDPR"],
    )
    def get(self, request: Request):  # noqa: R007
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
        return StapelResponse(self.get_response_serializer_class()(dto))


# =============================================================================
# Internal — called by remote services in microservices mode
# =============================================================================


class ExportPartReadyView(GDPRAPIView):
    """Remote service notifies us that its export portion is staged and ready."""

    # Body is a raw {"service", "bucket_path"} dict; response is an empty 204.
    request_serializer_class = None
    response_serializer_class = None

    # The declaration IS the enforcement (audit 2026-08-11). This used to say
    # IsAuthenticated and check IsServiceRequest inside post(), which meant
    # any subclass overriding post(), and every permission introspection or
    # audit of this module, saw "any logged-in user" on the endpoint that
    # marks another service's export part complete. DRF ANDs the list, so
    # this states exactly what was already required: a service call, made by
    # an authenticated caller.
    permission_classes = [IsServiceRequest, permissions.IsAuthenticated]

    @extend_schema(exclude=True)
    def post(self, request: Request, request_id: int):  # noqa: R007
        # Belt and braces: a subclass that swaps permission_classes for a
        # looser list still cannot mark somebody else's part complete.
        if not IsServiceRequest().has_permission(request, self):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN)

        service = request.data.get("service", "")
        if not service:
            return StapelErrorResponse(400, ERR_400_BAD_REQUEST)
        bucket_path = request.data.get("bucket_path", "")

        # mark_part_ready is keyed by correlation_id — resolve it from the
        # request row addressed by this URL.
        from .models import DataExportRequest

        req = DataExportRequest.objects.filter(pk=request_id).first()
        if req is None:
            return StapelErrorResponse(400, ERR_400_BAD_REQUEST)

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
