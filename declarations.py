"""What the INSTALLED LIBRARIES say they erase — the other half of the inventory.

:mod:`stapel_gdpr.owners` reads the *host's* declaration,
``STAPEL_GDPR["DATA_OWNERS"]``. This module reads the *libraries'*: every
installed app that can erase something says so in its own code, and the two
lists have to agree. Nothing compared them until 2026-09-07, when a fleet
deployment was found listing ``"profiles"`` and ``"cdn"`` — names no library
in the fleet has ever declared (they are ``"profile"`` and ``"media"``) — and
omitting ``video`` and ``agent`` entirely. Four stores were never asked to
erase anything; the two typo'd names were silently inferred *remote*, waited
out their clock and timed out; the omitted two produced no receipt slot at
all, so the request had nothing left to wait for and reported itself
complete. Every erasure in that deployment was wrong in the same way, for
months, and the only thing that ever said so was an external audit.

The canonical seam
------------------

One call, from the library's ``AppConfig.ready()``::

    from stapel_core.gdpr import register_gdpr_owner
    from .erasure import OWNER, SUBJECT_TYPES, erase_subject

    register_gdpr_owner(OWNER, SUBJECT_TYPES, erase_subject)

That is the only declaration that carries everything the host has to match:
the owner *name* and the *subject types* it can really erase. It is also the
only one that proves the erasure path is subscribed rather than merely
importable.

The two older seams are still read, because most of the fleet is still on
them and a check that only understood the newest one would report a clean
deployment as broken:

* ``GDPRProvider.section`` registered into ``stapel_core.gdpr.gdpr_registry``
  — a name, no subject types;
* module constants ``OWNER``/``GDPR_OWNER`` and
  ``SUBJECT_TYPES``/``GDPR_SUBJECT_TYPES`` in an installed app's ``erasure``
  or ``gdpr`` module — a *static* declaration, readable whether or not
  ``ready()`` got as far as registering anything. This is the seam that
  catches the incident above: ``stapel_cdn.erasure.OWNER == "media"`` is true
  of the installed package no matter how the host is wired.

Names that are NOT owner names
------------------------------

An app label is not an owner name, and the incident was exactly that
confusion: the ``cdn`` app owns ``media``, the ``profiles`` app owns
``profile``. Those labels are recorded as *aliases* so ``gdpr.E009`` can say
which name the host meant instead of only that this one is unknown.
"""
from __future__ import annotations

import difflib
import importlib
import importlib.util
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: The one way a library should declare itself. Named in every check hint.
CANONICAL_SEAM = "stapel_core.gdpr.register_gdpr_owner"

SEAM_REGISTRATION = "register_gdpr_owner"
SEAM_PROVIDER = "GDPRProvider.section"
SEAM_CONSTANT = "module constant"

#: Submodules of an installed app that may carry the static declaration.
_ERASURE_MODULES = ("erasure", "gdpr")
#: Owner-name constants, most specific first.
_OWNER_ATTRS = ("GDPR_OWNER", "OWNER")
#: Subject-type constants, most specific first.
_SUBJECT_ATTRS = ("GDPR_SUBJECT_TYPES", "SUBJECT_TYPES")

#: How close a host's name has to be to a declared one before ``gdpr.E009``
#: calls it a typo. 0.7 accepts profiles/profile and recording/recordings and
#: refuses media/agent.
_NEAREST_CUTOFF = 0.7

__all__ = [
    "CANONICAL_SEAM",
    "SEAM_CONSTANT",
    "SEAM_PROVIDER",
    "SEAM_REGISTRATION",
    "OwnerDeclaration",
    "installed_owner_declarations",
    "nearest_declared_name",
]


@dataclass(frozen=True)
class OwnerDeclaration:
    """One installed library's own statement of what it erases."""

    name: str
    #: Empty when the seam that declared it does not carry subject types
    #: (a bare ``GDPRProvider.section``). Empty means "unknown", never
    #: "none" — a check must not read silence as a claim.
    subject_types: tuple[str, ...] = ()
    #: Which seam this came from, for the hint that tells a library author
    #: where to move.
    seam: str = SEAM_REGISTRATION
    #: Dotted path of whatever declared it.
    source: str = ""
    #: App labels and package names that are NOT this owner's name but are
    #: routinely mistaken for it (the ``cdn`` app owns ``media``).
    aliases: tuple[str, ...] = field(default_factory=tuple)


def _safe_import(dotted: str):
    """Import *dotted* or return None. A check must not crash a boot."""
    try:
        if importlib.util.find_spec(dotted) is None:
            return None
    except (ImportError, AttributeError, ValueError):
        return None
    try:
        return importlib.import_module(dotted)
    except Exception:  # a library's import error is that library's finding
        logger.debug("gdpr: could not import %s while reading owner declarations",
                     dotted, exc_info=True)
        return None


def _names(raw) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    try:
        found = [str(item).strip() for item in raw]
    except TypeError:
        return ()
    return tuple(dict.fromkeys(n for n in found if n))


def _aliases_for(app_config) -> tuple[str, ...]:
    """The names this app answers to that are not necessarily its owner name."""
    module = str(getattr(app_config, "name", "") or "")
    tail = module.rsplit(".", 1)[-1]
    candidates = [
        str(getattr(app_config, "label", "") or ""),
        module,
        tail,
        tail.removeprefix("stapel_"),
    ]
    return tuple(dict.fromkeys(c for c in candidates if c))


