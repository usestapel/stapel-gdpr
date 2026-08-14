import tempfile
import uuid


def pytest_configure(config):
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret-key-not-for-production",
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.sessions",
                "django.contrib.messages",
                "django.contrib.admin",  # so stapel_gdpr.admin is importable/testable
                # CommonDjangoConfig ships the stapel_core management commands
                # (generate_error_keys, used by the docs/errors.json drift gate).
                "stapel_core.django.apps.CommonDjangoConfig",
                "stapel_core.django.users",
                "rest_framework",
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
            APPEND_SLASH=False,
            ROOT_URLCONF="tests.urls",
            MEDIA_ROOT=tempfile.mkdtemp(prefix="gdpr-tests-"),
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                }
            },
            # In-memory bus — no Kafka/Redis broker needed
            STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
            # Synchronous in-process comm — no outbox tables / relay needed
            STAPEL_COMM={
                "OUTBOX_ENABLED": False,
                "ACTION_TRANSPORT": "inprocess",
            },
            MIDDLEWARE=[
                "django.middleware.common.CommonMiddleware",
                "stapel_core.django.jwt.middleware.ServiceAPIKeyMiddleware",
            ],
            SERVICE_API_KEY="test-service-key",
            FRONTEND_URL="https://app.example.com",
            # A deployment that wired the seams the library fails closed on;
            # tests that want the unwired behavior override this explicitly.
            STAPEL_GDPR={"SESSION_REVOKER": "tests.support.record_revocation"},
            # Skip migrations — create tables directly from models
            MIGRATION_MODULES={
                "users": None,
                "gdpr": None,
            },
        )
        import django

        django.setup()

    # Run celery tasks inline (.delay executes synchronously)
    from celery import Celery

    celery_app = Celery("stapel-gdpr-tests")
    celery_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
        broker_url="memory://",
        result_backend="cache+memory://",
    )
    celery_app.set_default()


import pytest  # noqa: E402


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    u = User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password="testpass-1234",
    )
    return u


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def authed_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def fake_provider(settings):
    """Register an in-process GDPR provider for the duration of a test.

    Declares it as a data owner too: a provider the registry does not know
    about now blocks erasure completeness on purpose, and a fixture that
    skipped the declaration would be testing a misconfigured deployment.
    """
    from stapel_core.gdpr import GDPRProvider, gdpr_registry

    from tests.support import gdpr_conf

    settings.STAPEL_GDPR = gdpr_conf(
        DATA_OWNERS=["fake"], DATA_OWNERS_VERSION="test-registry-1",
    )

    class FakeProvider(GDPRProvider):
        section = "fake"

        def __init__(self):
            self.exported = []
            self.deleted = []
            self.anonymized = []
            self.fail_delete = False

        def export(self, user_id):
            self.exported.append(str(user_id))
            return {"user_id": str(user_id), "items": [1, 2, 3]}

        def delete(self, user_id):
            if self.fail_delete:
                raise RuntimeError("boom")
            self.deleted.append(str(user_id))

        def anonymize(self, user_id):
            self.anonymized.append(str(user_id))

    provider = FakeProvider()
    gdpr_registry.register(provider)
    yield provider
    gdpr_registry._providers.remove(provider)
