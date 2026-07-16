"""View edge matrix (download GET/POST), error envelopes, serializer seams, admin."""
import zipfile

import pytest
from django.utils import timezone

from stapel_gdpr.models import DataExportRequest
from stapel_gdpr.orchestrator import gdpr_orchestrator


def _ready_request(user, tmp_path):
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("README.txt", "hello")
    req = DataExportRequest.objects.create(
        user_id=user.pk,
        status=DataExportRequest.STATUS_READY,
        archive_path=str(archive),
        deadline=timezone.now(),
    )
    token = req.generate_download_token()
    return req, token


def _assert_error_envelope(resp, status, localizable_error):
    assert resp.status_code == status
    body = resp.json()
    assert body["localizable_error"] == localizable_error
    assert body["error"]  # human-readable text resolved from the registry
    assert "params" in body


@pytest.mark.django_db
class TestDownloadMatrix:
    def test_get_without_token_404(self, authed_client, user):
        _assert_error_envelope(
            authed_client.get("/gdpr/api/v1/user/data-export/download"),
            404, "error.404.gdpr.export_not_found",
        )

    def test_get_wrong_user_404(self, api_client, user, tmp_path):
        req, token = _ready_request(user, tmp_path)

        from django.contrib.auth import get_user_model

        other = get_user_model().objects.create_user(
            username="intruder", email="intruder@example.com", password="x-12345678",
        )
        api_client.force_authenticate(user=other)
        _assert_error_envelope(
            api_client.get(f"/gdpr/api/v1/user/data-export/download?token={token}"),
            404, "error.404.gdpr.export_not_found",
        )

    def test_get_not_ready_425(self, authed_client, user, tmp_path):
        req, token = _ready_request(user, tmp_path)
        DataExportRequest.objects.filter(pk=req.pk).update(
            status=DataExportRequest.STATUS_PROCESSING,
        )
        _assert_error_envelope(
            authed_client.get(f"/gdpr/api/v1/user/data-export/download?token={token}"),
            425, "error.425.gdpr.export_not_ready",
        )

    def test_get_expired_410_flips_status(self, authed_client, user, tmp_path):
        from datetime import timedelta

        req, token = _ready_request(user, tmp_path)
        DataExportRequest.objects.filter(pk=req.pk).update(
            download_expires_at=timezone.now() - timedelta(seconds=1),
        )
        _assert_error_envelope(
            authed_client.get(f"/gdpr/api/v1/user/data-export/download?token={token}"),
            410, "error.410.gdpr.download_expired",
        )
        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_EXPIRED

    def test_get_missing_file_500(self, authed_client, user, tmp_path):
        req, token = _ready_request(user, tmp_path)
        DataExportRequest.objects.filter(pk=req.pk).update(
            archive_path=str(tmp_path / "vanished.zip"),
        )
        _assert_error_envelope(
            authed_client.get(f"/gdpr/api/v1/user/data-export/download?token={token}"),
            500, "error.500.internal",
        )

    def test_post_success_streams_zip(self, authed_client, user, tmp_path):
        req, token = _ready_request(user, tmp_path)
        resp = authed_client.post(
            "/gdpr/api/v1/user/data-export/download", {"token": token}, format="json",
        )
        assert resp.status_code == 200
        assert resp["Content-Type"] == "application/zip"
        assert "personal_data_export_" in resp["Content-Disposition"]
        content = b"".join(resp.streaming_content)
        assert content.startswith(b"PK")  # actual zip bytes served


@pytest.mark.django_db
class TestErrorBranches:
    def test_export_request_unexpected_valueerror_500(
        self, authed_client, monkeypatch
    ):
        def boom(user_id):
            raise ValueError("something else")

        monkeypatch.setattr(gdpr_orchestrator, "request_export", boom)
        _assert_error_envelope(
            authed_client.post("/gdpr/api/v1/user/data-export/request"),
            500, "error.500.internal",
        )

    def test_close_unexpected_valueerror_500(self, authed_client, monkeypatch):
        def boom(user_id):
            raise ValueError("something else")

        monkeypatch.setattr(gdpr_orchestrator, "initiate_closure", boom)
        _assert_error_envelope(
            authed_client.post("/gdpr/api/v1/user/account/close"),
            500, "error.500.internal",
        )

    def test_close_status_404_when_no_closure(self, authed_client):
        _assert_error_envelope(
            authed_client.get("/gdpr/api/v1/user/account/close/status"),
            404, "error.404.gdpr.no_active_closure",
        )

    def test_close_status_payload_shape(self, authed_client, user):
        gdpr_orchestrator.initiate_closure(user.pk)
        resp = authed_client.get("/gdpr/api/v1/user/account/close/status")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"status", "grace_ends_at", "can_cancel"}
        assert body["status"] == "grace"
        assert body["can_cancel"] is True

    def test_export_request_payload_shape(self, authed_client, user):
        resp = authed_client.post("/gdpr/api/v1/user/data-export/request")
        assert resp.status_code == 202
        body = resp.json()
        assert set(body) == {"request_id", "status", "message"}
        assert body["request_id"] == DataExportRequest.objects.get(user_id=user.pk).pk
        assert "48 hours" in body["message"]

    def test_export_status_payload_shape(self, authed_client, user, settings):
        settings.GDPR_COLLECTING_SERVICES = ["auth", "cdn"]
        gdpr_orchestrator.request_export(user.pk)
        resp = authed_client.get("/gdpr/api/v1/user/data-export/status")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {
            "request_id", "status", "parts_done", "parts_total",
            "download_available", "expires_at",
        }
        assert body["parts_total"] == 2
        assert body["parts_done"] == 0
        assert body["download_available"] is False
        assert body["expires_at"] is None


