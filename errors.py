from stapel_core.django.api.errors import register_service_errors

ERR_409_EXPORT_COOLDOWN     = 'error.409.gdpr.export_cooldown'
ERR_409_CLOSURE_PENDING     = 'error.409.gdpr.closure_already_pending'
ERR_409_LEGAL_HOLD          = 'error.409.gdpr.legal_hold'
ERR_404_NO_ACTIVE_CLOSURE   = 'error.404.gdpr.no_active_closure'
ERR_404_EXPORT_NOT_FOUND    = 'error.404.gdpr.export_not_found'
ERR_403_ACCOUNT_CLOSED      = 'error.403.gdpr.account_closed'
ERR_410_DOWNLOAD_EXPIRED    = 'error.410.gdpr.download_expired'
ERR_410_DOWNLOAD_CONSUMED   = 'error.410.gdpr.download_consumed'
ERR_425_EXPORT_NOT_READY    = 'error.425.gdpr.export_not_ready'
ERR_503_CLOSURE_UNAVAILABLE = 'error.503.gdpr.closure_unavailable'
ERR_400_UNKNOWN_SUBJECT     = 'error.400.gdpr.unknown_subject_type'
ERR_400_UNKNOWN_DSAR_KIND   = 'error.400.gdpr.unknown_dsar_kind'
ERR_403_ERASURE_FORBIDDEN   = 'error.403.gdpr.erasure_forbidden'
ERR_404_ERASURE_NOT_FOUND   = 'error.404.gdpr.erasure_not_found'
ERR_404_DSAR_NOT_FOUND      = 'error.404.gdpr.dsar_not_found'

_ERRORS = {
    ERR_409_EXPORT_COOLDOWN:   'A data export was already requested in the last 30 days.',
    ERR_409_CLOSURE_PENDING:   'Account closure is already in progress.',
    ERR_409_LEGAL_HOLD:        'Account data is under a legal hold and cannot be deleted.',
    ERR_404_NO_ACTIVE_CLOSURE: 'No pending account closure found.',
    ERR_404_EXPORT_NOT_FOUND:  'Export request not found.',
    ERR_403_ACCOUNT_CLOSED:    'This account is being erased and can no longer be used.',
    ERR_410_DOWNLOAD_EXPIRED:  'Download link has expired.',
    ERR_410_DOWNLOAD_CONSUMED: 'Download link was already used. Request a new export.',
    ERR_425_EXPORT_NOT_READY:  'Export is still being prepared.',
    ERR_503_CLOSURE_UNAVAILABLE: 'Account closure is temporarily unavailable. Please retry later.',
    ERR_400_UNKNOWN_SUBJECT:   'This kind of data cannot be erased through this endpoint.',
    ERR_400_UNKNOWN_DSAR_KIND: 'Unknown kind of data-protection request.',
    ERR_403_ERASURE_FORBIDDEN: 'You are not allowed to request erasure of this item.',
    ERR_404_ERASURE_NOT_FOUND: 'Erasure request not found.',
    ERR_404_DSAR_NOT_FOUND:    'Data-protection request not found.',
}
register_service_errors(
    _ERRORS,
    remediation={
        # A subject type or a request kind outside the declared vocabulary is
        # a client-side mistake; the authorizer's refusal is not user-fixable.
        ERR_400_UNKNOWN_SUBJECT:   'fix_input',
        ERR_400_UNKNOWN_DSAR_KIND: 'fix_input',
        ERR_403_ERASURE_FORBIDDEN: 'contact_support',
    },
)


class SessionRevocationUnavailable(RuntimeError):
    """No seam could revoke the user's sessions, so the closure was refused.

    Raised out of :func:`stapel_gdpr.lifecycle.revoke_sessions` and allowed
    to abort ``initiate_closure``'s transaction: recording a closure that
    left every pre-closure access token alive is the defect, not the fix.
    """
