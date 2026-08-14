import hashlib
import secrets
from django.db import models
from django.utils import timezone
from datetime import timedelta

from stapel_core.access import access


def hash_download_token(token: str) -> str:
    """Digest of a download token, as stored.

    The plaintext token exists exactly once — in the ready-notification the
    user receives. A database that never holds it cannot leak it, and a
    backup/dump of the export tables is not a set of live download links.
    """
    return hashlib.sha256(token.encode()).hexdigest()


@access.ops
class DataExportRequest(models.Model):
    STATUS_PENDING    = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_ASSEMBLING = 'assembling'
    STATUS_READY      = 'ready'
    STATUS_FAILED     = 'failed'
    STATUS_EXPIRED    = 'expired'
    STATUS_CHOICES = [
        (STATUS_PENDING,    'Pending'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_ASSEMBLING, 'Assembling'),
        (STATUS_READY,      'Ready'),
        (STATUS_FAILED,     'Failed'),
        (STATUS_EXPIRED,    'Expired'),
    ]

    # Framework users have UUID primary keys (settings.AUTH_USER_MODEL).
    user_id             = models.UUIDField(db_index=True)
    status              = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    # UUID used as the bus correlation key — links this request to service completion events.
    correlation_id      = models.CharField(max_length=36, unique=True, null=True, blank=True, db_index=True)
    # Immutable list of services expected to contribute (snapshot at request time)
    expected_services   = models.JSONField(default=list)
    archive_path        = models.CharField(max_length=500, null=True, blank=True)
    # Plaintext token column of the pre-hardening format. Nulled by migration
    # 0003 and never written again; the contract-phase removal ships in the
    # release after this one (expand/contract, release-management.md §3).
    download_token      = models.CharField(max_length=64, unique=True, null=True, blank=True)
    # SHA-256 of the single-use token. Only the digest is ever stored.
    download_token_hash = models.CharField(max_length=64, unique=True, null=True, blank=True)
    # Set the moment a download is served; a second attempt is refused.
    download_consumed_at = models.DateTimeField(null=True, blank=True)
    # The archive could not be proven complete (an owner missing, failed or
    # timed out). Surfaced to the user instead of being hidden in a README.
    is_partial          = models.BooleanField(default=False)
    #: Owners that did not contribute — the "what is missing" the user is owed.
    missing_services    = models.JSONField(default=list, blank=True)
    created_at          = models.DateTimeField(auto_now_add=True)
    deadline            = models.DateTimeField()        # created_at + 24 h
    download_expires_at = models.DateTimeField(null=True, blank=True)  # + DOWNLOAD_TTL_HOURS
    error               = models.TextField(null=True, blank=True)

    class Meta:
        app_label = 'gdpr'
        ordering  = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.pk and not self.deadline:
            self.deadline = timezone.now() + timedelta(hours=48)
        super().save(*args, **kwargs)

    @property
    def is_complete(self):
        return self.parts.filter(status=DataExportPart.STATUS_PENDING).count() == 0

    @property
    def all_parts_done(self):
        return self.parts.exclude(status=DataExportPart.STATUS_DONE).count() == 0

    def generate_download_token(self) -> str:
        """Mint the single-use token and store only its digest.

        Returns the plaintext, which the caller must hand to the user right
        away — it is unrecoverable afterwards.
        """
        from .conf import gdpr_settings

        token = secrets.token_urlsafe(48)
        ttl = timedelta(hours=float(gdpr_settings.DOWNLOAD_TTL_HOURS or 24))
        self.download_token       = None
        self.download_token_hash  = hash_download_token(token)
        self.download_consumed_at = None
        self.download_expires_at  = timezone.now() + ttl
        self.save(update_fields=[
            'download_token', 'download_token_hash',
            'download_consumed_at', 'download_expires_at',
        ])
        return token

    def consume_download_token(self, token: str) -> bool:
        """Atomically spend the token. True exactly once per token.

        The conditional UPDATE is the whole mechanism: two concurrent
        downloads race on the same row and the database picks one, so
        "single-use" is a fact rather than a sentence in the API docs.
        """
        if not token:
            return False
        updated = type(self).objects.filter(
            pk=self.pk,
            download_token_hash=hash_download_token(token),
            download_consumed_at__isnull=True,
        ).update(download_consumed_at=timezone.now())
        if updated:
            self.refresh_from_db(fields=['download_consumed_at'])
        return bool(updated)


