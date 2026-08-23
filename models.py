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
    def erasure(self):
        """The subject-scoped erasure this closure is executed through.

        The closure stays the user-facing grace/cancel object; the receipts
        ledger it is certified against lives on the ErasureRequest, which is
        the same object an entity deletion produces.
        """
        return self.erasures.order_by('-requested_at').first()

    @property
    def all_remote_parts_done(self):
        """True when every expected owner confirmed erasure
        (vacuously true when no erasure was dispatched yet)."""
        erasure = self.erasure
        return erasure is None or not erasure.parts.exclude(
            state=ErasurePart.STATE_DONE,
        ).exists()

    @property
    def unreceipted_owners(self) -> list[str]:
        """Declared owners without a durable receipt — what blocks DELETED."""
        erasure = self.erasure
        if erasure is None:
            return []
        return erasure.unreceipted_owners


@access.ops
class ErasureRequest(models.Model):
    """One erasure of one subject, proven by one receipt per claiming owner.

    The machine is the account closure's, generalized: the account was only
    ever the first subject type. A workspace, a meeting, a recording, a
    document or a file gets the same clock, the same per-owner receipts and
    the same refusal to call itself DELETED on silence — so a product can
    show "pending deletion until X" for an entity exactly as it does for an
    account, instead of a hard delete nobody can audit.
    """

    ORIGIN_USER            = 'user'
    ORIGIN_DSAR            = 'dsar'
    ORIGIN_INACTIVITY      = 'inactivity'
    ORIGIN_RESTORE_REQUEUE = 'restore_requeue'
    ORIGIN_ADMIN           = 'admin'
    ORIGIN_CHOICES = [
        (ORIGIN_USER,            'User action'),
        (ORIGIN_DSAR,            'Data subject access request'),
        (ORIGIN_INACTIVITY,      'Inactivity'),
        (ORIGIN_RESTORE_REQUEUE, 'Re-queued after a backup restore'),
        (ORIGIN_ADMIN,           'Administrator'),
    ]

    STATE_QUEUED  = 'queued'
    STATE_ERASING = 'erasing'
    STATE_DELETED = 'deleted'
    STATE_TIMEOUT = 'timeout'
    STATE_CHOICES = [
        (STATE_QUEUED,  'Queued'),
        (STATE_ERASING, 'Erasing'),
        (STATE_DELETED, 'Deleted'),
        (STATE_TIMEOUT, 'Timed out'),
    ]

    #: What is being erased — one of STAPEL_GDPR["SUBJECT_TYPES"].
    subject_type   = models.CharField(max_length=32, db_index=True)
    #: The host's own id for that subject (a user id for ``account``).
    subject_key    = models.CharField(max_length=128, db_index=True)
    #: Set for owners that partition their stores by workspace, so an owner
    #: can scope its delete without a lookup back into the host.
    workspace_id   = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    requested_by   = models.UUIDField(null=True, blank=True, db_index=True)
    origin         = models.CharField(max_length=20, choices=ORIGIN_CHOICES, default=ORIGIN_USER)
    requested_at   = models.DateTimeField(default=timezone.now, db_index=True)
    #: Only the account has a cancellable grace: the UI removal of an entity
    #: already happened, so its clock is a purge SLA, not a waiting period.
    grace_ends_at  = models.DateTimeField(null=True, blank=True)
    due_at         = models.DateTimeField()
    state          = models.CharField(max_length=20, choices=STATE_CHOICES, default=STATE_QUEUED, db_index=True)
    completed_at   = models.DateTimeField(null=True, blank=True)
    correlation_id = models.CharField(max_length=36, unique=True, null=True, blank=True, db_index=True)
    #: Which data-owner inventory this erasure was judged by.
    registry_version = models.CharField(max_length=64, blank=True, default='')
    #: True when DELETED was reached through ALLOW_ERASURE_WITHOUT_RECEIPTS.
    completeness_waived = models.BooleanField(default=False)
    #: The account-closure object this erasure executes, when the subject is
    #: an account. Null for entity erasures — they have no grace to cancel.
    closure        = models.ForeignKey(
        AccountClosureRequest, on_delete=models.CASCADE,
        related_name='erasures', null=True, blank=True,
    )
    #: The request this one re-runs after a backup restore. Unique per
    #: origin, which is the whole idempotency mechanism: a second
    #: ``gdpr_requeue_after_restore`` for the same window re-derives the same
    #: (source, origin) pairs and writes nothing.
    source_request = models.ForeignKey(
        'self', on_delete=models.SET_NULL,
        related_name='requeues', null=True, blank=True,
    )
    #: The backup timestamp that motivated a re-queue — kept for the audit
    #: trail ("this data came back on ...").
    restored_from  = models.DateTimeField(null=True, blank=True)
    note           = models.TextField(blank=True, default='')

    class Meta:
        app_label = 'gdpr'
        ordering  = ['-requested_at']
        constraints = [
            models.UniqueConstraint(
                fields=['origin', 'source_request'],
                condition=models.Q(source_request__isnull=False),
                name='gdpr_erasure_one_requeue_per_source',
            ),
        ]
        indexes = [
            models.Index(fields=['subject_type', 'subject_key']),
        ]

    def save(self, *args, **kwargs):
        if not self.pk and not self.due_at:
            from .conf import gdpr_settings

            days = int(gdpr_settings.ERASURE_SLA_DAYS or 30)
            self.due_at = (self.requested_at or timezone.now()) + timedelta(days=days)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'ErasureRequest({self.subject_type}:{self.subject_key}, {self.state})'

    @property
    def unreceipted_owners(self) -> list[str]:
        """Owners without a durable receipt — what blocks DELETED."""
        return sorted(
            self.parts.exclude(state=ErasurePart.STATE_DONE).values_list('owner', flat=True)
        )

    @property
    def fully_erased_by(self):
        """When the last processor's contractual window closes.

        A product can only honestly say "erased everywhere on X" once the
        subprocessors' windows have run out too, so the date the status
        endpoints publish is the max of our own SLA and every obligation's.
        """
        latest = self.due_at
        for obligation in self.obligations.all():
            if obligation.due_at and obligation.due_at > latest:
                latest = obligation.due_at
        return latest


