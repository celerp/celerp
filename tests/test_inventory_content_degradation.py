# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Honest degradation of the inventory content fragment under a read failure.

The list, valuation, and required static metadata back a real table. When any
of them actually fails, the fragment must say so and offer a retry - never
render a blank table that reads as an empty catalog. A session-expiry (401)
still belongs to the caller's auth handler, so it propagates rather than being
swallowed into an error box. Static metadata (units, category labels) is
supplied by the shared snapshot, so the fragment never fetches them itself.
"""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient

import ui.routes.inventory as inv
from ui.api_client import APIError
from test_helpers import make_test_token


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


def _params(**over):
    p = {
        "q": "", "skus": "", "page": 1, "status": "", "category": "",
        "inventory_type": "", "location_id": "", "source": "", "filter": "",
        "on_memo_to": "", "consigned_from": "", "attr_filters": {},
        "sort": "", "dir": "desc", "per_page": 50, "cols": [],
    }
    p.update(over)
    return p


_COMPANY = {"currency": "USD", "settings": {"vertical": "jewelry"}}


async def _content(monkeypatch, *, valuation=None, items=None, p=None):
    """Render _inventory_content with the two dynamic getters stubbed.

    Passing an APIError instance for valuation or items makes that getter raise.
    """
    async def _get_valuation(_token, **_kw):
        if isinstance(valuation, Exception):
            raise valuation
        return valuation or {}

    async def _list_items(_token, _params):
        if isinstance(items, Exception):
            raise items
        return {"items": items or [], "total": len(items or [])}

    monkeypatch.setattr(inv.api, "get_valuation", _get_valuation)
    monkeypatch.setattr(inv.api, "list_items", _list_items)
    # Any internal metadata fetch would be a regression: units/labels are passed in.
    async def _forbidden(*_a, **_k):
        raise AssertionError("static metadata must come from the passed snapshot")
    monkeypatch.setattr(inv.api, "get_units", _forbidden)
    monkeypatch.setattr(inv.api, "get_category_display_names", _forbidden)

    return await inv._inventory_content(
        "tok", p or _params(), [], {}, {}, _COMPANY, [],
        [{"name": "each"}], {"ring": "Rings"},
        lang="en", role="owner",
    )


@pytest.mark.asyncio
async def test_list_read_failure_renders_honest_error_not_blank_table(monkeypatch):
    frag = await _content(monkeypatch, valuation={}, items=APIError(503, "saturated"))
    html = to_xml(frag)
    # An honest, retryable error - never an empty-catalog table.
    assert "flash--error" in html
    assert 'id="inventory-content"' in html
    assert "/inventory/content" in html  # retry targets the content URL
    assert "<table" not in html.lower()


@pytest.mark.asyncio
async def test_valuation_read_failure_renders_honest_error(monkeypatch):
    frag = await _content(monkeypatch, valuation=APIError(504, "timeout"), items=[])
    html = to_xml(frag)
    assert "flash--error" in html
    assert "<table" not in html.lower()


@pytest.mark.asyncio
async def test_session_expiry_propagates_to_caller(monkeypatch):
    # 401 is the auth handler's job; the fragment must not swallow it.
    with pytest.raises(APIError) as exc:
        await _content(monkeypatch, valuation=APIError(401, "expired"), items=[])
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_static_metadata_comes_from_snapshot_not_a_fetch(monkeypatch):
    # The stubs for get_units / get_category_display_names raise if called; a
    # successful render proves the passed snapshot values were used instead.
    frag = await _content(
        monkeypatch, valuation={"category_counts": {}}, items=[],
    )
    html = to_xml(frag)
    assert 'id="inventory-content"' in html


def _install_inventory_getters(monkeypatch, *, company=None, company_error=None,
                               metadata_error=None):
    """Stub every getter the /inventory full page reads and count the calls.

    Returns a dict of counters: `company` (fresh authorization reads) and
    `static` (the six cached static getters). `company_error`/`metadata_error`
    make the respective read raise.
    """
    import ui.api_client as api

    calls = {"company": 0, "static": 0}
    company = company if company is not None else {"currency": "USD", "settings": {"vertical": "jewelry"}}

    async def _get_company(_token):
        calls["company"] += 1
        if company_error is not None:
            raise company_error
        return company

    def _static(retval):
        async def _f(_token):
            calls["static"] += 1
            if metadata_error is not None:
                raise metadata_error
            return retval
        return _f

    async def _valuation(_token, **_kw):
        return {}

    async def _list_items(_token, _params):
        return {"items": [], "total": 0}

    api._reset_metadata_cache_for_tests()
    monkeypatch.setattr(api, "get_company", _get_company)
    monkeypatch.setattr(api, "get_item_schema", _static([]))
    monkeypatch.setattr(api, "get_all_category_schemas", _static({}))
    monkeypatch.setattr(api, "get_category_display_names", _static({}))
    monkeypatch.setattr(api, "get_column_prefs", _static({}))
    monkeypatch.setattr(api, "get_locations", _static({"items": []}))
    monkeypatch.setattr(api, "get_units", _static([]))
    monkeypatch.setattr(api, "get_valuation", _valuation)
    monkeypatch.setattr(api, "list_items", _list_items)
    return calls


@pytest.mark.asyncio
async def test_inventory_page_reads_company_fresh_once(ui_client, monkeypatch):
    # Items 4 and 5: the page reads company fresh exactly once per request and
    # base_shell reuses those settings rather than performing a second read.
    calls = _install_inventory_getters(monkeypatch)
    r = await ui_client.get("/inventory", cookies={"celerp_token": make_test_token()})
    assert r.status_code == 200
    assert calls["company"] == 1


@pytest.mark.asyncio
async def test_warm_fragment_reads_company_fresh_and_zero_static(ui_client, monkeypatch):
    # Item 4: after the static snapshot is warm, a second inventory fragment
    # request still reads company fresh (authorization is never cached) and makes
    # zero of the six static getter calls.
    calls = _install_inventory_getters(monkeypatch)
    cookies = {"celerp_token": make_test_token()}
    await ui_client.get("/inventory", cookies=cookies)
    assert calls["static"] == 6, "cold page primes the static snapshot once"
    company_before = calls["company"]
    r = await ui_client.get(
        "/inventory/content", cookies=cookies, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200
    assert calls["static"] == 6, "warm fragment makes zero new static getter calls"
    assert calls["company"] == company_before + 1, "company is still read fresh"


@pytest.mark.asyncio
async def test_company_read_failure_does_not_substitute_empty_settings(ui_client, monkeypatch):
    # Item 1: when the fresh company read fails, the page must not fall back to
    # {} settings and continue; it renders an honest retryable error, and the
    # authorization check is never run against fabricated empty settings.
    calls = _install_inventory_getters(monkeypatch, company_error=APIError(503, "saturated"))
    perm_calls = []
    real_perm = inv.role_has_permission
    monkeypatch.setattr(
        inv, "role_has_permission",
        lambda settings, role, perm: perm_calls.append((settings, perm)) or real_perm(settings, role, perm),
    )
    r = await ui_client.get("/inventory", cookies={"celerp_token": make_test_token()})
    assert r.status_code == 200
    assert "flash--error" in r.text
    assert not perm_calls, "authorization must not be decided on fabricated {} settings"


@pytest.mark.asyncio
async def test_company_read_failure_reads_company_exactly_once(ui_client, monkeypatch):
    # The failed company read is the page's only company read: the error shell it
    # renders must not perform a hidden SECOND read. A second read on the error
    # path both doubles load during an outage and (below) fabricates a default
    # sidebar from the settings that read cannot return.
    calls = _install_inventory_getters(monkeypatch, company_error=APIError(503, "saturated"))
    r = await ui_client.get("/inventory", cookies={"celerp_token": make_test_token()})
    assert r.status_code == 200
    assert "flash--error" in r.text
    assert calls["company"] == 1, "no hidden second company read on the error path"


@pytest.mark.asyncio
async def test_error_page_omits_default_granted_revoked_nav(ui_client, monkeypatch):
    # When the company read fails there is no authorization context, so the error
    # page must render a minimal shell with NO permission-filtered sidebar. Building
    # that sidebar from registry DEFAULT grants would present, as available, an entry
    # the company had revoked. "Company Details" is gated by manage_company_settings,
    # which the registry grants owner by default, so it is exactly such an entry.
    from ui.i18n import t
    company_details = t("nav.company_details", "en")

    # Positive control: on a healthy page the owner does see the default-granted
    # nav entry, so its absence below is a real omission and not a missing label.
    _install_inventory_getters(monkeypatch)
    ok = await ui_client.get("/inventory", cookies={"celerp_token": make_test_token()})
    assert ok.status_code == 200
    assert company_details in ok.text

    # Company read fails: the error page must show neither that default-granted
    # entry nor any permission-filtered sidebar link.
    _install_inventory_getters(monkeypatch, company_error=APIError(503, "saturated"))
    err = await ui_client.get("/inventory", cookies={"celerp_token": make_test_token()})
    assert err.status_code == 200
    assert "flash--error" in err.text
    assert company_details not in err.text
    assert "nav-link" not in err.text, "no permission-filtered sidebar on the no-auth error page"


@pytest.mark.asyncio
async def test_revoked_view_inventory_denied_even_when_metadata_fails(ui_client, monkeypatch):
    # Item 2: a dynamically revoked view_inventory user is redirected away even
    # when the static metadata read fails, because company (and its live
    # role_grants) is read fresh before any static metadata is touched.
    _install_inventory_getters(monkeypatch, metadata_error=APIError(503, "down"))
    monkeypatch.setattr(inv, "role_has_permission", lambda settings, role, perm: False)
    r = await ui_client.get("/inventory", cookies={"celerp_token": make_test_token()})
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"


@pytest.mark.asyncio
async def test_static_failure_after_company_success_shows_error_in_shell(ui_client, monkeypatch):
    # Item 3: company read succeeds but a static metadata read fails, so the page
    # renders the normal authenticated shell with an honest content error and a
    # retry, never a blank catalog.
    _install_inventory_getters(monkeypatch, metadata_error=APIError(503, "down"))
    r = await ui_client.get("/inventory", cookies={"celerp_token": make_test_token()})
    assert r.status_code == 200
    assert "flash--error" in r.text
    # The content region is the honest error fragment (empty-state + retry), not
    # a rendered data table dressed up as an empty catalog.
    assert 'id="inventory-content"' in r.text
    assert "empty-state" in r.text
