"""API smoke tests — UUID user ids must be accepted end-to-end."""
import json
from pathlib import Path

import pytest
from django.utils import timezone

from stapel_gdpr.models import AccountClosureRequest, DataExportRequest, LegalHold


@pytest.mark.django_db
class TestExportAPI:
    def test_request_export(self, authed_client, user):
        resp = authed_client.post("/gdpr/api/v1/user/data-export/request")
        assert resp.status_code == 202
        req = DataExportRequest.objects.get(user_id=user.pk)
        # celery eager: the export ran inline; no providers -> assembled empty
        assert req.status == DataExportRequest.STATUS_READY

    def test_request_export_cooldown(self, authed_client, user):
        authed_client.post("/gdpr/api/v1/user/data-export/request")
        resp = authed_client.post("/gdpr/api/v1/user/data-export/request")
        assert resp.status_code == 409

    def test_status(self, authed_client, user):
        authed_client.post("/gdpr/api/v1/user/data-export/request")
        resp = authed_client.get("/gdpr/api/v1/user/data-export/status")
        assert resp.status_code == 200

    def test_status_not_found(self, authed_client, user):
        resp = authed_client.get("/gdpr/api/v1/user/data-export/status")
        assert resp.status_code == 404

    def test_download_get_and_post(self, authed_client, user, tmp_path):
        archive = tmp_path / "export.zip"
        import zipfile

        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("README.txt", "hi")

        req = DataExportRequest.objects.create(
            user_id=user.pk,
            status=DataExportRequest.STATUS_READY,
            archive_path=str(archive),
            deadline=timezone.now(),
        )
        token = req.generate_download_token()

        resp = authed_client.get(f"/gdpr/api/v1/user/data-export/download?token={token}")
        assert resp.status_code == 200
        assert resp["Content-Type"] == "application/zip"

        resp = authed_client.post(
            "/gdpr/api/v1/user/data-export/download", {"token": token}, format="json",
        )
        assert resp.status_code == 200

        resp = authed_client.post(
            "/gdpr/api/v1/user/data-export/download", {"token": "wrong"}, format="json",
        )
        assert resp.status_code == 404

        resp = authed_client.post(
            "/gdpr/api/v1/user/data-export/download", {}, format="json",
        )
        assert resp.status_code == 404

    def test_download_expired(self, authed_client, user, tmp_path):
        from datetime import timedelta

        req = DataExportRequest.objects.create(
            user_id=user.pk,
            status=DataExportRequest.STATUS_READY,
            archive_path=str(tmp_path / "x.zip"),
            deadline=timezone.now(),
        )
        token = req.generate_download_token()
        DataExportRequest.objects.filter(pk=req.pk).update(
            download_expires_at=timezone.now() - timedelta(minutes=1),
        )
        resp = authed_client.get(f"/gdpr/api/v1/user/data-export/download?token={token}")
        assert resp.status_code == 410

    def test_unauthenticated_rejected(self, api_client, db):
        resp = api_client.post("/gdpr/api/v1/user/data-export/request")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestClosureAPI:
    def test_close_cancel_status_lifecycle(self, authed_client, user):
        resp = authed_client.post("/gdpr/api/v1/user/account/close")
        assert resp.status_code == 202
        closure = AccountClosureRequest.objects.get(user_id=user.pk)
        assert closure.status == AccountClosureRequest.STATUS_GRACE

        resp = authed_client.get("/gdpr/api/v1/user/account/close/status")
        assert resp.status_code == 200

        resp = authed_client.post("/gdpr/api/v1/user/account/cancel-close")
        assert resp.status_code == 200
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_CANCELLED

    def test_close_twice_409(self, authed_client, user):
        authed_client.post("/gdpr/api/v1/user/account/close")
        resp = authed_client.post("/gdpr/api/v1/user/account/close")
        assert resp.status_code == 409

    def test_legal_hold_409(self, authed_client, user):
        LegalHold.objects.create(user_id=user.pk, reason="litigation")
        resp = authed_client.post("/gdpr/api/v1/user/account/close")
        assert resp.status_code == 409
        assert "legal_hold" in resp.content.decode()

    def test_cancel_without_closure_404(self, authed_client, user):
        resp = authed_client.post("/gdpr/api/v1/user/account/cancel-close")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestInternalAPI:
    def test_part_ready_requires_service_key(self, authed_client, user, settings):
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        from stapel_gdpr.orchestrator import gdpr_orchestrator

        req = gdpr_orchestrator.request_export(user.pk)
        url = f"/gdpr/api/v1/internal/export/{req.pk}/part-ready"

        resp = authed_client.post(url, {"service": "auth"}, format="json")
        assert resp.status_code == 403

        resp = authed_client.post(
            url, {"service": "auth"}, format="json",
            headers={"X-API-KEY": "test-service-key"},
        )
        assert resp.status_code in (200, 204)
        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY


class TestSchemas:
    """Emitted payload shapes must match the committed JSON Schemas."""

    def _schema(self, name):
        import stapel_gdpr

        path = Path(stapel_gdpr.__file__).parent / "schemas" / "emits" / f"{name}.json"
        return json.loads(path.read_text())

    def test_user_deleted_schema_takes_uuid_string(self):
        import uuid

        import jsonschema

        schema = self._schema("user.deleted")
        payload = {
            "user_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "trigger": "manual",
        }
        jsonschema.validate(payload, schema)  # must not raise
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**payload, "user_id": 42}, schema)

    def test_deletion_initiated_schema_takes_uuid_string(self):
        import uuid

        import jsonschema

        schema = self._schema("user.deletion_initiated")
        payload = {
            "user_id": str(uuid.uuid4()),
            "trigger": "manual",
            "grace_ends_at": "2026-08-01T00:00:00+00:00",
        }
        jsonschema.validate(payload, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**payload, "user_id": 42}, schema)
