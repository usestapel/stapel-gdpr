"""GDPR-04: a subject's export must not live where the web server serves files.

RED BEFORE GREEN — the first two classes are the defect as 0.6.0 shipped it.

``STAGING_ROOT``/``ARCHIVE_ROOT`` defaulted to ``MEDIA_ROOT/gdpr/...``. The
ordinary Django/nginx shape serves MEDIA_ROOT as static files
(``location /media { alias /media; }``, commonly with
``Cache-Control: public, max-age=2592000``), so on a deployment that followed
that shape a ZIP holding everything the system knows about a person sat at a
guessable URL, unauthenticated and cacheable by every intermediary. Nothing
in the library refused; nothing even said so at boot.

The fix has three halves and this file guards all three:

* the default root is off every served root (``TestTheDefaultRootIsNotServed``),
* a deployment that puts it back on one is refused at boot with a named
  finding, ``gdpr.E013`` (``TestAServedRootIsRefusedAtBoot``),
* the archive has no URL *by construction* — the store the module writes
  through cannot produce one, so there is nothing to guess
  (``TestTheArchiveHasNoUrl``).
"""
import os
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.core.checks import Error, Warning as CheckWarning

from stapel_gdpr import export_store
from stapel_gdpr.checks import check_export_storage
from stapel_gdpr.models import DataExportPart, DataExportRequest
from stapel_gdpr.orchestrator import gdpr_orchestrator
from tests.support import gdpr_conf


def _served_roots() -> list[Path]:
    roots = [Path(django_settings.MEDIA_ROOT).resolve()]
    if getattr(django_settings, "STATIC_ROOT", None):
        roots.append(Path(django_settings.STATIC_ROOT).resolve())
    return roots


def _is_inside(path: Path, parent: Path) -> bool:
    return path.resolve() == parent or path.resolve().is_relative_to(parent)


class TestTheDefaultRootIsNotServed:
    """With nothing configured, nothing personal may land under MEDIA_ROOT."""

    def test_default_export_root_is_outside_every_served_root(self):
        root = export_store.export_root()
        for served in _served_roots():
            assert not _is_inside(root, served), (
                f"default export root {root} is inside the served root {served}"
            )

    def test_default_archive_and_staging_roots_are_outside_media_root(self):
        media = Path(django_settings.MEDIA_ROOT).resolve()
        assert not _is_inside(export_store.archive_root(), media)
        assert not _is_inside(export_store.staging_root(), media)

    def test_the_orchestrator_reads_the_same_roots(self):
        assert gdpr_orchestrator._archive_root() == export_store.archive_root()
        assert gdpr_orchestrator._staging_root() == export_store.staging_root()

    def test_a_deployment_without_base_dir_still_gets_a_private_root(self, settings):
        """No BASE_DIR is not a licence to fall back to MEDIA_ROOT."""
        settings.BASE_DIR = ""
        root = export_store.export_root()
        for served in _served_roots():
            assert not _is_inside(root, served)

    @pytest.mark.django_db
    def test_a_real_export_lands_outside_media_root(self, user, fake_provider):
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)
        req.refresh_from_db()

        assert req.status == DataExportRequest.STATUS_READY
        # What is stored is a store KEY, not an absolute filesystem path: the
        # download view runs in a different process from the celery task that
        # wrote the file, and a path is only meaningful in the writer's
        # container.
        assert not os.path.isabs(req.archive_path)

        located = Path(export_store.export_storage().path(req.archive_path))
        assert located.exists()
        for served in _served_roots():
            assert not _is_inside(located, served)

    @pytest.mark.django_db
    def test_no_zip_of_anybody_is_left_under_a_served_root(self, user, fake_provider):
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)

        for served in _served_roots():
            assert list(served.rglob("*.zip")) == []


