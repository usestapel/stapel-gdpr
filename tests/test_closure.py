"""Account closure / deletion lifecycle tests."""
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_gdpr.models import (
    AccountClosureRequest,
    AccountDeletionPart,
    LegalHold,
    ReRegistrationHash,
)
from stapel_gdpr.orchestrator import gdpr_orchestrator


def _expire_grace(closure):
    closure.grace_ends_at = timezone.now() - timedelta(hours=1)
    closure.save(update_fields=["grace_ends_at"])
    return closure


@pytest.mark.django_db
class TestClosureLifecycle:
    def test_initiate_starts_grace_and_deactivates(self, user):
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        assert closure.status == AccountClosureRequest.STATUS_GRACE
        assert closure.user_id == user.pk
        assert closure.grace_ends_at > timezone.now() + timedelta(days=29)
        user.refresh_from_db()
        assert user.is_active is False

    def test_double_initiate_rejected(self, user):
        gdpr_orchestrator.initiate_closure(user.pk)
        with pytest.raises(ValueError, match="closure_already_pending"):
            gdpr_orchestrator.initiate_closure(user.pk)

    def test_cancel_during_grace_reactivates(self, user):
        gdpr_orchestrator.initiate_closure(user.pk)
        closure = gdpr_orchestrator.cancel_closure(user.pk)
        assert closure.status == AccountClosureRequest.STATUS_CANCELLED
        assert closure.cancelled_at is not None
        user.refresh_from_db()
        assert user.is_active is True

    def test_cancel_without_closure_raises(self, user):
        with pytest.raises(ValueError, match="no_active_closure"):
            gdpr_orchestrator.cancel_closure(user.pk)

    def test_reinitiate_after_cancel_allowed(self, user):
        gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.cancel_closure(user.pk)
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        assert closure.status == AccountClosureRequest.STATUS_GRACE
        assert AccountClosureRequest.objects.filter(user_id=user.pk).count() == 2


@pytest.mark.django_db
class TestExecuteDeletion:
    def test_success_flips_deleted(self, user, fake_provider):
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETED
        assert closure.deleted_at is not None
        assert closure.correlation_id
        assert str(user.pk) in fake_provider.deleted
        assert str(user.pk) in fake_provider.anonymized

    def test_partial_failure_stays_deleting(self, user, fake_provider):
        fake_provider.fail_delete = True
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING
        assert closure.deleted_at is None

    def test_retry_after_failure_completes(self, user, fake_provider):
        fake_provider.fail_delete = True
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)
        fake_provider.fail_delete = False
        closure.refresh_from_db()
        gdpr_orchestrator.execute_deletion(closure)
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETED

    def test_grace_sweep_executes_deletion(self, user, fake_provider):
        from stapel_gdpr.tasks import process_expired_grace_periods

        closure = _expire_grace(gdpr_orchestrator.initiate_closure(user.pk))
        process_expired_grace_periods()
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETED

    def test_grace_not_elapsed_untouched(self, user, fake_provider):
        from stapel_gdpr.tasks import process_expired_grace_periods

        closure = gdpr_orchestrator.initiate_closure(user.pk)
        process_expired_grace_periods()
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_GRACE


@pytest.mark.django_db
class TestRemoteDeletionParts:
    def test_parts_created_and_confirmations_flip_deleted(self, settings, user):
        settings.STAPEL_GDPR = {"REMOTE_DELETION_SERVICES": ["profiles", "cdn"]}
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING
        assert closure.local_erasure_done is True
        assert set(closure.parts.values_list("service", flat=True)) == {"profiles", "cdn"}

        # Remote services confirm via the comm action our subscriber handles
        from stapel_core.comm import emit

        emit("gdpr.section.erased", {
            "user_id": str(user.pk),
            "correlation_id": closure.correlation_id,
            "service": "profiles",
        })
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING

        emit("gdpr.section.erased", {
            "user_id": str(user.pk),
            "correlation_id": closure.correlation_id,
            "service": "cdn",
        })
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETED
        assert closure.parts.filter(
            status=AccountDeletionPart.STATUS_DONE,
        ).count() == 2

    def test_remote_confirmation_before_local_success_does_not_finalize(
        self, settings, user, fake_provider
    ):
        settings.STAPEL_GDPR = {"REMOTE_DELETION_SERVICES": ["profiles"]}
        fake_provider.fail_delete = True
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        gdpr_orchestrator.mark_section_erased(closure.correlation_id, "profiles")
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETING

        # local retry succeeds -> now everything is confirmed
        fake_provider.fail_delete = False
        gdpr_orchestrator.execute_deletion(closure)
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_DELETED

    def test_unknown_correlation_is_ignored(self, user, db):
        gdpr_orchestrator.mark_section_erased(str(uuid.uuid4()), "profiles")  # no crash