@access.ops
class ErasurePart(models.Model):
    """Per-owner erasure receipt — one row per owner claiming the subject.

    Owners confirm by emitting ``gdpr.section.erased`` with this request's
    correlation_id. A part without a receipt keeps blocking DELETED, and
    ``sweep_deletion_deadlines`` flips an overdue one to TIMEOUT so silence
    becomes a state rather than an absence.
    """
    STATE_PENDING = 'pending'
    STATE_DONE    = 'done'
    STATE_FAILED  = 'failed'
    STATE_TIMEOUT = 'timeout'
    STATE_CHOICES = [
        (STATE_PENDING, 'Pending'),
        (STATE_DONE,    'Done'),
        (STATE_FAILED,  'Failed'),
        (STATE_TIMEOUT, 'Timed out'),
    ]

    KIND_LOCAL  = 'local'
    KIND_REMOTE = 'remote'
    KIND_CHOICES = [
        (KIND_LOCAL,  'In-process provider'),
        (KIND_REMOTE, 'Remote service'),
    ]

    request      = models.ForeignKey(ErasureRequest, on_delete=models.CASCADE, related_name='parts')
    owner        = models.CharField(max_length=50)
    state        = models.CharField(max_length=20, choices=STATE_CHOICES, default=STATE_PENDING)
    kind         = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_REMOTE)
    # The durable receipt: an opaque id the owner returns with its
    # confirmation. A DONE part without one is an assumption, not evidence.
    receipt_id   = models.CharField(max_length=128, blank=True, default='')
    receipt_at   = models.DateTimeField(null=True, blank=True)
    # What the owner reported having removed, by its own count.
    counts       = models.JSONField(default=dict, blank=True)
    # Past this, the part is swept to TIMEOUT — which blocks DELETED exactly
    # like a failure does. Silence is not consent.
    deadline     = models.DateTimeField(null=True, blank=True)
    note         = models.TextField(blank=True, default='')

    class Meta:
        app_label       = 'gdpr'
        unique_together = [('request', 'owner')]

    def record_receipt(self, receipt_id: str = '', counts: dict | None = None) -> None:
        self.state      = self.STATE_DONE
        self.receipt_id = receipt_id or f'local:{timezone.now().isoformat()}'
        self.receipt_at = timezone.now()
        if counts:
            self.counts = counts
        self.save(update_fields=['state', 'receipt_id', 'receipt_at', 'counts'])


