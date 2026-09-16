"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim. stapel-alerts 0.2.0
shipped ``GET /issues`` declared as ``Issue[]`` while the wire carried
``{count, offset, limit, results}``: the drift gate was green and the
frontend pair rendered ``undefined``.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body
it gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the family
  of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* an operation that genuinely cannot run in-process is listed by name in
  ``UNDRIVABLE`` with a one-line reason. That list is asserted to be exactly
  current: a stale entry, or a missing reason, fails;
* a collection that comes back empty fails in the populated pass — an empty
  array validates against any item schema, so an empty answer is a check that
  looked at nothing;
* every read, and every write with a nullable answer, is driven a SECOND time
  in its emptiest legal state (``EMPTY_STATE``). Every null finding in the
  first wave of this gate was there: an ``exp`` null for every active token, a
  counter null for every account without the feature, a ``created_at`` null
  for every account that had just signed up.

Runs on every interpreter: it reads the committed schema and never emits.

THE MOUNT. ``codegen_urls.py`` mounts ``gdpr/api/`` and the module's own
``urls.py`` contributes ``v1/``, so the document is written against
``/gdpr/api/v1/…``. ``tests/urls.py`` mounts the SAME prefix, so unlike five
of the first eight libraries in this wave this module's suite was already
looking where its document points. The emission mount is declared here anyway
(``test_every_declared_path_resolves_under_this_urlconf`` asserts against it),
so a later edit to ``tests/urls.py`` cannot silently unhook the contract.

What it found on its first run: 13 of 13 declared operations driven, 11 of
them a second time in their emptiest state, 0 red. The claims this module
makes about its own wire are honest — including every ``nullable`` one and
the two REQUIRED-and-nullable ones (``DsarStatusDTO.ack_sent_at``,
``ErasureStatusDTO.workspace_id``) that are exactly where the first wave's
lies lived — and ``test_the_gate_is_not_blind`` proves that is a finding
rather than a gate that never looked: it re-validates every driven body
against ``{"type": "string"}`` and requires all of them to fail.