@pytest.mark.django_db
class TestLegalHold:
    def test_hold_blocks_initiate(self, user):
        LegalHold.objects.create(user_id=user.pk, reason="litigation")
        with pytest.raises(ValueError, match="legal_hold"):
            gdpr_orchestrator.initiate_closure(user.pk)

    def test_hold_blocks_execute(self, user):
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        LegalHold.objects.create(user_id=user.pk, reason="litigation")
        with pytest.raises(ValueError, match="legal_hold"):
            gdpr_orchestrator.execute_deletion(closure)
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_GRACE

    def test_released_hold_does_not_block(self, user):
        LegalHold.objects.create(
            user_id=user.pk, reason="closed case", released_at=timezone.now(),
        )
        closure = gdpr_orchestrator.initiate_closure(user.pk)
        assert closure.status == AccountClosureRequest.STATUS_GRACE

    def test_grace_sweep_skips_held_users(self, user, fake_provider):
        from stapel_gdpr.tasks import process_expired_grace_periods

        closure = _expire_grace(gdpr_orchestrator.initiate_closure(user.pk))
        LegalHold.objects.create(user_id=user.pk, reason="litigation")
        process_expired_grace_periods()
        closure.refresh_from_db()
        assert closure.status == AccountClosureRequest.STATUS_GRACE
        assert fake_provider.deleted == []

    def test_retention_cleanup_skips_held_users(self, user, db):
        past = timezone.now() - timedelta(days=1)
        ReRegistrationHash.objects.create(
            hash_type="email", hash_value="h1", user_id_was=str(user.pk), expires_at=past,
        )
        other_id = uuid.uuid4()
        ReRegistrationHash.objects.create(
            hash_type="email", hash_value="h2", user_id_was=str(other_id), expires_at=past,
        )
        LegalHold.objects.create(user_id=user.pk, reason="litigation")

        from stapel_gdpr.tasks import run_retention_cleanup

        run_retention_cleanup()
        remaining = set(ReRegistrationHash.objects.values_list("user_id_was", flat=True))
        assert remaining == {str(user.pk)}


@pytest.mark.django_db
class TestReRegistrationHash:
    def test_hashes_written_on_deletion(self, user):
        from django.contrib.auth import get_user_model

        get_user_model().objects.filter(pk=user.pk).update(phone="+79991234567")
        email = user.email

        closure = gdpr_orchestrator.initiate_closure(user.pk)
        gdpr_orchestrator.execute_deletion(closure)

        hashes = ReRegistrationHash.objects.filter(user_id_was=str(user.pk))
        assert set(hashes.values_list("hash_type", flat=True)) == {"email", "phone"}
        # raw identifiers must never be stored
        for h in hashes:
            assert email not in h.hash_value
            assert "9999123" not in h.hash_value

        from stapel_gdpr.reregistration import is_reregistration

        assert is_reregistration(email=email) is True
        assert is_reregistration(email="EMAIL-CASE-" + email) is False
        assert is_reregistration(email=email.upper()) is True  # normalized
        assert is_reregistration(phone="+7 999 123-45-67") is True  # normalized
        assert is_reregistration(phone="+15550001111") is False

    def test_store_hashes_idempotent(self, user):
        from stapel_gdpr.reregistration import store_hashes

        assert store_hashes(user.pk, email=user.email) == 1
        assert store_hashes(user.pk, email=user.email) == 0
        assert ReRegistrationHash.objects.count() == 1

    def test_expired_hash_is_not_reregistration(self, user):
        from stapel_gdpr.reregistration import compute_hash, is_reregistration

        ReRegistrationHash.objects.create(
            hash_type="email",
            hash_value=compute_hash("email", "old@example.com"),
            user_id_was=str(user.pk),
            expires_at=timezone.now() - timedelta(days=1),
        )
        assert is_reregistration(email="old@example.com") is False
