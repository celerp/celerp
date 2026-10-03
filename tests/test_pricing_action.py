# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The pricing_action slot: module actions on the rows of an item's Pricing tab.

A contribution names an href_template with {entity_id}, {price_list} and
{field_name}, an optional permission, and optional show_on row traits
(editable/readonly, sell/cost, manual/derived; a row must carry every listed
trait). Visibility uses the same module-enabled and permission gate as every
other module contribution, item_action included, and every placeholder value is
URL-encoded. With nothing to show, the Pricing tab renders exactly as before.
"""
from __future__ import annotations

import uuid
from urllib.parse import quote

import pytest
from fasthtml.common import to_xml

from celerp.modules import slots
from celerp.modules.loader import ModuleLoadError, _load_one

# Retail and Wholesale are manual sell lists, Trade is derived from Retail, Cost is a cost list.
_LISTS = [
    {"name": "Retail", "description": ""},
    {"name": "Wholesale", "description": ""},
    {"name": "Trade", "description": "", "multiplier": 0.7, "rounding": 5},
    {"name": "Cost", "description": ""},
]
_ITEM = {"retail_price": 719, "wholesale_price": 600, "trade_price": 505.0, "cost_price": 300,
         "quantity": 2, "sell_by": "piece"}
# Wholesale is read-only for this role (manual but not editable); Trade is derived.
_FIELDS = [
    {"key": "retail_price", "editable": True},
    {"key": "wholesale_price", "editable": False},
    {"key": "trade_price", "editable": False},
    {"key": "cost_price", "editable": True},
]


def _form(role="owner", settings=None, entity_id="item:1", lists=_LISTS, item=_ITEM, fields=_FIELDS):
    from ui.routes.inventory import _pricing_form
    return to_xml(_pricing_form(entity_id, item, lists, "USD", fields, "Retail",
                                role=role, settings=settings or {}))


def _action(**over):
    return {"label": "Quote", "href_template": "/quoter/{entity_id}?list={price_list}&f={field_name}",
            "_module": "celerp-quoter", **over}


def _links(html: str) -> list[str]:
    return [chunk.split('"', 1)[0] for chunk in html.split('href="/quoter/')[1:]]


# ── rendering ────────────────────────────────────────────────────────────────

def test_pricing_tab_unchanged_without_a_visible_action():
    """No contribution, a contribution the role cannot use, and one from a module the
    company has switched off all render the same Pricing tab, with no actions column."""
    plain = _form()
    assert "Actions" not in plain
    slots.register("pricing_action", _action(permission="set_inventory_prices"))
    assert _form(role="viewer") == plain
    slots.clear()
    slots.register("pricing_action", _action())
    assert _form(settings={"enabled_modules": ["celerp-other"]}) == plain


def test_action_on_every_row_with_its_own_link():
    slots.register("pricing_action", _action())
    html = _form()
    assert sorted(_links(html)) == sorted(
        f"item%3A1?list={name}&amp;f={name.lower()}_price"
        for name in ("Retail", "Wholesale", "Trade", "Cost"))
    # Both cards gain one Actions column; the sold-price card is not a price-list row.
    assert html.count(">Actions</th>") == 2


def test_show_on_matches_rows_carrying_every_trait():
    slots.register("pricing_action", _action(show_on=["readonly", "sell", "manual"]))
    assert _links(_form()) == ["item%3A1?list=Wholesale&amp;f=wholesale_price"]
    slots.clear()
    slots.register("pricing_action", _action(show_on=["derived"]))
    assert _links(_form()) == ["item%3A1?list=Trade&amp;f=trade_price"]
    slots.clear()
    slots.register("pricing_action", _action(show_on=["cost"]))
    html = _form()
    assert _links(html) == ["item%3A1?list=Cost&amp;f=cost_price"]
    assert html.count(">Actions</th>") == 1  # only the card that has an action grows a column


def test_recipe_cost_row_is_readonly():
    slots.register("pricing_action", _action(show_on=["cost", "readonly"]))
    item = {**_ITEM, "recipe": {"components": [{"sku": "X"}]}}
    assert _links(_form(item=item)) == ["item%3A1?list=Cost&amp;f=cost_price"]


def test_placeholders_are_url_encoded():
    slots.register("pricing_action", _action(show_on=["sell", "manual", "editable"]))
    name = "A&B / C?#"
    html = _form(entity_id="item:1/x", lists=[{"name": name, "description": ""}],
                 item={"quantity": 1}, fields=[])
    (link,) = _links(html)
    from celerp.services.pricing import price_key
    assert link == f"{quote('item:1/x', safe='')}?list={quote(name, safe='')}&amp;f={quote(price_key(name), safe='')}"


def test_permission_denial_hides_the_action_and_grant_shows_it():
    slots.register("pricing_action", _action(permission="set_inventory_prices"))
    assert _links(_form(role="viewer")) == []
    assert len(_links(_form(role="manager"))) == 4
    granted = {"role_grants": {"set_inventory_prices": ["viewer", "operator", "manager", "admin", "owner"]}}
    assert len(_links(_form(role="viewer", settings=granted))) == 4


def test_disabled_module_contributes_nothing():
    slots.register("pricing_action", _action())
    assert _links(_form(settings={"enabled_modules": ["celerp-other"]})) == []
    assert len(_links(_form(settings={"enabled_modules": ["celerp-quoter"]}))) == 4


# ── item_action shares the gate and the encoding ─────────────────────────────

def _panel(role="owner", settings=None, entity_id="item:1/x"):
    from ui.routes.inventory import _advanced_panel
    return to_xml(_advanced_panel(entity_id, {"quantity": 1}, None, role=role, settings=settings or {}))


def test_item_action_is_gated_like_every_module_contribution():
    slots.register("item_action", {"label": "Ship it", "href_template": "/ship/{entity_id}",
                                   "permission": "adjust_inventory", "_module": "celerp-ship"})
    assert "Ship it" not in _panel(role="viewer")
    assert "Ship it" not in _panel(settings={"enabled_modules": ["celerp-other"]})
    assert 'href="/ship/item%3A1%2Fx"' in _panel()


# ── loader contract ──────────────────────────────────────────────────────────

def _load(tmp_path, contribution):
    name = f"pa_mod_{uuid.uuid4().hex[:8]}"
    pkg = tmp_path / name
    pkg.mkdir()
    manifest = {"name": name, "version": "1.0", "slots": {"pricing_action": contribution}}
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")
    return _load_one(pkg, name)


def test_loader_accepts_a_page_action(tmp_path):
    _load(tmp_path, [{"label": "Quote", "href_template": "/q/{entity_id}",
                      "show_on": ["sell"], "presentation": "page"}])
    assert [a["label"] for a in slots.get("pricing_action")] == ["Quote"]


@pytest.mark.parametrize("contribution, message", [
    ({"label": "Quote"}, "href_template"),
    ({"label": "Quote", "href_template": "/q", "show_on": ["sold"]}, "show_on"),
    ({"label": "Quote", "href_template": "/q", "show_on": "sell"}, "show_on"),
    ({"label": "Quote", "href_template": "/q", "show_on": ["editable", "readonly"]}, "never"),
    ({"label": "Quote", "href_template": "/q", "presentation": "modal"}, "presentation"),
    ({"label": "Quote", "href_template": "/q/{item}"}, "{item}"),
])
def test_loader_rejects_a_malformed_pricing_action(tmp_path, contribution, message):
    with pytest.raises(ModuleLoadError, match=message.replace("{", r"\{").replace("}", r"\}")):
        _load(tmp_path, contribution)
    assert slots.get("pricing_action") == []


@pytest.mark.parametrize("href", [
    "https://evil.example/q/{entity_id}",
    "javascript:alert(1)",
    "//evil.example/q/{entity_id}",
    "/\\evil.example/q/{entity_id}",
    "/q/{entity_id}\x01",
    "/q/{entity_id}\x7f",
    "q/{entity_id}",
    "{entity_id}",
])
def test_loader_rejects_an_href_template_outside_celerp(tmp_path, href):
    """The link core builds from the template, with the item id, price-list name and
    field name in it, must stay inside Celerp: the same app-local rule as every other
    module-supplied link (one leading /, never //, no backslash, no control character)."""
    with pytest.raises(ModuleLoadError, match="inside Celerp"):
        _load(tmp_path, {"label": "Quote", "href_template": href})
    assert slots.get("pricing_action") == []


@pytest.mark.parametrize("href", [
    "/q/{entity_id",
    "/q/entity_id}",
    "/q/{entity_id}}",
    "/q/{{entity_id}}",
    "/q/{entity_id}?x={",
    "/q/{price_list{entity_id}",
])
def test_loader_rejects_a_stray_brace_in_href_template(tmp_path, href):
    """A brace that does not wrap a placeholder would reach the browser as a broken link."""
    with pytest.raises(ModuleLoadError, match="brace"):
        _load(tmp_path, {"label": "Quote", "href_template": href})
    assert slots.get("pricing_action") == []


@pytest.mark.parametrize("key", ["href", "show", "presentaton", "requires_connector"])
def test_loader_rejects_an_unknown_pricing_action_key(tmp_path, key):
    """A misspelled key would otherwise be dead configuration nobody hears about."""
    with pytest.raises(ModuleLoadError, match=repr(key)):
        _load(tmp_path, {"label": "Quote", "href_template": "/q/{entity_id}", key: "x"})
    assert slots.get("pricing_action") == []


@pytest.mark.parametrize("contribution", [
    {"label": "Quote", "href_template": "/q"},
    {"label_key": "nav.inventory", "href_template": "/q/{entity_id}/{price_list}/{field_name}"},
    {"label": "Quote", "href_template": "/q/{entity_id}?list={price_list}&f={field_name}#top",
     "permission": "set_inventory_prices", "show_on": ["sell"], "presentation": "page"},
])
def test_loader_still_accepts_well_formed_actions(tmp_path, contribution):
    _load(tmp_path, [contribution])
    assert len(slots.get("pricing_action")) == 1


@pytest.mark.parametrize("value", [
    "//evil.example", "/\\evil.example", "https://evil.example", "\\\\evil", "a\x01b",
    "x/../../y", "?next=//evil", "#frag",
])
def test_filled_placeholders_cannot_lead_the_link_out_of_celerp(value):
    """The template is checked once at load; a value filled in at render is URL-encoded
    with no safe characters, so it can never add a /, a backslash or a control character
    and the finished link stays app-local whatever the item id or list name holds."""
    from celerp.services.app_paths import is_app_local_path
    from ui.routes.inventory import _slot_action_link
    encoded = quote(value, safe="")
    assert not set(encoded) & {"/", "\\", ":"} and encoded.isprintable()
    for template in ("/{entity_id}", "/q/{entity_id}?l={price_list}&f={field_name}"):
        link = to_xml(_slot_action_link({"label": "Q", "href_template": template},
                                        entity_id=value, price_list=value, field_name=value))
        href = link.split('href="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
        assert href == template.format(entity_id=encoded, price_list=encoded, field_name=encoded)
        assert is_app_local_path(href), href


# ── the module's own route is still the boundary ─────────────────────────────

@pytest.mark.asyncio
async def test_hidden_action_route_still_refuses_the_role(client, session):
    """Hiding the link is presentation. A module route behind require_permission refuses
    a role without that permission even when the URL is typed in directly."""
    from fastapi import APIRouter
    from celerp.main import app
    from celerp.services.permissions import require_permission
    from celerp.services.session_tracker import clear as clear_sessions

    router = APIRouter()

    @router.get("/quoter-test/{entity_id}")
    async def _quote(entity_id: str, _: None = require_permission("set_inventory_prices")):
        return {"entity_id": entity_id}

    before = list(app.router.routes)
    app.include_router(router)
    try:
        tag = uuid.uuid4().hex[:8]
        r = await client.post("/auth/register", json={
            "company_name": "QuoteCo", "email": f"owner-{tag}@quote.test", "name": "A",
            "password": "pwvalid1"})
        admin = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = await client.post("/companies/me/users", headers=admin, json={
            "name": "V", "email": f"viewer-{tag}@quote.test", "password": "testpass123",
            "role": "viewer"})
        assert r.status_code == 200, r.text
        await clear_sessions(session)
        r = await client.post("/auth/login", json={"email": f"viewer-{tag}@quote.test",
                                                   "password": "testpass123"})
        viewer = {"Authorization": f"Bearer {r.json()['access_token']}"}
        slots.register("pricing_action", _action(permission="set_inventory_prices"))
        assert _links(_form(role="viewer")) == []
        assert (await client.get("/quoter-test/item%3A1", headers=viewer)).status_code == 403
        r = await client.get("/quoter-test/item%3A1", headers=admin)
        assert (r.status_code, r.json()) == (200, {"entity_id": "item:1"})
    finally:
        app.router.routes[:] = before
