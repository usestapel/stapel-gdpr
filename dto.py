from dataclasses import dataclass
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
        download_available: Whether archive is ready to download. Example: true
        expires_at: ISO datetime when download link expires, null if not ready. Example: 2026-07-01T12:00:00Z
    """
    request_id: int
    status: str
    parts_done: int
    parts_total: int
    download_available: bool
    expires_at: Optional[str]


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