@access.ops
class DataExportPart(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_DONE    = 'done'
    STATUS_FAILED  = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_DONE,    'Done'),
        (STATUS_FAILED,  'Failed'),
    ]

    request      = models.ForeignKey(DataExportRequest, on_delete=models.CASCADE, related_name='parts')
    service      = models.CharField(max_length=50)   # section name: 'auth', 'profiles', 'cdn' …
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    # Object storage path where the service uploaded its export (microservices mode).
    # Null in monolith mode where the orchestrator writes to staging_dir directly.
    bucket_path  = models.CharField(max_length=500, null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error        = models.TextField(null=True, blank=True)

    class Meta:
        app_label     = 'gdpr'
        unique_together = [('request', 'service')]


@access.ops
class AccountClosureRequest(models.Model):
    TRIGGER_MANUAL     = 'manual'
    TRIGGER_INACTIVITY = 'inactivity'
    TRIGGER_PLATFORM   = 'platform'
    TRIGGER_CHOICES = [
        (TRIGGER_MANUAL,     'Manual'),
        (TRIGGER_INACTIVITY, 'Inactivity'),
        (TRIGGER_PLATFORM,   'Platform'),
    ]

    STATUS_GRACE     = 'grace'
    STATUS_DELETING  = 'deleting'
    STATUS_DELETED   = 'deleted'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_GRACE,     'Grace Period'),
        (STATUS_DELETING,  'Deleting'),
        (STATUS_DELETED,   'Deleted'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    # Not unique: a user may close, cancel, and later close again — the
    # orchestrator guards against concurrent *active* closures instead.
    user_id       = models.UUIDField(db_index=True)
    trigger       = models.CharField(max_length=20, choices=TRIGGER_CHOICES)
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_GRACE)
    # UUID used as the comm correlation key — links this closure to
    # gdpr.section.erased confirmations from remote services.
    correlation_id = models.CharField(max_length=36, unique=True, null=True, blank=True, db_index=True)
    # Set once every local (in-process) provider erased successfully.
    local_erasure_done = models.BooleanField(default=False)
    # Which data-owner inventory this erasure was judged by — an audit can
    # tell a closure certified against 3 owners from one certified against 11.
    registry_version = models.CharField(max_length=64, blank=True, default='')
    # True when DELETED was reached through ALLOW_ERASURE_WITHOUT_RECEIPTS
    # instead of through a receipt from every declared owner.
    completeness_waived = models.BooleanField(default=False)
    # When the primary users.User row itself was erased (anonymized or
    # deleted). Unset means the person is still on file whatever the
    # providers reported, so DELETED is refused.
    identity_erased_at = models.DateTimeField(null=True, blank=True)
    initiated_at  = models.DateTimeField(auto_now_add=True)
    grace_ends_at = models.DateTimeField()    # +30 days
    deleted_at    = models.DateTimeField(null=True, blank=True)
    cancelled_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'gdpr'

    def save(self, *args, **kwargs):
        if not self.pk and not self.grace_ends_at:
            self.grace_ends_at = timezone.now() + timedelta(days=30)
        super().save(*args, **kwargs)

    @property
    def all_remote_parts_done(self):
        """True when every expected remote service confirmed erasure
        (vacuously true when no remote services are configured)."""
        return not self.parts.exclude(status=AccountDeletionPart.STATUS_DONE).exists()

    @property
    def unreceipted_owners(self) -> list[str]:
        """Declared owners without a durable receipt — what blocks DELETED."""
        return sorted(
            self.parts.exclude(
                status=AccountDeletionPart.STATUS_DONE,
            ).values_list('service', flat=True)
        )


