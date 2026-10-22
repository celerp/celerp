"""Help search: a deterministic token score over the English pages.

Title and keywords weigh 3, the summary 2, step text and English labels 1; a
label's text in the caller's language found in the query adds 1, so a button
name typed in Thai still finds its page. Hits are flagged (module off, role
without the permission), never filtered out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from celerp.help.registry import CORE, get_index
from celerp.help.schema import HelpPage
from celerp.modules.loader import is_core_folded, loaded_modules
from celerp.modules.registry import uses_module
from celerp.services.permissions import role_has_permission
from ui.i18n import t

MAX_HITS = 8
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class Hit:
    page: HelpPage
    score: int
    module_enabled: bool
    permitted: bool


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _label_keys(page: HelpPage) -> list[str]:
    return [key for step in page.steps for key in step["labels"].values()]


def _score(page: HelpPage, query: str, words: set[str], lang: str) -> int:
    english = " ".join([*(s["text"] for s in page.steps), *(t(k, "en") for k in _label_keys(page))])
    score = (3 * len(words & _tokens(page.title)) + 3 * len(words & _tokens(" ".join(page.keywords)))
             + 2 * len(words & _tokens(page.summary)) + len(words & _tokens(english)))
    if lang != "en":
        lowered = query.lower()
        score += sum(1 for k in set(_label_keys(page)) if (label := t(k, lang).lower()) in lowered
                     and label != t(k, "en").lower())
    return score


def module_enabled(page: HelpPage, settings: dict | None) -> bool:
    """Whether the page's module is running here and the company uses it."""
    if page.module == CORE:
        return True
    running = is_core_folded(page.module) or any(m.get("name") == page.module for m in loaded_modules())
    return running and uses_module(settings, page.module)


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
