"""One renderer for the help API and the help viewer: English text, labels in the user's language."""
from __future__ import annotations

from celerp.help.render import render_step
from ui.i18n import t


def test_render_step_uses_user_language_labels():
    step = {"text": "Open {inv} from the sidebar.", "labels": {"inv": "nav.inventory"},
            "image": None, "capture": {"route": "/inventory", "selector": "main", "actions": []}}
    thai = t("nav.inventory", "th")
    assert thai != t("nav.inventory", "en")
    assert render_step(step, "th") == f"Open {thai} from the sidebar."
    assert render_step(step, "en") == f"Open {t('nav.inventory', 'en')} from the sidebar."