class TestAServedRootIsRefusedAtBoot:
    """``gdpr.E013`` — an Error, in the spirit of ``stapel_core.storage.E001``.

    A warning is what let the original defect run: a deployment that puts a
    personal-data archive back under a served root is not degraded, it is
    publishing, so the only honest thing the library can do is refuse to
    start and name the directory.
    """

    def test_export_root_inside_media_root_is_an_error(self, settings):
        settings.STAPEL_GDPR = gdpr_conf(
            EXPORT_ROOT=str(Path(settings.MEDIA_ROOT) / "gdpr"),
        )
        findings = [f for f in check_export_storage() if f.id == "gdpr.E013"]
        assert len(findings) == 1
        assert isinstance(findings[0], Error)
        assert "MEDIA_ROOT" in str(findings[0])

    def test_the_media_root_itself_is_an_error(self, settings):
        settings.STAPEL_GDPR = gdpr_conf(EXPORT_ROOT=settings.MEDIA_ROOT)
        assert any(f.id == "gdpr.E013" for f in check_export_storage())

    def test_the_legacy_flat_setting_is_checked_too(self, settings):
        """``GDPR_ARCHIVE_ROOT`` is the value the fleet actually carries."""
        settings.STAPEL_GDPR = gdpr_conf()
        settings.GDPR_ARCHIVE_ROOT = str(Path(settings.MEDIA_ROOT) / "gdpr" / "exports")
        assert any(f.id == "gdpr.E013" for f in check_export_storage())

    def test_static_root_counts_as_served(self, settings, tmp_path):
        settings.STATIC_ROOT = str(tmp_path / "static")
        settings.STAPEL_GDPR = gdpr_conf(
            EXPORT_ROOT=str(tmp_path / "static" / "gdpr"),
        )
        assert any(f.id == "gdpr.E013" for f in check_export_storage())

    def test_a_named_private_root_is_clean(self, settings, tmp_path):
        settings.STAPEL_GDPR = gdpr_conf(EXPORT_ROOT=str(tmp_path / "private"))
        assert check_export_storage() == []

    def test_an_unnamed_root_is_reported_but_does_not_block_boot(self, settings):
        """``gdpr.W013`` — the other half of the same defect.

        Nothing named means the archive lives on whichever filesystem the
        celery worker happens to have. When web and worker are separate
        containers the subject is told the export is READY and the download
        answers 500. A Warning, because a monolith is genuinely fine.
        """
        settings.STAPEL_GDPR = gdpr_conf()
        findings = check_export_storage()
        assert [f.id for f in findings] == ["gdpr.W013"]
        assert isinstance(findings[0], CheckWarning)

    def test_a_configured_storage_alias_silences_w013(self, settings, tmp_path):
        settings.STORAGES = {
            **settings.STORAGES,
            export_store.EXPORT_STORAGE_ALIAS: {
                "BACKEND": "stapel_gdpr.export_store.PrivateFileSystemStorage",
                "OPTIONS": {"location": str(tmp_path / "shared")},
            },
        }
        settings.STAPEL_GDPR = gdpr_conf()
        assert check_export_storage() == []

    def test_the_check_is_registered(self):
        from django.core.checks.registry import registry

        assert any(c is check_export_storage for c in registry.get_checks())


class TestTheArchiveHasNoUrl:
    """The download must not be a path — there must be no URL to guess."""

    def test_the_store_refuses_to_produce_a_url(self):
        with pytest.raises(ValueError, match="no URL"):
            export_store.export_storage().url("exports/anything/export_1.zip")

    def test_the_store_does_not_borrow_media_url(self, settings):
        """The trap this closes: ``FileSystemStorage(base_url=None)`` silently
        falls back to ``settings.MEDIA_URL``, so a 'private' store built the
        obvious way still hands out ``/media/<key>``."""
        settings.MEDIA_URL = "/media/"
        with pytest.raises(ValueError):
            export_store.export_storage().url("exports/anything/export_1.zip")

    @pytest.mark.django_db
    def test_the_location_is_not_derivable_from_the_request_id(self, user, fake_provider):
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)
        req.refresh_from_db()

        key = req.archive_path
        # The request id is public (it is in the ready event and the API); the
        # correlation id is not. Knowing the id must not name the object.
        assert key != f"export_{req.pk}.zip"
        prefix, _, filename = key.rpartition("/")
        assert prefix.split("/")[-1] == req.correlation_id
        assert filename == f"export_{req.pk}.zip"

        for guess in (
            f"gdpr/exports/export_{req.pk}.zip",
            f"exports/export_{req.pk}.zip",
            f"export_{req.pk}.zip",
        ):
            for served in _served_roots():
                assert not (served / guess).exists()
            with pytest.raises(ValueError):
                export_store.export_storage().url(guess)

    @pytest.mark.django_db
    def test_the_download_still_streams_and_is_uncacheable(
        self, authed_client, user, fake_provider,
    ):
        req = gdpr_orchestrator.request_export(user.pk)
        gdpr_orchestrator.run_export(req.pk)
        req.refresh_from_db()
        token = req.generate_download_token()

        resp = authed_client.post(
            "/gdpr/api/v1/user/data-export/download", {"token": token}, format="json",
        )
        assert resp.status_code == 200
        assert resp["Cache-Control"] == "no-store, private"
        assert b"".join(resp.streaming_content).startswith(b"PK")

        req.refresh_from_db()
        assert req.archive_path is None


