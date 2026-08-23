"""Opening an erasure from ANOTHER service: the Action, the Function, the seam.

Until 0.5.1 the only way in was ``gdpr_orchestrator.request_erasure`` — an
in-process call. The owner that detects the need (recordings' retention purge,
a host's delete view in a different container) has no import path to it, and
the gap showed up as owners quietly deleting outside the receipts ledger.

What these tests pin is the part that is easy to get wrong: at-least-once
delivery means the SAME ask arrives twice, and an intake that mints a second
erasure per redelivery produces subjects with two open requests, two sets of
receipt slots, and one of them permanently unfinishable.
"""
import pytest

from stapel_core.comm import call, emit

from stapel_gdpr import client
from stapel_gdpr.functions import ERASURE_OPEN, ERASURE_REQUEST
from stapel_gdpr.models import ErasureRequest
from tests.support import gdpr_conf

FLEET_OWNERS = {
    "recordings": ["account", "workspace", "meeting", "recording"],
    "media": ["account", "workspace", "file", "recording"],
}


@pytest.fixture
def fleet(settings):
    settings.STAPEL_GDPR = gdpr_conf(
        DATA_OWNERS=FLEET_OWNERS, DATA_OWNERS_VERSION="intake-1",
    )
    return FLEET_OWNERS


# ---------------------------------------------------------------------------
# gdpr.erasure.open — the fire-and-forget door
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestErasureOpenAction:
    def test_an_action_opens_the_same_request_the_api_opens(self, fleet):
        emit(ERASURE_OPEN, {
            "subject_type": "recording",
            "subject_key": "rec-1",
            "workspace_id": "ws-9",
        })

        erasure = ErasureRequest.objects.get(subject_type="recording", subject_key="rec-1")
        assert erasure.state == ErasureRequest.STATE_ERASING
        assert erasure.workspace_id == "ws-9"
        # One receipt slot per owner claiming `recording` — the whole point of
        # routing this through the orchestrator instead of a bespoke path.
        assert set(erasure.parts.values_list("owner", flat=True)) == {"recordings", "media"}

    def test_the_owners_are_told_about_it(self, fleet):
        from stapel_core.comm import subscribe_action

        seen = []
        subscribe_action("gdpr.erasure.requested", lambda e: seen.append(e.payload))

        emit(ERASURE_OPEN, {"subject_type": "recording", "subject_key": "rec-2"})

        assert seen and seen[-1]["subject_key"] == "rec-2"

    def test_the_same_idempotency_key_is_the_same_request(self, fleet):
        """The redelivery case: one subject, one erasure, one set of slots."""
        payload = {
            "subject_type": "recording",
            "subject_key": "rec-3",
            "idempotency_key": "purge:recording:rec-3",
        }
        emit(ERASURE_OPEN, payload)
        emit(ERASURE_OPEN, payload)
        emit(ERASURE_OPEN, payload)

        rows = ErasureRequest.objects.filter(subject_key="rec-3")
        assert rows.count() == 1
        assert rows.get().parts.count() == 2

    def test_a_redelivery_does_not_re_announce_the_request(self, fleet):
        """A second announcement would restart every owner's deadline clock."""
        from stapel_core.comm import subscribe_action

        seen = []
        subscribe_action("gdpr.erasure.requested", lambda e: seen.append(e.payload))

        payload = {
            "subject_type": "recording",
            "subject_key": "rec-4",
            "idempotency_key": "purge:recording:rec-4",
        }
        emit(ERASURE_OPEN, payload)
        emit(ERASURE_OPEN, payload)

        assert len([e for e in seen if e["subject_key"] == "rec-4"]) == 1

    def test_without_a_key_two_asks_are_two_erasures(self, fleet):
        """Opting out is opting out — and the docs say so, loudly."""
        payload = {"subject_type": "recording", "subject_key": "rec-5"}
        emit(ERASURE_OPEN, payload)
        emit(ERASURE_OPEN, payload)
        assert ErasureRequest.objects.filter(subject_key="rec-5").count() == 2

    def test_different_keys_for_one_subject_stay_different_requests(self, fleet):
        """The key is the caller's decision, not the subject's identity."""
        for key in ("a", "b"):
            emit(ERASURE_OPEN, {
                "subject_type": "recording",
                "subject_key": "rec-6",
                "idempotency_key": key,
            })
        assert ErasureRequest.objects.filter(subject_key="rec-6").count() == 2

    def test_an_unknown_subject_type_is_dropped_not_retried(self, fleet, caplog):
        """A typo cannot be fixed by redelivering it."""
        emit(ERASURE_OPEN, {"subject_type": "spaceship", "subject_key": "x-1"})
        assert not ErasureRequest.objects.filter(subject_key="x-1").exists()
        assert "Refused gdpr.erasure.open" in caplog.text

    def test_a_payload_without_a_subject_is_dropped(self, fleet, caplog):
        emit(ERASURE_OPEN, {"subject_type": "recording", "subject_key": ""})
        assert not ErasureRequest.objects.exists()
        assert "Refused gdpr.erasure.open" in caplog.text


