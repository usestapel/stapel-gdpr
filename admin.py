from django.contrib import admin

from stapel_core.django.admin.base import StapelModelAdmin

from .models import (
    AccountClosureRequest,
    DataExportRequest,
    DataOwnerHealth,
    DsarRequest,
    ErasurePart,
    ErasureRequest,
    LegalHold,
    ReRegistrationHash,
    SubprocessorObligation,
)

# Category notes (AS-5 / docs/admin-suite.md):
#
# - LegalHold stays undecorated (implicit ``business``): placing/releasing a
#   hold (setting ``released_at``) is a legitimate, expected staff workflow
#   through this exact admin — see MODULE.md "Placing/releasing legal holds
#   -> LegalHold ORM/admin".
# - AccountClosureRequest / ErasureRequest / ErasurePart / DataExportRequest /
#   DataExportPart / DataOwnerHealth / SubprocessorObligation /
#   ReRegistrationHash are all ``@access.ops``: their state machines are owned
#   entirely by ``GDPROrchestrator`` / scheduled tasks. MODULE.md is explicit —
#   "Do not flip AccountClosureRequest.status or ErasurePart rows directly" —
#   and there is no staff-facing cancel/approve/override action anywhere in
#   views.py or admin.py; closure cancellation is user-initiated only
#   (AccountCancelCloseView, keyed off the authenticated requester).
#   Hand-editing any of these rows through the admin (status, tokens, archive
#   paths, completion flags) would desync the state machine from the
#   orchestrator's bookkeeping.
# - DsarRequest is the exception among the new models and is deliberately NOT
#   ``@access.ops``: triaging a data-subject request (state, note, matching it
#   to an account) IS the staff workflow, and the module ships an authenticated
#   PATCH endpoint for exactly that. The admin is the same workflow by another
#   door, so it stays a business model.


@admin.register(LegalHold)
class LegalHoldAdmin(admin.ModelAdmin):
    list_display  = ('user_id', 'reason', 'created_by', 'created_at', 'released_at')
    list_filter   = ('released_at',)
    search_fields = ('user_id', 'reason', 'created_by')
    readonly_fields = ('created_at',)


class ErasurePartInline(admin.TabularInline):
    model = ErasurePart
    extra = 0
    readonly_fields = (
        'owner', 'state', 'unanswered_flag', 'kind', 'receipt_id', 'receipt_at',
        'deadline', 'note',
    )
    fields = readonly_fields
    # No has_add/change/delete_permission overrides needed here: ErasurePart
    # is declared @access.ops, so MandateBackend already forbids add/change/delete
    # on it (even for a superuser) at the permission layer the inline consults.

    @admin.display(boolean=True, description='Unanswered')
    def unanswered_flag(self, obj) -> bool:
        """Silence, spelled out. 'timeout' is a state name; this is a fact."""
        return obj.unanswered


class SubprocessorObligationInline(admin.TabularInline):
    model = SubprocessorObligation
    extra = 0
    readonly_fields = ('provider', 'window_days', 'recorded_at', 'due_at', 'state', 'note')


@admin.register(ErasureRequest)
class ErasureRequestAdmin(StapelModelAdmin):
    list_display = ('subject_type', 'subject_key', 'state', 'outcome_label',
                    'unanswered_label', 'origin', 'requested_at', 'due_at',
                    'completed_at')
    list_filter  = ('state', 'subject_type', 'origin')
    search_fields = ('subject_key', 'correlation_id', 'workspace_id')
    inlines = [ErasurePartInline, SubprocessorObligationInline]

    @admin.display(description='Outcome', ordering='state')
    def outcome_label(self, obj) -> str:
        """What a report may say. A TIMEOUT row reads 'incomplete' here.

        The state column already existed and was already correct; nobody
        reading it drew the conclusion, which is the whole finding of the
        2026-09-07 audit. So the conclusion is a column.
        """
        return obj.outcome

    @admin.display(description='Never answered')
    def unanswered_label(self, obj) -> str:
        return ', '.join(obj.unanswered_owners) or '—'


@admin.register(DataOwnerHealth)
class DataOwnerHealthAdmin(StapelModelAdmin):
    list_display = ('owner', 'last_alive_at', 'last_probe_at', 'declared_subject_types', 'answered_subject_types')
    search_fields = ('owner',)


@admin.register(DsarRequest)
class DsarRequestAdmin(admin.ModelAdmin):
    list_display = ('kind', 'channel', 'subject_email', 'state', 'received_at', 'ack_due_at', 'ack_sent_at', 'resolve_due_at')
    list_filter  = ('state', 'kind', 'channel')
    search_fields = ('subject_email', 'user_id', 'note')
    readonly_fields = ('received_at', 'ack_due_at', 'resolve_due_at', 'ack_sent_at')


@admin.register(AccountClosureRequest)
class AccountClosureRequestAdmin(StapelModelAdmin):
    list_display = ('user_id', 'trigger', 'status', 'initiated_at', 'grace_ends_at', 'deleted_at')
    list_filter  = ('status', 'trigger')
    search_fields = ('user_id', 'correlation_id')


@admin.register(DataExportRequest)
class DataExportRequestAdmin(StapelModelAdmin):
    list_display = ('user_id', 'status', 'created_at', 'deadline', 'download_expires_at')
    list_filter  = ('status',)
    search_fields = ('user_id', 'correlation_id')


@admin.register(ReRegistrationHash)
class ReRegistrationHashAdmin(StapelModelAdmin):
    list_display = ('hash_type', 'user_id_was', 'created_at', 'expires_at')
    list_filter  = ('hash_type',)
    # hash_value is an irreversible salted hash, not a live credential, so the
    # model is `ops` rather than `secret` — but it is still a hash of PII
    # (email/phone) and matches the SECRET_FIELD_PATTERNS "hash" substring, so
    # it is pinned explicitly, mirroring stapel-core's own precedent of
    # masking session_key/session_data on the (ops) Session admin.
    secret_fields = ('hash_value',)
