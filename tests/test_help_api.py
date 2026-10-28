# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Help API: search, read and route lookup over the help index, and their agent tools.

The index is replaced by a small fixed set of pages so ranking and flags are
asserted against known content, not against whatever pages ship.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from celerp.ai import tools as ai_tools
from celerp.help import registry
from celerp.help.schema import HelpPage
from celerp.modules import loader
from celerp.services.permissions import missing_permission_text
from test_helpers import invite_user, real_agent_app, register_admin

pytestmark = pytest.mark.asyncio

_GOOD = Path(__file__).parent / "fixtures" / "help" / "good"


def _step(text: str, labels: dict | None = None) -> dict:
    return {"text": text, "labels": labels or {}, "image": None,
            "capture": {"route": "/inventory", "selector": "main", "actions": []}}


def _page(page_id: str, title: str, *, module: str = "celerp-inventory", source: str = "first_party",
          summary: str = "A page.", steps: list | None = None, routes: list | None = None,
          permission: str | None = None, keywords: list | None = None) -> HelpPage:
    return HelpPage(
        id=page_id, title=title, summary=summary, keywords=keywords or [], routes=routes or ["/inventory"],
        permission=permission, order=1, steps=steps or [_step("Open the list.")], related=[],
        help_dir=_GOOD, source=source, module=module, module_display=module,
    )


@pytest.fixture
def help_pages(monkeypatch):
    pages = [
        _page("inventory.supplier-notes", "Item notes", steps=[_step("Add a supplier note to the item.")]),
        _page("contacts.add-supplier", "Add a supplier", module="celerp-contacts",
              routes=["/contacts", "/contacts/{id}"]),
        _page("acme.widgets", "Supplier widgets", module="acme-widgets", source="third_party",
              permission="manage_users"),
    ]
    monkeypatch.setattr(registry, "_index", registry.Index(pages={p.id: p for p in pages}))
    # The harness never runs load_all; celerp-contacts runs, acme-widgets does not.
    monkeypatch.setattr(loader, "_loaded", [{"name": "celerp-contacts", "version": "1.0.0"}])
    return pages


async def _headers(client) -> dict:
    return {"Authorization": f"Bearer {await register_admin(client)}"}


async def test_help_search_ranks_title_over_body(client, help_pages):
    r = await client.get("/help/search", params={"q": "supplier"}, headers=await _headers(client))
    assert r.status_code == 200, r.text
    ids = [hit["id"] for hit in r.json()["hits"]]
    assert ids.index("contacts.add-supplier") < ids.index("inventory.supplier-notes")


async def test_help_search_flags_disabled_module_and_permission(client, session, help_pages):
    admin = await _headers(client)
    viewer = await invite_user(client, session, admin, "viewer@help.example", "viewer")
    r = await client.get("/help/search", params={"q": "widgets"},
                         headers={"Authorization": f"Bearer {viewer}"})
    assert r.status_code == 200, r.text
    hit = next(h for h in r.json()["hits"] if h["id"] == "acme.widgets")
    assert hit["module_enabled"] is False
    assert hit["permitted"] is False
    assert hit["permission_text"] == missing_permission_text("manage_users")
    assert hit["source"] == "third_party"


async def test_help_search_rejects_long_query(client, help_pages):
    headers = await _headers(client)
    r = await client.get("/help/search", params={"q": "x" * 201}, headers=headers)
    assert r.status_code == 422
    assert (await client.get("/help/search", params={"q": " "}, headers=headers)).status_code == 422


async def test_help_search_unknown_module(client, help_pages):
    r = await client.get("/help/search", params={"q": "supplier", "module": "celerp-nothing"},
                         headers=await _headers(client))
    assert r.status_code == 200
    assert r.json()["hits"] == []
    assert r.json()["message"] == "No module named celerp-nothing."


