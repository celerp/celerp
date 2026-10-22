"""One renderer for the help API and the help viewer: English text, labels in the user's language."""
from __future__ import annotations

import pytest

from celerp.help.render import render_step
from ui.i18n import t


def test_render_step_uses_user_language_labels():
    step = {"text": "Open {inv} from the sidebar.", "labels": {"inv": "nav.inventory"},
            "image": None, "capture": {"route": "/inventory", "selector": "main", "actions": []}}
    thai = t("nav.inventory", "th")
    assert thai != t("nav.inventory", "en")
    assert render_step(step, "th", {}) == f"Open {thai} from the sidebar."
    assert render_step(step, "en", {}) == f"Open {t('nav.inventory', 'en')} from the sidebar."


@pytest.fixture
def module_catalogs_unregistered(monkeypatch):
    """A module that is installed but not running never registers its UI catalog."""
    from celerp.help import registry
    from ui import i18n
    monkeypatch.setattr(i18n, "_registry", {})
    i18n._invalidate_cache()
    registry.reset()
    yield
    monkeypatch.undo()
    i18n._invalidate_cache()
    registry.reset()


def test_disabled_module_page_labels_come_from_its_own_catalog(module_catalogs_unregistered):
    from celerp.help.registry import get_index
    from celerp.help.search import search
    page = get_index().pages["fixed-assets.register"]
    step = next(s for s in page.steps if "fixed_assets.add_asset" in (s.get("labels") or {}).values())
    assert t("fixed_assets.add_asset", "th") == "fixed_assets.add_asset"
    thai = render_step(step, "th", page.catalogs)
    assert "fixed_assets." not in thai and "เพิ่มสินทรัพย์" in thai
    assert "Add asset" in render_step(step, "en", page.catalogs)
    hits = search("เพิ่มสินทรัพย์", "th", None, "owner")
    assert "fixed-assets.register" in [h.page.id for h in hits]
