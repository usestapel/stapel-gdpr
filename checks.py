"""Boot-time checks — the misconfiguration has to be loud at deploy, not at audit.

Every finding this module guards against had the same shape: a mechanism
existed, no consumer wired it, and nothing said so until someone went looking
years later. A ``manage.py check`` that fails at boot is the cheapest place
for that sentence to appear.

* ``gdpr.E001`` — no data-owner inventory. Erasure cannot be certified.
* ``gdpr.E002`` — declared owners that nothing can reach, or providers nobody
  declared. The inventory is wrong, so the receipts it produces mean nothing.
* ``gdpr.W003`` — an escape hatch is open. Not an error (someone chose it),
  but it must never be silent.
* ``gdpr.E004`` — re-registration hashes written outside ``store_hashes``:
  an unverified, probably unsalted digest of an email or a phone number.
* ``gdpr.W005`` — no session-revocation seam resolves, so closure will fail.
* ``gdpr.E006`` — the primary-identity erasure strategy cannot run, so the
  users.User row would survive every erasure.
"""
from __future__ import annotations

from django.core.checks import Error, Warning as CheckWarning, register

from .conf import gdpr_settings
from .owners import data_owner_report

__all__ = ["check_data_owner_registry", "check_reregistration_hashes", "register_checks"]

_HATCHES = (
    (
        "ALLOW_ERASURE_WITHOUT_RECEIPTS",
        "closures can be marked DELETED without a receipt from every declared "
        "data owner — an erasure this deployment cannot prove happened",
    ),
    (
        "ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION",
        "account closure proceeds without revoking sessions — every access "
        "token minted before the closure keeps working until it expires",
    ),
)


def check_data_owner_registry(app_configs=None, **kwargs):
    report = data_owner_report()
    problems = []

    if report.unconfigured:
        problems.append(
            Error(
                'STAPEL_GDPR["DATA_OWNERS"] is empty: this deployment has no '
                "inventory of the stores holding personal data.",
                hint=(
                    "List every store (auth, profiles, recordings, billing, "
                    "CDN, search indexes, provider copies...) in "
                    'STAPEL_GDPR["DATA_OWNERS"] and stamp '
                    'STAPEL_GDPR["DATA_OWNERS_VERSION"]. Until then no '
                    "account closure can reach the DELETED status and every "
                    "export is reported to the user as partial."
                ),
                id="gdpr.E001",
            )
        )
    else:
        if report.missing:
            problems.append(
                Error(
                    "Declared GDPR data owners have no registered provider: "
                    f"{', '.join(report.missing)}.",
                    hint=(
                        "Add the provider to GDPR_PROVIDERS, or declare the "
                        'owner as {"name": ..., "kind": "remote"} if it '
                        "confirms erasure over comm."
                    ),
                    id="gdpr.E002",
                )
            )
        if report.undeclared:
            problems.append(
                Error(
                    "GDPR providers are registered but missing from "
                    f'STAPEL_GDPR["DATA_OWNERS"]: {", ".join(report.undeclared)}.',
                    hint=(
                        "An inventory that does not list a store already "
                        "wired in is stale; add the names or drop the "
                        "providers."
                    ),
                    id="gdpr.E002",
                )
            )
        if not report.version:
            problems.append(
                CheckWarning(
                    'STAPEL_GDPR["DATA_OWNERS_VERSION"] is unset, so closures '
                    "cannot record which inventory certified them.",
                    hint='Set a version string and bump it whenever DATA_OWNERS changes.',
                    id="gdpr.W003",
                )
            )

    for name, consequence in _HATCHES:
        if getattr(gdpr_settings, name):
            problems.append(
                CheckWarning(
                    f'STAPEL_GDPR["{name}"] is on: {consequence}.',
                    hint="Remove the setting once the underlying gap is closed.",
                    id="gdpr.W003",
                )
            )

    from .lifecycle import IDENTITY_ANONYMIZE, IDENTITY_DELETE, _resolve_revoker

    identity_strategy = str(gdpr_settings.PRIMARY_IDENTITY_ERASURE or IDENTITY_ANONYMIZE)
    if identity_strategy not in (IDENTITY_ANONYMIZE, IDENTITY_DELETE):
        from django.utils.module_loading import import_string

        try:
            import_string(identity_strategy)
        except ImportError as e:
            problems.append(
                Error(
                    'STAPEL_GDPR["PRIMARY_IDENTITY_ERASURE"] is neither '
                    f'"{IDENTITY_ANONYMIZE}"/"{IDENTITY_DELETE}" nor an '
                    f"importable erase(user) callable: {e}",
                    hint=(
                        "Every closure would stop short of erasing the "
                        "users.User row and no account would reach DELETED."
                    ),
                    id="gdpr.E006",
                )
            )

    try:
        _, strategy = _resolve_revoker()
    except Exception as e:  # a bad dotted path must not crash `manage.py check`
        problems.append(
            Error(
                f'STAPEL_GDPR["SESSION_REVOKER"] cannot be imported: {e}',
                id="gdpr.E002",
            )
        )
    else:
        if strategy == "none" and not gdpr_settings.ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION:
            problems.append(
                CheckWarning(
                    "No session-revocation seam resolves, so every account "
                    "closure will fail with SessionRevocationUnavailable.",
                    hint=(
                        'Set STAPEL_GDPR["SESSION_REVOKER"] to a '
                        "revoke(user) callable, install stapel-auth, or "
                        "subscribe to user.sessions_revoked."
                    ),
                    id="gdpr.W005",
                )
            )

    return problems


def check_reregistration_hashes(app_configs=None, **kwargs):
    """Report rows written outside ``store_hashes`` (unknown digest format)."""
    from django.db import DatabaseError

    from .models import ReRegistrationHash

    try:
        count = ReRegistrationHash.objects.exclude(
            scheme__in=[
                ReRegistrationHash.SCHEME_HMAC_V1,
                # Pre-0003 rows are grandfathered: they age out with
                # retention and cannot be attributed to a writer any more.
                ReRegistrationHash.SCHEME_LEGACY,
            ],
        ).count()
    except DatabaseError:
        # Unmigrated or unreachable database: the deployment check is not the
        # place to fail for that.
        return []

    if not count:
        return []
    return [
        Error(
            f"{count} ReRegistrationHash rows were written outside "
            "stapel_gdpr.reregistration.store_hashes and carry an unverified "
            "digest format.",
            hint=(
                "An unsalted SHA-256 of a normalized email or phone number is "
                "recoverable from a dictionary. Point every writer at "
                "stapel_gdpr.reregistration.store_hashes and clear the old "
                "rows with `manage.py gdpr_purge_unverified_hashes`."
            ),
            id="gdpr.E004",
        )
    ]


def register_checks() -> None:
    """Called from ``AppConfig.ready``; idempotent."""
    register(check_data_owner_registry, "gdpr")
    register(check_reregistration_hashes, "gdpr", "database")