@access.ops
class AccountDeletionPart(models.Model):
    """Per-service deletion confirmation — mirrors DataExportPart.

    One row per remote service expected to erase its slice of user data
    (STAPEL_GDPR["REMOTE_DELETION_SERVICES"]). Services confirm by emitting
    ``gdpr.section.erased`` with the closure's correlation_id.
    """
    STATUS_PENDING = 'pending'
    STATUS_DONE    = 'done'
    STATUS_FAILED  = 'failed'
    STATUS_TIMEOUT = 'timeout'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_DONE,    'Done'),
        (STATUS_FAILED,  'Failed'),
        (STATUS_TIMEOUT, 'Timed out'),
    ]

    KIND_LOCAL  = 'local'
    KIND_REMOTE = 'remote'
    KIND_CHOICES = [
        (KIND_LOCAL,  'In-process provider'),
        (KIND_REMOTE, 'Remote service'),
    ]

    closure      = models.ForeignKey(AccountClosureRequest, on_delete=models.CASCADE, related_name='parts')
    service      = models.CharField(max_length=50)
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    kind         = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_REMOTE)
    # The durable receipt: an opaque id the owner returns with its
    # confirmation. A DONE part without one is an assumption, not evidence.
    receipt_id   = models.CharField(max_length=128, blank=True, default='')
    # Past this, the part is swept to TIMEOUT — which blocks DELETED exactly
    # like a failure does. Silence is not consent.
    deadline     = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error        = models.TextField(null=True, blank=True)

    class Meta:
        app_label       = 'gdpr'
        unique_together = [('closure', 'service')]

    def record_receipt(self, receipt_id: str = '') -> None:
        self.status       = self.STATUS_DONE
        self.receipt_id   = receipt_id or f'local:{timezone.now().isoformat()}'
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'receipt_id', 'completed_at'])


class LegalHold(models.Model):
    """Blocks account closure / deletion while litigation or an official
    investigation requires the data to be preserved (GDPR Art. 17(3))."""

    user_id     = models.UUIDField(db_index=True)
    reason      = models.TextField()
    created_by  = models.CharField(max_length=150, blank=True, default='')
    created_at  = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'gdpr'
        ordering  = ['-created_at']

    @classmethod
    def is_held(cls, user_id) -> bool:
        return cls.objects.filter(user_id=user_id, released_at__isnull=True).exists()

    def __str__(self):
        state = 'released' if self.released_at else 'active'
        return f'LegalHold({self.user_id}, {state})'


@access.ops
class ReRegistrationHash(models.Model):
    """Irreversible hashes of deleted-user PII for re-registration detection (24 months retention).

    The hash format is this library's to define (see reregistration.py): one
    purpose-bound keyed HMAC, recorded per row in ``scheme``. Rows written
    without going through :func:`stapel_gdpr.reregistration.store_hashes`
    carry :data:`SCHEME_UNVERIFIED` — a plain SHA-256 of an email or a phone
    number is dictionary-recoverable in seconds, so such rows are reported by
    the ``gdpr.E004`` system check, ignored by lookups, and meant to be
    purged.
    """
    TYPE_EMAIL = 'email'
    TYPE_PHONE = 'phone'
    TYPE_CHOICES = [
        (TYPE_EMAIL, 'Email'),
        (TYPE_PHONE, 'Phone'),
    ]

    #: Purpose-bound keyed HMAC — the scheme every new row uses.
    SCHEME_HMAC_V1  = 'hmac-sha256-v1'
    #: Rows that predate the scheme column (migration 0003). Unattributable:
    #: either this library's old salted SHA-256 or another writer's unsalted
    #: one, indistinguishable after the fact. Still matched on lookup so 24
    #: months of re-registration memory is not thrown away; ages out with
    #: retention and never written again.
    SCHEME_LEGACY   = 'legacy-pre-hmac'
    #: Written by anything that bypassed store_hashes: format unknown.
    SCHEME_UNVERIFIED = 'unverified'
    SCHEME_CHOICES = [
        (SCHEME_HMAC_V1,    'HMAC-SHA256 v1 (purpose-bound, keyed)'),
        (SCHEME_LEGACY,     'Legacy (pre-scheme row, format unattributable)'),
        (SCHEME_UNVERIFIED, 'Unverified (written outside store_hashes)'),
    ]

    hash_type   = models.CharField(max_length=10, choices=TYPE_CHOICES)
    hash_value  = models.CharField(max_length=128, db_index=True)
    scheme      = models.CharField(max_length=32, choices=SCHEME_CHOICES, default=SCHEME_UNVERIFIED)
    user_id_was = models.CharField(max_length=64)  # str(pk) — supports both int and UUID PKs
    created_at  = models.DateTimeField(auto_now_add=True)
    expires_at  = models.DateTimeField()    # +24 months

    class Meta:
        app_label     = 'gdpr'
        unique_together = [('hash_type', 'hash_value')]
