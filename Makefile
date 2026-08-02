PYTHON ?= python3

.PHONY: migration-lint contract contract-check

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict


# docs/llms.txt — the fifth contract artifact (badge-canon §3, stapel_tools.llms_txt),
# an agent-sized slice of docs/capabilities.json.
#
# These targets manage ONLY docs/llms.txt. docs/capabilities.json in this
# module is HAND-AUTHORED (git log: "author capabilities.json for the
# stapel-catalog sweep") — no generator exists for it here. Do not point
# `contract`/`contract-check` at it: regenerating it would risk clobbering
# hand-written content this file carries.
contract:
	$(PYTHON) -m stapel_tools.llms_txt .

contract-check:
	$(PYTHON) -m stapel_tools.llms_txt . --check
