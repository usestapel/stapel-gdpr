"""GDPR-03: the export download is a one-shot credential with a short life.

Before: the token was documented as single-use but never consumed, lived for
seven days, was accepted from the query string (access logs, browser history,
Referer, proxies), and the archive it unlocked was written to the local
filesystem with nothing scheduled to ever remove it.
"""
import zipfile
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_gdpr.models import (
    DataExportRequest,
    ReRegistrationHash,
    hash_download_token,
)
from stapel_gdpr.orchestrator import gdpr_orchestrator
from tests.support import gdpr_conf


def _ready_export(user, tmp_path, name="export.zip"):
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("README.txt", "hello")
    req = DataExportRequest.objects.create(
        user_id=user.pk,
        status=DataExportRequest.STATUS_READY,
        archive_path=str(archive),
        deadline=timezone.now(),
    )
    return req, req.generate_download_token(), archive


@pytest.mark.django_db
class TestTokenStorage:
    def test_only_the_digest_is_stored(self, user, tmp_path):
        req, token, _ = _ready_export(user, tmp_path)
        req.refresh_from_db()

        assert req.download_token is None
        assert req.download_token_hash == hash_download_token(token)
        assert token not in str(req.__dict__)

    def test_ttl_is_hours_not_days(self, settings, user, tmp_path):
        settings.STAPEL_GDPR = gdpr_conf(DOWNLOAD_TTL_HOURS=2)
        req, _, _ = _ready_export(user, tmp_path)

        assert req.download_expires_at < timezone.now() + timedelta(hours=3)


@pytest.mark.django_db
class TestSingleUse:
    def test_consume_succeeds_exactly_once(self, user, tmp_path):
        req, token, _ = _ready_export(user, tmp_path)

        assert req.consume_download_token(token) is True
        assert req.consume_download_token(token) is False

    def test_wrong_token_never_consumes(self, user, tmp_path):
        req, token, _ = _ready_export(user, tmp_path)

        assert req.consume_download_token("not-the-token") is False
        assert req.consume_download_token("") is False
        assert req.consume_download_token(token) is True

    def test_second_download_is_refused_and_archive_is_gone(
        self, authed_client, user, tmp_path,
    ):
        req, token, archive = _ready_export(user, tmp_path)

        first = authed_client.post(
            "/gdpr/api/v1/user/data-export/download", {"token": token}, format="json",
        )
        assert first.status_code == 200
        body = b"".join(first.streaming_content)
        assert body.startswith(b"PK")  # the archive really was served
        assert first["Cache-Control"] == "no-store, private"

        second = authed_client.post(
            "/gdpr/api/v1/user/data-export/download", {"token": token}, format="json",
        )
        assert second.status_code == 410
        assert second.json()["localizable_error"] == "error.410.gdpr.download_consumed"
        assert not archive.exists()

        req.refresh_from_db()
        assert req.download_consumed_at is not None
        assert req.archive_path is None


@pytest.mark.django_db
class TestArchiveRetention:
    def test_purge_task_removes_expired_archives(self, user, tmp_path):
        from stapel_gdpr.tasks import purge_expired_exports

        req, _, archive = _ready_export(user, tmp_path)
        DataExportRequest.objects.filter(pk=req.pk).update(
            download_expires_at=timezone.now() - timedelta(seconds=1),
        )

        assert purge_expired_exports() == 1

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_EXPIRED
        assert req.archive_path is None
        assert not archive.exists()

    def test_purge_leaves_live_exports_alone(self, user, tmp_path):
        from stapel_gdpr.tasks import purge_expired_exports

        req, _, archive = _ready_export(user, tmp_path)

        assert purge_expired_exports() == 0
        assert archive.exists()

    def test_purge_is_wired_into_the_beat_schedule(self):
        from stapel_gdpr.tasks import get_gdpr_beat_schedule

        entry = get_gdpr_beat_schedule()["gdpr-export-archive-purge"]
        assert entry["task"] == "stapel_gdpr.tasks.purge_expired_exports"


