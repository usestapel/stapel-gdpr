"""Stapel GDPR — data export (Art. 15/20) and account deletion (Art. 17) for Django.

Public API (all attributes are lazily imported via PEP 562, so importing
``stapel_gdpr`` itself never touches Django):

- ``gdpr_settings``     — the ``STAPEL_GDPR`` settings namespace (:mod:`stapel_gdpr.conf`).
- ``gdpr_orchestrator`` — the :class:`~stapel_gdpr.orchestrator.GDPROrchestrator`
  singleton coordinating export and deletion across providers/services.
- ``is_reregistration`` — signup-time check: does an email/phone belong to a
  previously deleted account? (:mod:`stapel_gdpr.reregistration`)
- ``store_hashes``      — persist salted identifier hashes before erasure
  (:mod:`stapel_gdpr.reregistration`).
- ``LegalHold``         — Django model blocking closure/deletion while data must
  be preserved (GDPR Art. 17(3)). Requires configured Django settings with
  ``stapel_gdpr`` in ``INSTALLED_APPS``::

      from stapel_gdpr import LegalHold
      LegalHold.objects.create(user_id=user.pk, reason="litigation", created_by="legal")
"""

__all__ = [
    "LegalHold",
    "gdpr_orchestrator",
    "gdpr_settings",
    "is_reregistration",
    "store_hashes",
]

# name -> (module, attribute); resolved on first access so that plain
# `import stapel_gdpr` stays free of Django (and Django-settings) imports.
_LAZY_EXPORTS = {
    "gdpr_settings": ("stapel_gdpr.conf", "gdpr_settings"),
    "gdpr_orchestrator": ("stapel_gdpr.orchestrator", "gdpr_orchestrator"),
    "is_reregistration": ("stapel_gdpr.reregistration", "is_reregistration"),
    "store_hashes": ("stapel_gdpr.reregistration", "store_hashes"),
    "LegalHold": ("stapel_gdpr.models", "LegalHold"),
}


def __getattr__(name):  # PEP 562
    try:
        module_path, attr = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    from importlib import import_module

    value = getattr(import_module(module_path), attr)
    globals()[name] = value  # cache so subsequent lookups skip __getattr__
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
