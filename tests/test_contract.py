"""Per-module contract triad + drift gate (contract-pipeline.md §2-3).

stapel-gdpr emits its **own** contract triad — ``docs/schema.json``
(drf-spectacular OpenAPI), ``docs/flows.json`` (the ``generate_flow_docs``
machine artifact, ``[]`` here: this module annotates no ``@flow_step``) and
``docs/errors.json`` (the ``generate_error_keys`` registry) — from a
single-module ``{gdpr + core}`` Django instance mounted at the canonical
``/gdpr/api/v1/`` prefix, plus ``docs/llms.txt`` and ``README.md`` on top.

gdpr is not mounted in stapel-example-monolith, so there is no aggregate slice
to diff against for byte-identity. Standalone validation (contract-pipeline.md
§9 fallback) substitutes: determinism, self-contained ``$ref`` closure, JWT
security on protected operations, canonical-prefix paths, and — the gate that
actually protects the frontend pair — every operation carrying a typed body
rather than a bare ``{}``.

``docs/capabilities.json`` stays hand-authored (git log: "author
capabilities.json for the stapel-catalog sweep"); only its ``surface`` section
is derived, and ``make contract`` refreshes that with ``--patch``. This file
therefore owns the triad, ``llms.txt`` and ``README.md`` — the files those
targets are allowed to regenerate.

Regenerate after any serializer/view/url/error change:

    make contract        # or: python -m stapel_gdpr._codegen --out docs

The triad harness runs in a subprocess: this test process already configured
Django (bare test urlconf) and the harness needs its own canonical-prefix
urlconf + drf-spectacular singleton — a clean interpreter is the honest way to
exercise exactly what ``make contract`` runs.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import stapel_tools  # noqa: F401  (probe: the emitter must be importable)
except ImportError as exc:  # pragma: no cover - environment failure, not a branch
    # NOT pytest.importorskip. A drift gate that skips when its emitter is
    # missing reports `1 skipped`, exits 0, and disappears among a hundred
    # green tests — making "the tool is absent" indistinguishable from "there
    # is no drift". A gate that cannot run has FAILED; it has not passed.
    raise RuntimeError(
        "contract drift gate cannot run: stapel-tools is not importable, and "
        "it carries the emitters this gate measures drift against. CI "
        "installs it; locally use the workspace venv or `pip install "
        "stapel-tools`. This is a hard failure on purpose — a skipped drift "
        "gate is silently no gate."
    ) from exc

from stapel_tools.llms_txt import load_inputs, render  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
COMMITTED = DOCS / "llms.txt"
TRIAD = ("schema.json", "flows.json", "errors.json")
#: Must match LLMS_TXT_BUDGET in the Makefile. Raised from the generator's
#: 4000 default when docs/schema.json arrived and its operation catalog put
#: the render 228 tokens over — the same deliberate exception
#: stapel-workspaces (4500), stapel-calendar (5000) and stapel-auth (8000)
#: already take. The ceiling stays enforced, just at 4500.
LLMS_TXT_BUDGET = 4500
_PY = sys.version_info[:2]
if _PY != (3, 12):
    pytest.skip(
        "stapel-gdpr contract tests require Python 3.12 (the CI/monolith pin) "
        f"— running {_PY[0]}.{_PY[1]}. drf-spectacular renders component "
        "descriptions differently across Python minors, so drift/identity "
        "checks are only defined on 3.12.",
        allow_module_level=True,
    )


def _emit(out_dir: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "stapel_gdpr._codegen", "--out", str(out_dir)],
        cwd=str(REPO),
        check=True,
        capture_output=True,
    )


def _schema() -> dict:
    return json.loads((DOCS / "schema.json").read_text())


# --- the triad ---------------------------------------------------------------


def test_triad_is_committed():
    for name in TRIAD:
        assert (DOCS / name).is_file(), f"missing docs/{name} — run `make contract`"


def test_triad_has_no_drift(tmp_path):
    _emit(tmp_path)
    for name in TRIAD:
        assert (DOCS / name).read_bytes() == (tmp_path / name).read_bytes(), (
            f"docs/{name} drifted — run `make contract` and commit docs/{name}"
        )


def test_emission_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _emit(a)
    _emit(b)
    for name in TRIAD:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_paths_carry_canonical_prefix():
    schema = _schema()
    assert schema["paths"], "schema has no paths"
    assert all(p.startswith("/gdpr/api/v1/") for p in schema["paths"])


def test_every_public_endpoint_is_in_the_schema():
    """The named surface, spelled out.

    A schema missing an endpoint is worse than no schema: the pair generates a
    client that silently has no method for it, and the omission reads as "that
    endpoint does not exist".
    """
    expected = {
        "/gdpr/api/v1/user/data-export/request",
        "/gdpr/api/v1/user/data-export/status",
        "/gdpr/api/v1/user/data-export/download",
        "/gdpr/api/v1/user/account/close",
        "/gdpr/api/v1/user/account/cancel-close",
        "/gdpr/api/v1/user/account/close/status",
        "/gdpr/api/v1/erasures",
        "/gdpr/api/v1/erasures/{request_id}",
        "/gdpr/api/v1/me/erasures",
        "/gdpr/api/v1/dsar",
        "/gdpr/api/v1/dsar/{dsar_id}",
        "/gdpr/api/v1/owners/health",
        "/gdpr/api/v1/internal/export/{request_id}/part-ready",
    }
    assert set(_schema()["paths"]) == expected


def test_the_schema_view_is_not_itself_an_operation():
    """``urls_v1`` appends ``get_app_swagger_urls``, which routes the
    drf-spectacular schema view. On drf's default it emits itself as an
    endpoint — a client method for fetching the document the client was
    generated from. The harness pins SERVE_INCLUDE_SCHEMA to False, the way
    stapel-core's canonical SPECTACULAR_SETTINGS does."""
    assert not [p for p in _schema()["paths"] if p.endswith("/schema/")]


