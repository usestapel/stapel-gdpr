"""Re-drive erasures the deadline sweep parked in TIMEOUT.

A timed-out erasure is not a finished one and nothing moves it on its own:
the sweep flips silent parts to TIMEOUT and stops, and the requeue-after-
restore command only looks at DELETED requests. This command re-opens each
TIMEOUT request, resets its unanswered parts with a fresh deadline, re-emits
the original ``gdpr.erasure.requested`` under the same correlation id and
finalizes the request as soon as the receipts are complete.

Idempotent: only requests still in TIMEOUT are touched, under a row lock, so
a second run (or a concurrent one) finds nothing to do. ``--dry-run`` prints
counts only and writes nothing; it counts with the same planner the apply
path uses.
"""
from django.core.management.base import BaseCommand

from stapel_gdpr.models import ErasureRequest
from stapel_gdpr.orchestrator import gdpr_orchestrator


class Command(BaseCommand):
    help = (
        "Re-drive TIMEOUT erasures: reset unanswered parts, re-emit the erase "
        "request to their owners, finalize when receipts arrive. Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print counts of what would be re-driven and write nothing.",
        )
        parser.add_argument(
            "--subject-type",
            default=None,
            help="Only requests for this subject type (e.g. account).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Re-drive at most N requests, oldest first.",
        )

    def handle(self, *args, **options):
        qs = ErasureRequest.objects.filter(
            state=ErasureRequest.STATE_TIMEOUT,
        ).select_related("closure").order_by("pk")
        if options["subject_type"]:
            qs = qs.filter(subject_type=options["subject_type"])
        if options["limit"]:
            qs = qs[: options["limit"]]
        ids = list(qs.values_list("pk", flat=True))

        dry = options["dry_run"]
        requests = parts = failed = undeclared = local = 0
        finalized = skipped = 0
        for pk in ids:
            if dry:
                plan = gdpr_orchestrator.plan_redrive(
                    ErasureRequest.objects.get(pk=pk)
                )
            else:
                plan = gdpr_orchestrator.redrive_timed_out(pk)
                if plan is None:
                    skipped += 1
                    continue
                if ErasureRequest.objects.filter(
                    pk=pk, state=ErasureRequest.STATE_DELETED,
                ).exists():
                    finalized += 1
            requests += 1
            parts += len(plan.unanswered)
            failed += len(plan.failed)
            undeclared += len(plan.undeclared)
            local += int(plan.local_retry)

        prefix = "would re-drive" if dry else "re-drove"
        self.stdout.write(
            f"{prefix} {requests} timed-out erasure request(s); "
            f"unanswered parts reset: {parts}; "
            f"closures re-entering local erasure: {local}; "
            f"failed parts left as they are: {failed}; "
            f"parts whose owner is no longer declared: {undeclared}"
        )
        if not dry:
            self.stdout.write(
                f"finalized now: {finalized}; "
                f"no longer in timeout (skipped): {skipped}"
            )
