"""Boot-time checks — the misconfiguration has to be loud at deploy, not at audit.

Every finding this module guards against had the same shape: a mechanism
existed, no consumer wired it, and nothing said so until someone went looking
years later. A ``manage.py check`` that fails at boot is the cheapest place
for that sentence to appear.

* ``gdpr.E001`` — no data-owner inventory. Erasure cannot be certified.
* ``gdpr.E002`` — declared owners that nothing can reach, or providers nobody
  declared. The inventory is wrong, so the receipts it produces mean nothing.
* ``gdpr.W003`` — an escape hatch is open. Not an error (someone chose it),
  but it must never be silent.
* ``gdpr.E004`` — re-registration hashes written outside ``store_hashes``:
  an unverified, probably unsalted digest of an email or a phone number.
* ``gdpr.W005`` — no session-revocation seam resolves, so closure will fail.
* ``gdpr.E006`` — the primary-identity erasure strategy cannot run, so the
  users.User row would survive every erasure.
* ``gdpr.W006`` — a declared data owner has not answered ``gdpr.owner.probe``
  within ``OWNER_ALIVE_MAX_AGE_HOURS``. Its erasures will time out; this says
  so at boot instead of at the first missed deadline.
* ``gdpr.W007`` — ``EXPORT_BUCKET_PREFIX`` is empty, so a peer service may
  name any key in the bucket and have its bytes copied into a user's export.
* ``gdpr.W008`` — a data-subject request is past its acknowledgement deadline
  with nothing sent. A statutory clock, visible where deploys are looked at.
* ``gdpr.E009`` — ``DATA_OWNERS`` names an owner no installed library
  declares, and an installed library declares a name it was plainly meant to
  be. A misspelled owner is inferred *remote*, so nothing refuses it: the
  store is never asked, its part waits out the clock and times out.
* ``gdpr.E010`` — an installed library declares an erasure owner that
  ``DATA_OWNERS`` does not list. No receipt slot is ever created for it, so
  the request has nothing to wait for and reports itself complete over a
  store that still holds the data.
* ``gdpr.W011`` — the same, for a name listed in ``DATA_OWNERS_OPT_OUT``:
  the deployment chose it, and the choice stays visible.
* ``gdpr.W012`` — an owner claims a subject type the host does not list for
  it (or does not list at all). That subject's erasures never reach the
  store, while the same store answers for the subjects the host did list —
  which is what makes the gap invisible.

E009/E010/W011/W012 are the 2026-09-07 fleet incident, found only by an
external audit: a host listing ``"profiles"``/``"cdn"`` (the app labels;
the libraries declare ``"profile"`` and ``"media"``) and omitting ``video``
and ``agent`` altogether. Four stores were never asked to erase anything and
every request still ended. See :mod:`stapel_gdpr.declarations`.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.checks import Error, Warning as CheckWarning, register

from .conf import gdpr_settings
from .declarations import (
    CANONICAL_SEAM,
    installed_owner_declarations,
    nearest_declared_name,
)
from .owners import data_owner_report, subject_types

__all__ = [
    "check_data_owner_names",
    "check_data_owner_registry",
    "check_dsar_deadlines",
    "check_data_owner_liveness",
    "check_reregistration_hashes",
    "register_checks",
]

_HATCHES = (
    (
        "ALLOW_ERASURE_WITHOUT_RECEIPTS",
        "closures can be marked DELETED without a receipt from every declared "
        "data owner — an erasure this deployment cannot prove happened",
    ),
    (
        "ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION",
        "account closure proceeds without revoking sessions — every access "
        "token minted before the closure keeps working until it expires",
    ),
)


def check_data_owner_registry(app_configs=None, **kwargs):
    report = data_owner_report()
    problems = []

    if report.unconfigured:
        problems.append(
            Error(
                'STAPEL_GDPR["DATA_OWNERS"] is empty: this deployment has no '
                "inventory of the stores holding personal data.",
                hint=(
                    "List every store (auth, profiles, recordings, billing, "
                    "CDN, search indexes, provider copies...) in "
                    'STAPEL_GDPR["DATA_OWNERS"] and stamp '
                    'STAPEL_GDPR["DATA_OWNERS_VERSION"]. Until then no '
                    "account closure can reach the DELETED status and every "
                    "export is reported to the user as partial."
                ),
                id="gdpr.E001",
            )
        )
    else:
        if report.missing:
            problems.append(
                Error(
                    "Declared GDPR data owners have no registered provider: "
                    f"{', '.join(report.missing)}.",
                    hint=(
                        "Add the provider to GDPR_PROVIDERS, or declare the "
                        'owner as {"name": ..., "kind": "remote"} if it '
                        "confirms erasure over comm."
                    ),
                    id="gdpr.E002",
                )
            )
        if report.undeclared:
            problems.append(
                Error(
                    "GDPR providers are registered but missing from "
                    f'STAPEL_GDPR["DATA_OWNERS"]: {", ".join(report.undeclared)}.',
                    hint=(
                        "An inventory that does not list a store already "
                        "wired in is stale; add the names or drop the "
                        "providers."
                    ),
                    id="gdpr.E002",
                )
            )
        if not report.version:
            problems.append(
                CheckWarning(
                    'STAPEL_GDPR["DATA_OWNERS_VERSION"] is unset, so closures '
                    "cannot record which inventory certified them.",
                    hint='Set a version string and bump it whenever DATA_OWNERS changes.',
                    id="gdpr.W003",
                )
            )

    for name, consequence in _HATCHES:
        if getattr(gdpr_settings, name):
            problems.append(
                CheckWarning(
                    f'STAPEL_GDPR["{name}"] is on: {consequence}.',
                    hint="Remove the setting once the underlying gap is closed.",
                    id="gdpr.W003",
                )
            )

    if not gdpr_settings.EXPORT_BUCKET_PREFIX:
        problems.append(
            CheckWarning(
                'STAPEL_GDPR["EXPORT_BUCKET_PREFIX"] is empty: a peer service '
                "may name any key in the storage bucket as its export part, "
                "and those bytes are copied into an archive the requesting "
                "user downloads.",
                hint=(
                    'Restore the default "gdpr/{correlation_id}/" so a peer '
                    "can only name a key belonging to the export it was "
                    "asked about."
                ),
                id="gdpr.W007",
            )
        )

    from .lifecycle import IDENTITY_ANONYMIZE, IDENTITY_DELETE, _resolve_revoker

    identity_strategy = str(gdpr_settings.PRIMARY_IDENTITY_ERASURE or IDENTITY_ANONYMIZE)
    if identity_strategy not in (IDENTITY_ANONYMIZE, IDENTITY_DELETE):
        from django.utils.module_loading import import_string

        try:
            import_string(identity_strategy)
        except ImportError as e:
            problems.append(
                Error(
                    'STAPEL_GDPR["PRIMARY_IDENTITY_ERASURE"] is neither '
                    f'"{IDENTITY_ANONYMIZE}"/"{IDENTITY_DELETE}" nor an '
                    f"importable erase(user) callable: {e}",
                    hint=(
                        "Every closure would stop short of erasing the "
                        "users.User row and no account would reach DELETED."
                    ),
                    id="gdpr.E006",
                )
            )

    try:
        _, strategy = _resolve_revoker()
    except Exception as e:  # a bad dotted path must not crash `manage.py check`
        problems.append(
            Error(
                f'STAPEL_GDPR["SESSION_REVOKER"] cannot be imported: {e}',
                id="gdpr.E002",
            )
        )
    else:
        if strategy == "none" and not gdpr_settings.ALLOW_CLOSURE_WITHOUT_SESSION_REVOCATION:
            problems.append(
                CheckWarning(
                    "No session-revocation seam resolves, so every account "
                    "closure will fail with SessionRevocationUnavailable.",
                    hint=(
                        'Set STAPEL_GDPR["SESSION_REVOKER"] to a '
                        "revoke(user) callable, install stapel-auth, or "
                        "subscribe to user.sessions_revoked."
                    ),
                    id="gdpr.W005",
                )
            )

    return problems


def check_data_owner_names(app_configs=None, **kwargs):
    """The host's inventory against what the installed libraries declare.

    :func:`check_data_owner_registry` compares ``DATA_OWNERS`` with the
    providers *registered at runtime*, which is why the 2026-09-07 incident
    slipped past it: a misspelled name registers nothing, so it is inferred
    ``remote`` and looks exactly like an owner in another container, and an
    omitted name has nothing to be absent from. This one compares against the
    libraries' own declarations (:mod:`stapel_gdpr.declarations`) — static
    facts of the installed packages, true before any of them is asked
    anything.

    Silent when no owner library is installed here: that is an ordinary
    microservices shape (the erasure service holds no stores of its own) and
    there is nothing to compare against. It is never read as "no owners".
    """
    report = data_owner_report()
    if report.unconfigured:
        return []  # gdpr.E001 already says the louder thing.

    declarations = installed_owner_declarations()
    if not declarations:
        return []

    problems = []
    host_owners = {owner.name: owner for owner in report.owners}
    opted_out = {
        str(name).strip()
        for name in (gdpr_settings.DATA_OWNERS_OPT_OUT or [])
        if str(name).strip()
    }

    # --- gdpr.E009: a name nothing declares, next to one that is declared ---
    misspelled = []
    for name in host_owners:
        if name in declarations:
            continue
        nearest = nearest_declared_name(name, declarations)
        if nearest is None:
            # Unknown here, but this process cannot tell a typo from an owner
            # living in another container. gdpr.W006 catches it when it never
            # answers a probe; guessing would break every microservice.
            continue
        misspelled.append((name, nearest, declarations[nearest].source))
    if misspelled:
        named = "; ".join(
            f'"{name}" -> "{nearest}" (declared by {source})'
            for name, nearest, source in misspelled
        )
        problems.append(
            Error(
                'STAPEL_GDPR["DATA_OWNERS"] names data owners no installed '
                f"library declares: {named}.",
                hint=(
                    "An owner name that matches nothing registered is "
                    "inferred 'remote', so nothing refuses it: the store is "
                    "never asked to erase, its receipt slot waits out "
                    "OWNER_TIMEOUT_HOURS and the erasure times out. An app "
                    "label is not an owner name — the 'cdn' app owns 'media', "
                    "the 'profiles' app owns 'profile'. Use the name the "
                    f"library declares to {CANONICAL_SEAM}."
                ),
                id="gdpr.E009",
            )
        )

    # --- gdpr.E010 / gdpr.W011: an installed store nobody asks ---
    # A name gdpr.E009 already told the host to write is not ALSO a missing
    # owner: one edit fixes both, and reporting it twice reads as two stores
    # to go and find.
    renamed_to = {nearest for _, nearest, _ in misspelled}
    absent, deliberate = [], []
    for name, declaration in sorted(declarations.items()):
        if name in host_owners or name in renamed_to:
            continue
        described = f'"{name}"'
        if declaration.subject_types:
            described += f" (claims {', '.join(declaration.subject_types)})"
        described += f", declared by {declaration.source}"
        (deliberate if name in opted_out else absent).append(described)
    if absent:
        problems.append(
            Error(
                "Installed libraries declare GDPR data owners that "
                f'STAPEL_GDPR["DATA_OWNERS"] does not list: {"; ".join(absent)}.',
                hint=(
                    "No receipt slot is ever created for an owner the "
                    "inventory does not name, so the erasure has nothing to "
                    "wait for and completes over a store that still holds "
                    "the data — the failure mode has no symptom at all. Add "
                    "the name (with the subject types it claims) and bump "
                    'STAPEL_GDPR["DATA_OWNERS_VERSION"], or name it in '
                    'STAPEL_GDPR["DATA_OWNERS_OPT_OUT"] if this deployment '
                    "deliberately does not ask it."
                ),
                id="gdpr.E010",
            )
        )
    if deliberate:
        problems.append(
            CheckWarning(
                'STAPEL_GDPR["DATA_OWNERS_OPT_OUT"] excludes installed data '
                f"owners from every erasure: {'; '.join(deliberate)}.",
                hint=(
                    "These stores are never asked to erase anything and no "
                    "erasure waits for them. Remove the opt-out once they "
                    "are wired, or record why they are exempt."
                ),
                id="gdpr.W011",
            )
        )

    # --- gdpr.W012: a subject the owner erases and the host never names ---
    host_subjects = set(subject_types())
    unlisted = []
    for name, owner in host_owners.items():
        declaration = declarations.get(name)
        if declaration is None or not declaration.subject_types:
            continue
        for claimed in declaration.subject_types:
            if claimed not in owner.subjects:
                unlisted.append(
                    f'"{name}" claims {claimed!r}, which its DATA_OWNERS entry '
                    "does not list"
                )
            elif claimed not in host_subjects:
                unlisted.append(
                    f'"{name}" claims {claimed!r}, which is not in '
                    'STAPEL_GDPR["SUBJECT_TYPES"]'
                )
    if unlisted:
        problems.append(
            CheckWarning(
                "Declared data owners can erase subject types this host does "
                f"not ask them about: {'; '.join(unlisted)}.",
                hint=(
                    "The owner gets a receipt slot only for the subjects its "
                    "DATA_OWNERS entry lists, so those erasures never reach "
                    "it — while the same owner answers for the subjects that "
                    "were listed, which is what keeps the gap invisible. "
                    'Give the entry its subject types ({"cdn": ["account", '
                    '"workspace", "file"]}) and add any missing type to '
                    'STAPEL_GDPR["SUBJECT_TYPES"].'
                ),
                id="gdpr.W012",
            )
        )

    return problems


def check_reregistration_hashes(app_configs=None, databases=None, **kwargs):
    """Report rows written outside ``store_hashes`` (unknown digest format).

    Database-backed, so it obeys Django's contract for such checks
    (``django.core.checks.database.check_database_backends`` is the canonical
    example): *databases* names the aliases the caller opted into — ``migrate``
    and ``manage.py check --database <alias>`` pass them, everything else
    passes ``None``, which means "query nothing". A boot smoke test that runs
    without a database is exactly that caller, and it must get an empty list,
    not a traceback.
    """
    from django.core.exceptions import ImproperlyConfigured
    from django.db import DatabaseError, router

    from .models import ReRegistrationHash

    if not databases:
        return []

    problems = []
    for alias in databases:
        # The router decides where this model lives; an alias that does not
        # hold it has nothing to say about it.
        if not router.allow_migrate_model(alias, ReRegistrationHash):
            continue
        try:
            count = ReRegistrationHash.objects.using(alias).exclude(
                scheme__in=[
                    ReRegistrationHash.SCHEME_HMAC_V1,
                    # Pre-0003 rows are grandfathered: they age out with
                    # retention and cannot be attributed to a writer any more.
                    ReRegistrationHash.SCHEME_LEGACY,
                ],
            ).count()
        except (DatabaseError, ImproperlyConfigured):
            # Unmigrated, unreachable, or not configured at all (the dummy
            # backend raises ImproperlyConfigured, not DatabaseError): a
            # deployment check reports what it can see and stays silent about
            # what it cannot reach. It is not the place to fail the boot for
            # an absent database.
            continue

        if not count:
            continue
        problems.append(
            Error(
                f"{count} ReRegistrationHash rows in database {alias!r} were "
                "written outside stapel_gdpr.reregistration.store_hashes and "
                "carry an unverified digest format.",
                hint=(
                    "An unsalted SHA-256 of a normalized email or phone number "
                    "is recoverable from a dictionary. Point every writer at "
                    "stapel_gdpr.reregistration.store_hashes and clear the old "
                    "rows with `manage.py gdpr_purge_unverified_hashes`."
                ),
                id="gdpr.E004",
            )
        )
    return problems


def _db_query(databases, model, query):
    """Run *query* against the first alias that actually holds *model*.

    Same contract as :func:`check_reregistration_hashes`: *databases* names
    the aliases the caller opted into, ``None`` means "query nothing", and an
    unmigrated or unreachable database is silence, never a traceback. A boot
    smoke test with no database must get an empty finding list.
    """
    from django.core.exceptions import ImproperlyConfigured
    from django.db import DatabaseError, router

    if not databases:
        return None
    for alias in databases:
        if not router.allow_migrate_model(alias, model):
            continue
        try:
            return query(alias)
        except (DatabaseError, ImproperlyConfigured):
            continue
    return None


def check_data_owner_liveness(app_configs=None, databases=None, **kwargs):
    """``gdpr.W006`` — declared owners that have not proven they are listening.

    Reads ``DataOwnerHealth``, which ``probe_data_owners`` fills from the
    owners' own ``gdpr.owner.alive`` answers. Database-backed on purpose: the
    finding has to fire on every boot of the service that owns the table, so
    a silent owner is named at deploy rather than discovered when an erasure
    times out thirty days later.
    """
    from django.utils import timezone

    from .models import DataOwnerHealth

    report = data_owner_report()
    if report.unconfigured:
        return []  # gdpr.E001 already says the louder thing.

    max_age = timedelta(hours=float(gdpr_settings.OWNER_ALIVE_MAX_AGE_HOURS or 48))
    cutoff = timezone.now() - max_age

    rows = _db_query(
        databases,
        DataOwnerHealth,
        lambda alias: {
            row.owner: row
            for row in DataOwnerHealth.objects.using(alias).all()
        },
    )
    if rows is None:
        return []

    silent, never = [], []
    for owner in report.owners:
        row = rows.get(owner.name)
        if row is None or row.last_alive_at is None:
            never.append(owner.name)
        elif row.last_alive_at < cutoff:
            silent.append(f"{owner.name} (last alive {row.last_alive_at.isoformat()})")

    problems = []
    if never or silent:
        named = ", ".join(sorted(never) + sorted(silent))
        problems.append(
            CheckWarning(
                "Declared GDPR data owners have not answered gdpr.owner.probe "
                f"in the last {max_age}: {named}.",
                hint=(
                    "Every erasure naming a subject these owners claim will "
                    "time out. Check that each one runs a consume_actions "
                    "process subscribed to gdpr.erasure.requested and answers "
                    "gdpr.owner.alive from the same subscriber, and that "
                    "probe_data_owners is in this service's beat schedule "
                    "(get_gdpr_beat_schedule)."
                ),
                id="gdpr.W006",
            )
        )
    return problems


def check_dsar_deadlines(app_configs=None, databases=None, **kwargs):
    """``gdpr.W008`` — data-subject requests past their acknowledgement clock.

    The acknowledgement is automated at intake, so a row here means the
    notification could not be requested — the one failure that turns a met
    statutory deadline into a missed one without anything looking wrong.

    Numbered W008, not the W007 the spec asked for: ``gdpr.W007`` has been
    the ``EXPORT_BUCKET_PREFIX`` warning since 0.4.x, and silently reusing a
    published check id would break every deployment that silenced it.
    """
    from django.utils import timezone

    from .models import DsarRequest

    open_states = [
        DsarRequest.STATE_RECEIVED,
        DsarRequest.STATE_ACKNOWLEDGED,
        DsarRequest.STATE_IN_PROGRESS,
    ]
    count = _db_query(
        databases,
        DsarRequest,
        lambda alias: DsarRequest.objects.using(alias).filter(
            ack_sent_at__isnull=True,
            ack_due_at__lte=timezone.now(),
            state__in=open_states,
        ).count(),
    )
    if not count:
        return []
    return [
        CheckWarning(
            f"{count} data-subject request(s) are past their acknowledgement "
            "deadline with no acknowledgement sent.",
            hint=(
                "The acknowledgement is sent automatically at intake, so this "
                "means stapel_core.notifications could not be reached. Check "
                "the notifications transport, then resolve the queue at "
                "GET /gdpr/api/v1/dsar."
            ),
            id="gdpr.W008",
        )
    ]


def register_checks() -> None:
    """Called from ``AppConfig.ready``; idempotent."""
    register(check_data_owner_registry, "gdpr")
    register(check_data_owner_names, "gdpr")
    register(check_reregistration_hashes, "gdpr", "database")
    register(check_data_owner_liveness, "gdpr", "database")
    register(check_dsar_deadlines, "gdpr", "database")
