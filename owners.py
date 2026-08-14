"""The data-owner registry — who holds personal data, declared and versioned.

Why this module exists. Erasure completeness used to be whatever happened to
be plugged in at runtime: the orchestrator asked ``gdpr_registry`` for the
in-process providers, created a deletion part per entry of
``REMOTE_DELETION_SERVICES`` (empty in every deployment we have seen), and
flipped the closure to DELETED once those returned. A deployment that
registered a single provider therefore reported a *completed erasure* while
recordings, transcripts, profiles, workspaces, docs, billing, CDN objects and
provider copies were untouched. The mechanism existed; nobody registered with
it; nothing anywhere said so.

So the inventory is now a declaration, not a derivation:

* every store is listed in ``STAPEL_GDPR["DATA_OWNERS"]`` with a version;
* a declared owner with no way to reach it (``kind='local'`` with no
  registered provider) is a *missing* owner;
* a registered provider nobody declared is an *undeclared* owner — the
  inventory is stale, and a stale inventory is exactly what this module
  refuses to trust;
* an empty/unset registry is not "nothing to erase", it is "the question was
  never answered".

All three block erasure completeness (:func:`RegistryReport.blocking_reason`)
and therefore the ``DELETED`` status. Exports still run — Art. 15 is not
improved by refusing to answer — but they are marked partial to the user.

The escape hatch is named ``ALLOW_ERASURE_WITHOUT_RECEIPTS`` and is off.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from .conf import gdpr_settings

logger = logging.getLogger(__name__)

#: Owner served by an in-process ``GDPRProvider`` (monolith / same container).
KIND_LOCAL = "local"
#: Owner that confirms out of band with ``gdpr.section.erased``.
KIND_REMOTE = "remote"

__all__ = [
    "KIND_LOCAL",
    "KIND_REMOTE",
    "DataOwner",
    "RegistryReport",
    "data_owner_report",
    "registry_version",
]


@dataclass(frozen=True)
class DataOwner:
    """One declared holder of personal data."""

    name: str
    kind: str
    timeout: timedelta

    @property
    def is_local(self) -> bool:
        return self.kind == KIND_LOCAL


@dataclass(frozen=True)
class RegistryReport:
    """What the declaration says, measured against what is actually wired."""

    version: str
    owners: tuple[DataOwner, ...] = ()
    #: Declared local owners with no registered provider — unreachable.
    missing: tuple[str, ...] = ()
    #: Registered providers absent from the declaration — stale inventory.
    undeclared: tuple[str, ...] = ()
    #: True when ``DATA_OWNERS`` is empty: the question was never answered.
    unconfigured: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(o.name for o in self.owners)

    def owner(self, name: str) -> DataOwner | None:
        for o in self.owners:
            if o.name == name:
                return o
        return None

    @property
    def problems(self) -> tuple[str, ...]:
        """Every reason this registry cannot certify an erasure."""
        found = []
        if self.unconfigured:
            found.append(
                'STAPEL_GDPR["DATA_OWNERS"] is empty: no inventory of the '
                "stores holding personal data, so no erasure can be proven "
                "complete"
            )
        if self.missing:
            found.append(
                "declared data owners are unreachable (no in-process provider "
                f"registered): {', '.join(self.missing)}"
            )
        if self.undeclared:
            found.append(
                "GDPR providers are registered but absent from "
                f'STAPEL_GDPR["DATA_OWNERS"]: {", ".join(self.undeclared)}'
            )
        return tuple(found)

    def blocking_reason(self) -> str | None:
        """The first reason this registry cannot certify an erasure."""
        problems = self.problems
        return problems[0] if problems else None


def registry_version() -> str:
    return str(gdpr_settings.DATA_OWNERS_VERSION or "")


def _registered_sections() -> set[str]:
    from stapel_core.gdpr import gdpr_registry

    return set(gdpr_registry.sections)


def _declarations() -> list[dict]:
    """Raw declaration list: DATA_OWNERS plus the legacy remote setting."""
    declared: list[dict] = []
    seen: set[str] = set()
    for entry in gdpr_settings.DATA_OWNERS or []:
        spec = {"name": entry} if isinstance(entry, str) else dict(entry)
        name = str(spec.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        declared.append(spec)
    # REMOTE_DELETION_SERVICES predates the registry; an entry there is a
    # remote owner whether or not DATA_OWNERS repeats it.
    for name in gdpr_settings.REMOTE_DELETION_SERVICES or []:
        if name in seen:
            continue
        seen.add(name)
        declared.append({"name": name, "kind": KIND_REMOTE})
    return declared


def data_owner_report() -> RegistryReport:
    """Resolve the declaration against the providers actually registered."""
    declared = _declarations()
    registered = _registered_sections()
    default_timeout = timedelta(hours=float(gdpr_settings.OWNER_TIMEOUT_HOURS or 24))

    owners: list[DataOwner] = []
    missing: list[str] = []
    for spec in declared:
        name = spec["name"]
        kind = spec.get("kind")
        if kind not in (KIND_LOCAL, KIND_REMOTE):
            # Inference is only ever a convenience for the bare-string form;
            # it can never invent a reachable owner, because a name that is
            # neither registered nor remote-confirmable lands in `missing`
            # through the local branch below.
            kind = KIND_LOCAL if name in registered else KIND_REMOTE
        hours = spec.get("timeout_hours")
        timeout = timedelta(hours=float(hours)) if hours else default_timeout
        owners.append(DataOwner(name=name, kind=kind, timeout=timeout))
        if kind == KIND_LOCAL and name not in registered:
            missing.append(name)

    undeclared = sorted(registered - {o.name for o in owners})
    return RegistryReport(
        version=registry_version(),
        owners=tuple(owners),
        missing=tuple(missing),
        undeclared=tuple(undeclared),
        unconfigured=not owners,
    )
