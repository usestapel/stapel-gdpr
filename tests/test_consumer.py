"""consume_gdpr_completions management command tests (MemoryBus, no broker)."""
from io import StringIO

import pytest
from django.core.management import call_command

from stapel_core.bus.event import Event
from stapel_core.gdpr import GDPR_DELETE_COMPLETED, GDPR_EXPORT_COMPLETED

from stapel_gdpr.management.commands.consume_gdpr_completions import Command
from stapel_gdpr.models import DataExportRequest
from stapel_gdpr.orchestrator import gdpr_orchestrator


def _command():
    out, err = StringIO(), StringIO()
    return Command(stdout=out, stderr=err), out, err


@pytest.mark.django_db
class TestConsumeGdprCompletions:
    def test_consumer_loop_marks_part_ready(self, settings, user):
        """Full path: publish completion on the bus, run the command, part flips."""
        from stapel_core.bus import get_bus

        settings.GDPR_COLLECTING_SERVICES = ["auth"]
        req = gdpr_orchestrator.request_export(user.pk)

        get_bus().publish(GDPR_EXPORT_COMPLETED, Event(
            event_type=GDPR_EXPORT_COMPLETED,
            service="auth",
            payload={"correlation_id": req.correlation_id, "bucket_path": ""},
        ))

        out = StringIO()
        call_command("consume_gdpr_completions", stdout=out)

        assert "Starting consumer group=gdpr-orchestrator" in out.getvalue()
        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY
        assert req.parts.get(service="auth").status == "done"

    def test_export_completed_handler_direct(self, settings, user):
        settings.GDPR_COLLECTING_SERVICES = ["cdn"]
        req = gdpr_orchestrator.request_export(user.pk)

        cmd, _, _ = _command()
        cmd.handle_event(Event(
            event_type=GDPR_EXPORT_COMPLETED,
            service="cdn",
            payload={"correlation_id": req.correlation_id, "bucket_path": ""},
        ))

        req.refresh_from_db()
        assert req.status == DataExportRequest.STATUS_READY

    def test_malformed_export_event_written_to_stderr(self, db):
        cmd, out, err = _command()
        cmd.handle_event(Event(
            event_type=GDPR_EXPORT_COMPLETED,
            service="auth",
            payload={},  # no correlation_id
        ))
        assert "Malformed gdpr.export.completed" in err.getvalue()
        cmd.handle_event(Event(
            event_type=GDPR_EXPORT_COMPLETED,
            service="",  # no service
            payload={"correlation_id": "abc"},
        ))

    def test_delete_completed_is_logged(self):
        cmd, out, err = _command()
        cmd.handle_event(Event(
            event_type=GDPR_DELETE_COMPLETED,
            service="profiles",
            payload={"user_id": "u-1", "correlation_id": "c-1"},
        ))
        text = out.getvalue()
        assert "GDPR delete completed" in text
        assert "service=profiles" in text
        assert "user=u-1" in text
        assert "correlation=c-1" in text

    def test_unknown_event_type_ignored(self):
        cmd, out, err = _command()
        cmd.handle_event(Event(event_type="something.else", service="x", payload={}))
        assert out.getvalue() == ""
        assert err.getvalue() == ""

    def test_topics_and_group(self):
        assert Command.topics == [GDPR_EXPORT_COMPLETED, GDPR_DELETE_COMPLETED]
        assert Command.consumer_group == "gdpr-orchestrator"
