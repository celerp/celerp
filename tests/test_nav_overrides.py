# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The company hides and reorders its side menu; the choices only narrow what a role already sees.

Entries and groups are named by stable ids (an entry's key or href, a group's declared name), so
the choices apply the same in every language. Settings is never hidden, a module installed later
keeps its declared place, and saving no choices is a reset.
"""
from __future__ import annotations

import re
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient

from test_helpers import invite_user, make_test_token, register_admin
import ui.app  # noqa: F401  (loads the modules, and with them the menu entries)
from ui.components.shell import _sidebar, visible_nav
from ui.nav_overrides import apply_nav_overrides, entry_id, group_id

KEY = "nav_overrides"


def _ids(items) -> list[str]:
    return [entry_id(i) for i in items]


def _settings(**chosen) -> dict:
    return {KEY: chosen}


def _grouped(items=None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in items if items is not None else visible_nav(None, "owner"):
        if item.get("group"):
            out.setdefault(group_id(item["group"]), []).append(item)
    return out


def _a_group_of_three() -> tuple[str, list[str]]:
    gid, members = next((g, m) for g, m in _grouped().items() if len(m) >= 3)
    return gid, _ids(members)


def _hrefs(html: str) -> list[str]:
    """The menu links on a rendered sidebar, in order."""
    found = re.findall(r'<a href="([^"]+)" class="nav-link', html)
    assert found, html
    return found


def _href_of(key: str) -> str:
    return next(i for i in visible_nav(None, "owner") if entry_id(i) == key)["href"]


# ── Hiding ────────────────────────────────────────────────────────────────

def test_nav_overrides_hide_entry():
    gid, ids = _a_group_of_three()
    victim = ids[1]
    default = _ids(visible_nav(None, "owner"))

    shown = visible_nav(_settings(hidden_entries=[victim]), "owner")

    assert victim not in _ids(shown)
    assert set(_ids(shown)) == set(default) - {victim}
    sidebar = to_xml(_sidebar("dashboard", role="owner", settings=_settings(hidden_entries=[victim])))
    assert _href_of(victim) not in _hrefs(sidebar)
    assert _href_of(ids[0]) in _hrefs(sidebar)  # its neighbours stay


def test_nav_overrides_hide_group():
    gid, ids = _a_group_of_three()

    shown = visible_nav(_settings(hidden_groups=[gid]), "owner")

    assert not set(ids) & set(_ids(shown))
    assert f'data-group="{gid}"' not in to_xml(_sidebar("dashboard", role="owner", settings=_settings(hidden_groups=[gid])))


# ── Reordering ────────────────────────────────────────────────────────────

def test_nav_overrides_reorder_entry():
    gid, ids = _a_group_of_three()
    reordered = [ids[2], ids[0], ids[1]] + ids[3:]

    shown = _grouped(visible_nav(_settings(entry_order=reordered), "owner"))[gid]

    assert _ids(shown) == reordered
    # Every other group is where it was.
    default = _grouped()
    for other, members in _grouped(visible_nav(_settings(entry_order=reordered), "owner")).items():
        if other != gid:
            assert _ids(members) == _ids(default[other])


def test_groups_are_reordered():
    default = list(_grouped())
    assert len(default) >= 3
    wanted = [default[2], default[0], default[1]] + default[3:]

    shown = list(_grouped(visible_nav(_settings(group_order=wanted), "owner")))

    assert shown == wanted


def test_a_module_installed_later_keeps_its_declared_place():
    items = [{"key": k, "href": f"/{k}", "group": "G", "order": n} for n, k in enumerate("abcd")]
    # The saved order knows a, b and d; c arrived afterwards and was never placed.
    out = apply_nav_overrides(items, _settings(entry_order=["d", "b", "a"]))

    assert _ids(out) == ["d", "b", "c", "a"]  # c still sits third, where it was declared


def test_choices_naming_a_module_that_is_gone_do_nothing():
    default = _ids(visible_nav(None, "owner"))

    shown = visible_nav(_settings(hidden_entries=["no-such-entry"], hidden_groups=["no_such_group"],
                                  entry_order=["no-such-entry"], group_order=["no_such_group"]), "owner")

    assert sorted(_ids(shown)) == sorted(default)


# ── Permissions come first ────────────────────────────────────────────────

def test_nav_overrides_never_shows_permission_denied_entry():
    everything = visible_nav(None, "owner")
    allowed = visible_nav(None, "viewer")
    denied = set(_ids(everything)) - set(_ids(allowed))
    assert denied, "the viewer role must lack at least one entry for this test to mean anything"

    # Choices that try every way of surfacing what the role may not see.
    hostile = _settings(entry_order=sorted(denied) + _ids(allowed), group_order=list(_grouped(everything)),
                        hidden_entries=[], hidden_groups=[])
    shown = visible_nav(hostile, "viewer")

    assert not denied & set(_ids(shown))
    assert set(_ids(shown)) <= set(_ids(allowed))
    denied_hrefs = {i["href"] for i in everything if entry_id(i) in denied}
    links = set(_hrefs(to_xml(_sidebar("dashboard", role="viewer", settings=hostile))))
    assert not denied_hrefs & links


# ── Settings ──────────────────────────────────────────────────────────────

def test_settings_cannot_be_hidden():
    items = [{"key": "a", "href": "/a", "group": "Tools", "order": 1},
             {"key": "labels", "href": "/settings/labels", "group": "Tools", "order": 2},
             {"key": "settings", "href": "/settings/general", "order": 3},
             {"key": "b", "href": "/b", "order": 4}]

    out = apply_nav_overrides(items, _settings(
        hidden_entries=["labels", "settings", "a", "b"], hidden_groups=["tools"]))

    assert _ids(out) == ["settings", "labels"]  # only Settings pages stay; everything else was hidden


@pytest.mark.asyncio
async def test_the_api_refuses_to_hide_settings(client):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}

    for hidden in (["settings"], ["/settings/general"], ["/settings/labels?x=1"]):
        r = await client.patch("/companies/me/nav-overrides", json={"hidden_entries": hidden}, headers=h)
        assert r.status_code == 422, (hidden, r.text)
    assert (await client.get("/companies/me/nav-overrides", headers=h)).json()["hidden_entries"] == []


# ── Language ──────────────────────────────────────────────────────────────

def test_choices_survive_a_language_switch():
    gid, ids = _a_group_of_three()
    chosen = _settings(hidden_entries=[ids[1]], entry_order=[ids[2], ids[0]] + ids[3:],
                       group_order=list(reversed(list(_grouped()))))

    pages = {lang: to_xml(_sidebar("dashboard", lang=lang, role="owner", settings=chosen)) for lang in ("en", "de", "th")}

    assert _hrefs(pages["en"]) == _hrefs(pages["de"]) == _hrefs(pages["th"])
    groups = {lang: re.findall(r'data-group="([^"]+)"', html) for lang, html in pages.items()}
    assert groups["en"] == groups["de"] == groups["th"] == list(reversed(list(_grouped())))
    # The pages really are in different languages, so the match above is not two copies of English.
    assert pages["en"] != pages["de"]
    assert _href_of(ids[1]) not in _hrefs(pages["en"])


# ── Saving and resetting ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_choices_are_saved_in_company_settings_and_reset(client, session):
    from celerp.models.company import Company
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    cid = (await client.get("/companies/me", headers=h)).json()["id"]
    payload = {"hidden_entries": ["customers", "customers"], "hidden_groups": ["finance"],
               "entry_order": ["b", "a"], "group_order": ["x", "y"]}

    saved = await client.patch("/companies/me/nav-overrides", json=payload, headers=h)

    assert saved.status_code == 200, saved.text
    assert saved.json() == {"hidden_entries": ["customers"], "hidden_groups": ["finance"],
                            "entry_order": ["b", "a"], "group_order": ["x", "y"]}
    assert (await client.get("/companies/me/nav-overrides", headers=h)).json() == saved.json()
    company = await session.get(Company, uuid.UUID(cid))
    await session.refresh(company)
    assert company.settings[KEY]["hidden_groups"] == ["finance"]

    reset = await client.delete("/companies/me/nav-overrides", headers=h)

    assert reset.status_code == 200, reset.text
    assert (await client.get("/companies/me/nav-overrides", headers=h)).json() == {
        "hidden_entries": [], "hidden_groups": [], "entry_order": [], "group_order": []}
    await session.refresh(company)
    assert KEY not in company.settings
    assert _ids(visible_nav(company.settings, "owner")) == _ids(visible_nav(None, "owner"))


@pytest.mark.asyncio
async def test_saving_no_choices_is_a_reset(client):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    await client.patch("/companies/me/nav-overrides", json={"hidden_entries": ["customers"]}, headers=h)

    r = await client.patch("/companies/me/nav-overrides", json={}, headers=h)

    assert r.status_code == 200 and r.json()["hidden_entries"] == []


@pytest.mark.asyncio
async def test_only_a_role_that_edits_permissions_can_change_the_menu(client, session):
    """The same gate as the role matrix: manage_permissions, which only the owner holds."""
    admin = {"Authorization": f"Bearer {await register_admin(client)}"}
    other = {"Authorization": f"Bearer {await invite_user(client, session, admin, 'adm2@ex.test', 'admin')}"}
    viewer = {"Authorization": f"Bearer {await invite_user(client, session, admin, 'view@ex.test', 'viewer')}"}

    for h in (other, viewer):
        assert (await client.patch("/companies/me/nav-overrides", json={"hidden_entries": ["x"]}, headers=h)).status_code == 403
        assert (await client.delete("/companies/me/nav-overrides", headers=h)).status_code == 403
    assert (await client.patch("/companies/me/nav-overrides", json={"hidden_entries": ["x"]}, headers=admin)).status_code == 200


@pytest.mark.asyncio
async def test_the_api_refuses_malformed_choices(client):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}

    for body in ({"hidden_entries": [""]}, {"hidden_entries": ["x" * 201]}, {"entry_order": ["a\nb"]},
                 {"entry_order": [f"e{n}" for n in range(201)]}):
        r = await client.patch("/companies/me/nav-overrides", json=body, headers=h)
        assert r.status_code == 422, (list(body), r.text)


# ── The settings page ─────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        yield c


def _page_patches(saved: dict | None = None, role_settings: dict | None = None):
    store = {"hidden_entries": [], "hidden_groups": [], "entry_order": [], "group_order": [], **(saved or {})}
    calls: list[dict] = []

    async def company(token):
        return {"settings": {**(role_settings or {}), KEY: {k: v for k, v in store.items() if v}}}

    async def get(token):
        return dict(store)

    async def patch(token, body):
        calls.append(body)
        store.update(body)
        return dict(store)

    async def reset(token):
        store.update({k: [] for k in store})
        return dict(store)

    return calls, (patch_("get_company", company), patch_("get_nav_overrides", get),
                   patch_("patch_nav_overrides", patch), patch_("reset_nav_overrides", reset))


def patch_(name, fn):
    return patch(f"ui.api_client.{name}", new=fn)


@pytest.mark.asyncio
async def test_the_page_lists_entries_and_hides_one(ui_client):
    gid, ids = _a_group_of_three()
    calls, patches = _page_patches()
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patches[0], patches[1], patches[2], patches[3]:
        page = (await ui_client.get("/settings/menu")).text
        hidden = await ui_client.post("/settings/menu/hide", data={"kind": "entry", "id": ids[1], "hidden": "1"})

    assert 'id="menu-editor"' in page and f'data-group="{gid}"' in page and f'data-entry="{ids[1]}"' in page
    assert calls[-1]["hidden_entries"] == [ids[1]]
    assert f'data-entry="{ids[1]}"' in hidden.text and "menu-editor__row--hidden" in hidden.text


@pytest.mark.asyncio
async def test_the_page_moves_an_entry_up_and_a_group_down(ui_client):
    gid, ids = _a_group_of_three()
    calls, patches = _page_patches()
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patches[0], patches[1], patches[2], patches[3]:
        await ui_client.post("/settings/menu/move", data={"kind": "entry", "id": ids[1], "direction": "up"})
        await ui_client.post("/settings/menu/move", data={"kind": "group", "id": gid, "direction": "down"})

    moved = calls[0]["entry_order"]
    assert moved.index(ids[1]) < moved.index(ids[0])
    groups = list(_grouped())
    assert calls[1]["group_order"].index(gid) == groups.index(gid) + 1


@pytest.mark.asyncio
async def test_the_page_resets(ui_client):
    calls, patches = _page_patches(saved={"hidden_entries": ["customers"]})
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patches[0], patches[1], patches[2], patches[3]:
        r = await ui_client.post("/settings/menu/reset")

    assert r.status_code == 200 and "menu-editor__row--hidden" not in r.text


@pytest.mark.asyncio
async def test_the_page_is_for_the_owner(ui_client):
    calls, patches = _page_patches()
    ui_client.cookies.update({"celerp_token": make_test_token(role="admin")})
    with patches[0], patches[1], patches[2], patches[3]:
        page = (await ui_client.get("/settings/menu")).text
        posted = (await ui_client.post("/settings/menu/hide", data={"kind": "entry", "id": "customers", "hidden": "1"})).text

    assert "Only the owner can change the side menu." in page and 'id="menu-editor"' not in page
    assert "Only the owner can change the side menu." in posted
    assert calls == []
