# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A quotation an earlier version totalled by its own rule says so on its page, with
Recalculate total; once recalculated it offers Restore previous total. A refused convert
links straight to the notice, and a completed or void quotation keeps its total, saying why.
Each case runs the real list page against the in-process API on a demo company."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from test_list_carried_totals import _as_saved_by_earlier_version, _quotation, _state
from ui.i18n import t


@asynccontextmanager
async def _browser(token: str):
    """The real UI app signed in with ``token``, its API client routed to the in-process API."""
    from celerp.main import app as api_app
    from ui.app import app as ui_app

    def _bridged(tok=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        merged = dict(headers or {})
        if tok is not None:
            merged["Authorization"] = f"Bearer {tok}"
        return AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test",
                           headers=merged, timeout=timeout, follow_redirects=follow_redirects)

    with patch("ui.api_client._local_client", _bridged):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                               cookies={"celerp_token": token}) as c:
            yield c


def _token(auth) -> str:
    return auth["headers"]["Authorization"].split()[1]


def _toast(r) -> dict:
    return json.loads(r.headers["HX-Trigger"])["celerpToast"]


async def test_old_rule_total_reads_both_totals_or_nulls(client, session):
    auth, lid = await _quotation(client, session)
    h = auth["headers"]
    r = await client.get(f"/lists/{lid}/old-rule-total", headers=h)
    assert r.status_code == 200 and r.json() == {"stored": None, "current": None}, r.text
    await _as_saved_by_earlier_version(session, auth["company_id"], lid)
    r = await client.get(f"/lists/{lid}/old-rule-total", headers=h)
    assert r.json() == {"stored": "100.00", "current": "99.00"}, r.text
    assert (await client.get("/lists/no-such-list/old-rule-total", headers=h)).status_code == 404


async def test_the_page_offers_recalculate_then_restore(client, session):
    auth, lid = await _quotation(client, session)
    cid = auth["company_id"]
    async with _browser(_token(auth)) as ui:
        page = (await ui.get(f"/lists/{lid}")).text
        assert 'id="list-old-rule-notice"' not in page

        await _as_saved_by_earlier_version(session, cid, lid)
        page = (await ui.get(f"/lists/{lid}")).text
        assert 'id="list-old-rule-notice"' in page
        assert f'hx-post="/lists/{lid}/action/recalculate-total"' in page
        assert t("documents.btn_recalculate_total") in page

        r = await ui.post(f"/lists/{lid}/action/recalculate-total")
        assert r.status_code == 204 and r.headers["HX-Redirect"] == f"/lists/{lid}", r.text
        assert (await _state(session, cid, lid))["total"] == 99.0
        page = (await ui.get(f"/lists/{lid}")).text
        assert f'hx-post="/lists/{lid}/action/restore-total"' in page
        assert f'hx-post="/lists/{lid}/action/recalculate-total"' not in page

        r = await ui.post(f"/lists/{lid}/action/restore-total")
        assert r.status_code == 204, r.text
        assert (await _state(session, cid, lid))["total"] == 100.0
        page = (await ui.get(f"/lists/{lid}")).text
        assert f'hx-post="/lists/{lid}/action/recalculate-total"' in page

        # Recalculated and then edited back to agreeing totals: nothing left to offer.
        await ui.post(f"/lists/{lid}/action/recalculate-total")
        h = auth["headers"]
        assert (await client.post(f"/lists/{lid}/revert-to-draft", headers=h, json={})).status_code == 200
        r = await client.patch(f"/lists/{lid}", headers=h, json={"notes": "Agreed"})
        assert r.status_code == 200, r.text
        page = (await ui.get(f"/lists/{lid}")).text
        assert 'id="list-old-rule-notice"' not in page


async def test_a_refused_convert_links_to_the_notice(client, session):
    auth, lid = await _quotation(client, session)
    await _as_saved_by_earlier_version(session, auth["company_id"], lid)
    async with _browser(_token(auth)) as ui:
        r = await ui.post(f"/lists/{lid}/action/convert-invoice")
    toast = _toast(r)
    assert toast["message"] == t("documents.err_list_old_rule_total", stored="100.00", current="99.00")
    assert toast["action"] == {"href": "#list-old-rule-notice", "label": t("documents.btn_recalculate_total")}


async def test_a_void_quotation_keeps_its_total_and_says_why(client, session):
    auth, lid = await _quotation(client, session)
    cid = auth["company_id"]
    await _as_saved_by_earlier_version(session, cid, lid)
    assert (await client.post(f"/lists/{lid}/void", headers=auth["headers"])).status_code == 200
    async with _browser(_token(auth)) as ui:
        page = (await ui.get(f"/lists/{lid}")).text
        assert f'hx-post="/lists/{lid}/action/recalculate-total"' in page
        for action in ("recalculate-total", "restore-total"):
            r = await ui.post(f"/lists/{lid}/action/{action}")
            assert _toast(r)["message"] == t("documents.err_list_total_status")
    assert (await _state(session, cid, lid))["total"] == 100.0
