"""Localized error catalogs (``translations/errors.<lang>.json``) + provenance gate.

i18n-shipping.md §5. This module owns ten ``error.*.gdpr.*`` keys. Since
stapel-core 0.23.1 a reader resolves a key it does not own from the **owner's**
catalog (:func:`stapel_core.i18n.catalogs.module_catalog`), and since 0.22.0 a
writer may only translate the keys it owns. Shipping no catalog therefore meant
every consumer's localized error reference fell back to English for all ten —
stapel-auth's ``docs/errors.{ru,es}.md`` rendered ``_(en)_`` rows for the keys
the 2026-08-11 GDPR wave added, and the older ones only survived because
stapel-auth still carried a pre-ownership-scoping copy of them.

Provenance: values are **seeded** from the curated stapel-translate builtin
corpus (``origin: seed:stapel-builtin``) — the single home of these strings.
Nothing here is LLM-generated, so there is no machine-translation table to
maintain: a new key gets its translations in stapel-translate first, and this
module seeds from there.

That order is enforced, not described: every owned key must be present in
the corpus for every language the corpus ships, and every owned value in
``.state.json`` must carry a seeded (or human) origin. An ``imported`` value
would mean a string was authored here to get past coverage while the corpus
stayed blind to it — 0.5.5 shipped three that way, and the corpus only learned
of them from a changelog note.

Regenerate after adding/changing an error key or a translation:

    STAPEL_REGEN_ERROR_I18N=1 python -m pytest tests/test_error_i18n.py::test_regen

then commit ``translations/errors.<lang>.json`` + ``translations/.state.json``.
Without the env var the same module is the CI gate.
"""
import os
from pathlib import Path

from stapel_core.i18n import (
    check_translation_catalogs,
    source_texts,
    summarize,
    translate_catalog,
)
from stapel_core.i18n.catalogs import load_catalog_file

REPO = Path(__file__).resolve().parent.parent
TRANSLATIONS = REPO / "translations"
#: Languages this module ships error catalogs in. en is the canon (the
#: registry literals); every other tag needs a catalog.
LANGUAGES = ["en", "ru", "es"]
#: The languages that need a catalog — everything but the source language.
TARGET_LANGUAGES = [lang for lang in LANGUAGES if lang != "en"]

def _corpus_dir() -> Path:
    """The stapel-translate builtin fixtures (the curated seed corpus).

    STAPEL_TRANSLATE_FIXTURES, else a sibling checkout, else the installed
    ``stapel_translate`` package. Raises when none is present: a coverage
    test with no corpus to divide by must fail, not skip.
    """
    explicit = os.environ.get("STAPEL_TRANSLATE_FIXTURES")
    if explicit:
        return Path(explicit)
    sibling = REPO.parent / "stapel-translate" / "fixtures" / "builtin"
    if sibling.is_dir():
        return sibling
    from stapel_translate.management.commands.load_builtin_translations import (
        FIXTURES_DIR,
    )

    return Path(FIXTURES_DIR)


def _seed_from_fixtures(lang: str) -> dict[str, str]:
    """Flat ``{error.*: text}`` seed from the builtin fixtures for *lang*."""
    import json

    try:
        path = _corpus_dir() / f"{lang}.json"
    except ImportError:
        return {}
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and k.startswith("error.")
        and isinstance(v, str) and v
    }


def _regen(lang: str):
    """Materialize one target-language catalog from the curated corpus."""
    return translate_catalog(
        "errors", lang, TRANSLATIONS,
        source_texts=source_texts("errors"),
        seed=_seed_from_fixtures(lang),
        seed_label="stapel-builtin",
    )


def test_regen():
    """Regenerate (env-gated) or assert every catalog is a no-op regen (drift)."""
    if os.environ.get("STAPEL_REGEN_ERROR_I18N"):
        for lang in TARGET_LANGUAGES:
            result = _regen(lang)
            assert not result.missing, f"{lang}: still missing: {result.missing}"
        return

    for lang in TARGET_LANGUAGES:
        path = TRANSLATIONS / f"errors.{lang}.json"
        before = path.read_bytes()
        _regen(lang)
        assert path.read_bytes() == before, (
            f"errors.{lang}.json drifted — run "
            f"STAPEL_REGEN_ERROR_I18N=1 pytest tests/test_error_i18n.py::test_regen"
        )


def test_catalog_gate_green():
    """E: missing / stale / params-mismatch / not-byte-stable — all zero."""
    issues = check_translation_catalogs(
        "errors", TRANSLATIONS,
        source_texts=source_texts("errors"),
        languages=LANGUAGES,
    )
    errors, _warnings = summarize(issues)
    blocking = [i for i in issues if i.level == "error"]
    assert not blocking, "\n".join(f"[{i.code}] {i.message}" for i in blocking)
    assert errors == 0


def test_every_language_covers_every_key_this_module_owns():
    """Coverage is scoped to OWNERSHIP: every gdpr key, in every language."""
    from stapel_core.i18n import owned_keys, owner_of_dir, source_owners

    source = owned_keys(
        source_texts("errors"),
        source_owners("errors"),
        owner_of_dir(TRANSLATIONS),
    )
    assert source, "ownership resolved to nothing — is stapel_gdpr installed?"
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        missing = [k for k in source if k not in catalog]
        assert not missing, (
            f"{lang} catalog missing {len(missing)} key(s): {missing[:8]}"
        )


def test_this_module_owns_only_its_own_keys():
    """The catalogs carry gdpr keys and nothing else (no fleet-wide copies)."""
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        stray = [k for k in catalog if ".gdpr." not in k]
        assert not stray, f"{lang}: not this module's keys: {stray}"


def test_translations_preserve_placeholders():
    """Every localized text keeps exactly the canon's ``{param}`` slots (§3)."""
    from stapel_core.i18n.domains import params_of

    source = source_texts("errors")
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        for key, text in catalog.items():
            if key in source:
                assert set(params_of(text)) == set(params_of(source[key])), \
                    f"{lang}: {key}"


def _owned() -> dict[str, str]:
    from stapel_core.i18n import owned_keys, owner_of_dir, source_owners

    source = owned_keys(
        source_texts("errors"), source_owners("errors"), owner_of_dir(TRANSLATIONS),
    )
    assert source, "ownership resolved to nothing — is stapel_gdpr installed?"
    return source


def test_corpus_carries_every_owned_key_in_every_corpus_language():
    """The corpus is where a gdpr string is born: all owned keys, every language it ships."""
    import json

    corpus = _corpus_dir()
    fixtures = sorted(corpus.glob("*.json"))
    assert len(fixtures) >= 3, f"no corpus at {corpus}"
    problems = []
    for path in fixtures:
        data = json.loads(path.read_text(encoding="utf-8"))
        missing = [k for k in _owned() if not (data.get(k) or "").strip()]
        if missing:
            problems.append(f"{path.stem}: {missing}")
    assert problems == [], "owned keys the corpus does not carry: " + "; ".join(problems)


def test_owned_keys_are_seeded_from_the_corpus():
    """No owned value may be ``imported``: authored here means the corpus is blind to it."""
    from stapel_core.i18n.catalogs import StateSidecar, is_reviewed, is_seeded

    state = StateSidecar(TRANSLATIONS / ".state.json")
    problems = []
    for lang in TARGET_LANGUAGES:
        for key in _owned():
            origin = (state.get("errors", lang, key) or {}).get("origin")
            if not (is_seeded(origin) or is_reviewed(origin)):
                problems.append(f"{lang}:{key}={origin}")
    assert problems == [], "not seeded from the corpus: " + ", ".join(problems)
