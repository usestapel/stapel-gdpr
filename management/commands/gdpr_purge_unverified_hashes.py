"""Delete re-registration hashes written outside ``store_hashes``.

The remediation half of the ``gdpr.E004`` system check: rows whose digest
format this library never produced (typically a bare SHA-256 of a normalized
email or phone number, which is dictionary-recoverable) are personal data
retained for a purpose they cannot serve — they never match a lookup.

    python manage.py gdpr_purge_unverified_hashes --dry-run
    python manage.py gdpr_purge_unverified_hashes
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Delete ReRegistrationHash rows not written through store_hashes'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report the row count without deleting anything.',
        )
        parser.add_argument(
            '--include-legacy',
            action='store_true',
            help=(
                'Also drop pre-0003 rows. Use it when another writer is known '
                'to have hashed into this table: legacy rows cannot be told '
                'apart from its unsalted ones after the fact.'
            ),
        )

    def handle(self, *args, **options):
        from stapel_gdpr.models import ReRegistrationHash
        from stapel_gdpr.reregistration import purge_unverified_hashes

        keep = [ReRegistrationHash.SCHEME_HMAC_V1]
        if not options['include_legacy']:
            keep.append(ReRegistrationHash.SCHEME_LEGACY)

        if options['dry_run']:
            count = ReRegistrationHash.objects.exclude(scheme__in=keep).count()
            self.stdout.write(f'{count} re-registration hashes would be deleted (dry run)')
            return

        count = purge_unverified_hashes(include_legacy=options['include_legacy'])
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {count} re-registration hashes',
        ))