Two operations declare a 2xx that carries no ``application/json`` body and
are therefore outside this gate by construction, not by exemption:
``POST /internal/export/{request_id}/part-ready`` (204) and
``POST /user/data-export/download`` (200 ``application/zip``).
"""
import copy
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import include, path as url_path
from django.utils import timezone
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client
#: (``codegen_urls.py``: ``gdpr/api/`` + the module's own ``v1/``).
urlpatterns = [
    url_path("gdpr/api/", include("stapel_gdpr.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/gdpr/api/v1"

#: The export store's root for the duration of a test — set by the autouse
#: fixture below so the recipes, which are module-level functions, can reach
#: the per-test ``tmp_path`` without taking it as an argument.
_EXPORT_ROOT = ""


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    """Pin every root the export machinery can write through.

    ``MEDIA_ROOT`` is set to a ``mkdtemp`` in ``conftest.py``, but the value
    is process-wide and shared with every other module in the run; pinned per
    test it cannot leak between them. ``EXPORT_ROOT`` matters more here: an
    export runs inline (celery eager) on three of these recipes and assembles
    a real ZIP, and with no ``BASE_DIR`` in the harness settings the store
    falls back to a system temp directory that nothing ever cleans.
    """
    global _EXPORT_ROOT
    _EXPORT_ROOT = str(tmp_path / "private-gdpr")
    with override_settings(MEDIA_ROOT=str(tmp_path / "media")):
        yield
    _EXPORT_ROOT = ""


@pytest.fixture(autouse=True)
def _deployment():
    """The wiring a real deployment provides, and one budget stood down.

    ``SESSION_REVOKER`` and ``DATA_OWNERS`` are the two things the library
    fails closed on: without the first a closure answers 503 by design, and
    without the second ``owners/health`` has no row to describe. Both are
    deployment facts, not response shapes.

    ``INTAKE_RATE_LIMIT_PER_HOUR`` is a clock, not a shape: the anonymous
    DSAR intake is budgeted per IP in the process-wide locmem cache, which
    every other module in the run shares, so a 429 here would depend on what
    ran before. Its own suite (``tests/test_intake_budget.py``) pins that
    behaviour; this gate is about the 201.
    """
    with override_settings(
        STAPEL_GDPR={
            "SESSION_REVOKER": "tests.support.record_revocation",
            "DATA_OWNERS": {"auth": ["account"], "media": ["account", "file"]},
            "DATA_OWNERS_VERSION": "wire-contract-1",
            "SUBJECT_TYPES": ["account", "file", "recording"],
            "INTAKE_RATE_LIMIT_PER_HOUR": 0,
            "EXPORT_ROOT": _EXPORT_ROOT,
        }
    ):
        yield


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergences that matter here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type``;
    JSON Schema has no such keyword and would refuse the null — which is
    exactly the value most of these fields answer in their empty state.
    Everything else drf-spectacular emits here (``$ref``, ``allOf``, ``enum``,
    ``required``, ``additionalProperties``) is JSON Schema as written.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def anonymous():
    return APIClient()


def make_user(**kwargs):
    User = get_user_model()
    defaults = dict(
        username=_unique("wire-"),
        email=f"{_unique('wire-')}@example.com",
        password="wire-contract-password-7",
    )
    defaults.update(kwargs)
    return User.objects.create_user(**defaults)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def staff():
    """The default ``ERASURE_AUTHORIZER`` and ``IsAdminUser`` are both
    ``is_staff``, so one kind of privileged caller covers both doors."""
    return make_user(is_staff=True)


def no_acknowledgement():
    """Stand the acknowledgement mail down for one intake.

    ``ack_sent_at`` is a REQUIRED nullable field, and the ONLY state it is
    null in is an intake whose notification could not be requested —
    ``_acknowledge`` records the send, not the attempt (dsar.py:58-80). That
    is the shape this gate exists to look at, so the failure is produced
    rather than waited for.
    """
    return patch(
        "stapel_core.notifications.request_notification",
        side_effect=RuntimeError("notifications are down"),
    )


def make_dsar(client=None, **data):
    """One DSAR through the real intake, returning its id."""
    payload = {"kind": "access", "email": f"{_unique('subject-')}@example.com"}
    payload.update(data)
    response = (client or anonymous()).post(f"{V1}/dsar", payload, format="json")
    assert response.status_code == 201, response.content
    return response.json()["request_id"]


def open_erasure(actor, subject_type="account", **extra):
    """One erasure through the real endpoint, returning ``(actor, id)``."""
    payload = {"subject_type": subject_type, "subject_key": _unique("subject-")}
    payload.update(extra)
    response = client_for(actor).post(f"{V1}/erasures", payload, format="json")
    assert response.status_code == 202, response.content
    return response.json()["request_id"]


def close_account(user):
    """Close one account and hand back its single-purpose closure token."""
    response = client_for(user).post(f"{V1}/user/account/close", {}, format="json")
    assert response.status_code == 202, response.content
    return response.json()["closure_token"]


def make_owner_health(owner, **kwargs):
    from stapel_gdpr.models import DataOwnerHealth

    defaults = dict(
        last_alive_at=timezone.now(),
        last_probe_at=timezone.now(),
        declared_subject_types=["account"],
        answered_subject_types=["account"],
    )
    defaults.update(kwargs)
    return DataOwnerHealth.objects.create(owner=owner, **defaults)


def make_obligation(erasure_id, provider="openai", window_days=30):
    from stapel_gdpr.models import ErasureRequest, SubprocessorObligation

    return SubprocessorObligation.objects.create(
        request=ErasureRequest.objects.get(pk=erasure_id),
        provider=provider,
        window_days=window_days,
        due_at=timezone.now() + timedelta(days=window_days),
    )


def receipt(erasure_id, owner):
    """Mark one owner's receipt slot DONE, so ``parts`` carries a real one."""
    from stapel_gdpr.models import ErasurePart, ErasureRequest

    part = ErasurePart.objects.get(request_id=erasure_id, owner=owner)
    part.state = ErasurePart.STATE_DONE
    part.receipt_at = timezone.now()
    part.receipt_id = f"{owner}:job-8812"
    part.counts = {owner: 3}
    part.save()
    return ErasureRequest.objects.get(pk=erasure_id)


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def __call__(self, client, params=None, data=None, query="", **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        send = getattr(client, self.method.lower())
        if self.method in ("GET", "DELETE"):
            return send(url + query, **extra)
        return send(url + query, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template, status code)``. ``code`` is
#: ``None`` for the usual case of one 2xx per operation.
RECIPES = {}

#: The same operations again, in the emptiest state the contract still has to
#: describe: no rows, or the one row the operation addresses carrying none of
#: its optional values. A populated answer cannot say what a field holds when
#: there is nothing to hold, and that is where every null finding in the first
#: wave of this gate was.
EMPTY_STATE = {}


def recipe(method, path, code=None, table=None):
    def register(fn):
        target = RECIPES if table is None else table
        key = (method, V1 + path, code)
        assert key not in target, f"duplicate recipe for {method} {path} {code}"
        target[key] = fn
        return fn

    return register


def empty_state(method, path, code=None):
    return recipe(method, path, code, table=EMPTY_STATE)


#: Operations that cannot be driven in-process, by name and with the reason.
#: A short, visible list is acceptable here; a silent skip is not.
#:
#: EMPTY. Every operation this module declares with a JSON body is reachable
#: from a test client with no seam stood in for: the module calls no sibling
#: by name on any of these paths, and the one deployment seam it does have
#: (``SESSION_REVOKER``) is wired to the suite's own recorder, exactly as a
#: deployment wires stapel-auth's.
UNDRIVABLE: dict = {}


# ── DSAR intake and triage ───────────────────────────────────────────────────


@recipe("POST", "/dsar", code=201)
def _dsar_create(call):
    """An account-matched access request: intake starts the export that
    answers it, so ``export_request_id`` is filled and ``channel`` is app."""
    user = make_user()
    return call(client_for(user), data={"kind": "access", "note": "everything you hold"})


@empty_state("POST", "/dsar", code=201)
def _dsar_create_bare(call):
    """The public form: no account behind the address, so nothing is wired —
    null ``erasure_request_id``, null ``export_request_id``, empty ``note`` —
    and with the acknowledgement mail down, the null ``ack_sent_at`` that is
    REQUIRED in the declaration and unreachable any other way."""
    with no_acknowledgement():
        return call(
            anonymous(),
            data={"kind": "rectification", "email": f"{_unique('subject-')}@example.com"},
        )


@recipe("GET", "/dsar")
def _dsar_list(call):
    make_dsar()
    return call(client_for(staff()))


@empty_state("GET", "/dsar")
def _dsar_list_empty(call):
    """A deployment nobody has filed against — the array is genuinely []."""
    return call(client_for(staff()))


@recipe("GET", "/dsar/{dsar_id}")
def _dsar_get(call):
    user = make_user()
    dsar_id = make_dsar(client_for(user), kind="access", note="matched to account")
    return call(client_for(staff()), params={"dsar_id": dsar_id})


@empty_state("GET", "/dsar/{dsar_id}")
def _dsar_get_bare(call):
    with no_acknowledgement():
        dsar_id = make_dsar(kind="rectification")
    return call(client_for(staff()), params={"dsar_id": dsar_id})


@recipe("PATCH", "/dsar/{dsar_id}")
def _dsar_patch(call):
    dsar_id = make_dsar()
    return call(
        client_for(staff()),
        params={"dsar_id": dsar_id},
        data={"state": "in_progress", "note": "triaged by the wire gate"},
    )


@empty_state("PATCH", "/dsar/{dsar_id}")
def _dsar_patch_clears(call):
    """Clearing the note on an unacknowledged form request: the declared
    answer is the empty string, not null, and ``ack_sent_at`` stays null."""
    with no_acknowledgement():
        dsar_id = make_dsar(kind="portability", note="a typo")
    return call(client_for(staff()), params={"dsar_id": dsar_id}, data={"note": ""})


# ── subject-scoped erasure ───────────────────────────────────────────────────


@recipe("POST", "/erasures", code=202)
def _erasure_open(call):
    """An account subject: both declared owners claim it, so the response
    carries two real receipt slots."""
    return call(
        client_for(staff()),
        data={
            "subject_type": "account",
            "subject_key": _unique("subject-"),
            "workspace_id": "ws-42",
        },
    )


@empty_state("POST", "/erasures", code=202)
def _erasure_open_unclaimed(call):
    """A subject type no declared owner claims, opened with no workspace:
    empty ``parts``, empty ``obligations``, and the null ``workspace_id``
    that the declaration marks REQUIRED."""
    return call(
        client_for(staff()),
        data={"subject_type": "recording", "subject_key": _unique("subject-")},
    )


@recipe("GET", "/erasures/{request_id}")
def _erasure_status(call):
    actor = staff()
    erasure_id = open_erasure(actor, workspace_id="ws-42")
    receipt(erasure_id, "auth")
    make_obligation(erasure_id)
    return call(client_for(actor), params={"request_id": erasure_id})


@empty_state("GET", "/erasures/{request_id}")
def _erasure_status_empty(call):
    """Just opened, nothing claimed, nothing answered: null ``workspace_id``,
    null ``completed_at``, null ``grace_ends_at``, and every part still
    carrying the null ``receipt_at`` its declaration marks REQUIRED."""
    actor = staff()
    return call(
        client_for(actor),
        params={"request_id": open_erasure(actor, subject_type="recording")},
    )


@recipe("GET", "/me/erasures")
def _my_erasures(call):
    """The list is keyed on who OPENED the erasure, and opening one is
    staff-only under the default ``ERASURE_AUTHORIZER``."""
    actor = staff()
    erasure_id = open_erasure(actor, workspace_id="ws-42")
    receipt(erasure_id, "media")
    return call(client_for(actor))


@empty_state("GET", "/me/erasures")
def _my_erasures_empty(call):
    return call(client_for(make_user()))


# ── data-owner health ────────────────────────────────────────────────────────


@recipe("GET", "/owners/health")
def _owners_health(call):
    make_owner_health("auth")
    make_owner_health("media", declared_subject_types=["account", "file"],
                      answered_subject_types=["account", "file"])
    return call(client_for(staff()))


@empty_state("GET", "/owners/health")
def _owners_health_never_probed(call):
    """Declared owners that have never been asked and never answered — the
    null ``last_alive_at``/``last_probe_at`` pair the declaration marks
    REQUIRED, and the empty ``answered_subject_types`` beside them."""
    return call(client_for(staff()))


# ── account closure ──────────────────────────────────────────────────────────


@recipe("POST", "/user/account/close", code=202)
def _account_close(call):
    """The one answer that carries a non-null ``closure_token``."""
    return call(client_for(make_user()))


@recipe("GET", "/user/account/close/status")
def _close_status(call):
    """The token path: the closure revoked every session, so this header is
    normally the only credential the caller still holds."""
    token = close_account(make_user())
    return call(anonymous(), HTTP_X_CLOSURE_TOKEN=token)


@empty_state("GET", "/user/account/close/status")
def _close_status_by_session(call):
    """The session path, on an account still inside grace: the same body with
    the null ``closure_token`` that everything except the 202 answers."""
    user = make_user()
    close_account(user)
    return call(client_for(user))


@recipe("POST", "/user/account/cancel-close")
def _cancel_close(call):
    token = close_account(make_user())
    return call(anonymous(), HTTP_X_CLOSURE_TOKEN=token)


@empty_state("POST", "/user/account/cancel-close")
def _cancel_close_by_session(call):
    user = make_user()
    close_account(user)
    return call(client_for(user))


# ── data export ──────────────────────────────────────────────────────────────


@recipe("POST", "/user/data-export/request", code=202)
def _export_request(call):
    return call(client_for(make_user()))


@recipe("GET", "/user/data-export/status")
def _export_status(call):
    """Celery runs eager in this harness, so the export assembled inline and
    the row is READY with a real download window."""
    client = client_for(make_user())
    opened = client.post(f"{V1}/user/data-export/request", {}, format="json")
    assert opened.status_code == 202, opened.content
    return call(client)


@empty_state("GET", "/user/data-export/status")
def _export_status_pending(call):
    """A request that has not run yet: no parts, no archive, and the null
    ``expires_at`` the declaration marks REQUIRED. Written directly because
    the endpoint's own POST cannot leave it pending — the task is eager."""
    from stapel_gdpr.models import DataExportRequest

    user = make_user()
    DataExportRequest.objects.create(
        user_id=user.pk,
        status=DataExportRequest.STATUS_PENDING,
        deadline=timezone.now() + timedelta(hours=48),
    )
    return call(client_for(user))


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


#: Operations whose declared body the wire does not send.
#:
#: EMPTY, and that is the finding rather than the absence of one: 13 of 13
#: operations were driven, 11 of them twice, and every declared body held.
#: The mechanism stays because the next change will need it — an entry must
#: name the defect AND its owner, and ``strict=True`` turns a fixed one into a
#: failure until the entry is deleted, so a finding can be neither forgotten
#: nor quietly kept.
KNOWN_MISMATCHES: dict = {}


def _recipe_for(table, method, path, code):
    """The code-specific recipe if there is one, else the operation's."""
    return table.get((method, path, code)) or table.get((method, path, None))


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Five of the first eight libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: one mounted a different
    prefix AND one segment short, one mounted the paths bare, one mounted less
    than the emission did, one doubled a segment to reproduce a host's
    deployed prefix. In every case the operations were "covered" by a file
    that could not have reached a single one of them.

    That is the same family as a gate nobody asks: the recipes can all be
    written, the run can be green, and not one request went where the contract
    says it goes. A missing recipe already fails loudly; this fails when the
    MOUNT is wrong, which no per-operation check can see, because when the
    mount is wrong every operation is equally and silently unreachable.

    Asserted against the urlconf this module declares, so it fails at the one
    moment it is cheap to fix: when somebody changes a mount.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment, and this URL set uses
    # int converters. A path counts as reachable if any one shape resolves:
    # the question here is whether the mount exists, not whether an id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    missing = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if _recipe_for(RECIPES, method, path, code) is None
        and (method, path) not in UNDRIVABLE
    ]
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in missing)
    )

    declared_codes = {(m, p, c) for m, p, c, _ in OPERATIONS}
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}
    stale = sorted(
        key
        for key in RECIPES
        if (key[0], key[1]) not in declared_ops
        or (key[2] is not None and key not in declared_codes)
    )
    assert not stale, (
        "recipes for operations/status codes the contract no longer declares:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in stale)
    )
    stale_exclusions = sorted(set(UNDRIVABLE) - declared_ops)
    assert not stale_exclusions, (
        f"exclusions for operations the contract no longer declares: {stale_exclusions}"
    )
    both = sorted((m, p) for m, p, _c in RECIPES if (m, p) in UNDRIVABLE)
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"

    # RECIPES ∪ UNDRIVABLE is EXACTLY the declared set, in both directions.
    covered = {(m, p) for m, p, _c in RECIPES} | set(UNDRIVABLE)
    assert covered == declared_ops, (
        "the covered set and the declared set differ:\n"
        f"  declared and not covered: {sorted(declared_ops - covered)}\n"
        f"  covered and not declared: {sorted(covered - declared_ops)}"
    )


