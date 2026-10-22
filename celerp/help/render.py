"""One renderer for help steps, shared by the help API and the help viewer."""
from __future__ import annotations

from celerp.help.schema import PLACEHOLDER
from ui.i18n import t


def render_step(step: dict, lang: str) -> str:
    """The step's English text with each {name} replaced by its label in ``lang``,
    so a button is named the way the user sees it on screen."""
    labels = step.get("labels") or {}
    return PLACEHOLDER.sub(lambda m: t(labels[m.group(1)], lang), step["text"])
