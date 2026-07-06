from django.contrib import admin

from stapel_core.django.admin.base import StapelModelAdmin

from .models import (
    AccountClosureRequest,
    AccountDeletionPart,
    DataExportRequest,
    LegalHold,
    ReRegistrationHash,
)

# Category notes (AS-5 / docs/admin-suite.md):
#
# - LegalHold stays undecorated (implicit ``business``): placing/releasing a
#   hold (setting ``released_at``) is a legitimate, expected staff workflow
#   through this exact admin — see MODULE.md "Placing/releasing legal holds
#   -> LegalHold ORM/admin".
# - AccountClosureRequest / AccountDeletionPart / DataExportRequest /
#   DataExportPart / ReRegistrationHash are all ``@access.ops``: their state
#   machines are owned entirely by ``GDPROrchestrator`` / scheduled tasks.
#   MODULE.md is explicit — "Do not flip AccountClosureRequest.status or
#   AccountDeletionPart rows directly" — and there is no staff-facing
#   cancel/approve/override action anywhere in views.py or admin.py; closure
#   cancellation is user-initiated only (AccountCancelCloseView, keyed off
#   the authenticated requester). Hand-editing any of these rows through the
#   admin (status, tokens, archive paths, completion flags) would desync the
#   state machine from the orchestrator's bookkeeping.


@admin.register(LegalHold)
class LegalHoldAdmin(admin.ModelAdmin):
    list_display  = ('user_id', 'reason', 'created_by', 'created_at', 'released_at')
    list_filter   = ('released_at',)
    search_fields = ('user_id', 'reason', 'created_by')
    readonly_fields = ('created_at',)


class AccountDeletionPartInline(admin.TabularInline):
    model = AccountDeletionPart
    extra = 0
    readonly_fields = ('service', 'status', 'completed_at', 'error')
    # No has_add/change/delete_permission overrides needed here: AccountDeletionPart
    # is declared @access.ops, so MandateBackend already forbids add/change/delete
    # on it (even for a superuser) at the permission layer the inline consults.


@admin.register(AccountClosureRequest)
class AccountClosureRequestAdmin(StapelModelAdmin):
    list_display = ('user_id', 'trigger', 'status', 'initiated_at', 'grace_ends_at', 'deleted_at')
    list_filter  = ('status', 'trigger')
    search_fields = ('user_id', 'correlation_id')
    inlines = [AccountDeletionPartInline]


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
