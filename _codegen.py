"""stapel-gdpr contract-emission harness (contract-pipeline.md §2-3).

Emits the module's own contract triad into ``docs/`` from a single-module
``{gdpr + core}`` Django instance mounted at the canonical ``/gdpr/api/v1``
prefix (see ``_codegen_settings.py`` / ``codegen_urls.py``):

  docs/schema.json   drf-spectacular OpenAPI, this module only, canonical prefix
  docs/flows.json    generate_flow_docs machine artifact ([] — no @flow_step here)
  docs/errors.json   generate_error_keys registry

The *mechanism* is stapel_tools.codegen (unchanged, shared across the fleet);
this file is the thin per-module *config* that wires this module's settings +
canonical mount into it. Until this existed, ``docs/`` carried only the
capabilities/llms/readme half, and the frontend pair had no schema to generate
a typed client from.

``docs/errors.json`` was already committed and gated by
``tests/test_error_keys.py`` (which calls ``generate_error_keys`` directly);
emitting it here too is deliberate — the triad is emitted from one instance,
and the two gates agreeing is the check that the harness configures the same
error registry the suite does.

Usage:
    python -m stapel_gdpr._codegen --out docs        # `make contract`
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _configure() -> None:
    """Configure + boot the single-module Django instance for emission."""
    repo_root = os.path.dirname(os.path.abspath(__file__))
    # `python -m` prepends cwd to sys.path; this package is flat-layout, so
    # the repo root would also expose `views`, `models`, `conf` … as top-level
    # modules. Strip it the way the flat-layout conftests do.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != repo_root]

    from django.conf import settings

    if not settings.configured:
        from stapel_gdpr._codegen_settings import settings_kwargs

        settings.configure(
            **settings_kwargs(root_urlconf="stapel_gdpr.codegen_urls", contract=True)
        )

    import django

    django.setup()

    from drf_spectacular.settings import spectacular_settings

    from stapel_gdpr._codegen_settings import CODEGEN_SCHEMA_PATH_PREFIX

    # drf-spectacular froze its settings singleton at import time (before
    # configure() ran), so it is on drf defaults. The one knob to force is
    # SCHEMA_PATH_PREFIX: left None, drf derives the operationId prefix from
    # the common path of every endpoint — "/" across a multi-module aggregate
    # but "/gdpr/api/v1" in a single-module harness, which would strip the
    # names down to bare `retrieve`/`create`. Pin it to the aggregate
    # convention.
    spectacular_settings.SCHEMA_PATH_PREFIX = CODEGEN_SCHEMA_PATH_PREFIX

    # Same class of forcing, second knob. Unlike every other pair-backend,
    # this module's own urls_v1 appends `get_app_swagger_urls(...)`, so the
    # drf-spectacular schema *view* is itself a routed endpoint here. Left on
    # drf's default (True), it emits itself as `/gdpr/api/v1/gdpr/schema/` —
    # an operation the frontend generator would turn into a client method for
    # fetching the very document it was generated from. stapel-core's canonical
    # SPECTACULAR_SETTINGS pins this to False, so a real deployment does not
    # publish it either; the harness cannot read those settings (frozen
    # singleton), so it states the same thing here.
    spectacular_settings.SERVE_INCLUDE_SCHEMA = False

    # A real multi-module host registers drf-spectacular's JWT cookie-auth
    # extension as a side effect of wiring its Swagger URLs — a global
    # registration, not tied to any one module's urls.py. This module's own
    # urls_v1 happens to trigger it via get_app_swagger_urls, but the emitted
    # `security: [{"JWTCookieAuth": []}]` entries are too load-bearing to rest
    # on that side effect: register explicitly.
    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    _register_jwt_auth_extension()


def _require_python_312() -> None:
    """Abort emission if not running the pinned 3.12 interpreter.

    drf-spectacular's rendering of component descriptions (``Optional[X]`` vs
    ``X | None``) depends on the Python minor version — contracts emitted on
    anything other than 3.12 (the CI/monolith pin) produce false diffs against
    the committed docs/*.json.
    """
    if sys.version_info[:2] != (3, 12):
        got = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise SystemExit(
            f"stapel-gdpr contract emission ABORTED: running Python {got}, but "
            "contracts must be emitted on Python 3.12 (the CI/monolith pin). "
            "drf-spectacular renders component descriptions (Optional[X] vs "
            "X | None) differently across Python minor versions, so emitting on "
            "any other minor produces false diffs against the committed "
            "docs/*.json. Re-run under a 3.12 interpreter."
        )


def main(argv: list[str] | None = None) -> int:
    _require_python_312()

    parser = argparse.ArgumentParser(
        prog="stapel-gdpr-contract",
        description="Emit this module's contract triad (schema.json + flows.json "
        "+ errors.json) into --out, canonical /gdpr/api/v1 prefix.",
    )
    parser.add_argument(
        "--out",
        default="docs",
        help="Output directory for the triad (default: docs).",
    )
    args = parser.parse_args(argv)

    _configure()

    from stapel_tools.codegen import emit_errors, emit_flows, emit_schema

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = emit_schema(out / "schema.json")
    flows = emit_flows(out / "flows.json")
    errors = emit_errors(out / "errors.json")

    print(
        f"stapel-gdpr contract: {paths} paths, {flows} flows, {errors} error keys "
        f"→ {out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
