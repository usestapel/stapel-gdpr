# stapel-gdpr

[![CI](https://github.com/usestapel/stapel-gdpr/actions/workflows/ci.yml/badge.svg)](https://github.com/usestapel/stapel-gdpr/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/usestapel/stapel-gdpr/graph/badge.svg)](https://codecov.io/gh/usestapel/stapel-gdpr)

> GDPR compliance — data export (Art. 15/20), account deletion with grace period (Art. 17), inactivity closure, retention cleanup

Part of the [Stapel framework](https://github.com/usestapel) — composable Django apps for building production-grade platforms.

## Installation

```bash
pip install stapel-gdpr
```

## Quick start

```python
# settings.py
INSTALLED_APPS = [
    ...
    'stapel_gdpr',
]
```

## Bus events

### Emits
| `user.deleted` | [schema](schemas/emits/user.deleted.json) | All user PII permanently deleted after grace period. Every package storing user  |
| `user.deletion_initiated` | [schema](schemas/emits/user.deletion_initiated.json) | Account closure started. 30-day grace period begins; account is deactivated. |
| `user.export_ready` | [schema](schemas/emits/user.export_ready.json) | Data export archive is ready for download. |

## License

MIT — see [LICENSE](LICENSE)