def test_every_read_is_also_driven_in_its_emptiest_state():
    """A populated answer cannot say what a field holds when there is nothing.

    Every null finding in the first wave of this gate was on the empty state.
    A gate that only ever seeds three rows and asks never sees any of them.

    So every GET is required to have an ``EMPTY_STATE`` recipe as well, and so
    is every write whose declared body has a nullable field. The exemptions
    are named here, each with its reason.
    """
    exempt = {
        # The ONLY answer in this contract that carries a non-null
        # `closure_token`: the 202 mints it once and no other state of this
        # operation exists. Its null half is driven on both closure reads.
        ("POST", V1 + "/user/account/close"),
        # ExportRequestDTO has three REQUIRED, non-nullable fields and no
        # optional one — there is no emptier state for it to answer in.
        ("POST", V1 + "/user/data-export/request"),
    }
    reads = {
        (method, path)
        for method, path, _code, _schema in OPERATIONS
        if method == "GET"
    } | {
        (method, path)
        for method, path, _code, schema in OPERATIONS
        if method != "GET" and '"nullable": true' in json.dumps(
            _resolved(schema), sort_keys=True
        )
    }
    covered = {(m, p) for m, p, _c in EMPTY_STATE}
    missing = sorted(reads - covered - exempt)
    assert not missing, (
        "operations driven only against a populated database — the state where "
        "every null claim in this gate's history was found is unchecked:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}
    stale = sorted({(m, p) for m, p, _c in EMPTY_STATE} - declared_ops)
    assert not stale, f"empty-state recipes for undeclared operations: {stale}"


def _resolved(node, _depth=0):
    """A response schema with its component ``$ref``s inlined, one level at a
    time — enough to see whether the body it describes has a nullable field."""
    if _depth > 6:
        return node
    if isinstance(node, list):
        return [_resolved(item, _depth + 1) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        target = SCHEMA["components"]["schemas"].get(name, {})
        return _resolved(target, _depth + 1)
    return {k: _resolved(v, _depth + 1) for k, v in node.items()}


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason.

    Without this, an operation that is renamed or removed leaves an entry that
    silences nothing and reads like a known problem forever.
    """
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for key, reason in KNOWN_MISMATCHES.items():
        assert key in declared, (
            f"{key} is recorded as a known mismatch but the contract no longer "
            "declares it — delete the entry"
        )
        assert reason and reason.strip(), f"{key} is recorded with no reason"


def _drive(table, method, path, code, body_schema, *, expect_rows):
    perform = _recipe_for(table, method, path, code)
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    response = perform(Call(method, path))
    assert response.status_code == code, (
        f"{method} {path}: expected the declared {code}, got "
        f"{response.status_code}: {response.content[:400]}"
    )

    body = response.json()
    errors = sorted(_validator(body_schema).iter_errors(body), key=lambda e: list(e.path))
    assert not errors, (
        f"{method} {path} answers a body the contract does not describe:\n"
        + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
        + f"\n  body: {json.dumps(body)[:600]}"
    )
    # An empty list validates against any item schema, so a collection must
    # actually carry a row for the check to have looked at anything.
    if expect_rows and isinstance(body, list):
        assert body, f"{method} {path}: the declared collection came back empty"
    return body


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(RECIPES, method, path, code, body_schema, expect_rows=True)


_EMPTY_OPERATIONS = [
    (method, path, code, schema)
    for method, path, code, schema in OPERATIONS
    if _recipe_for(EMPTY_STATE, method, path, code) is not None
]


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    _EMPTY_OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in _EMPTY_OPERATIONS],
)
def test_the_wire_matches_the_declared_response_when_there_is_nothing_there(
    method, path, code, body_schema, request
):
    """The same claim, asked in the state where the nulls live."""
    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(EMPTY_STATE, method, path, code, body_schema, expect_rows=False)


def test_the_gate_is_not_blind():
    """A canary: swap a declared schema for one the wire cannot satisfy.

    Everything above can be green for two reasons — the claims are honest, or
    the check never looks at the body. This tells them apart by validating a
    real response against ``{"type": "string"}``: every operation here answers
    an object or an array, so every one of them must fail. If any passes, the
    validation in ``_drive`` is not reaching the received body and this whole
    file proves nothing. With ``KNOWN_MISMATCHES`` empty this covers the
    entire declared surface.
    """
    honest = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if (method, path) not in KNOWN_MISMATCHES and (method, path) not in UNDRIVABLE
    ]
    assert honest, "nothing left to canary"

    survivors = []
    for method, path, code in honest:
        try:
            _drive(RECIPES, method, path, code, {"type": "string"}, expect_rows=False)
        except AssertionError:
            continue
        survivors.append(f"{method} {path}")
    assert not survivors, (
        "these operations passed validation against {'type': 'string'} — the "
        "gate is not looking at the body it received:\n  " + "\n  ".join(survivors)
    )
