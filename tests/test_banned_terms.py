"""No user-facing surface of the app carries a banned product name.

Each surface is built through the code that produces it: the published OpenAPI document of
the app with every first-party module enabled, every UI and module locale catalog, every
shipped help page and help translation, and every first-party module manifest."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from celerp.help import checks
from celerp.help.translations import catalog, catalog_languages
from test_helpers import real_agent_app

# The authoritative list is rule 5 of the website build check (celerp-cloud
# website/ci_check.py): the website publishes the app's OpenAPI text, so a term banned there
# is banned here. Keep this list in step with that rule.
BANNED_TERMS = ("Celerp Cloud",)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _strings(value, where: str):
    """Every string inside a JSON-like value, with where it sits."""
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            yield from _strings(item, f"{where}[{i}]")


def _hits(surface: str, value) -> list[str]:
    return [f"{surface}{where}: {term!r}" for where, text in _strings(value, "")
            for term in BANNED_TERMS if term in text]


def _assert_none(found: list[str]) -> None:
    assert not found, "banned terms found:\n" + "\n".join(found)


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_openapi_carries_no_banned_term():
    _assert_none(_hits("openapi", real_agent_app().openapi()))


def test_locale_catalogs_carry_no_banned_term():
    catalogs = sorted((REPO_ROOT / "ui" / "locales").glob("*.json"))
    for _name, path, _manifest in checks._first_party_modules():
        catalogs += sorted(path.rglob("locales/*.json"))
    assert len(catalogs) > 1
    _assert_none([hit for path in catalogs for hit in _hits(str(path.relative_to(REPO_ROOT)), _json(path))])


def test_help_pages_carry_no_banned_term():
    pages = checks.shipped_pages()
    assert pages
    found = [hit for page in pages for hit in _hits(page.path.name, page.data)]
    found += [hit for lang in catalog_languages() for hit in _hits(f"help/{lang}", catalog(lang))]
    _assert_none(found)


def test_module_manifests_carry_no_banned_term():
    modules = checks._first_party_modules()
    assert modules
    _assert_none([hit for name, _path, manifest in modules for hit in _hits(name, manifest)])


@pytest.mark.parametrize("term", BANNED_TERMS)
def test_the_guard_finds_a_banned_term(term):
    assert _hits("x", {"a": [f"Use {term} now."]}) == [f"x.a[0]: {term!r}"]