@access.ops
class DataOwnerHealth(models.Model):
    """Last time a declared owner proved its erasure path is consumed.

    An owner answers ``gdpr.owner.alive`` from the *same* subscriber that
    handles erasure, so a row here is evidence the consumer runs — not that
    a container is deployed. The gap this closes is the one that cost the
    fleet months: seven declared owners silently had no consumer process at
    all, and nothing said so until an erasure timed out.
    """

    owner                   = models.CharField(max_length=50, unique=True)
    last_alive_at           = models.DateTimeField(null=True, blank=True)
    last_probe_at           = models.DateTimeField(null=True, blank=True)
    #: What the registry says this owner claims, at the last probe.
    declared_subject_types  = models.JSONField(default=list, blank=True)
    #: What the owner itself answered with — a disagreement means the
    #: inventory and the code have drifted apart.
    answered_subject_types  = models.JSONField(default=list, blank=True)

    class Meta:
        app_label = 'gdpr'
        ordering  = ['owner']

    def __str__(self):
        return f'DataOwnerHealth({self.owner}, alive={self.last_alive_at})'


@access.ops
class SubprocessorObligation(models.Model):
    """One processor's contractual deletion window for one erasure.

    The obligation used to be a log line ("recorded the DPA obligation"),
    which is unqueryable and therefore unauditable. A row per processor per
    erasure is the trail a DPA actually asks for, and it is what
    ``fully_erased_by`` is computed from.
    """

    STATE_PENDING   = 'pending'
    STATE_CONFIRMED = 'confirmed'
    STATE_OVERDUE   = 'overdue'
    STATE_CHOICES = [
        (STATE_PENDING,   'Window open'),
        (STATE_CONFIRMED, 'Confirmed deleted'),
        (STATE_OVERDUE,   'Window closed without confirmation'),
    ]

    request      = models.ForeignKey(ErasureRequest, on_delete=models.CASCADE, related_name='obligations')
    provider     = models.CharField(max_length=64)
    window_days  = models.PositiveIntegerField(default=0)
    recorded_at  = models.DateTimeField(default=timezone.now)
    due_at       = models.DateTimeField()
    state        = models.CharField(max_length=20, choices=STATE_CHOICES, default=STATE_PENDING)
    note         = models.TextField(blank=True, default='')

    class Meta:
        app_label       = 'gdpr'
        unique_together = [('request', 'provider')]
        ordering        = ['provider']

    def __str__(self):
        return f'SubprocessorObligation({self.provider}, due={self.due_at})'


def business_days_from(start, days: int):
    """*start* plus *days* business days (Mon-Fri), naive about holidays.

    The GDPR acknowledgement deadline is stated in business days, and
    counting them in calendar days is how an automated acknowledgement
    quietly misses the statutory clock over a long weekend. Public holidays
    are jurisdiction-specific and deliberately not modelled — the answer is
    never later than the true one, which is the safe direction.
    """
    moment = start
    remaining = int(days)
    while remaining > 0:
        moment = moment + timedelta(days=1)
        if moment.weekday() < 5:
            remaining -= 1
    return moment


