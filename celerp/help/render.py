"""One renderer for help steps, shared by the help API and the help viewer."""
from __future__ import annotations

from celerp.help.schema import PLACEHOLDER
from ui.i18n import t


def label(key: str, lang: str, own: dict[str, dict]) -> str:
    """The label for ``key`` in ``lang``. A key no registered catalog holds comes from
    the page's module catalogs ``own`` (a module that is not running registers none),
    in ``lang`` then English, as t() falls back."""
    text = t(key, lang)
    if text != key:
        return text
    for catalog in (own.get(lang) or {}, own.get("en") or {}):
        if isinstance(catalog.get(key), str):
            return catalog[key]
    return key


def render_step(step: dict, lang: str, own: dict[str, dict]) -> str:
    """The step's English text with each {name} replaced by its label in ``lang``,
    so a button is named the way the user sees it on screen. ``own`` is the page's
    module catalogs (HelpPage.catalogs)."""
    labels = step.get("labels") or {}
    return PLACEHOLDER.sub(lambda m: label(labels[m.group(1)], lang, own), step["text"])
