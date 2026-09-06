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
from stapel_core.django.captcha import captcha_protected
from stapel_core.django.openapi.schemas import StapelErrorSerializer

from .dto import (
    ClosureStatusDTO,
    DataOwnerHealthDTO,
    DsarStatusDTO,
    ErasurePartDTO,
    ErasureStatusDTO,
    ExportRequestDTO,
    ExportStatusDTO,
    SubprocessorObligationDTO,
)
from .errors import (
    ERR_400_UNKNOWN_DSAR_KIND,
    ERR_400_UNKNOWN_SUBJECT,
    ERR_403_ERASURE_FORBIDDEN,
    ERR_404_DSAR_NOT_FOUND,
    ERR_404_ERASURE_NOT_FOUND,
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
from .guards import AccountNotClosed, erasure_authorized
from .models import (
    AccountClosureRequest,
    DataExportRequest,
    DataOwnerHealth,
    DsarRequest,
    ErasureRequest,
    hash_download_token,
)
from .orchestrator import gdpr_orchestrator
from .serializers import (
    ClosureStatusSerializer,
    DataOwnerHealthSerializer,
    DsarStatusSerializer,
    ErasureStatusSerializer,
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
    """Stop a closure that is still inside its grace period.

    The account is reactivated and a ``user.deletion_cancelled`` comm action
    is emitted — the mirror of the ``user.deletion_initiated`` that started
    the closure, so every consumer that took a reversible action on the
    initiation (suppressed notifications, hidden content, suspended
    memberships) is told to lift it instead of waiting for its next sync.
    """

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
        # The 404 is the ordinary answer, not an exception: almost nobody has
        # a closure on record. Undeclared, a generated client models this
        # endpoint as always-succeeding and every caller learns about the
        # refusal from a runtime throw.
        responses={200: ClosureStatusSerializer, 404: StapelErrorSerializer},
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

    # Published, not hidden. This endpoint is service-to-service, but it is
    # still part of the contract another service has to implement against, and
    # a hidden operation is one nobody can generate a typed caller for — the
    # remote side ends up hand-rolling the body shape and discovering the
    # required `service` field from a 400.
    @extend_schema(
        summary="Mark an export part ready (service-to-service)",
        description=(
            "Called by a remote data owner once its portion of an export is "
            "staged. Requires a service credential (`IsServiceRequest`), not a "
            "user session. When the last outstanding part arrives the archive "
            "is assembled."
        ),
        request=inline_serializer(
            name="GDPRExportPartReadyRequest",
            fields={
                "service": serializers.CharField(
                    help_text="The data owner's section name, e.g. \"recordings\"."
                ),
                "bucket_path": serializers.CharField(
                    required=False, allow_blank=True,
                    help_text="Where the staged payload lives, empty for in-process owners.",
                ),
            },
        ),
        responses={
            204: None,
            400: StapelErrorSerializer,
            403: StapelErrorSerializer,
        },
        tags=["GDPR"],
    )
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


# =============================================================================
# Subject-scoped erasure — GDPR Art. 17 for anything that is not an account
# =============================================================================


def _erasure_dto(request_row: ErasureRequest) -> ErasureStatusDTO:
    """One erasure, with everything a product needs to explain the wait."""
    return ErasureStatusDTO(
        request_id=request_row.pk,
        subject_type=request_row.subject_type,
        subject_key=request_row.subject_key,
        workspace_id=request_row.workspace_id,
        state=request_row.state,
        # The machine's state and the report's word are not the same thing:
        # a TIMEOUT is a finished machine and an unfinished erasure, and
        # until 0.5.4 nothing above the ORM ever said the second half.
        outcome=request_row.outcome,
        origin=request_row.origin,
        requested_at=request_row.requested_at.isoformat(),
        due_at=request_row.due_at.isoformat(),
        fully_erased_by=request_row.fully_erased_by.isoformat(),
        completed_at=(
            request_row.completed_at.isoformat() if request_row.completed_at else None
        ),
        grace_ends_at=(
            request_row.grace_ends_at.isoformat() if request_row.grace_ends_at else None
        ),
        parts=[
            ErasurePartDTO(
                owner=part.owner,
                state=part.state,
                receipt_at=part.receipt_at.isoformat() if part.receipt_at else None,
                receipt_id=part.receipt_id,
                counts=part.counts or {},
                unanswered=part.unanswered,
            )
            for part in request_row.parts.all()
        ],
        obligations=[
            SubprocessorObligationDTO(
                provider=obligation.provider,
                window_days=obligation.window_days,
                due_at=obligation.due_at.isoformat(),
                state=obligation.state,
            )
            for obligation in request_row.obligations.all()
        ],
        unreceipted_owners=request_row.unreceipted_owners,
        unanswered_owners=request_row.unanswered_owners,
    )


class ErasureRequestView(GDPRAPIView):
    """The host's "erase this entity" hook, after its own soft-delete.

    Deliberately not an ownership check of its own: only the host knows
    whether this user owns that recording. The check is the
    ``ERASURE_AUTHORIZER`` seam, and it defaults to staff-only rather than
    to a permissive guess (``guards.erasure_authorized``).
    """

    permission_classes = [permissions.IsAuthenticated, AccountNotClosed]
    request_serializer_class = None
    response_serializer_class = ErasureStatusSerializer

    @extend_schema(
        summary="Request erasure of one subject",
        description=(
            "Opens an erasure for a subject the host has already removed from "
            "its UI: one receipt slot per data owner that claims this subject "
            "type, a purge SLA in `due_at`, and a `gdpr.erasure.requested` "
            "action. Authorization is the host's `ERASURE_AUTHORIZER` "
            "callable; the default is staff only."
        ),
        request=inline_serializer(
            name="GDPRErasureRequest",
            fields={
                "subject_type": serializers.CharField(
                    help_text='One of STAPEL_GDPR["SUBJECT_TYPES"], e.g. "recording".'
                ),
                "subject_key": serializers.CharField(
                    help_text="The host's own id for the subject."
                ),
                "workspace_id": serializers.CharField(
                    required=False, allow_blank=True,
                    help_text="Workspace the subject belongs to, for owners that partition by it.",
                ),
            },
        ),
        responses={
            202: ErasureStatusSerializer,
            400: StapelErrorSerializer,
            403: StapelErrorSerializer,
        },
        tags=["GDPR"],
    )
    def post(self, request: Request):  # noqa: R007
        subject_type = str(request.data.get("subject_type", "")).strip()
        subject_key = str(request.data.get("subject_key", "")).strip()
        if not subject_type or not subject_key:
            return StapelErrorResponse(400, ERR_400_BAD_REQUEST)

        if not erasure_authorized(request, subject_type, subject_key):
            return StapelErrorResponse(403, ERR_403_ERASURE_FORBIDDEN)

        try:
            erasure = gdpr_orchestrator.request_erasure(
                subject_type,
                subject_key,
                workspace_id=(request.data.get("workspace_id") or None),
                requested_by=request.user.pk,
            )
        except ValueError as e:
            if str(e) == "unknown_subject_type":
                return StapelErrorResponse(400, ERR_400_UNKNOWN_SUBJECT)
            return error_500_internal()

        return StapelResponse(
            self.get_response_serializer_class()(_erasure_dto(erasure)), status=202,
        )


class ErasureStatusView(GDPRAPIView):
    """State, receipts, obligations and `fully_erased_by` for one erasure."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = None
    response_serializer_class = ErasureStatusSerializer

    @extend_schema(
        summary="Get erasure status",
        responses={200: ErasureStatusSerializer, 404: StapelErrorSerializer},
        tags=["GDPR"],
    )
    def get(self, request: Request, request_id: int):  # noqa: R007
        erasure = ErasureRequest.objects.filter(pk=request_id).first()
        if erasure is None:
            return StapelErrorResponse(404, ERR_404_ERASURE_NOT_FOUND)
        # A requester sees their own; anyone else needs the same authority
        # that could have opened it. Otherwise the endpoint enumerates every
        # deletion in the deployment by integer id.
        if str(erasure.requested_by) != str(request.user.pk) and not erasure_authorized(
            request, erasure.subject_type, erasure.subject_key,
        ):
            return StapelErrorResponse(404, ERR_404_ERASURE_NOT_FOUND)
        return StapelResponse(self.get_response_serializer_class()(_erasure_dto(erasure)))


class MyErasuresView(GDPRAPIView):
    """The caller's own erasures — the "pending deletion" list a UI shows."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = None
    response_serializer_class = ErasureStatusSerializer

    @extend_schema(
        summary="List my erasure requests",
        responses={200: ErasureStatusSerializer(many=True)},
        tags=["GDPR"],
    )
    def get(self, request: Request):  # noqa: R007
        rows = ErasureRequest.objects.filter(
            requested_by=request.user.pk,
        ).prefetch_related("parts", "obligations")
        serializer = self.get_response_serializer_class()(
            [_erasure_dto(row) for row in rows], many=True,
        )
        return StapelResponse(serializer)


# =============================================================================
# Data owner health — silence, made visible
# =============================================================================


class DataOwnerHealthView(GDPRAPIView):
    """Which declared owners are answering, and which have gone quiet."""

    permission_classes = [permissions.IsAdminUser]
    request_serializer_class = None
    response_serializer_class = DataOwnerHealthSerializer

    @extend_schema(
        summary="Data owner liveness table (staff)",
        description=(
            "The table behind the `gdpr.W006` boot warning: every declared "
            "data owner, when it last answered `gdpr.owner.probe`, and "
            "whether the subjects it claims match the inventory."
        ),
        responses={200: DataOwnerHealthSerializer(many=True)},
        tags=["GDPR"],
    )
    def get(self, request: Request):  # noqa: R007
        from datetime import timedelta

        from .conf import gdpr_settings
        from .owners import data_owner_report

        cutoff = timezone.now() - timedelta(
            hours=float(gdpr_settings.OWNER_ALIVE_MAX_AGE_HOURS or 48),
        )
        rows = {row.owner: row for row in DataOwnerHealth.objects.all()}
        report = data_owner_report()

        dtos = []
        for owner in report.owners:
            row = rows.get(owner.name)
            last_alive = row.last_alive_at if row else None
            dtos.append(
                DataOwnerHealthDTO(
                    owner=owner.name,
                    alive=bool(last_alive and last_alive >= cutoff),
                    last_alive_at=last_alive.isoformat() if last_alive else None,
                    last_probe_at=(
                        row.last_probe_at.isoformat()
                        if row and row.last_probe_at else None
                    ),
                    declared_subject_types=list(owner.subjects),
                    answered_subject_types=list(row.answered_subject_types) if row else [],
                )
            )
        return StapelResponse(self.get_response_serializer_class()(dtos, many=True))


# =============================================================================
# DSAR intake — GDPR Art. 12/15/16/17/20
# =============================================================================


def _dsar_dto(dsar: DsarRequest) -> DsarStatusDTO:
    return DsarStatusDTO(
        request_id=dsar.pk,
        kind=dsar.kind,
        channel=dsar.channel,
        subject_email=dsar.subject_email,
        state=dsar.state,
        received_at=dsar.received_at.isoformat(),
        ack_due_at=dsar.ack_due_at.isoformat(),
        ack_sent_at=dsar.ack_sent_at.isoformat() if dsar.ack_sent_at else None,
        resolve_due_at=dsar.resolve_due_at.isoformat(),
        erasure_request_id=dsar.erasure_request_id,
        export_request_id=dsar.export_request_id,
        note=dsar.note,
    )


class DsarView(GDPRAPIView):
    """Intake and queue for data-subject requests.

    ``POST`` takes both an authenticated app request and an anonymous one
    from a public /privacy form — the form is the channel a regulator
    expects to exist, and it cannot require a login. The anonymous variant
    goes through the core's tiered captcha policy
    (``@captcha_protected``); an unconfigured captcha backend leaves the
    form open exactly as before, which is a host's decision to make.
    """

    permission_classes = [permissions.AllowAny]
    request_serializer_class = None
    response_serializer_class = DsarStatusSerializer

    @extend_schema(
        summary="Submit a data-protection request",
        description=(
            "Records the request, sends the acknowledgement that satisfies "
            "the three-business-day clock, notifies staff, and — for a "
            "request matched to an account — starts the machine that answers "
            "it (erasure: the cancellable closure; access/portability: a data "
            "export). Anonymous submissions require a captcha token when a "
            "captcha backend is configured."
        ),
        request=inline_serializer(
            name="GDPRDsarRequest",
            fields={
                "kind": serializers.ChoiceField(
                    choices=[k for k, _ in DsarRequest.KIND_CHOICES],
                    help_text="access, erasure, rectification or portability.",
                ),
                "email": serializers.EmailField(
                    required=False,
                    help_text="Required for anonymous submissions; ignored when authenticated.",
                ),
                "note": serializers.CharField(
                    required=False, allow_blank=True,
                    help_text="What the subject is asking for, in their words.",
                ),
                "captcha_token": serializers.CharField(
                    required=False, allow_blank=True,
                    help_text="Captcha token for anonymous submissions.",
                ),
            },
        ),
        responses={
            201: DsarStatusSerializer,
            400: StapelErrorSerializer,
        },
        tags=["GDPR"],
    )
    @captcha_protected(action="gdpr_dsar")
    def post(self, request: Request):  # noqa: R007
        from .dsar import create_dsar

        kind = str(request.data.get("kind", "")).strip()
        if kind not in {k for k, _ in DsarRequest.KIND_CHOICES}:
            return StapelErrorResponse(400, ERR_400_UNKNOWN_DSAR_KIND)

        user = getattr(request, "user", None)
        authenticated = bool(user and user.is_authenticated)
        email = (
            getattr(user, "email", "") if authenticated
            else str(request.data.get("email", "")).strip()
        )
        if not email:
            return StapelErrorResponse(400, ERR_400_BAD_REQUEST)

        dsar = create_dsar(
            kind=kind,
            subject_email=email,
            channel=DsarRequest.CHANNEL_APP if authenticated else DsarRequest.CHANNEL_FORM,
            user_id=user.pk if authenticated else None,
            note=str(request.data.get("note", "")),
        )
        return StapelResponse(self.get_response_serializer_class()(_dsar_dto(dsar)), status=201)

    @extend_schema(
        summary="List data-protection requests (staff)",
        # The view is AllowAny because POST has to accept an anonymous
        # submission, so the staff check on GET is hand-rolled in the handler
        # and drf-spectacular cannot infer it from permission_classes. Declared
        # here or it is invisible to every consumer of the contract.
        responses={
            200: DsarStatusSerializer(many=True),
            403: StapelErrorSerializer,
        },
        tags=["GDPR"],
    )
    def get(self, request: Request):  # noqa: R007
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated and user.is_staff):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN)
        rows = DsarRequest.objects.all()
        serializer = self.get_response_serializer_class()(
            [_dsar_dto(row) for row in rows], many=True,
        )
        return StapelResponse(serializer)


