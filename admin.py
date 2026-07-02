from django.contrib import admin

from .models import (
    AccountClosureRequest,
    AccountDeletionPart,
    DataExportRequest,
    LegalHold,
    ReRegistrationHash,
)


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


@admin.register(AccountClosureRequest)
class AccountClosureRequestAdmin(admin.ModelAdmin):
    list_display = ('user_id', 'trigger', 'status', 'initiated_at', 'grace_ends_at', 'deleted_at')
    list_filter  = ('status', 'trigger')
    search_fields = ('user_id', 'correlation_id')
    inlines = [AccountDeletionPartInline]


@admin.register(DataExportRequest)
class DataExportRequestAdmin(admin.ModelAdmin):
    list_display = ('user_id', 'status', 'created_at', 'deadline', 'download_expires_at')
    list_filter  = ('status',)
    search_fields = ('user_id', 'correlation_id')


@admin.register(ReRegistrationHash)
class ReRegistrationHashAdmin(admin.ModelAdmin):
    list_display = ('hash_type', 'user_id_was', 'created_at', 'expires_at')
    list_filter  = ('hash_type',)
