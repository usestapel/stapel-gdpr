# stapel-gdpr

[![CI](https://img.shields.io/github/actions/workflow/status/usestapel/stapel-gdpr/ci.yml?branch=main&logo=github&label=CI)](https://github.com/usestapel/stapel-gdpr/actions/workflows/ci.yml?query=branch%3Amain)
[![coverage](https://img.shields.io/codecov/c/github/usestapel/stapel-gdpr?branch=main&logo=codecov&label=coverage)](https://app.codecov.io/gh/usestapel/stapel-gdpr)
[![pypi](https://img.shields.io/pypi/v/stapel-gdpr?logo=pypi&logoColor=white&label=pypi)](https://pypi.org/project/stapel-gdpr/)
[![downloads](https://static.pepy.tech/badge/stapel-gdpr/month)](https://pepy.tech/project/stapel-gdpr)
[![python](https://img.shields.io/pypi/pyversions/stapel-gdpr?logo=python&logoColor=white)](https://pypi.org/project/stapel-gdpr/)
[![license](https://img.shields.io/github/license/usestapel/stapel-gdpr)](https://github.com/usestapel/stapel-gdpr/blob/main/LICENSE)

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
