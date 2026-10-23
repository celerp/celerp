"""Every shipped first-party and core help page passes the mechanical content checks."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from celerp.help import checks

REPO_ROOT = Path(__file__).resolve().parent.parent

# The help viewer's own interface strings; help page prose never lives in a catalog.
_HELP_CHROME_KEYS = {
    "help.title", "help.search_placeholder", "help.no_results", "help.ask_ai",
    "help.answers_in_your_language", "help.for_this_page", "help.none_for_this_page",
    "help.browse", "help.third_party", "help.related", "help.load_failed", "help.empty_query",
    "help.no_module", "help.core_section",
}


@pytest.fixture(scope="module")
def pages() -> list[checks.PageFile]:
    return checks.shipped_pages()


def test_help_content_schema(pages):
    assert checks.check_schema(pages) == []


def test_help_content_label_keys(pages):
    assert checks.check_label_keys(pages) == []


def test_help_content_routes(pages):
    assert checks.check_routes(pages) == []


def test_help_content_permissions(pages):
    assert checks.check_permissions(pages) == []


def test_help_content_module_coverage(pages):
    assert checks.check_module_coverage(pages) == []


def test_help_content_images(pages):
    assert checks.check_images(pages) == []


def test_no_help_text_in_catalogs(pages):
    catalog = json.loads((REPO_ROOT / "ui" / "locales" / "en.json").read_text(encoding="utf-8"))
    assert {k for k in catalog if k.startswith("help.")} <= _HELP_CHROME_KEYS
    values = set(catalog.values())
    prose = [p.path.name for p in pages if isinstance(p.data, dict) and (
        p.data.get("title") in values or p.data.get("summary") in values
        or any(isinstance(s, dict) and s.get("text") in values for s in p.data.get("steps") or []))]
    assert prose == []