async def test_help_read_unknown_id_404(client, help_pages):
    headers = await _headers(client)
    assert (await client.get("/help/page/contacts.nothing", headers=headers)).status_code == 404
    assert (await client.get("/help/page/Not_An_Id", headers=headers)).status_code == 404
    r = await client.get("/help/page/contacts.add-supplier", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Add a supplier"
    assert body["steps"][0]["text"] == "Open the list."
    assert body["module_enabled"] is True and body["permitted"] is True and body["source"] == "first_party"


async def test_help_read_renders_labels_and_image_urls(client, monkeypatch):
    page = _page("inventory.add-item", "Add an item",
                 steps=[{**_step("Open {inv}.", {"inv": "nav.inventory"}), "image": "img/add-item-1.webp"}])
    monkeypatch.setattr(registry, "_index", registry.Index(pages={page.id: page}))
    headers = {**await _headers(client), "Accept-Language": "th"}
    body = (await client.get("/help/page/inventory.add-item", headers=headers)).json()
    from ui.i18n import t
    assert body["steps"][0]["text"] == f"Open {t('nav.inventory', 'th')}."
    assert body["steps"][0]["image_url"] == "/help/img/inventory.add-item/1"


async def test_help_for_route_matches_patterns(client, help_pages):
    headers = await _headers(client)
    r = await client.get("/help/for-route", params={"path": "/contacts/abc"}, headers=headers)
    assert r.status_code == 200
    assert [p["id"] for p in r.json()["pages"]] == ["contacts.add-supplier"]
    r = await client.get("/help/for-route", params={"path": "/contacts/abc/edit"}, headers=headers)
    assert r.json()["pages"] == []
    assert (await client.get("/help/for-route", params={"path": "contacts"}, headers=headers)).status_code == 422
    # A query on the screen's address still finds the screen's pages.
    r = await client.get("/help/for-route", params={"path": "/contacts/abc?tab=notes"}, headers=headers)
    assert [p["id"] for p in r.json()["pages"]] == ["contacts.add-supplier"]
    assert (await client.get("/help/for-route", params={"path": "/" + "a" * 300},
                             headers=headers)).status_code == 422


async def test_help_for_route_long_list_address(client, help_pages):
    """A list address carries its filters (a split's sku list, an encoded search), so only
    the path part is held to the path bound; the whole address gets the address bound."""
    headers = await _headers(client)
    short = await client.get("/help/for-route", params={"path": "/inventory"}, headers=headers)
    r = await client.get("/help/for-route", params={"path": "/inventory?skus=" + "A" * 1000},
                         headers=headers)
    assert r.status_code == 200
    assert r.json()["pages"] == short.json()["pages"] != []
    for path in ("/" + "a" * 300 + "?x=1", "/inventory?q=" + "a" * 4096):
        assert (await client.get("/help/for-route", params={"path": path},
                                 headers=headers)).status_code == 422


async def test_help_routes_are_agent_tools():
    compiled = ai_tools.compile_agent_capabilities(real_agent_app(), {})
    assert compiled["help_search"]["path"] == "/help/search"
    assert compiled["help_read"]["path"] == "/help/page/{page_id}"
    assert not any(cap["path"] == "/help/for-route" for cap in compiled.values())


async def test_help_for_route_matches_tab_routes(client, monkeypatch):
    found = [_page("core.users", "Users", routes=["/settings/general?tab=users"]),
             _page("core.companies", "Companies", routes=["/settings/general?tab=company"])]
    monkeypatch.setattr(registry, "_index", registry.Index(pages={p.id: p for p in found}))
    headers = await _headers(client)

    async def ids(path: str) -> list[str]:
        r = await client.get("/help/for-route", params={"path": path}, headers=headers)
        assert r.status_code == 200
        return [p["id"] for p in r.json()["pages"]]

    assert await ids("/settings/general?tab=users&x=1") == ["core.users"]
    assert await ids("/settings/general?x=1&tab=company") == ["core.companies"]
    assert await ids("/settings/general?tab=backup") == []
    assert await ids("/settings/general") == []


async def test_every_first_party_screen_gets_its_module_help_on_both_routes(client):
    """Over the pages that ship, not the fixed set: each sidebar and settings screen of
    every first-party module (the AI page and its settings among them) is answered by
    the route lookup with a page of that module, and that page opens on the read
    route. Pages count only as first-party, so a module whose content no longer matches
    its lock entry (loaded as third-party) fails here."""
    from celerp.help import checks
    registry.reset()
    try:
        headers = await _headers(client)
        index = registry.get_index()
        screens = checks.nav_screens()
        assert {"/ai", "/ai/settings"} <= set(screens["celerp-ai"])
        missing = []
        for module, paths in sorted(screens.items()):
            for path in dict.fromkeys(paths):
                r = await client.get("/help/for-route", params={"path": path}, headers=headers)
                assert r.status_code == 200
                ids = [p["id"] for p in r.json()["pages"]
                       if index.pages[p["id"]].module == module
                       and index.pages[p["id"]].source == "first_party"]
                if not ids:
                    missing.append(f"{module}: {path}")
                for page_id in ids:
                    assert (await client.get(f"/help/page/{page_id}", headers=headers)).status_code == 200
        assert missing == []
    finally:
        registry.reset()
