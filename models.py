import secrets
from django.db import models
from django.utils import timezone
from datetime import timedelta


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
    download_token      = models.CharField(max_length=64, unique=True, null=True, blank=True)
    created_at          = models.DateTimeField(auto_now_add=True)
    deadline            = models.DateTimeField()        # created_at + 24 h
    download_expires_at = models.DateTimeField(null=True, blank=True)  # +7 days after ready
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

    def generate_download_token(self):
        self.download_token      = secrets.token_urlsafe(48)
        self.download_expires_at = timezone.now() + timedelta(days=7)
        self.save(update_fields=['download_token', 'download_expires_at'])
        return self.download_token


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


class AccountDeletionPart(models.Model):
    """Per-service deletion confirmation — mirrors DataExportPart.

    One row per remote service expected to erase its slice of user data
    (STAPEL_GDPR["REMOTE_DELETION_SERVICES"]). Services confirm by emitting
    ``gdpr.section.erased`` with the closure's correlation_id.
    """
    STATUS_PENDING = 'pending'
    STATUS_DONE    = 'done'
    STATUS_FAILED  = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_DONE,    'Done'),
        (STATUS_FAILED,  'Failed'),
    ]

    closure      = models.ForeignKey(AccountClosureRequest, on_delete=models.CASCADE, related_name='parts')
    service      = models.CharField(max_length=50)
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    completed_at = models.DateTimeField(null=True, blank=True)
    error        = models.TextField(null=True, blank=True)

    class Meta:
        app_label       = 'gdpr'
        unique_together = [('closure', 'service')]


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


class ReRegistrationHash(models.Model):
    """Irreversible hashes of deleted-user PII for re-registration detection (24 months retention)."""
    TYPE_EMAIL = 'email'
    TYPE_PHONE = 'phone'
    TYPE_CHOICES = [
        (TYPE_EMAIL, 'Email'),
        (TYPE_PHONE, 'Phone'),
    ]

    hash_type   = models.CharField(max_length=10, choices=TYPE_CHOICES)
    hash_value  = models.CharField(max_length=128, db_index=True)
    user_id_was = models.CharField(max_length=64)  # str(pk) — supports both int and UUID PKs
    created_at  = models.DateTimeField(auto_now_add=True)
    expires_at  = models.DateTimeField()    # +24 months

    class Meta:
        app_label     = 'gdpr'
        unique_together = [('hash_type', 'hash_value')]