def test_no_operation_carries_an_untyped_body():
    """Every request and every documented response resolves to a real schema.

    An operation whose body renders as ``{}`` type-checks as ``unknown`` on the
    frontend, which is the same as having no contract at all.
    """
    untyped = []
    for path, operations in _schema()["paths"].items():
        for method, op in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            body = op.get("requestBody")
            if body is not None:
                for media in body["content"].values():
                    if not media.get("schema"):
                        untyped.append(f"{method.upper()} {path} request")
            for status, response in op.get("responses", {}).items():
                # 204 legitimately carries no content.
                for media in (response.get("content") or {}).values():
                    if not media.get("schema"):
                        untyped.append(f"{method.upper()} {path} -> {status}")
    assert not untyped, f"untyped bodies: {untyped}"


def test_flows_are_empty_no_flow_step_annotations():
    assert json.loads((DOCS / "flows.json").read_text()) == []


def _all_refs(obj) -> set:
    return set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(obj)))


def test_schema_refs_are_self_contained():
    schema = _schema()
    comps = schema.get("components", {}).get("schemas", {})
    seen: set = set()
    stack = list(_all_refs(schema["paths"]))
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in comps:
            stack.extend(_all_refs(comps[name]))
    dangling = seen - set(comps)
    assert not dangling, f"dangling $ref(s): {dangling}"


def test_protected_paths_carry_jwt_security():
    """Every operation offers JWTCookieAuth. The DSAR intake additionally
    offers the anonymous alternative (``{}``), because a public privacy form
    cannot require a login — but it still accepts a session when there is one.
    """
    missing = []
    for path, operations in _schema()["paths"].items():
        for method, op in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            security = op.get("security") or []
            if not any("JWTCookieAuth" in entry for entry in security):
                missing.append(f"{method.upper()} {path}")
    assert not missing, f"operations missing JWTCookieAuth security: {missing}"


def test_errors_json_agrees_with_the_standalone_gate():
    """``tests/test_error_keys.py`` regenerates docs/errors.json by calling
    ``generate_error_keys`` inside the SUITE's Django instance; the harness
    emits it from its own. Both gates passing is the check that the two
    instances register the same error registry — a divergence there means the
    published catalog depends on which rig you ran."""
    entries = json.loads((DOCS / "errors.json").read_text())
    assert entries and {e["code"] for e in entries} >= {
        "error.404.gdpr.export_not_found",
        "error.409.gdpr.export_cooldown",
    }


# --- docs/llms.txt — the fifth artifact (badge-canon §3) ---------------------


def test_llms_txt_committed():
    assert COMMITTED.is_file(), (
        "docs/llms.txt is missing — run `make contract` and commit it"
    )


def test_llms_txt_has_no_drift():
    rendered = render(load_inputs(REPO), budget=LLMS_TXT_BUDGET)
    assert COMMITTED.read_text() == rendered, (
        "docs/llms.txt is stale — run `make contract` and commit it"
    )


def test_llms_txt_emission_is_deterministic():
    """Two independent emissions are byte-identical (drift gate is meaningful)."""
    a = render(load_inputs(REPO), budget=LLMS_TXT_BUDGET)
    b = render(load_inputs(REPO), budget=LLMS_TXT_BUDGET)
    assert a == b


# --- README.md — the sixth artifact (tracker #257) ---------------------------
#
# README.md is assembled by ``stapel_tools.readme`` from docs/readme.md (the
# human half: what this module is and how to think about it) plus the contract
# documents above (badges, version, surface counts, doc links). Everything a
# hand-written README used to restate — and therefore used to get wrong one
# release later — is generated here and gated below.

def test_readme_is_assembled_and_has_no_drift():
    from stapel_tools.readme import load_inputs, render, static_languages

    inputs = load_inputs(REPO)
    languages = static_languages(REPO)
    assert languages == ["en"], "expected exactly the English static body docs/readme.md"
    committed = (REPO / "README.md").read_text()
    assert committed == render(REPO, inputs, "en", languages), (
        "README.md drifted — run `make contract` and commit README.md "
        "(edit prose in docs/readme.md, never README.md itself)"
    )


def test_readme_version_matches_the_package():
    """The #226 gate, at the point where the number is published.

    A capabilities.json whose version lags pyproject.toml is exactly the
    defect tracked as #226; the generator refuses to render around it, so
    this test fails loudly rather than shipping a README stating a version
    the wheel does not have.
    """
    import tomllib

    from stapel_tools.readme import load_inputs, resolve_version

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert resolve_version(load_inputs(REPO)) == pyproject["project"]["version"]
