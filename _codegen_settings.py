"""Single-module Django settings for stapel-gdpr's contract-emission harness.

The ``settings.configure(...)`` block behind ``_codegen.py`` / ``make
contract``: a ``{gdpr + core}`` instance mounted on the CANONICAL public
prefix (``stapel_gdpr.codegen_urls`` -> ``gdpr/api/``; the module's own
``urls.py`` contributes the mandatory ``v1/`` segment, so the full prefix is
``/gdpr/api/v1``), with drf-spectacular installed and the production
``REST_FRAMEWORK`` block, so the emitted ``schema.json`` is what a real
deployment serves rather than what a test rig happens to produce.

``contract=False`` returns the bare-mount variant. ``conftest.py`` still
carries its own copy of the test configuration (it needs the celery-eager
bootstrap and the fixtures around it, and rewiring the suite is not a
contract change); this file is the harness's source of truth, and the two
are kept honest by ``tests/test_contract.py`` asserting the emitted paths and
security entries the suite exercises.

``SPECTACULAR_SETTINGS`` is deliberately not set: drf-spectacular freezes its
settings singleton at import time, before a ``configure()``-based harness can
populate it, so the emitter runs on drf defaults — the state every other
pair-backend's harness emits under. The one knob that must still be forced,
``SCHEMA_PATH_PREFIX``, is patched on the singleton directly by the harness.
"""
from __future__ import annotations


def settings_kwargs(
    *,
    root_urlconf: str = "tests.urls",
    contract: bool = False,
) -> dict:
    """The ``settings.configure(**kwargs)`` for a single-module gdpr instance."""
    if contract:
        # Mirror stapel_core.django.settings.REST_FRAMEWORK exactly (the
        # config a real deployment emits under). Inlined, not imported, to
        # dodge the import-time settings read.
        rest_framework = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
                "rest_framework.renderers.BrowsableAPIRenderer",
            ],
            "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
            "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
        }
    else:
        # Not "no dict": DRF's defaults everywhere EXCEPT the one key any
        # settings module that writes REST_FRAMEWORK must carry. Absent, DRF
        # falls back to its own exception handler and every refusal no view
        # code raises — 401/403 from authenticators and permission classes,
        # 404 from get_object_or_404, 405/406/415 from dispatch, 429 from a
        # throttle — answers a bare {"detail": ...} instead of the fleet
        # envelope (stapel_core.error_envelope.W001). Read off core's own
        # preset rather than re-typed; importing it reads no settings, so the
        # hazard the contract branch inlines against does not apply.
        from stapel_core.testing import BASE_REST_FRAMEWORK

        rest_framework = {
            "EXCEPTION_HANDLER": BASE_REST_FRAMEWORK["EXCEPTION_HANDLER"],
        }

    kwargs = dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.messages",
            # so stapel_gdpr.admin is importable
            "django.contrib.admin",
            # CommonDjangoConfig ships the stapel_core management commands
            # (generate_error_keys / generate_flow_docs, which emit two thirds
            # of the triad).
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            "rest_framework",
            "drf_spectacular",
            "stapel_gdpr",
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        # This module's paths carry no trailing slash; the deployment setting
        # travels with the mount, not with the schema, but keeping it here
        # means the harness resolves the same URLs the suite does.
        APPEND_SLASH=False,
        ROOT_URLCONF=root_urlconf,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        MEDIA_ROOT="/tmp/stapel_gdpr_codegen_media",
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        STAPEL_COMM={
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
        },
        MIDDLEWARE=[
            "django.middleware.common.CommonMiddleware",
            "stapel_core.django.jwt.middleware.ServiceAPIKeyMiddleware",
        ],
        SERVICE_API_KEY="codegen-service-key",
        FRONTEND_URL="https://app.example.com",
        # Skip migrations — create tables directly from models.
        MIGRATION_MODULES={
            "users": None,
            "gdpr": None,
        },
    )
    if rest_framework is not None:
        kwargs["REST_FRAMEWORK"] = rest_framework
    return kwargs


# The multi-module common path prefix drf-spectacular auto-detects when every
# pair-backend's schema is emitted inside an all-modules aggregate. Forced on
# the singleton by the harness so a single-module instance derives the same
# operationIds. Uniform across all pair-backends.
CODEGEN_SCHEMA_PATH_PREFIX = "/"
