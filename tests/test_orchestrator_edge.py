"""Orchestrator branch tests — microservices bucket path, failure/edge branches."""
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from django.utils import timezone

from stapel_gdpr.models import (
    AccountClosureRequest,
    DataExportPart,
    DataExportRequest,
)
from stapel_gdpr.orchestrator import gdpr_orchestrator


@pytest.mark.django_db
class TestMicroservicesExportPath:
    def test_request_export_publishes_bus_event(self, settings, user):
        from stapel_core.bus import get_bus
        from stapel_core.gdpr import GDPR_EXPORT_REQUESTED

        settings.GDPR_COLLECTING_SERVICES = ["auth", "cdn"]
        req = gdpr_orchestrator.request_export(user.pk)

        event = next(
            e for e in reversed(get_bus().events)
            if e.event_type == GDPR_EXPORT_REQUESTED
            and e.payload.get("correlation_id") == req.correlation_id
        )
        assert event.service == "gdpr"
        assert event.payload == {
            "correlation_id": req.correlation_id,
            "user_id": str(user.pk),
            "request_id": req.pk,
        }
        assert event.key == str(user.pk)

    def test_bucket_part_downloaded_into_archive(self, settings, user):
        """Remote service uploads to object storage; assemble pulls it in."""
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)

        payload = {"user_id": str(user.pk), "logins": 7}
        bucket_path = f"gdpr/{req.correlation_id}/auth/export.json"
        default_storage.save(bucket_path, ContentFile(json.dumps(payload).encode()))

        gdpr_orchestrator.mark_part_ready(req.correlation_id, "auth", bucket_path)

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY
        part = req.parts.get(service="auth")
        assert part.status == DataExportPart.STATUS_DONE
        assert part.bucket_path == bucket_path

        with zipfile.ZipFile(req.archive_path) as zf:
            name = next(n for n in zf.namelist() if n.endswith("auth/export.json"))
            assert json.loads(zf.read(name)) == payload

    def test_download_skips_already_staged_file(self, settings, user, tmp_path):
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)
        req.parts.update(
            status=DataExportPart.STATUS_DONE,
            bucket_path="gdpr/nowhere/auth/export.json",
        )

        staged = tmp_path / "auth" / "export.json"
        staged.parent.mkdir(parents=True)
        staged.write_text('{"already": "here"}')

        gdpr_orchestrator._download_bucket_parts(req, tmp_path)
        assert staged.read_text() == '{"already": "here"}'  # untouched, no download

    def test_mark_part_ready_unknown_correlation_is_noop(self, db):
        gdpr_orchestrator.mark_part_ready(str(uuid.uuid4()), "auth", "some/path")
        assert DataExportRequest.objects.count() == 0

    def test_publish_failure_propagates(self, settings, user, monkeypatch):
        settings.GDPR_COLLECTING_SERVICES = ["auth"]

        def broken_bus():
            raise RuntimeError("broker down")

        monkeypatch.setattr("stapel_core.bus.router.get_bus", broken_bus)
        with pytest.raises(RuntimeError, match="broker down"):
            gdpr_orchestrator.request_export(user.pk)


@pytest.mark.django_db
class TestRunExportBranches:
    def test_run_export_noop_when_already_ready(self, user, fake_provider):
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)
        req.refresh_from_db()
        mtime = Path(req.archive_path).stat().st_mtime_ns

        gdpr_orchestrator.run_export(req.pk)  # early return

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY
        assert Path(req.archive_path).stat().st_mtime_ns == mtime

    def test_run_export_skips_providers_without_part_or_done(
        self, settings, user, fake_provider
    ):
        # 'fake' provider registered but only 'auth' expected -> no part -> skip
        settings.GDPR_COLLECTING_SERVICES = ["auth", "fake"]
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)
        assert req.parts.get(service="fake").status == DataExportPart.STATUS_DONE
        assert req.parts.get(service="auth").status == DataExportPart.STATUS_PENDING

        # rerun with the 'fake' part already done -> continue branch
        DataExportRequest.objects.filter(pk=req.pk).update(
            status=DataExportRequest.STATUS_PROCESSING,
        )
        exported_before = list(fake_provider.exported)
        gdpr_orchestrator.run_export(req.pk)
        assert fake_provider.exported == exported_before  # not re-exported

    def test_assembly_failure_returns_request_to_processing(
        self, settings, user, monkeypatch
    ):
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)
        req.parts.update(status=DataExportPart.STATUS_DONE)
        req.refresh_from_db()

        def boom(req, staging_dir, partial=False):
            raise OSError("disk full")

        monkeypatch.setattr(gdpr_orchestrator, "_assemble_zip", boom)
        with pytest.raises(OSError, match="disk full"):
            gdpr_orchestrator._try_assemble(req, gdpr_orchestrator._staging_dir(req.pk))

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_PROCESSING
        assert "disk full" in req.error

    def test_ready_notification_failure_does_not_break_assembly(
        self, user, fake_provider, monkeypatch
    ):
        def boom(**kwargs):
            raise RuntimeError("notifications down")

        monkeypatch.setattr("stapel_core.notifications.request_notification", boom)
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)
        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY

    def test_configured_staging_and_archive_roots(self, settings, tmp_path):
        settings.STAPEL_GDPR = {
            "STAGING_ROOT": str(tmp_path / "stage"),
            "ARCHIVE_ROOT": str(tmp_path / "arch"),
        }
        assert gdpr_orchestrator._staging_root() == tmp_path / "stage"
        assert gdpr_orchestrator._archive_root() == tmp_path / "arch"


