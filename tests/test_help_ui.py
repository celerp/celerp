# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The help viewer: search and browse at /help, one page at /help/{id}, its screenshots,
and the "Help for this page" panel in the top bar.

The index is replaced by a small fixed set of pages; the UI either talks to the real
API in process (``ui_get``) or to a stubbed API client where the failure path is the point.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlencode

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import ui.routes.help as help_route
from celerp.help import registry
from celerp.help.schema import HelpPage
from ui.api_client import APIError
from ui.i18n import t
from test_helpers import invite_user, make_test_token, register_admin, ui_get

_GOOD = Path(__file__).parent / "fixtures" / "help" / "good"


def _page(page_id: str, title: str, *, steps: list | None = None, routes: list | None = None,
          module: str = "celerp-contacts") -> HelpPage:
    return HelpPage(
        id=page_id, title=title, summary="A page.", keywords=[], routes=routes or ["/contacts"],
        permission=None, order=1,
        steps=steps or [{"text": "Open the list.", "labels": {}, "image": None,
                         "capture": {"route": "/contacts", "selector": "main", "actions": []}}],
        related=[], help_dir=_GOOD, source="first_party", module=module, module_display="Contacts",
    )


@pytest.fixture
def pages(monkeypatch):
    found = [
        _page("contacts.add-supplier", "Add a supplier", routes=["/contacts", "/contacts/{id}"],
              steps=[{"text": "Open {nav}, then add the supplier.", "labels": {"nav": "nav.contacts"},
                      "image": "img/add-item-1.webp",
                      "capture": {"route": "/contacts", "selector": "main", "actions": []}}]),
        _page("inventory.add-item", "Add an item", module="celerp-inventory", routes=["/inventory"]),
    ]
    monkeypatch.setattr(registry, "_index", registry.Index(pages={p.id: p for p in found}))
    return found


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


def _signed_in(ui_client):
    ui_client.cookies.update({"celerp_token": make_test_token()})
    return ui_client


@pytest.mark.asyncio
async def test_help_requires_login(ui_client, pages):
    for path in ("/help", "/help/contacts.add-supplier", "/help/img/contacts.add-supplier/1",
                 "/help/for-page?path=/contacts"):
        r = await ui_client.get(path)
        assert r.status_code in (302, 303), path
        assert r.headers["location"].startswith("/login"), path


@pytest.mark.asyncio
async def test_help_search_q_in_url(client, pages):
    headers = {"Authorization": f"Bearer {await register_admin(client)}"}
    r = await ui_get(headers, "/help?q=supplier")
    assert r.status_code == 200
    assert 'method="get"' in r.text and 'name="q"' in r.text and 'value="supplier"' in r.text
    assert 'href="/help/contacts.add-supplier"' in r.text
    assert 'href="/help/inventory.add-item"' not in r.text

    # No q: browse by module, every page listed under its module.
    browse = await ui_get(headers, "/help")
    assert t("help.browse") in browse.text
    assert 'href="/help/contacts.add-supplier"' in browse.text and 'href="/help/inventory.add-item"' in browse.text
    one = await ui_get(headers, "/help?module=celerp-inventory")
    assert 'href="/help/inventory.add-item"' in one.text and 'href="/help/contacts.add-supplier"' not in one.text

    none = await ui_get(headers, "/help?q=zzzzqqq")
    assert t("help.no_results") in none.text
    assert 'href="/ai"' in none.text


@pytest.mark.asyncio
async def test_help_page_labels_in_user_language_with_note(client, pages):
    headers = {"Authorization": f"Bearer {await register_admin(client)}"}
    r = await ui_get(headers, "/help/contacts.add-supplier", lang="th")
    assert r.status_code == 200
    assert f"Open {t('nav.contacts', 'th')}, then add the supplier." in r.text
    assert t("help.answers_in_your_language", "th") in r.text
    assert 'src="/help/img/contacts.add-supplier/1"' in r.text
    assert 'href="/help"' in r.text and t("acct.post_again_back", "th", page=t("help.title", "th")) in r.text

    missing = await ui_get(headers, "/help/contacts.nothing")
    assert missing.status_code == 404
    assert t("error.not_found_title") in missing.text and 'href="/help"' in missing.text


