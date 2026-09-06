from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExportRequestDTO:
    """Response after initiating a data export request.

    Attributes:
        request_id: Unique export request ID. Example: 42
        status: Current status. Example: pending
        message: Human-readable status message. Example: Your archive will be ready within 48 hours.
    """
    request_id: int
    status: str
    message: str


@dataclass
class ExportStatusDTO:
    """Status of a data export request.

    Attributes:
        request_id: Export request ID. Example: 42
        status: One of pending, processing, ready, failed, expired. Example: ready
        parts_done: Number of sections completed. Example: 4
        parts_total: Total sections expected. Example: 5
        download_available: Whether archive is ready to download (single-use token unspent). Example: true
        expires_at: ISO datetime when download link expires, null if not ready. Example: 2026-07-01T12:00:00Z
        is_partial: Whether sections are missing from the archive. Example: false
        missing_services: Sections that could not be included. Example: ["recordings"]
    """
    request_id: int
    status: str
    parts_done: int
    parts_total: int
    download_available: bool
    expires_at: Optional[str]
    is_partial: bool = False
    missing_services: list[str] = field(default_factory=list)


@dataclass
class ErasurePartDTO:
    """One data owner's receipt for an erasure.

    Attributes:
        owner: Data owner name. Example: recordings
        state: One of pending, done, failed, timeout. Example: done
        receipt_at: ISO datetime the owner confirmed, null while pending. Example: 2026-08-24T09:12:00Z
        receipt_id: The owner's own durable proof of erasure. Example: recordings:job-8812
        counts: What the owner removed, by its own count. Example: {"recordings": 3}
        unanswered: Whether this owner never answered at all — still waiting, or timed out in silence. A failed part answered; this one did not. Example: false
    """
    owner: str
    state: str
    receipt_at: Optional[str]
    receipt_id: str = ''
    counts: dict = field(default_factory=dict)
    unanswered: bool = False


@dataclass
class SubprocessorObligationDTO:
    """One processor's contractual deletion window for an erasure.

    Attributes:
        provider: Processor name. Example: openai
        window_days: Contractual window in days from our own completion. Example: 30
        due_at: ISO datetime the window closes. Example: 2026-09-23T09:12:00Z
        state: One of pending, confirmed, overdue. Example: pending
    """
    provider: str
    window_days: int
    due_at: str
    state: str


@dataclass
class ErasureStatusDTO:
    """State of an erasure request, with everything it is waiting on.

    Attributes:
        request_id: Erasure request ID. Example: 17
        subject_type: What is being erased. Example: recording
        subject_key: The host's id for that subject. Example: 9f1c2d3e
        workspace_id: Workspace the subject belongs to, null when not partitioned. Example: ws-42
        state: One of queued, erasing, deleted, timeout. Example: erasing
        outcome: What the report may say — pending, complete, or incomplete. Never complete while an owner is unanswered or completeness was waived. Example: incomplete
        origin: Why this erasure exists. Example: user
        requested_at: ISO datetime the erasure was opened. Example: 2026-08-24T09:00:00Z
        due_at: ISO datetime our own purge SLA expires. Example: 2026-09-23T09:00:00Z
        fully_erased_by: ISO datetime every subprocessor window has also closed. Example: 2026-10-18T09:00:00Z
        completed_at: ISO datetime the erasure was certified, null while open. Example: 2026-08-24T09:12:00Z
        grace_ends_at: ISO datetime a cancellable grace ends (accounts only). Example: 2026-09-23T09:00:00Z
        parts: Per-owner receipts. Example: []
        obligations: Per-processor deletion windows. Example: []
        unreceipted_owners: Owners still blocking completion. Example: ["media"]
        unanswered_owners: Owners that never answered — the subset that went silent rather than reporting a failure. Example: ["media"]
    """
    request_id: int
    subject_type: str
    subject_key: str
    workspace_id: Optional[str]
    state: str
    origin: str
    requested_at: str
    due_at: str
    fully_erased_by: str
    outcome: str = 'pending'
    completed_at: Optional[str] = None
    grace_ends_at: Optional[str] = None
    parts: list[ErasurePartDTO] = field(default_factory=list)
    obligations: list[SubprocessorObligationDTO] = field(default_factory=list)
    unreceipted_owners: list[str] = field(default_factory=list)
    unanswered_owners: list[str] = field(default_factory=list)


@dataclass
class DsarStatusDTO:
    """A data-subject request and the statutory clocks on it.

    Attributes:
        request_id: DSAR ID, quoted back to the subject as their reference. Example: 5
        kind: One of access, erasure, rectification, portability. Example: access
        channel: How it arrived — app, form or email. Example: form
        subject_email: Email the request was made from. Example: person@example.com
        state: One of received, acknowledged, in_progress, resolved, rejected. Example: acknowledged
        received_at: ISO datetime the request arrived. Example: 2026-08-24T09:00:00Z
        ack_due_at: ISO datetime the acknowledgement is due (3 business days). Example: 2026-08-27T09:00:00Z
        ack_sent_at: ISO datetime the acknowledgement went out, null if not yet. Example: 2026-08-24T09:00:03Z
        resolve_due_at: ISO datetime resolution is due (30 days). Example: 2026-09-23T09:00:00Z
        erasure_request_id: Linked erasure, null when none was started. Example: 17
        export_request_id: Linked data export, null when none was started. Example: 42
        note: Staff notes and automation outcomes. Example: matched to account
    """
    request_id: int
    kind: str
    channel: str
    subject_email: str
    state: str
    received_at: str
    ack_due_at: str
    ack_sent_at: Optional[str]
    resolve_due_at: str
    erasure_request_id: Optional[int] = None
    export_request_id: Optional[int] = None
    note: str = ''


@dataclass
class DataOwnerHealthDTO:
    """Whether a declared data owner is answering probes.

    Attributes:
        owner: Data owner name. Example: workspaces
        alive: Whether it answered within OWNER_ALIVE_MAX_AGE_HOURS. Example: false
        last_alive_at: ISO datetime of its last answer, null if it never answered. Example: 2026-08-20T05:00:00Z
        last_probe_at: ISO datetime it was last asked. Example: 2026-08-24T05:00:00Z
        declared_subject_types: Subjects the inventory says it holds. Example: ["account", "workspace"]
        answered_subject_types: Subjects it says it holds. Example: []
    """
    owner: str
    alive: bool
    last_alive_at: Optional[str]
    last_probe_at: Optional[str]
    declared_subject_types: list[str] = field(default_factory=list)
    answered_subject_types: list[str] = field(default_factory=list)


@dataclass
class ClosureStatusDTO:
    """Status of an account closure request.

    Attributes:
        status: One of grace, deleting, deleted, cancelled. Example: grace
        grace_ends_at: ISO datetime when grace period ends. Example: 2026-07-24T10:00:00Z
        can_cancel: Whether the closure can still be cancelled. Example: true
    """
    status: str
    grace_ends_at: str
    can_cancel: bool