@pytest.mark.django_db
class TestDeletionBranches:
    def test_emit_failure_on_initiate_still_creates_closure(self, user, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("comm down")

        monkeypatch.setattr("stapel_core.comm.emit", boom)
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        assert closure.status == AccountClosureRequest.STATUS_GRACE
        user.refresh_from_db()
        assert user.is_active is False

    def test_emit_failure_on_delete_leaves_closure_deleting(
        self, user, fake_provider, monkeypatch
    ):
        closure = gdpr_orchestrator.initiate_closure(user.pk)

        def boom(*args, **kwargs):
            raise RuntimeError("comm down")

        monkeypatch.setattr("stapel_core.comm.emit", boom)
        gdpr_orchestrator.execute_deletion(closure)

        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING
        assert closure.local_erasure_done is False
        assert closure.deleted_at is None

    def test_anonymize_failure_blocks_finalization(self, user, fake_provider, monkeypatch):
        def boom(user_id):
            raise RuntimeError("anon failed")

        monkeypatch.setattr(fake_provider, "anonymize", boom)
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING

    def test_duplicate_section_erased_confirmation_is_noop(self, settings, user):
        settings.STAPEL_GDPR = {"REMOTE_DELETION_SERVICES": ["profiles", "cdn"]}
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        gdpr_orchestrator.mark_section_erased(closure.correlation_id, "profiles")
        gdpr_orchestrator.mark_section_erased(closure.correlation_id, "profiles")

        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING  # cdn missing

    def test_maybe_finalize_ignores_non_deleting_closure(self, user):
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator._maybe_finalize(closure)
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_GRACE

    def test_store_hashes_missing_user_is_noop(self, db):
        from stapel_gdpr.models import ReRegistrationHash

        gdpr_orchestrator._store_reregistration_hashes(uuid.uuid4())
        assert ReRegistrationHash.objects.count() == 0

    def test_store_hashes_failure_swallowed(self, user, monkeypatch, caplog):
        def boom(user_id, email=None, phone=None):
            raise RuntimeError("hash store down")

        monkeypatch.setattr("stapel_gdpr.reregistration.store_hashes", boom)
        gdpr_orchestrator._store_reregistration_hashes(user.pk)  # must not raise
        assert any(
            "Failed to store re-registration hashes" in r.message
            for r in caplog.records
        )

    def test_publish_delete_requested(self, db):
        from stapel_core.bus import get_bus
        from stapel_core.gdpr import GDPR_DELETE_REQUESTED

        uid, corr = uuid.uuid4(), str(uuid.uuid4())
        gdpr_orchestrator._publish_delete_requested(uid, corr, ["profiles"])

        event = next(
            e for e in reversed(get_bus().events)
            if e.event_type == GDPR_DELETE_REQUESTED
            and e.payload.get("correlation_id") == corr
        )
        assert event.payload == {
            "correlation_id": corr,
            "user_id": str(uid),
            "services": ["profiles"],
        }

    def test_publish_delete_requested_failure_propagates(self, monkeypatch):
        def broken_bus():
            raise RuntimeError("broker down")

        monkeypatch.setattr("stapel_core.bus.router.get_bus", broken_bus)
        with pytest.raises(RuntimeError, match="broker down"):
            gdpr_orchestrator._publish_delete_requested(uuid.uuid4(), "c", [])

    def test_deactivate_reactivate_swallow_bad_ids(self, db, caplog):
        gdpr_orchestrator._deactivate_user("not-a-uuid")
        gdpr_orchestrator._reactivate_user("not-a-uuid")
        messages = [r.message for r in caplog.records]
        assert any("Failed to deactivate user" in m for m in messages)
        assert any("Failed to reactivate user" in m for m in messages)


@pytest.mark.django_db
class TestSweepPartialAssemble:
    def test_sweep_builds_partial_archive_with_bucket_and_missing_parts(
        self, settings, user
    ):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        settings.GDPR_COLLECTING_SERVICES = ["auth", "cdn", "profiles"]
        req = gdpr_orchestrator.request_export(user.pk)
        DataExportRequest.objects.filter(pk=req.pk).update(
            status=DataExportRequest.STATUS_PROCESSING,
            deadline=timezone.now(),
        )

        bucket_path = f"gdpr/{req.correlation_id}/auth/export.json"
        default_storage.save(bucket_path, ContentFile(b'{"a": 1}'))
        req.parts.filter(service="auth").update(
            status=DataExportPart.STATUS_DONE, bucket_path=bucket_path,
        )

        gdpr_orchestrator.sweep_deadlines()

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY
        with zipfile.ZipFile(req.archive_path) as zf:
            readme = zf.read(
                next(n for n in zf.namelist() if n.endswith("README.txt"))
            ).decode()
            assert "partial export" in readme
            assert "- cdn" in readme and "- profiles" in readme
            assert "Included sections:\n  - auth" in readme
            assert any(n.endswith("auth/export.json") for n in zf.namelist())
