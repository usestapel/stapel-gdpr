"""Opening an erasure from another service — one dotted path for owner libs.

``gdpr_orchestrator.request_erasure`` is an in-process call. In a monolith
that is the whole story; in a fleet the owner that DETECTS the need for an
erasure is almost never the service that runs this module — stapel-recordings'
``purge_soft_deleted_recordings``, a host's delete view in some other
container — and it cannot import its way here.

So the intake has two doors (``gdpr.erasure.open`` as an Action,
``gdpr.erasure.request`` as a Function), and this module is the one thing an
owner library has to know about: :func:`request_erasure` picks the door by
deployment, and :class:`CommErasureClient` is the same choice packaged as the
object an owner's ``ERASURE_CLIENT`` seam instantiates. A host wires

    STAPEL_RECORDINGS = {"ERASURE_CLIENT": "stapel_gdpr.client.CommErasureClient"}

and the same recordings container works in a monolith and in a fleet: which
transport carries the request is ``STAPEL_COMM["FUNCTION_TRANSPORT"]``, which
is deployment configuration, not code.

The rule, in one line: **the Function when a transport is configured, the
in-process orchestrator otherwise.** ``FUNCTION_TRANSPORT`` at its default
``"inprocess"`` means there is no RPC to make — either this process has the
orchestrator or nobody does, and going through ``call()`` would only add a
registry lookup and a wrapped exception on the way to the same object.
"""
import logging

logger = logging.getLogger(__name__)

#: The Function this helper calls when a transport is configured.
ERASURE_REQUEST_FUNCTION = "gdpr.erasure.request"

#: The fire-and-forget Action, for a caller that wants no answer at all.
#: Emit it yourself (``stapel_core.comm.emit``) when you do not need the id.
ERASURE_OPEN_ACTION = "gdpr.erasure.open"


def _remote() -> bool:
    """Is a Function transport configured, i.e. is the provider elsewhere?"""
    from stapel_core.comm import comm_setting

    return str(comm_setting("FUNCTION_TRANSPORT", "inprocess") or "inprocess") != "inprocess"


def _orchestrator():
    """``gdpr_orchestrator`` when this module runs in THIS process, else None.

    Two failure shapes, one meaning: ``ImportError`` when the package is not
    installed, ``RuntimeError`` when it is importable but absent from
    ``INSTALLED_APPS``, so its models have no app registry to belong to.
    """
    try:
        from .orchestrator import gdpr_orchestrator
    except (ImportError, RuntimeError):  # pragma: no cover - env failure
        return None
    return gdpr_orchestrator


def request_erasure(
    subject_type: str,
    subject_key: str,
    *,
    workspace_id: str | None = None,
    requested_by: str | None = None,
    origin: str = "user",
    idempotency_key: str = "",
) -> dict:
    """Open an erasure for one subject, wherever this module happens to run.

    Returns ``{"request_id", "due_at", "state"}`` — the same three values on
    both paths, so a caller never learns which transport answered.

    Pass ``idempotency_key`` from anything that can ask twice (a retry, a
    daily sweep, an at-least-once redelivery): the same key returns the
    request that already exists instead of a second erasure of one subject.

    Raises ``ValueError`` for an unknown ``subject_type`` in-process; over a
    transport the same refusal arrives wrapped as
    ``stapel_core.comm.FunctionCallError``, because that is what a Function
    failure is on the wire.
    """
    payload = {
        "subject_type": str(subject_type),
        "subject_key": str(subject_key),
        "workspace_id": str(workspace_id or ""),
        "requested_by": str(requested_by or ""),
        "origin": str(origin or "user"),
        "idempotency_key": str(idempotency_key or ""),
    }

    if _remote():
        from stapel_core.comm import call

        return call(ERASURE_REQUEST_FUNCTION, payload)

    from .functions import open_from_payload

    request = open_from_payload(payload)
    return {
        "request_id": request.pk,
        "due_at": request.due_at.isoformat(),
        "state": request.state,
    }


class CommErasureClient:
    """The ``ERASURE_CLIENT`` an owner library points at.

    Duck-typed against the seam stapel-recordings declares
    (``available`` / ``has_open_erasure`` / ``request_erasure``) rather than
    subclassing its ABC — modules never import each other, and an owner
    library must not become a dependency of the module it reports to.

    Unlike the owner's own default client, this one works in BOTH deployments:
    it is the in-process orchestrator in a monolith and the
    ``gdpr.erasure.request`` Function once a transport is configured.
    """

    #: Erasures opened through this client carry a deterministic key derived
    #: from the subject, so an owner's daily sweep asking again about the same
    #: recording gets the same request back rather than a fresh one. Requests
    #: opened by any OTHER path (the API, an account closure, the restore
    #: re-queue) carry no key and are unaffected.
    key_prefix = "owner"

    def idempotency_key(self, subject_type: str, subject_key: str) -> str:
        """The de-duplication key for one subject. Override to scope it."""
        return f"{self.key_prefix}:{subject_type}:{subject_key}"

    def available(self) -> bool:
        """Can this client open erasures right now?

        True when a Function transport is configured (the provider is another
        service, and whether it answers is a call-time failure, not a wiring
        one) or when this process carries the orchestrator itself.
        """
        return _remote() or _orchestrator() is not None

    def has_open_erasure(self, subject_type: str, subject_key: str) -> bool:
        """Is an erasure for this subject already in flight?

        Answered from the database in-process. Over a transport it answers
        **False** on purpose: there is no read Function for this question, and
        the honest alternative — inventing one — is not what keeps the caller
        safe. :meth:`request_erasure` does, by sending
        :meth:`idempotency_key`; asking again returns the SAME request, so a
        sweep that re-asks every day still opens exactly one erasure per
        subject. The visible difference is a counter (a caller tallies
        "requested" where a local client would tally "already open"), not a
        duplicate.
        """
        if _remote():
            return False
        orchestrator = _orchestrator()
        if orchestrator is None:  # pragma: no cover - callers check available()
            return False
        from .models import ErasureRequest

        return ErasureRequest.objects.filter(
            subject_type=subject_type,
            subject_key=str(subject_key),
            state__in=(ErasureRequest.STATE_QUEUED, ErasureRequest.STATE_ERASING),
        ).exists()

    def request_erasure(
        self,
        subject_type: str,
        subject_key: str,
        *,
        workspace_id: str | None = None,
    ) -> str | None:
        """Open one erasure; returns its request id as a string."""
        result = request_erasure(
            subject_type,
            str(subject_key),
            workspace_id=workspace_id,
            # The user's own delete started this clock; an owner's retention
            # sweep only opens the request when the window closes.
            origin="user",
            idempotency_key=self.idempotency_key(subject_type, str(subject_key)),
        )
        request_id = (result or {}).get("request_id")
        return str(request_id) if request_id is not None else None


__all__ = [
    "CommErasureClient",
    "ERASURE_OPEN_ACTION",
    "ERASURE_REQUEST_FUNCTION",
    "request_erasure",
]
