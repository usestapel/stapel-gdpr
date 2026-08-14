"""Data export pipeline tests (request → run_export → assemble)."""
import json
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone

from stapel_gdpr.models import DataExportPart, DataExportRequest
from stapel_gdpr.orchestrator import gdpr_orchestrator


@pytest.mark.django_db
class TestExportPipeline:
    def test_request_creates_parts_and_correlation(self, user, fake_provider):
        req = gdpr_orchestrator.request_export(user.pk)
        assert req.user_id == user.pk
        assert req.status == DataExportRequest.STATUS_PENDING
        assert req.correlation_id
        assert req.expected_services == ["fake"]
        assert req.parts.count() == 1

    def test_cooldown(self, user, fake_provider):
        gdpr_orchestrator.request_export(user.pk)
        with pytest.raises(ValueError, match="export_cooldown"):
            gdpr_orchestrator.request_export(user.pk)

    def test_run_export_assembles_archive(self, user, fake_provider):
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY
        # Only the digest is stored, and the window is hours, not a week.
        assert req.download_token is None
        assert req.download_token_hash
        assert req.download_consumed_at is None
        assert req.download_expires_at > timezone.now() + timedelta(hours=23)
        assert req.download_expires_at < timezone.now() + timedelta(hours=25)

        # archive lands under MEDIA_ROOT/gdpr/exports with the provider data
        archive = Path(req.archive_path)
        assert archive.exists()
        assert str(archive).startswith(str(Path(settings.MEDIA_ROOT) / "gdpr" / "exports"))
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            data_file = next(n for n in names if n.endswith("fake/fake.json"))
            data = json.loads(zf.read(data_file))
            assert data["user_id"] == str(user.pk)
            assert any(n.endswith("README.txt") for n in names)

        # staging directory is removed after successful assembly
        assert not gdpr_orchestrator._staging_dir(req.pk).exists()

    def test_dirs_are_owner_only(self, user, fake_provider):
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)
        archive_root = gdpr_orchestrator._archive_root()
        assert (archive_root.stat().st_mode & 0o777) == 0o700

    def test_provider_failure_marks_part_failed(self, user, fake_provider):
        def boom(user_id, staging_dir):
            raise RuntimeError("disk full")

        fake_provider.export_to_staging = boom
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)

        part = req.parts.get(service="fake")
        assert part.status == DataExportPart.STATUS_FAILED
        assert "disk full" in part.error
        req.refresh_from_db()
        # not all parts done and deadline not reached -> no archive yet
        assert req.status == DataExportRequest.STATUS_PROCESSING


@pytest.mark.django_db
class TestRemotePartsAndSweep:
    def test_mark_part_ready_assembles_when_complete(self, settings, user):
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)
        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_PROCESSING

        gdpr_orchestrator.mark_part_ready(req.correlation_id, "auth", "")
        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY

    def test_deadline_sweep_assembles_partial(self, settings, user):
        settings.GDPR_COLLECTING_SERVICES = ["auth", "cdn"]
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)
        gdpr_orchestrator.mark_part_ready(req.correlation_id, "auth", "")

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_PROCESSING

        DataExportRequest.objects.filter(pk=req.pk).update(
            deadline=timezone.now() - timedelta(minutes=1),
        )
        gdpr_orchestrator.sweep_deadlines()

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY
        with zipfile.ZipFile(req.archive_path) as zf:
            readme = next(n for n in zf.namelist() if n.endswith("README.txt"))
            text = zf.read(readme).decode()
            assert "PARTIAL export" in text
            assert "cdn" in text


@pytest.mark.django_db
class TestAssemblyRace:
    def test_ready_request_is_not_reassembled(self, user, fake_provider):
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)
        req.refresh_from_db()
        token_before = req.download_token_hash
        mtime_before = Path(req.archive_path).stat().st_mtime_ns

        # a late duplicate completion must be a no-op
        gdpr_orchestrator._try_assemble(req, gdpr_orchestrator._staging_dir(req.pk))
        gdpr_orchestrator.mark_part_ready(req.correlation_id, "fake", "")

        req.refresh_from_db()
        assert req.download_token_hash == token_before
        assert Path(req.archive_path).stat().st_mtime_ns == mtime_before

    def test_assembling_status_blocks_second_builder(self, settings, user):
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)
        DataExportRequest.objects.filter(pk=req.pk).update(
            status=DataExportRequest.STATUS_ASSEMBLING,
        )
        req.refresh_from_db()
        gdpr_orchestrator._try_assemble(
            req, gdpr_orchestrator._staging_dir(req.pk), force=True,
        )
        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_ASSEMBLING
        assert req.archive_path is None


