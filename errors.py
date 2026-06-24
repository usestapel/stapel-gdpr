from stapel_core.django.errors import register_service_errors

ERR_409_EXPORT_COOLDOWN     = 'error.409.gdpr.export_cooldown'
ERR_409_CLOSURE_PENDING     = 'error.409.gdpr.closure_already_pending'
ERR_404_NO_ACTIVE_CLOSURE   = 'error.404.gdpr.no_active_closure'
ERR_404_EXPORT_NOT_FOUND    = 'error.404.gdpr.export_not_found'
ERR_410_DOWNLOAD_EXPIRED    = 'error.410.gdpr.download_expired'
ERR_425_EXPORT_NOT_READY    = 'error.425.gdpr.export_not_ready'

_ERRORS = {
    ERR_409_EXPORT_COOLDOWN:   'A data export was already requested in the last 30 days.',
    ERR_409_CLOSURE_PENDING:   'Account closure is already in progress.',
    ERR_404_NO_ACTIVE_CLOSURE: 'No pending account closure found.',
    ERR_404_EXPORT_NOT_FOUND:  'Export request not found.',
    ERR_410_DOWNLOAD_EXPIRED:  'Download link has expired.',
    ERR_425_EXPORT_NOT_READY:  'Export is still being prepared.',
}
register_service_errors(_ERRORS)
