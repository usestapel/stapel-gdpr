## Quick start

```python
# settings.py
INSTALLED_APPS = [
    ...
    'stapel_gdpr',
]

MIDDLEWARE = [
    ...
    # After authentication: refuses every request of an account being erased,
    # whatever a still-valid token claims.
    'stapel_gdpr.guards.AccountClosureGuardMiddleware',
]

STAPEL_GDPR = {
    # Every store holding personal data. Erasure is only ever reported
    # complete when each of these returned a deletion receipt, so an owner
    # missing here is a store that quietly keeps the data. `manage.py check`
    # fails while this is empty.
    'DATA_OWNERS': ['auth', 'profiles', {'name': 'cdn', 'kind': 'remote'}],
    'DATA_OWNERS_VERSION': '2026-08-13.1',
    # How the user's sessions are revoked at closure. Auto-detected when
    # stapel-auth is installed; without any seam, closure is refused rather
    # than performed with live tokens left behind.
    'SESSION_REVOKER': 'stapel_auth.sessions.services.SessionService.revoke_all',
}
```

Run `manage.py check` after wiring: a missing or stale data-owner inventory,
hash rows written outside `store_hashes`, and every open escape hatch are
reported there rather than discovered in an audit.

## Bus events

### Emits
| `user.deleted` | [schema](schemas/emits/user.deleted.json) | All user PII permanently deleted after grace period. Every package storing user  |
| `user.deletion_cancelled` | [schema](schemas/emits/user.deletion_cancelled.json) | Account closure cancelled during the grace period; every reversible reaction to `user.deletion_initiated` must be lifted. |
| `user.deletion_initiated` | [schema](schemas/emits/user.deletion_initiated.json) | Account closure started. 30-day grace period begins; account is deactivated. |
| `user.export_ready` | [schema](schemas/emits/user.export_ready.json) | Data export archive is ready for download. |
| `user.sessions_revoked` | [schema](schemas/emits/user.sessions_revoked.json) | Closure revoked every session and access JTI of the user. |