# ---------------------------------------------------------------------------
# gdpr.erasure.request — the synchronous door
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestErasureRequestFunction:
    def test_the_function_answers_the_request_identity(self, fleet):
        result = call(ERASURE_REQUEST, {
            "subject_type": "recording",
            "subject_key": "rec-10",
            "workspace_id": "ws-1",
        })

        assert set(result) == {"request_id", "due_at", "state"}
        erasure = ErasureRequest.objects.get(pk=result["request_id"])
        assert erasure.subject_key == "rec-10"
        assert result["state"] == ErasureRequest.STATE_ERASING
        assert result["due_at"] == erasure.due_at.isoformat()

    def test_the_same_key_answers_the_same_request_id(self, fleet):
        payload = {
            "subject_type": "recording",
            "subject_key": "rec-11",
            "idempotency_key": "purge:recording:rec-11",
        }
        first = call(ERASURE_REQUEST, payload)
        second = call(ERASURE_REQUEST, payload)

        assert first["request_id"] == second["request_id"]
        assert ErasureRequest.objects.filter(subject_key="rec-11").count() == 1

    def test_origin_and_requester_ride_along(self, fleet, user):
        result = call(ERASURE_REQUEST, {
            "subject_type": "account",
            "subject_key": str(user.pk),
            "requested_by": str(user.pk),
            "origin": "dsar",
        })
        erasure = ErasureRequest.objects.get(pk=result["request_id"])
        assert erasure.origin == ErasureRequest.ORIGIN_DSAR
        assert str(erasure.requested_by) == str(user.pk)

    def test_an_unknown_subject_type_reaches_the_caller(self, fleet):
        """The Function's caller CAN fix a typo, so it is told about one."""
        from stapel_core.comm import FunctionCallError

        with pytest.raises((FunctionCallError, ValueError)):
            call(ERASURE_REQUEST, {"subject_type": "spaceship", "subject_key": "x-2"})
        assert not ErasureRequest.objects.exists()

    def test_the_function_is_registered_under_its_published_name(self):
        from stapel_core.comm import function_registry

        assert function_registry.get(ERASURE_REQUEST) is not None


# ---------------------------------------------------------------------------
# stapel_gdpr.client — one dotted path, both deployments
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestClientHelper:
    def test_in_process_it_uses_the_orchestrator_directly(self, fleet, monkeypatch):
        """Default transport: no Function call is made at all."""
        called = []
        # The name the client binds: `from stapel_core.comm import call`
        # resolves the attribute on the package, so this is the one that
        # would be reached if the routing decision went the other way.
        monkeypatch.setattr(
            "stapel_core.comm.call", lambda *a, **kw: called.append(a),
        )
        result = client.request_erasure("recording", "rec-20")

        assert not called
        assert ErasureRequest.objects.get(pk=result["request_id"]).subject_key == "rec-20"

    def test_with_a_transport_configured_it_calls_the_function(self, fleet, settings):
        """Bus deployment: the same three values, over the wire.

        The transport is faked with a dotted path — exactly the seam
        STAPEL_COMM offers for a custom RPC — so the routing decision is
        exercised without standing up NATS.
        """
        settings.STAPEL_COMM = {
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "FUNCTION_TRANSPORT": "tests.test_erasure_intake.fake_transport",
        }
        _transport_calls.clear()

        result = client.request_erasure(
            "recording", "rec-21", idempotency_key="purge:recording:rec-21",
        )

        assert _transport_calls, "the client did not go through the transport"
        name, payload = _transport_calls[-1]
        assert name == ERASURE_REQUEST
        assert payload["subject_key"] == "rec-21"
        assert payload["idempotency_key"] == "purge:recording:rec-21"
        assert set(result) == {"request_id", "due_at", "state"}

    def test_both_paths_answer_the_same_shape(self, fleet, settings):
        local = client.request_erasure("recording", "rec-22")
        settings.STAPEL_COMM = {
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "FUNCTION_TRANSPORT": "tests.test_erasure_intake.fake_transport",
        }
        remote = client.request_erasure("recording", "rec-23")
        assert set(local) == set(remote)