def _merge(into: dict[str, OwnerDeclaration], declaration: OwnerDeclaration) -> None:
    """Keep the richest statement about one owner.

    A library on the canonical seam usually ALSO registers a provider and
    ALSO carries the module constants; three sightings of one owner is the
    normal case, not a conflict. Subject types win over no subject types, and
    aliases accumulate — the app label is only ever visible on the static
    seam, and it is what ``gdpr.E009`` needs to name a typo.
    """
    existing = into.get(declaration.name)
    if existing is None:
        into[declaration.name] = declaration
        return
    into[declaration.name] = OwnerDeclaration(
        name=existing.name,
        subject_types=existing.subject_types or declaration.subject_types,
        seam=existing.seam,
        source=existing.source or declaration.source,
        aliases=tuple(dict.fromkeys(existing.aliases + declaration.aliases)),
    )


def _from_registrations(found: dict[str, OwnerDeclaration]) -> None:
    """The canonical seam: ``register_gdpr_owner`` in ``ready()``."""
    try:
        from stapel_core.gdpr import registered_gdpr_owners
    except ImportError:  # older stapel-core: the seam did not exist yet
        return
    try:
        registrations = registered_gdpr_owners()
    except Exception:  # pragma: no cover — defensive; a check never crashes a boot
        logger.debug("gdpr: registered_gdpr_owners() failed", exc_info=True)
        return
    for name, types in registrations.items():
        _merge(found, OwnerDeclaration(
            name=str(name),
            subject_types=_names(types),
            seam=SEAM_REGISTRATION,
            source=CANONICAL_SEAM,
        ))


def _from_providers(found: dict[str, OwnerDeclaration]) -> None:
    """The legacy in-process registry: a name, and rarely subject types."""
    try:
        from stapel_core.gdpr import gdpr_registry
    except ImportError:  # pragma: no cover — stapel-core is a hard dependency
        return
    for provider in getattr(gdpr_registry, "providers", []):
        name = str(getattr(provider, "section", "") or "")
        if not name:
            continue
        _merge(found, OwnerDeclaration(
            name=name,
            subject_types=_names(getattr(provider, "subject_types", None)),
            seam=SEAM_PROVIDER,
            source=f"{type(provider).__module__}.{type(provider).__qualname__}",
        ))


def _from_modules(found: dict[str, OwnerDeclaration]) -> None:
    """The static seam: ``OWNER``/``SUBJECT_TYPES`` in an installed app."""
    from django.apps import apps

    if not apps.apps_ready:  # pragma: no cover — checks run after ready()
        return
    for app_config in apps.get_app_configs():
        package = str(getattr(app_config, "name", "") or "")
        if not package or package == "stapel_gdpr":
            continue
        aliases = _aliases_for(app_config)
        for submodule in _ERASURE_MODULES:
            dotted = f"{package}.{submodule}"
            module = _safe_import(dotted)
            if module is None:
                continue
            name = next(
                (str(getattr(module, attr)) for attr in _OWNER_ATTRS
                 if isinstance(getattr(module, attr, None), str)
                 and getattr(module, attr).strip()),
                "",
            ).strip()
            if not name:
                continue
            types = next(
                (_names(getattr(module, attr)) for attr in _SUBJECT_ATTRS
                 if getattr(module, attr, None) is not None),
                (),
            )
            _merge(found, OwnerDeclaration(
                name=name,
                subject_types=types,
                seam=SEAM_CONSTANT,
                source=dotted,
                aliases=tuple(a for a in aliases if a != name),
            ))


def installed_owner_declarations() -> dict[str, OwnerDeclaration]:
    """Owner name -> what the installed library that owns it declares.

    Read in order of authority — the canonical registration first, then the
    provider registry, then the module constants — so ``seam`` names the best
    seam a library is on while ``subject_types`` and ``aliases`` are filled in
    from whichever sighting actually carries them.

    Empty in a service that installs no owner library, which is a normal
    microservices shape and never a finding by itself: the checks that read
    this only ever compare names, never conclude "there are no owners".
    """
    found: dict[str, OwnerDeclaration] = {}
    _from_registrations(found)
    _from_providers(found)
    _from_modules(found)
    return found


def nearest_declared_name(
    name: str, declarations: dict[str, OwnerDeclaration],
) -> str | None:
    """The declared owner name *name* was most likely meant to be.

    An exact alias hit first — ``cdn`` is the app label of the library that
    owns ``media``, and no amount of string distance would ever find that.
    Then ordinary near-misses, which is what catches ``profiles`` for
    ``profile``.

    ``None`` when nothing is close. That is deliberately not a finding: in a
    microservices deployment most declared owners live in other containers,
    their libraries are not installed here, and a name this process cannot
    see is a *remote owner*, not a typo. Such a name is still caught — by
    ``gdpr.W006``, when it never answers a probe.
    """
    wanted = str(name or "").strip()
    if not wanted or wanted in declarations:
        return None
    for declaration in declarations.values():
        if wanted in declaration.aliases:
            return declaration.name
    matches = difflib.get_close_matches(
        wanted, list(declarations), n=1, cutoff=_NEAREST_CUTOFF,
    )
    return matches[0] if matches else None
