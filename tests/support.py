"""Deployment wiring the tests stand in for.

The library now fails closed on two things a real deployment must provide: a
session-revocation seam and a declared data-owner inventory. The helpers here
are that deployment's half, so the suite exercises the *configured* path by
default and the fail-closed path only where a test says so explicitly.
"""

#: Every user id whose sessions the fake revoker was asked to revoke.
revoked_users: list[str] = []


def record_revocation(user) -> None:
    """Stand-in for stapel-auth's ``SessionService.revoke_all``."""
    revoked_users.append(str(user.pk))


def no_op_erasure(user) -> None:
    """An identity-erasure strategy that reports success and does nothing.

    The shape the audit found in a provider's ``anonymize()``. Used to prove
    the orchestrator verifies the result instead of taking the strategy's
    word for it.
    """


def gdpr_conf(**overrides) -> dict:
    """STAPEL_GDPR with the baseline wiring, plus per-test overrides."""
    conf = {"SESSION_REVOKER": "tests.support.record_revocation"}
    conf.update(overrides)
    return conf