@pytest.mark.django_db
class TestPeerPartsDoNotLinger:
    """Sweep finding: parts a peer service uploaded were never deleted.

    A remote data owner writes its slice to ``default_storage`` under
    ``gdpr/<correlation_id>/<service>/export.json``, the orchestrator copies
    the bytes into the ZIP — and left the object there forever. On the
    ordinary deployment ``default_storage`` is MEDIA_ROOT-backed, so those
    slices sat in the served root too, with nothing scheduled to remove them.
    """

    def _put(self, key: bytes, body=b'{"a": 1}'):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        return default_storage.save(key, ContentFile(body))

    def test_bucket_objects_are_gone_once_they_are_in_the_archive(self, settings, user):
        from django.core.files.storage import default_storage

        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)
        key = self._put(f"gdpr/{req.correlation_id}/auth/export.json")

        gdpr_orchestrator.mark_part_ready(req.correlation_id, "auth", key)

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY
        assert not default_storage.exists(key)
        assert not req.parts.get(service="auth").bucket_path
        # and the bytes really did make it into the archive first
        import zipfile

        with export_store.open_archive(req.archive_path) as fh, zipfile.ZipFile(fh) as zf:
            assert any(n.endswith("auth/export.json") for n in zf.namelist())

    def test_a_part_that_was_never_staged_is_left_alone(self, settings, user):
        """A key the export gate refused is not ours to delete."""
        from django.core.files.storage import default_storage

        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)
        foreign = self._put("gdpr/somebody-elses-export/auth/export.json")
        req.parts.update(status=DataExportPart.STATUS_DONE, bucket_path=foreign)

        gdpr_orchestrator._try_assemble(
            req, gdpr_orchestrator._staging_dir(req.pk), force=True,
        )

        assert default_storage.exists(foreign)

    def test_a_remote_owner_on_media_backed_storage_is_reported(self, settings):
        settings.STAPEL_GDPR = gdpr_conf(
            EXPORT_ROOT=str(Path(settings.MEDIA_ROOT).parent / "private-gdpr"),
            DATA_OWNERS={"auth": {"subject_types": ["account"], "kind": "remote"}},
            DATA_OWNERS_VERSION="test-1",
        )
        ids = [f.id for f in check_export_storage()]
        assert ids == ["gdpr.W014"]


class TestLegacyAbsolutePathsStillResolve:
    """Rows written by 0.6.0 hold an absolute path; they must keep working."""

    @pytest.mark.django_db
    def test_an_absolute_archive_path_is_still_served(self, authed_client, user, tmp_path):
        import zipfile

        archive = tmp_path / "legacy.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("README.txt", "hi")
        from django.utils import timezone

        req = DataExportRequest.objects.create(
            user_id=user.pk,
            status=DataExportRequest.STATUS_READY,
            archive_path=str(archive),
            deadline=timezone.now(),
        )
        token = req.generate_download_token()

        resp = authed_client.post(
            "/gdpr/api/v1/user/data-export/download", {"token": token}, format="json",
        )
        assert resp.status_code == 200
        b"".join(resp.streaming_content)
        assert not archive.exists()
