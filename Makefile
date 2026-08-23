PYTHON ?= python3

# Raised from the generator's 4000 default the moment docs/schema.json existed:
# its operation catalog costs llms.txt ~270 tokens, which put the render 228
# over. Raised deliberately rather than by trimming the `surface` intent lines
# — that section is the one part of the file an agent reads to avoid rewriting
# a mechanism that already exists, and "a deployment that never wires the beat
# schedule silently never executes an erasure" does not survive being
# compressed to a clause. Same deliberate exception stapel-workspaces (4500),
# stapel-calendar (5000) and stapel-auth (8000) already take; the ceiling is
# still enforced. Must match LLMS_TXT_BUDGET in tests/test_contract.py.
LLMS_TXT_BUDGET ?= 4500

.PHONY: migration-lint contract contract-check

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict


# First: the contract triad (contract-pipeline.md §2-3) — docs/schema.json
# (drf-spectacular OpenAPI), docs/flows.json ([]; this module annotates no
# @flow_step) and docs/errors.json — emitted by _codegen.py from a
# single-module {gdpr + core} Django instance mounted at the canonical
# /gdpr/api/v1 prefix. schema.json is what the frontend pair generates its
# typed client from, so it has to be the paths a real host serves, not the
# bare test mount. errors.json is also gated independently by
# tests/test_error_keys.py; the two agreeing is the check that the harness
# and the suite configure the same error registry.
#
# Second: the `surface` section of docs/capabilities.json — the symbols a
# product is meant to CALL (discoverability-design.md §1.2). get_gdpr_beat_schedule
# and process_expired_grace_periods are the reason this exists here: a
# deployment that never wires the beat schedule has working closure/cancel
# endpoints and silently never executes a single erasure. Entries are derived
# by AST from the roots declared in docs/capabilities.meta.json; a selected
# export with no curated intent line fails this target naming the symbol.
#
# NOTE the rest of docs/capabilities.json is still HAND-AUTHORED (git log:
# "author capabilities.json for the stapel-catalog sweep") — no generator
# exists for provides/axes/extension_points/requires here. `--patch` refreshes
# only the derivable parts: module/version and `surface`.
#
# Third: docs/llms.txt, the fifth contract artifact (badge-canon §3,
# stapel_tools.llms_txt) — rendered from the docs/capabilities.json the step
# above produces PLUS the triad, which is why it comes last of the machine
# artifacts: its operation catalog is read out of docs/schema.json.
#
# Fourth: assemble README.md (stapel_tools.readme) from docs/readme.md — the
# human half, the only file a person edits — plus the artifacts above. The
# badge row, the version, the surface counts and every doc link are generated,
# so they cannot lag a release the way a hand-written README always has.
contract:
	$(PYTHON) -m stapel_gdpr._codegen --out docs
	$(PYTHON) -m stapel_tools.surface . --patch
	$(PYTHON) -m stapel_tools.llms_txt . --budget $(LLMS_TXT_BUDGET)
	$(PYTHON) -m stapel_tools.readme .

# Drift gate: the triad is regenerated into a temp dir and diffed against the
# committed docs/*.json (nothing else can compare those three); surface
# --check compares the derivable parts of docs/capabilities.json; llms_txt's
# own --check mode compares a fresh render (from the COMMITTED docs/, so a
# stale llms.txt is caught independently of the triad) against docs/llms.txt.
#
# One shell block, one exit code: a per-line recipe would stop at the first
# stale artifact and hide the rest, and the point of a drift gate is to name
# everything that needs regenerating in a single run.
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_gdpr._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.surface . --patch --check || rc=1; \
	$(PYTHON) -m stapel_tools.llms_txt . --check --budget $(LLMS_TXT_BUDGET) || rc=1; \
	$(PYTHON) -m stapel_tools.readme . --check || rc=1; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors,capabilities,llms.txt} + README.md up to date"; fi; \
	exit $$rc
