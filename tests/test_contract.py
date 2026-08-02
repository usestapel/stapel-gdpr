"""Drift gate for ``docs/llms.txt`` — the fifth per-module contract artifact
(badge-canon §3), emitted from ``docs/capabilities.json`` by
``stapel_tools.llms_txt`` (``make contract`` / ``make contract-check``).

``docs/capabilities.json`` in this module is hand-authored (git log: "author
capabilities.json for the stapel-catalog sweep") — this gate owns ONLY
``docs/llms.txt``, the one file these targets are allowed to regenerate. A
hand-written ``llms.txt`` would rot exactly like a hand-written
``capabilities.json`` did elsewhere in the fleet: nothing but a model reads
it, and a model cannot tell a stale surface slice from a current one.
"""
from pathlib import Path

import pytest

stapel_tools = pytest.importorskip(
    "stapel_tools",
    reason="stapel-tools is not installed — the llms.txt drift gate needs the "
    "emitter. CI installs it; locally use the workspace venv or "
    "`pip install stapel-tools`.",
)

from stapel_tools.llms_txt import load_inputs, render  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
COMMITTED = REPO / "docs" / "llms.txt"


def test_llms_txt_committed():
    assert COMMITTED.is_file(), (
        "docs/llms.txt is missing — run `make contract` and commit it"
    )


def test_llms_txt_has_no_drift():
    rendered = render(load_inputs(REPO))
    assert COMMITTED.read_text() == rendered, (
        "docs/llms.txt is stale — run `make contract` and commit it"
    )


def test_llms_txt_emission_is_deterministic():
    """Two independent emissions are byte-identical (drift gate is meaningful)."""
    a = render(load_inputs(REPO))
    b = render(load_inputs(REPO))
    assert a == b