@pytest.mark.django_db
class TestDownloadUrl:
    def test_token_rides_in_the_fragment_by_default(self):
        url = gdpr_orchestrator._build_download_url("tok-123")

        assert url.endswith("#token=tok-123")
        assert "?token=" not in url

    def test_template_is_configurable(self, settings):
        settings.STAPEL_GDPR = gdpr_conf(
            DOWNLOAD_URL_TEMPLATE="{frontend_url}/data/#t={token}",
        )
        assert gdpr_orchestrator._build_download_url("tok").endswith("/data/#t=tok")


@pytest.mark.django_db
class TestReRegistrationHashScheme:
    def test_new_rows_use_the_keyed_hmac_scheme(self, user):
        from stapel_gdpr.reregistration import compute_hash, store_hashes

        store_hashes(user.pk, email="Person@Example.com")
        row = ReRegistrationHash.objects.get()

        assert row.scheme == ReRegistrationHash.SCHEME_HMAC_V1
        assert row.hash_value == compute_hash("email", "person@example.com")

    def test_digest_depends_on_the_key(self, settings, user):
        from stapel_gdpr.reregistration import compute_hash

        settings.STAPEL_GDPR = gdpr_conf(REREG_SALT="key-one")
        one = compute_hash("email", "person@example.com")
        settings.STAPEL_GDPR = gdpr_conf(REREG_SALT="key-two")
        two = compute_hash("email", "person@example.com")

        assert one != two

    def test_plain_sha256_of_the_email_is_never_the_stored_value(self, user):
        """The exact shape another writer was found storing into this table."""
        import hashlib

        from stapel_gdpr.reregistration import store_hashes

        store_hashes(user.pk, email="person@example.com")
        unsalted = hashlib.sha256(b"person@example.com").hexdigest()

        assert not ReRegistrationHash.objects.filter(hash_value=unsalted).exists()

    def test_unverified_rows_never_match_a_lookup(self, user):
        import hashlib

        from stapel_gdpr.reregistration import is_reregistration

        ReRegistrationHash.objects.create(
            hash_type="email",
            hash_value=hashlib.sha256(b"person@example.com").hexdigest(),
            user_id_was=str(user.pk),
            expires_at=timezone.now() + timedelta(days=30),
        )
        row = ReRegistrationHash.objects.get()
        assert row.scheme == ReRegistrationHash.SCHEME_UNVERIFIED
        assert is_reregistration(email="person@example.com") is False

    def test_legacy_rows_still_match(self, user):
        from stapel_gdpr.reregistration import _legacy_digest, is_reregistration

        ReRegistrationHash.objects.create(
            hash_type="email",
            hash_value=_legacy_digest("email", "person@example.com"),
            scheme=ReRegistrationHash.SCHEME_LEGACY,
            user_id_was=str(user.pk),
            expires_at=timezone.now() + timedelta(days=30),
        )
        assert is_reregistration(email="Person@Example.com") is True

    def test_boot_check_reports_unverified_rows(self, user):
        import hashlib

        from stapel_gdpr.checks import check_reregistration_hashes

        ReRegistrationHash.objects.create(
            hash_type="email",
            hash_value=hashlib.sha256(b"person@example.com").hexdigest(),
            user_id_was=str(user.pk),
            expires_at=timezone.now() + timedelta(days=30),
        )
        assert [m.id for m in check_reregistration_hashes(databases=["default"])] == [
            "gdpr.E004"
        ]

    def test_purge_command_clears_them(self, user):
        import hashlib

        from django.core.management import call_command

        from stapel_gdpr.reregistration import store_hashes

        store_hashes(user.pk, email="keep@example.com")
        ReRegistrationHash.objects.create(
            hash_type="phone",
            hash_value=hashlib.sha256(b"+15550001111").hexdigest(),
            user_id_was=str(user.pk),
            expires_at=timezone.now() + timedelta(days=30),
        )

        call_command("gdpr_purge_unverified_hashes")

        remaining = list(ReRegistrationHash.objects.values_list("scheme", flat=True))
        assert remaining == [ReRegistrationHash.SCHEME_HMAC_V1]