@access.ops
class DsarRequest(models.Model):
    """A data subject's request, with the clocks the regulation puts on it.

    Intake was the missing edge: the machine could erase and export, and no
    surface existed for a person to *ask*. A row here starts both statutory
    clocks (acknowledge within three business days, resolve within thirty)
    and links to whatever the request set in motion.
    """

    KIND_ACCESS        = 'access'
    KIND_ERASURE       = 'erasure'
    KIND_RECTIFICATION = 'rectification'
    KIND_PORTABILITY   = 'portability'
    KIND_CHOICES = [
        (KIND_ACCESS,        'Access (Art. 15)'),
        (KIND_ERASURE,       'Erasure (Art. 17)'),
        (KIND_RECTIFICATION, 'Rectification (Art. 16)'),
        (KIND_PORTABILITY,   'Portability (Art. 20)'),
    ]

    CHANNEL_APP   = 'app'
    CHANNEL_FORM  = 'form'
    CHANNEL_EMAIL = 'email'
    CHANNEL_CHOICES = [
        (CHANNEL_APP,   'In-app (authenticated)'),
        (CHANNEL_FORM,  'Public form (anonymous)'),
        (CHANNEL_EMAIL, 'Email, transcribed by staff'),
    ]

    STATE_RECEIVED     = 'received'
    STATE_ACKNOWLEDGED = 'acknowledged'
    STATE_IN_PROGRESS  = 'in_progress'
    STATE_RESOLVED     = 'resolved'
    STATE_REJECTED     = 'rejected'
    STATE_CHOICES = [
        (STATE_RECEIVED,     'Received'),
        (STATE_ACKNOWLEDGED, 'Acknowledged'),
        (STATE_IN_PROGRESS,  'In progress'),
        (STATE_RESOLVED,     'Resolved'),
        (STATE_REJECTED,     'Rejected'),
    ]

    #: Acknowledgement deadline, in business days (Art. 12(3) practice).
    ACK_BUSINESS_DAYS = 3
    #: Resolution deadline, in calendar days (Art. 12(3)).
    RESOLVE_DAYS = 30

    kind            = models.CharField(max_length=20, choices=KIND_CHOICES)
    channel         = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default=CHANNEL_APP)
    subject_email   = models.EmailField()
    #: Set when the requester is a known account; null for the public form
    #: until staff match the request to a person.
    user_id         = models.UUIDField(null=True, blank=True, db_index=True)
    received_at     = models.DateTimeField(default=timezone.now, db_index=True)
    ack_due_at      = models.DateTimeField()
    #: When the automated acknowledgement went out — the proof the 3-day
    #: clock was met, rather than an assumption that mail is sent somewhere.
    ack_sent_at     = models.DateTimeField(null=True, blank=True)
    resolve_due_at  = models.DateTimeField()
    erasure_request = models.ForeignKey(
        ErasureRequest, on_delete=models.SET_NULL,
        related_name='dsar_requests', null=True, blank=True,
    )
    export_request  = models.ForeignKey(
        DataExportRequest, on_delete=models.SET_NULL,
        related_name='dsar_requests', null=True, blank=True,
    )
    state           = models.CharField(max_length=20, choices=STATE_CHOICES, default=STATE_RECEIVED, db_index=True)
    note            = models.TextField(blank=True, default='')
    overdue_notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'gdpr'
        ordering  = ['-received_at']

    def save(self, *args, **kwargs):
        if not self.pk:
            received = self.received_at or timezone.now()
            self.received_at = received
            if not self.ack_due_at:
                self.ack_due_at = business_days_from(received, self.ACK_BUSINESS_DAYS)
            if not self.resolve_due_at:
                self.resolve_due_at = received + timedelta(days=self.RESOLVE_DAYS)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'DsarRequest({self.kind}, {self.state})'


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
