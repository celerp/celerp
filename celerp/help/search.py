"""Help search: a deterministic token score over the English pages.

Title and keywords weigh 3, the summary 2, step text and English labels 1; a
label's text in the caller's language found in the query adds 1, so a button
name typed in Thai still finds its page. Hits are flagged (module off, role
without the permission), never filtered out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import parse_qsl

from celerp.help.registry import CORE, get_index
from celerp.help.render import label
from celerp.help.schema import HelpPage
from celerp.modules.loader import is_running
from celerp.modules.registry import uses_module
from celerp.services.permissions import role_has_permission

MAX_HITS = 8
_WORD = re.compile(r"\w+")
_SEGMENT = re.compile(r"\{[^/{}]+\}")


@dataclass(frozen=True)
class Hit:
    page: HelpPage
    score: int
    module_enabled: bool
    permitted: bool


@lru_cache(maxsize=512)
def _route_pattern(route: str) -> re.Pattern:
    """A help page route's path as a full-path match; each {name} stands for one path segment."""
    parts = _SEGMENT.split(route)
    return re.compile("[^/]+".join(re.escape(p) for p in parts) + r"/?\Z")


def _covers(route: str, path: str, query: set[tuple[str, str]]) -> bool:
    route_path, _, route_query = route.partition("?")
    route_pairs = set(parse_qsl(route_query))
    return (_route_pattern(route_path).match(path) is not None
            and route_pairs <= query
            and dict(route_pairs).get("type") == dict(query).get("type"))


def covers(routes, address: str) -> bool:
    """Whether a page with these help routes covers the screen at ``address`` (its query
    included): a route's path matches, every query pair it names (a tab, say) is on the
    address, and both name the same ``type``. A list path such as ``/docs`` serves a
    different screen per type, so a route without a type covers only the untyped list."""
    path, _, query = address.partition("?")
    pairs = set(parse_qsl(query))
    return any(_covers(route, path, pairs) for route in routes)


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _label_keys(page: HelpPage) -> list[str]:
    return [key for step in page.steps for key in step["labels"].values()]


def _score(page: HelpPage, query: str, words: set[str], lang: str) -> int:
    english = " ".join([*(s["text"] for s in page.steps), *(label(k, "en", page.catalogs) for k in _label_keys(page))])
    score = (3 * len(words & _tokens(page.title)) + 3 * len(words & _tokens(" ".join(page.keywords)))
             + 2 * len(words & _tokens(page.summary)) + len(words & _tokens(english)))
    if lang != "en":
        lowered = query.lower()
        score += sum(1 for k in set(_label_keys(page)) if (text := label(k, lang, page.catalogs).lower()) in lowered
                     and text != label(k, "en", page.catalogs).lower())
    return score


def module_enabled(page: HelpPage, settings: dict | None) -> bool:
    """Whether the page's module is running here and the company uses it."""
    if page.module == CORE:
        return True
    return is_running(page.module) and uses_module(settings, page.module)


def permitted(page: HelpPage, settings: dict | None, role: str) -> bool:
    """Whether the role holds the permission the page's screens need."""
    return page.permission is None or role_has_permission(settings, role, page.permission)


def flag(page: HelpPage, settings: dict | None, role: str, score: int = 0) -> Hit:
    return Hit(page=page, score=score, module_enabled=module_enabled(page, settings),
               permitted=permitted(page, settings, role))


def search(query: str, lang: str, settings: dict | None, role: str,
           module: str | None = None) -> list[Hit]:
    """The best matching pages, highest score first, at most MAX_HITS."""
    words = _tokens(query)
    scored = []
    for page in get_index().pages.values():
        if module and page.module != module:
            continue
        score = _score(page, query, words, lang)
        if score > 0:
            scored.append((score, page))
    scored.sort(key=lambda sp: (-sp[0], sp[1].order, sp[1].id))
    return [flag(page, settings, role, score) for score, page in scored[:MAX_HITS]]
