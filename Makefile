PYTHON ?= python3

.PHONY: migration-lint contract contract-check

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict


# First: the `surface` section of docs/capabilities.json — the symbols a
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
# Second: docs/llms.txt, the fifth contract artifact (badge-canon §3,
# stapel_tools.llms_txt) — rendered straight from the docs/capabilities.json
# the step above produces.
contract:
	$(PYTHON) -m stapel_tools.surface . --patch
	$(PYTHON) -m stapel_tools.llms_txt .

contract-check:
	$(PYTHON) -m stapel_tools.surface . --patch --check
	$(PYTHON) -m stapel_tools.llms_txt . --check