@pytest.mark.django_db
class TestPartReadyBranches:
    HEADERS = {"X-API-KEY": "test-service-key"}

    def test_missing_service_400(self, authed_client, user, settings):
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)
        resp = authed_client.post(
            f"/gdpr/api/v1/internal/export/{req.pk}/part-ready",
            {}, format="json", headers=self.HEADERS,
        )
        _assert_error_envelope(resp, 400, "error.400.bad_request")

    def test_unknown_request_400(self, authed_client, db):
        resp = authed_client.post(
            "/gdpr/api/v1/internal/export/999999/part-ready",
            {"service": "auth"}, format="json", headers=self.HEADERS,
        )
        _assert_error_envelope(resp, 400, "error.400.bad_request")

    def test_orchestrator_failure_500(self, authed_client, user, settings, monkeypatch):
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)

        def boom(correlation_id, service, bucket_path):
            raise RuntimeError("assembly exploded")

        monkeypatch.setattr(gdpr_orchestrator, "mark_part_ready", boom)
        resp = authed_client.post(
            f"/gdpr/api/v1/internal/export/{req.pk}/part-ready",
            {"service": "auth"}, format="json", headers=self.HEADERS,
        )
        _assert_error_envelope(resp, 500, "error.500.internal")


@pytest.mark.django_db
class TestSerializerSeams:
    def test_every_view_exposes_seams(self):
        from stapel_gdpr import views
        from stapel_gdpr.serializers import (
            ClosureStatusSerializer,
            ExportRequestSerializer,
            ExportStatusSerializer,
        )

        expected = {
            views.DataExportRequestView: (None, ExportRequestSerializer),
            views.DataExportStatusView: (None, ExportStatusSerializer),
            views.DataExportDownloadView: (None, None),
            views.AccountCloseView: (None, ClosureStatusSerializer),
            views.AccountCancelCloseView: (None, ClosureStatusSerializer),
            views.AccountCloseStatusView: (None, ClosureStatusSerializer),
            views.ExportPartReadyView: (None, None),
        }
        for view_cls, (req_ser, resp_ser) in expected.items():
            view = view_cls()
            assert view.request_serializer_class is req_ser
            assert view.response_serializer_class is resp_ser
            assert view.get_request_serializer_class() is req_ser
            assert view.get_response_serializer_class() is resp_ser

    def test_subclass_swapping_serializer_changes_response(self, user):
        """A subclass overriding response_serializer_class changes the envelope."""
        from rest_framework.test import APIRequestFactory, force_authenticate

        from stapel_gdpr.serializers import ClosureStatusSerializer
        from stapel_gdpr.views import AccountCloseStatusView

        class AuditedClosureStatusSerializer(ClosureStatusSerializer):
            def to_representation(self, instance):
                data = super().to_representation(instance)
                data["audited"] = True
                return data

        class AuditedCloseStatusView(AccountCloseStatusView):
            response_serializer_class = AuditedClosureStatusSerializer

        gdpr_orchestrator.initiate_closure(user.pk)
        factory = APIRequestFactory()

        request = factory.get("/status")
        force_authenticate(request, user=user)
        resp = AuditedCloseStatusView.as_view()(request)
        assert resp.status_code == 200
        assert resp.data["audited"] is True
        assert resp.data["status"] == "grace"

        # the base view is untouched — the seam, not the body, changed behavior
        request = factory.get("/status")
        force_authenticate(request, user=user)
        base_resp = AccountCloseStatusView.as_view()(request)
        assert base_resp.status_code == 200
        assert "audited" not in base_resp.data


class TestAdminSmoke:
    def test_admin_registrations(self):
        from django.contrib import admin as dj_admin

        from stapel_gdpr import admin as gdpr_admin
        from stapel_gdpr.models import (
            AccountClosureRequest,
            DataExportRequest,
            LegalHold,
            ReRegistrationHash,
        )

        registry = dj_admin.site._registry
        assert isinstance(registry[LegalHold], gdpr_admin.LegalHoldAdmin)
        assert isinstance(
            registry[AccountClosureRequest], gdpr_admin.AccountClosureRequestAdmin,
        )
        assert isinstance(registry[DataExportRequest], gdpr_admin.DataExportRequestAdmin)
        assert isinstance(
            registry[ReRegistrationHash], gdpr_admin.ReRegistrationHashAdmin,
        )
        assert gdpr_admin.AccountClosureRequestAdmin.inlines == [
            gdpr_admin.AccountDeletionPartInline,
        ]
        assert "user_id" in registry[LegalHold].list_display

    @pytest.mark.django_db
    def test_legal_hold_str(self, user):
        from stapel_gdpr.models import LegalHold

        hold = LegalHold.objects.create(user_id=user.pk, reason="litigation")
        assert str(hold) == f"LegalHold({user.pk}, active)"
        hold.released_at = timezone.now()
        assert str(hold) == f"LegalHold({user.pk}, released)"
