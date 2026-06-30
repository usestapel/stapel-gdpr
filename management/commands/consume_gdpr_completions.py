"""
Bus consumer for GDPR completion events published by individual services.

Run one instance per GDPR service deployment:

    python manage.py consume_gdpr_completions
"""
from stapel_core.bus.consumer import BaseBusConsumerCommand
from stapel_core.bus.event import Event
from stapel_core.gdpr import GDPR_DELETE_COMPLETED, GDPR_EXPORT_COMPLETED


class Command(BaseBusConsumerCommand):
    help = 'Consume GDPR export/delete completion events from services'

    topics = [GDPR_EXPORT_COMPLETED, GDPR_DELETE_COMPLETED]
    consumer_group = 'gdpr-orchestrator'

    def handle_event(self, event: Event) -> None:
        if event.event_type == GDPR_EXPORT_COMPLETED:
            self._on_export_completed(event)
        elif event.event_type == GDPR_DELETE_COMPLETED:
            self._on_delete_completed(event)

    def _on_export_completed(self, event: Event) -> None:
        from stapel_gdpr.orchestrator import gdpr_orchestrator
        correlation_id = event.payload.get('correlation_id')
        service        = event.service
        bucket_path    = event.payload.get('bucket_path', '')

        if not correlation_id or not service:
            self.stderr.write(f'Malformed gdpr.export.completed event: {event.event_id}')
            return

        gdpr_orchestrator.mark_part_ready(correlation_id, service, bucket_path)

    def _on_delete_completed(self, event: Event) -> None:
        # Deletion is fire-and-forget — just log. Closure status is updated by sweep task
        # once all services have confirmed (or deadline passes).
        user_id = event.payload.get('user_id')
        correlation_id = event.payload.get('correlation_id')
        self.stdout.write(
            f'GDPR delete completed: service={event.service} user={user_id} '
            f'correlation={correlation_id}'
        )