@pytest.mark.asyncio
async def test_help_page_step_with_unreadable_image_keeps_its_text(client, monkeypatch):
    """A step whose screenshot cannot be shown keeps its text and shows no image;
    the other steps keep theirs."""
    def _step(text, image):
        return {"text": text, "labels": {}, "image": image,
                "capture": {"route": "/contacts", "selector": "main", "actions": []}}

    page = _page("contacts.add-supplier", "Add a supplier", steps=[
        _step("Open the list.", "img/add-item-1.webp"),
        _step("Choose New.", "img/not-there.webp"),
        _step("Save the supplier.", None),
    ])
    monkeypatch.setattr(registry, "_index", registry.Index(pages={page.id: page}))
    headers = {"Authorization": f"Bearer {await register_admin(client)}"}
    api = (await client.get("/help/page/contacts.add-supplier", headers=headers)).json()
    assert [s["image_url"] for s in api["steps"]] == ["/help/img/contacts.add-supplier/1", None, None]

    r = await ui_get(headers, "/help/contacts.add-supplier")
    assert r.status_code == 200
    assert all(text in r.text for text in ("Open the list.", "Choose New.", "Save the supplier."))
    assert 'src="/help/img/contacts.add-supplier/1"' in r.text
    assert r.text.count("help-step__img") == 1


@pytest.mark.asyncio
async def test_help_img_unknown_step_404(client, pages):
    headers = {"Authorization": f"Bearer {await register_admin(client)}"}
    good = await ui_get(headers, "/help/img/contacts.add-supplier/1")
    assert good.status_code == 200
    assert good.headers["content-type"] == "image/webp"
    assert good.headers["x-content-type-options"] == "nosniff"
    assert good.content == (_GOOD / "img" / "add-item-1.webp").read_bytes()
    for path in ("/help/img/contacts.add-supplier/2", "/help/img/contacts.add-supplier/0",
                 "/help/img/contacts.add-supplier/x", "/help/img/inventory.add-item/1",
                 "/help/img/contacts.nothing/1"):
        assert (await ui_get(headers, path)).status_code == 404, path


async def _refused_to_every_bad_session(paths, tokens, ok):
    for name, token in tokens.items():
        for path in paths:
            r = await ui_get({"Authorization": f"Bearer {token}"}, path)
            assert r.status_code in (302, 303), (name, path)
            assert r.headers["location"].startswith("/login"), (name, path)
            assert "Add a supplier" not in r.text and r.content != ok[path], (name, path)


