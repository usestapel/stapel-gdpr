# Contributing to stapel-gdpr

Part of the [Stapel framework](https://github.com/usestapel) — composable
Django apps for monolith-or-microservices deployments.

## Dev setup

```bash
git clone https://github.com/usestapel/stapel-gdpr.git && cd stapel-gdpr
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]" || pip install -e .
pip install pytest pytest-django ruff
./setup-hooks.sh   # enables the ruff pre-commit/pre-push hooks
```

## Running tests

The packages use a flat layout (`package-dir = {{"stapel_gdpr": "."}}`), so the
repo root shadows the installed package under pytest. Run tests the way CI
does:

```bash
pip install .            # not -e: install a snapshot
mv __init__.py __init__.py.hidden
pytest tests/
mv __init__.py.hidden __init__.py
```

## Lint

```bash
ruff check . --select E,F,W --ignore E501
```

The pre-push hook runs the same command; CI rejects anything it flags.

## Design rules (the short version)

- **No new hardcoded behavior.** Anything a host project might want to
  change goes through the package's settings namespace
  (`STAPEL_<PACKAGE>` dict; see `conf.py`) — with a dotted-path
  `import_string` escape hatch for swappable classes.
- **Modules never import each other.** Cross-module communication uses
  `stapel_core.comm`: Actions (`emit`/`@on_action`, transactional outbox),
  Functions (`call`/`@function`), Tasks for long-running work.
- **Every event/function has a JSON Schema** in `schemas/` — CI validates
  payloads against them.
- **Raise the floor, don't guard the ceiling**: security checks fail
  closed; user-supplied redirect targets are allowlisted; money paths are
  idempotent.

## Commit style

Conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`); one logical
change per commit; add a CHANGELOG entry under **Unreleased**.
