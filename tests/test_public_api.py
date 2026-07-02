"""Public API surface: __all__, PEP 562 lazy exports, Django-free import."""
import os
import subprocess
import sys

import pytest


def test_all_contents():
    import stapel_gdpr

    assert stapel_gdpr.__all__ == [
        "LegalHold",
        "gdpr_orchestrator",
        "gdpr_settings",
        "is_reregistration",
        "store_hashes",
    ]
    # every exported name resolves and shows up in dir()
    for name in stapel_gdpr.__all__:
        assert name in dir(stapel_gdpr)


def test_lazy_attributes_resolve_under_django():
    import stapel_gdpr
    from stapel_gdpr.conf import gdpr_settings as conf_settings
    from stapel_gdpr.models import LegalHold as models_legal_hold
    from stapel_gdpr.orchestrator import gdpr_orchestrator as orch_singleton
    from stapel_gdpr.reregistration import is_reregistration, store_hashes

    assert stapel_gdpr.gdpr_settings is conf_settings
    assert stapel_gdpr.gdpr_orchestrator is orch_singleton
    assert stapel_gdpr.is_reregistration is is_reregistration
    assert stapel_gdpr.store_hashes is store_hashes
    assert stapel_gdpr.LegalHold is models_legal_hold


def test_unknown_attribute_raises():
    import stapel_gdpr

    with pytest.raises(AttributeError, match="no attribute 'nonsense'"):
        stapel_gdpr.nonsense


def test_import_is_django_free():
    """`import stapel_gdpr` must succeed without settings and without pulling Django."""
    code = (
        "import sys\n"
        "import stapel_gdpr\n"
        "django_mods = [m for m in sys.modules"
        " if m == 'django' or m.startswith('django.')]\n"
        "assert not django_mods, f'django imported eagerly: {django_mods}'\n"
        "assert stapel_gdpr.__all__\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