class DsarDetailView(GDPRAPIView):
    """Staff triage of one request: state, note, and matching it to a person."""

    permission_classes = [permissions.IsAdminUser]
    request_serializer_class = None
    response_serializer_class = DsarStatusSerializer

    @extend_schema(
        summary="Update a data-protection request (staff)",
        description=(
            "Setting `user_id` on a request that arrived anonymously matches "
            "it to an account and wires it to the mechanism that answers it — "
            "intake deliberately refuses to do that itself, since turning an "
            "unverified email into an erasure is a deletion oracle."
        ),
        request=inline_serializer(
            name="GDPRDsarPatch",
            fields={
                "state": serializers.ChoiceField(
                    choices=[s for s, _ in DsarRequest.STATE_CHOICES], required=False,
                ),
                "note": serializers.CharField(required=False, allow_blank=True),
                "user_id": serializers.CharField(required=False, allow_blank=True),
            },
        ),
        responses={200: DsarStatusSerializer, 404: StapelErrorSerializer},
        tags=["GDPR"],
    )
    def patch(self, request: Request, dsar_id: int):  # noqa: R007
        from .dsar import wire_dsar

        dsar = DsarRequest.objects.filter(pk=dsar_id).first()
        if dsar is None:
            return StapelErrorResponse(404, ERR_404_DSAR_NOT_FOUND)

        updated = []
        state = request.data.get("state")
        if state is not None:
            if state not in {s for s, _ in DsarRequest.STATE_CHOICES}:
                return StapelErrorResponse(400, ERR_400_BAD_REQUEST)
            dsar.state = state
            updated.append("state")
        if "note" in request.data:
            dsar.note = str(request.data.get("note") or "")
            updated.append("note")
        matched = False
        if request.data.get("user_id"):
            dsar.user_id = request.data["user_id"]
            updated.append("user_id")
            matched = True
        if updated:
            dsar.save(update_fields=updated)
        if matched:
            wire_dsar(dsar)
            dsar.refresh_from_db()

        return StapelResponse(self.get_response_serializer_class()(_dsar_dto(dsar)))

    @extend_schema(
        summary="Get one data-protection request (staff)",
        responses={200: DsarStatusSerializer, 404: StapelErrorSerializer},
        tags=["GDPR"],
    )
    def get(self, request: Request, dsar_id: int):  # noqa: R007
        dsar = DsarRequest.objects.filter(pk=dsar_id).first()
        if dsar is None:
            return StapelErrorResponse(404, ERR_404_DSAR_NOT_FOUND)
        return StapelResponse(self.get_response_serializer_class()(_dsar_dto(dsar)))
