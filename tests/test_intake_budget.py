"""A budget on the doors a stranger can knock on — audit 2026-09-11, L-6.

`POST /gdpr/api/v1/dsar` is `AllowAny` because a regulator expects a public
privacy form to exist, and it cannot require a login. `@captcha_protected` is
a no-op when no captcha backend is configured, which is the state of most
deployments, so the intake was an unauthenticated mail trigger: anyone could
make the instance send an acknowledgement to any address and grow the
`DsarRequest` table, at request speed, for as long as they liked.

The two authenticated self-service doors below it — closing an account and
asking for an export — are cheaper to hold open but not free: each one starts
a job and each one sends mail.
"""
import pytest

from tests.support import gdpr_conf

DSAR = "/gdpr/api/v1/dsar"


@pytest.fixture(autouse=True)
def wiring(settings):
    settings.STAPEL_GDPR = gdpr_conf(
        DATA_OWNERS=["fake"],
        DSAR_STAFF_EMAILS=["privacy@example.com"],
    )


@pytest.fixture(autouse=True)
def _empty_budget_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def _submit(client, email="subject@example.com"):
    return client.post(
        DSAR, {"kind": "access", "email": email}, format="json",
    )


@pytest.mark.django_db
class TestTheIntakeBudget:

    def test_the_eleventh_request_from_one_address_in_an_hour_is_429(
        self, api_client,
    ):
        for n in range(10):
            assert _submit(api_client).status_code == 201, n
        refused = _submit(api_client)
        assert refused.status_code == 429
        assert refused.data["localizable_error"] == "error.429.rate_limit"

    def test_a_refused_request_records_nothing_and_mails_nobody(
        self, api_client, mailoutbox,
    ):
        from stapel_gdpr.models import DsarRequest

        for _ in range(10):
            _submit(api_client)
        rows = DsarRequest.objects.count()
        sent = len(mailoutbox)
        assert _submit(api_client).status_code == 429
        assert DsarRequest.objects.count() == rows
        assert len(mailoutbox) == sent

    def test_another_address_has_its_own_budget(self, api_client, settings):
        for _ in range(10):
            _submit(api_client)
        assert _submit(api_client).status_code == 429
        # A different client address: a full budget of its own, so one
        # abuser cannot close the statutory form for everybody.
        other = _submit(api_client)
        assert other.status_code == 429  # same address, still refused
        api_client.defaults["REMOTE_ADDR"] = "203.0.113.9"
        assert _submit(api_client).status_code == 201

    def test_zero_disables_the_budget(self, api_client, settings):
        settings.STAPEL_GDPR = gdpr_conf(
            DATA_OWNERS=["fake"],
            DSAR_STAFF_EMAILS=["privacy@example.com"],
            INTAKE_RATE_LIMIT_PER_HOUR=0,
        )
        for n in range(12):
            assert _submit(api_client).status_code == 201, n


@pytest.mark.django_db
class TestTheSelfServiceDoorsShareTheMechanism:

    def test_account_closure_is_budgeted(self, authed_client):
        seen = set()
        for _ in range(12):
            seen.add(
                authed_client.post("/gdpr/api/v1/user/account/close").status_code
            )
        assert 429 in seen

    def test_data_export_request_is_budgeted(self, authed_client):
        seen = set()
        for _ in range(12):
            seen.add(
                authed_client.post(
                    "/gdpr/api/v1/user/data-export/request"
                ).status_code
            )
        assert 429 in seen