@pytest.mark.django_db
class TestExportReadyEvent:
    """user.export_ready must actually leave over comm when the archive is
    assembled — the schema (schemas/emits/user.export_ready.json) existed
    without any emit (2026-07-16 audit); only the email notification went
    out. The emit is one outbox unit with the READY flip."""

    def test_assembly_emits_schema_valid_event(self, user, fake_provider):
        import jsonschema

        import stapel_gdpr
        from stapel_core.comm import subscribe_action

        captured = []
        subscribe_action("user.export_ready", lambda event: captured.append(event))

        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY
        assert len(captured) == 1
        payload = captured[0].payload
        assert payload["user_id"] == str(user.pk)
        assert payload["request_id"] == req.pk
        assert payload["download_expires_at"] == req.download_expires_at.isoformat()

        schema = json.loads(
            (
                Path(stapel_gdpr.__file__).parent
                / "schemas"
                / "emits"
                / "user.export_ready.json"
            ).read_text()
        )
        jsonschema.validate(payload, schema)

    def test_no_event_before_assembly(self, user, fake_provider):
        from stapel_core.comm import subscribe_action

        captured = []
        subscribe_action("user.export_ready", lambda event: captured.append(event))

        gdpr_orchestrator.request_export(user.pk)
        # Export requested but not yet run/assembled — nothing must leave.
        assert captured == []


@pytest.mark.django_db
class TestBucketPathBoundary:
    """A peer service's `bucket_path` is remote input (audit 2026-08-11).

    It arrives over HTTP (ExportPartReadyView) or the bus, and whatever it
    names is copied verbatim into an archive a USER downloads. Django's
    FileSystemStorage refuses traversal; an S3 backend does not — keys are
    opaque strings there. So the shape is checked here, at both ends: when
    the part is accepted, and again when the object is opened.
    """

    def _staged(self, req, key, body=b'{"leaked": true}'):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        default_storage.save(key, ContentFile(body))
        return key

    def test_a_key_outside_this_export_is_refused_on_ingest(self, settings, user):
        """A peer may only name a key belonging to the export it was asked about."""
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)
        foreign = self._staged(req, "gdpr/some-other-correlation/auth/export.json")

        gdpr_orchestrator.mark_part_ready(req.correlation_id, "auth", foreign)

        part = req.parts.get(service="auth")
        assert part.status != DataExportPart.STATUS_DONE
        assert not part.bucket_path

    @pytest.mark.parametrize(
        "key",
        [
            "gdpr/../../etc/passwd",
            "/etc/passwd",
            "s3://other-bucket/secrets.json",
            "gdpr/{cid}/auth/../../../other/export.json",
        ],
    )
    def test_traversal_and_absolute_keys_are_refused(self, settings, user, key):
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)

        gdpr_orchestrator.mark_part_ready(
            req.correlation_id, "auth", key.format(cid=req.correlation_id)
        )

        assert req.parts.get(service="auth").status != DataExportPart.STATUS_DONE

    def test_a_stored_foreign_key_is_never_opened(self, settings, user, tmp_path):
        """Rows written before the rule, or by something other than the
        orchestrator, must not be readable either."""
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)
        foreign = self._staged(req, "gdpr/another-users-export/auth/export.json")
        # Straight to the DB: the ingest gate never saw this value.
        req.parts.update(
            status=DataExportPart.STATUS_DONE, bucket_path=foreign,
        )

        gdpr_orchestrator._download_bucket_parts(req, tmp_path)

        assert not (tmp_path / "auth" / "export.json").exists()

    def test_the_export_own_key_still_works(self, settings, user):
        """The closed rule must not break the shape peers actually use."""
        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)
        key = self._staged(
            req, f"gdpr/{req.correlation_id}/auth/export.json", b'{"mine": true}'
        )

        gdpr_orchestrator.mark_part_ready(req.correlation_id, "auth", key)

        assert req.parts.get(service="auth").status == DataExportPart.STATUS_DONE

    def test_the_prefix_rule_is_an_explicit_opt_out(self, settings, user):
        """A deployment whose peers stage elsewhere says so — and even then
        traversal and absolute keys stay refused."""
        from tests.support import gdpr_conf

        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        settings.STAPEL_GDPR = gdpr_conf(EXPORT_BUCKET_PREFIX="")
        req = gdpr_orchestrator.request_export(user.pk)
        elsewhere = self._staged(req, "exports/auth/somewhere-else.json")

        gdpr_orchestrator.mark_part_ready(req.correlation_id, "auth", elsewhere)
        assert req.parts.get(service="auth").status == DataExportPart.STATUS_DONE

        req2 = DataExportRequest.objects.create(
            user_id=user.pk, correlation_id="cid-2", status=DataExportRequest.STATUS_PROCESSING,
        )
        DataExportPart.objects.create(request=req2, service="auth")
        gdpr_orchestrator.mark_part_ready("cid-2", "auth", "../../etc/passwd")
        assert req2.parts.get(service="auth").status != DataExportPart.STATUS_DONE

    def test_the_open_prefix_is_reported_by_manage_py_check(self, settings):
        from tests.support import gdpr_conf

        from stapel_gdpr.checks import check_data_owner_registry

        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake"], EXPORT_BUCKET_PREFIX="",
        )
        assert any(p.id == "gdpr.W007" for p in check_data_owner_registry())

        settings.STAPEL_GDPR = gdpr_conf(DATA_OWNERS=["fake"])
        assert not any(p.id == "gdpr.W007" for p in check_data_owner_registry())
