"""gdpr.W015 / gdpr.W016 — two seams a host has to wire, said at boot.

W015: without AccountClosureGuardMiddleware an account in its closure grace
period, or being erased, keeps using every endpoint outside this library.
W016: a notification type this library requests that the installed
stapel-notifications does not register is dropped as unknown — the DSAR
acknowledgement among them, while the row says ACKNOWLEDGED.
"""
import sys
import types

from stapel_gdpr.checks import (
    CLOSURE_GUARD,
    REQUESTED_NOTIFICATION_TYPES,
    check_closure_guard_wired,
    check_requested_notification_types,
)


def _ids(results):
    return [r.id for r in results]


def test_w015_fires_without_the_closure_guard(settings):
    settings.MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
    assert _ids(check_closure_guard_wired()) == ["gdpr.W015"]


def test_w015_is_silent_with_the_closure_guard(settings):
    settings.MIDDLEWARE = ["django.middleware.common.CommonMiddleware", CLOSURE_GUARD]
    assert check_closure_guard_wired() == []


def _fake_notifications(monkeypatch, types_known):
    from django.apps import apps

    routing = types.ModuleType("stapel_notifications.routing")
    routing.registered_types = lambda: sorted(types_known)
    pkg = types.ModuleType("stapel_notifications")
    pkg.routing = routing
    monkeypatch.setitem(sys.modules, "stapel_notifications", pkg)
    monkeypatch.setitem(sys.modules, "stapel_notifications.routing", routing)
    real = apps.is_installed
    monkeypatch.setattr(
        apps, "is_installed",
        lambda name: name == "stapel_notifications" or real(name),
    )


def test_w016_names_the_unregistered_dsar_types(monkeypatch):
    _fake_notifications(monkeypatch, {"gdpr.export_ready", "gdpr.inactivity_warning",
                                      "gdpr.inactivity_closed"})
    results = check_requested_notification_types()
    assert _ids(results) == ["gdpr.W016"]
    assert "gdpr.dsar.received" in results[0].msg
    assert "gdpr.dsar.opened" in results[0].msg


def test_w016_is_silent_when_every_type_is_registered(monkeypatch):
    _fake_notifications(monkeypatch, set(REQUESTED_NOTIFICATION_TYPES))
    assert check_requested_notification_types() == []


def test_w016_is_silent_without_notifications_in_process():
    # This suite does not install stapel-notifications: a fleet whose
    # notifications run elsewhere cannot be asked from here.
    assert check_requested_notification_types() == []


def test_both_checks_are_registered():
    from django.core.checks import registry

    names = {c.__name__ for c in registry.registry.registered_checks}
    assert {"check_closure_guard_wired", "check_requested_notification_types"} <= names
