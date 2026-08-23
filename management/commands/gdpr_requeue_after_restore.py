"""Re-arm the erasure clock for data a backup restore brought back.

A restore is the one operation that silently undoes a completed erasure: the
rows come back, the owners have long since confirmed, and the request says
DELETED. Nothing in the system notices, because nothing is watching the
database for resurrection — and it must not be, since that watcher would be
a second mechanism with its own failure modes.

So the runbook is one command, and it is the deploy README's one line:
*after any restore, run this with the backup's timestamp*. Every erasure
that completed inside the restored window (plus a day of slack, because a
backup's own clock and ours are never exactly the same) is cloned as
``origin="restore_requeue"`` and re-dispatched to its owners.

Idempotent by construction rather than by a flag file: the clone carries a
FK to the request it re-runs and ``(origin, source_request)`` is unique, so
a second run for the same — or an overlapping — window re-derives the same
pairs and writes nothing.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from datetime import timedelta

from stapel_gdpr.models import ErasureRequest
from stapel_gdpr.orchestrator import gdpr_orchestrator

#: How far before the restore point to look. A backup's timestamp is the
#: moment the snapshot started, not the moment every table in it was
#: consistent, so an erasure that completed just before it may still be in
#: the restored data.
SLACK = timedelta(days=1)


class Command(BaseCommand):
    help = (
        "Re-queue erasures whose data a backup restore may have brought back. "
        "Idempotent: safe to re-run for the same or an overlapping window."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--restored-from",
            required=True,
            help="ISO datetime of the backup that was restored (e.g. 2026-08-01T03:00:00Z).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be re-queued and write nothing.",
        )

    def handle(self, *args, **options):
        raw = options["restored_from"]
        restored_from = parse_datetime(raw)
        if restored_from is None:
            raise CommandError(
                f"--restored-from {raw!r} is not an ISO datetime "
                "(e.g. 2026-08-01T03:00:00Z)"
            )
        if timezone.is_naive(restored_from):
            restored_from = timezone.make_aware(restored_from)

        cutoff = restored_from - SLACK
        # Only completed erasures can have been undone: an open one is still
        # being worked and its owners will erase the restored rows anyway.
        candidates = ErasureRequest.objects.filter(
            state=ErasureRequest.STATE_DELETED,
            completed_at__gte=cutoff,
        ).exclude(
            # Already re-queued for an earlier (or the same) restore.
            requeues__origin=ErasureRequest.ORIGIN_RESTORE_REQUEUE,
        ).order_by("pk")

        requeued = skipped = 0
        for source in candidates:
            if options["dry_run"]:
                self.stdout.write(
                    f"would re-queue {source.subject_type}:{source.subject_key} "
                    f"(request {source.pk}, completed {source.completed_at.isoformat()})"
                )
                requeued += 1
                continue
            try:
                clone = gdpr_orchestrator.request_erasure(
                    source.subject_type,
                    source.subject_key,
                    workspace_id=source.workspace_id,
                    requested_by=source.requested_by,
                    origin=ErasureRequest.ORIGIN_RESTORE_REQUEUE,
                    source_request=source,
                    restored_from=restored_from,
                    note=f"re-queued after restore from {restored_from.isoformat()}",
                )
            except IntegrityError:
                # The unique (origin, source_request) pair lost a race with a
                # concurrent run. That is the mechanism working, not an error.
                skipped += 1
                continue
            except ValueError as e:
                # A subject type that has since left SUBJECT_TYPES: report it
                # rather than skipping quietly — the data is back and nothing
                # is going to erase it.
                self.stderr.write(
                    f"cannot re-queue request {source.pk} "
                    f"({source.subject_type}:{source.subject_key}): {e}"
                )
                skipped += 1
                continue
            requeued += 1
            self.stdout.write(
                f"re-queued {source.subject_type}:{source.subject_key} "
                f"as request {clone.pk}"
            )

        verb = "would re-queue" if options["dry_run"] else "re-queued"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {requeued} erasure(s) completed since "
                f"{cutoff.isoformat()}; {skipped} skipped."
            )
        )