@pytest.mark.django_db
class TestCommErasureClientSeam:
    """The object an owner library's ``ERASURE_CLIENT`` setting names."""

    def test_it_satisfies_the_owner_seam_by_shape(self):
        instance = client.CommErasureClient()
        for method in ("available", "has_open_erasure", "request_erasure"):
            assert callable(getattr(instance, method))

    def test_available_in_process_when_the_module_is_installed(self, fleet):
        assert client.CommErasureClient().available() is True

    def test_it_opens_an_erasure_and_returns_its_id(self, fleet):
        seam = client.CommErasureClient()
        request_id = seam.request_erasure("recording", "rec-30", workspace_id="ws-2")

        erasure = ErasureRequest.objects.get(pk=int(request_id))
        assert erasure.subject_key == "rec-30"
        assert erasure.workspace_id == "ws-2"
        assert erasure.idempotency_key == "owner:recording:rec-30"

    def test_asking_again_tomorrow_does_not_mint_a_second_erasure(self, fleet):
        """The daily-sweep case, which is how duplicates get made."""
        seam = client.CommErasureClient()
        first = seam.request_erasure("recording", "rec-31")
        second = seam.request_erasure("recording", "rec-31")

        assert first == second
        assert ErasureRequest.objects.filter(subject_key="rec-31").count() == 1

    def test_has_open_erasure_reads_the_ledger_in_process(self, fleet):
        seam = client.CommErasureClient()
        assert seam.has_open_erasure("recording", "rec-32") is False
        seam.request_erasure("recording", "rec-32")
        assert seam.has_open_erasure("recording", "rec-32") is True

    def test_a_completed_erasure_is_not_open_any_more(self, fleet):
        seam = client.CommErasureClient()
        seam.request_erasure("recording", "rec-33")
        ErasureRequest.objects.filter(subject_key="rec-33").update(
            state=ErasureRequest.STATE_DELETED,
        )
        assert seam.has_open_erasure("recording", "rec-33") is False

    def test_over_a_transport_it_defers_to_idempotency(self, fleet, settings):
        """No read Function exists, so the answer is False and the KEY is what
        keeps a re-asking sweep from opening a second erasure."""
        settings.STAPEL_COMM = {
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "FUNCTION_TRANSPORT": "tests.test_erasure_intake.fake_transport",
        }
        seam = client.CommErasureClient()
        assert seam.available() is True
        assert seam.has_open_erasure("recording", "rec-34") is False

        first = seam.request_erasure("recording", "rec-34")
        assert seam.has_open_erasure("recording", "rec-34") is False
        second = seam.request_erasure("recording", "rec-34")

        assert first == second
        assert ErasureRequest.objects.filter(subject_key="rec-34").count() == 1


# ---------------------------------------------------------------------------
# A stand-in for NATS/HTTP: the STAPEL_COMM dotted-path transport seam.
# It ends up back in this process (there is only one), which is exactly what
# makes it a fake — what it proves is that the CLIENT routed through a
# transport instead of reaching for the orchestrator directly.
# ---------------------------------------------------------------------------

_transport_calls: list[tuple] = []


def fake_transport(name: str, payload: dict, *, timeout=None):
    _transport_calls.append((name, payload))
    from stapel_core.comm import function_registry

    return function_registry.get(name)(payload)