@pytest.mark.asyncio
async def test_help_browse_and_images_need_a_valid_session(client, session, pages):
    from jose import jwt
    from celerp.config import settings
    from celerp.services.session_tracker import invalidate_sessions
    paths = ("/help", "/help?module=celerp-contacts", "/help/img/contacts.add-supplier/1")
    token = await register_admin(client)
    ok = {}
    for path in paths:
        r = await ui_get({"Authorization": f"Bearer {token}"}, path)
        assert r.status_code == 200, path
        ok[path] = r.content
    claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    expired = jwt.encode({**claims, "exp": 1}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    forged = jwt.encode(claims, "not-the-key", algorithm=settings.jwt_algorithm)
    await _refused_to_every_bad_session(paths, {
        "unsigned": make_test_token(), "malformed": "not-a-token", "expired": expired, "forged": forged,
    }, ok)
    # A session ended on the server (signed out everywhere, or a newer sign-in) opens nothing.
    await invalidate_sessions(session, claims["sub"])
    await _refused_to_every_bad_session(paths, {"ended": token}, ok)


@pytest.mark.asyncio
async def test_help_page_api_error_shows_retry(ui_client, pages, monkeypatch):
    async def _fail(*_a, **_k):
        raise APIError(500, "boom")

    monkeypatch.setattr(help_route.api, "help_read", _fail)
    monkeypatch.setattr(help_route.api, "help_search", _fail)
    _signed_in(ui_client)
    for path in ("/help/contacts.add-supplier", "/help?q=supplier"):
        r = await ui_client.get(path)
        assert r.status_code == 200, path
        assert t("btn.retry") in r.text, path
        assert f'href="{path}"' in r.text, path


@pytest.mark.asyncio
async def test_help_for_page_lists_matching_pages(client, ui_client, pages):
    headers = {"Authorization": f"Bearer {await register_admin(client)}"}
    r = await ui_get(headers, "/help/for-page?path=/contacts/abc")
    assert r.status_code == 200
    assert t("help.for_this_page") in r.text
    assert 'href="/help/contacts.add-supplier"' in r.text
    assert 'href="/help"' in r.text and 'href="/ai"' in r.text

    empty = await ui_get(headers, "/help/for-page?path=/reports")
    assert t("help.none_for_this_page") in empty.text
    assert 'href="/help"' in empty.text and 'href="/ai"' in empty.text

    # The slot sits in the top bar of every page and loads the panel for the page it is on,
    # query included, so a tab of a tabbed page finds its own help.
    page = await ui_get(headers, "/help")
    assert 'hx-get="/help/for-page"' in page.text
    assert 'hx-vals="js:{path: location.pathname + location.search}"' in page.text


@pytest.mark.asyncio
async def test_help_for_page_long_list_address(client, pages):
    """Help for this page on a list whose address is long (a split's sku list, a non-Latin
    search) lists the list's pages; past the address bound the query is left out."""
    headers = {"Authorization": f"Bearer {await register_admin(client)}"}
    for address in ("/inventory?skus=" + "A" * 1000, "/inventory?q=" + "%E0%B8%81" * 2000):
        r = await ui_get(headers, "/help/for-page?" + urlencode({"path": address}))
        assert r.status_code == 200
        assert t("help.load_failed") not in r.text
        assert 'href="/help/inventory.add-item"' in r.text


@pytest.mark.asyncio
async def test_global_config_unknown_tab_shows_company_for_every_admin(client, session):
    """An admin who is not the owner asking for a tab that does not exist sees the Company
    tab, and the Help slot names that tab."""
    owner = {"Authorization": f"Bearer {await register_admin(client)}"}
    admin_token = await invite_user(client, session, owner, "adm@help.example", "admin")
    for headers in (owner, {"Authorization": f"Bearer {admin_token}"}):
        page = await ui_get(headers, "/settings/general?" + urlencode({"tab": "bogus&tab=users"}))
        assert page.status_code == 200
        assert 'hx-vals=\'{"path": "/settings/general?tab=company"}\'' in page.text


@pytest.mark.asyncio
async def test_global_config_panel_asks_for_the_tab_it_shows(client):
    """Global Config picks its tab from the role when the address names none, so the
    page tells the panel which tab it shows instead of leaving it to the address."""
    headers = {"Authorization": f"Bearer {await register_admin(client)}"}
    page = await ui_get(headers, "/settings/general")
    assert 'hx-vals=\'{"path": "/settings/general?tab=company"}\'' in page.text
    users = await ui_get(headers, "/settings/general?tab=users")
    assert 'hx-vals=\'{"path": "/settings/general?tab=users"}\'' in users.text


@pytest.mark.asyncio
async def test_help_ended_session_goes_to_login(ui_client, pages, monkeypatch):
    async def _ended(*_a, **_k):
        raise APIError(401, "Session expired")

    for name in ("help_read", "help_search", "help_for_route", "help_browse", "help_image"):
        monkeypatch.setattr(help_route.api, name, _ended)
    _signed_in(ui_client)
    for path in ("/help/contacts.add-supplier", "/help?q=supplier", "/help/for-page?path=/contacts",
                 "/help", "/help/img/contacts.add-supplier/1"):
        r = await ui_client.get(path)
        assert r.status_code in (302, 303), path
        assert r.headers["location"].startswith("/login"), path
    # The panel loads through HTMX, so the sign-in page replaces the window, not the slot.
    r = await ui_client.get("/help/for-page?path=/contacts", headers={"HX-Request": "true"})
    assert r.headers["hx-redirect"].startswith("/login")


@pytest.mark.asyncio
async def test_help_search_box_and_browse_headings(client, pages, monkeypatch):
    core = _page("core.users", "Adding staff", module="core")
    monkeypatch.setattr(registry, "_index", registry.Index(pages={p.id: p for p in [*pages, core]}))
    headers = {"Authorization": f"Bearer {await register_admin(client)}"}
    r = await ui_get(headers, "/help")
    assert 'class="form-input help-search__input"' in r.text
    assert f"<h2>{t('help.browse')}</h2>" in r.text
    assert "<h3>Contacts</h3>" in r.text
    assert f"<h3>{t('help.core_section')}</h3>" in r.text
    assert t("help.core_section") != t("help.title")
