"""What the pages say about a period's documents after it is voided, reopened and billed
again is read from the agreement as stored: the let-go document says which bill now covers
its period, the agreement's Periods row and History name every bill, and a refusal names the
document as it stands."""
from __future__ import annotations

import uuid

from bs4 import BeautifulSoup

from agreement_kit import agreement, freeze_day, generate_due, register
from ui.i18n import sentence, t


def _key() -> dict:
    return {"idempotency_key": str(uuid.uuid4())}


def _text(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


async def _void_and_reopen(api, eid) -> dict:
    first = (await generate_due(api, {}, eid)).json()
    assert (await api.post(f"/docs/{first['doc_id']}/void", json={"reason": "x", **_key()})).status_code == 200
    assert (await api.post(f"/subscriptions/{eid}/periods/0/reopen")).status_code == 200
    return first


async def _doc_page(ui, doc_id: str) -> str:
    """A document's page with the note it loads from its agreement."""
    page = BeautifulSoup((await ui.get(f"/docs/{doc_id}")).text, "html.parser")
    [note] = page.select(".doc-period-badge [hx-get]")
    return page.get_text(" ", strip=True) + " " + _text((await ui.get(note["hx-get"])).text)


async def test_a_let_go_document_names_the_bill_that_now_covers_its_period(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01")
    first = await _void_and_reopen(owner_ui.api, eid)
    reopened = await _doc_page(owner_ui, first["doc_id"])
    assert t("subscriptions.doc_period_reopened") in reopened
    assert t("subscriptions.doc_void_period") not in reopened
    second = (await generate_due(owner_ui.api, {}, eid)).json()
    rebilled = await _doc_page(owner_ui, first["doc_id"])
    assert sentence("subscriptions.doc_period_rebilled", newer=second["ref_id"]) in rebilled
    current = await _doc_page(owner_ui, second["doc_id"])
    assert second["ref_id"] not in current.split(sentence("subscriptions.doc_bills_period",
                                                           start="2026-03-01", end="2026-03-31"))[-1]


async def test_the_periods_row_and_history_name_every_bill_of_a_rebilled_period(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01")
    first = await _void_and_reopen(owner_ui.api, eid)
    second = (await generate_due(owner_ui.api, {}, eid)).json()
    rows = (await owner_ui.api.get(f"/subscriptions/{eid}/periods")).json()["rows"]
    row = next(r for r in rows if r["period_index"] == 0)
    assert row["ref_id"] == second["ref_id"]
    assert [r["ref_id"] for r in row["released"]] == [first["ref_id"]]
    page = BeautifulSoup((await owner_ui.get(f"/subscriptions/{eid}")).text, "html.parser")
    periods = page.select_one("#sub-periods").get_text(" ", strip=True)
    assert f"{t('subscriptions.period_earlier_bills')} {first['ref_id']}" in periods
    history = _text((await owner_ui.get(f"/docs/{eid}/history?per_page=100")).text)
    dates = {"start": "2026-03-01", "end": "2026-03-31"}
    assert sentence("subscriptions.history_billed_ref", ref=first["ref_id"], **dates) in history
    assert sentence("subscriptions.history_reopened_ref", ref=first["ref_id"], **dates) in history
    assert sentence("subscriptions.history_billed_ref", ref=second["ref_id"], **dates) in history


async def test_a_refused_finalize_names_the_document_as_it_stands(client, monkeypatch):
    """A purchase order finalizes into a bill and draws a new number; when the guard refuses
    it, that number is rolled back, so the refusal names the order the user is looking at."""
    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01", doc_type="subscription_po")
    first = (await generate_due(client, hd, eid)).json()
    assert (await client.post(f"/docs/{first['doc_id']}/revert-to-draft", headers=hd, json=_key())).status_code == 200
    assert (await client.post(f"/subscriptions/{eid}/periods/0/reopen", headers=hd)).status_code == 200
    second = (await generate_due(client, hd, eid)).json()
    refused = await client.post(f"/docs/{first['doc_id']}/finalize", headers=hd)
    assert refused.status_code == 409
    stored = (await client.get(f"/docs/{first['doc_id']}", headers=hd)).json()["ref_id"]
    params = refused.json()["detail"]["params"]
    assert params["ref"] == stored and params["newer"] == second["ref_id"], params
    assert refused.json()["detail"]["message"].startswith(stored)


async def test_an_escape_the_page_sends_itself_clears_nothing_the_user_typed(owner_ui, monkeypatch):
    """A refused inline edit restores itself by sending Escape; the panel's Escape handler
    clears typed text only for a key the user pressed."""
    freeze_day(monkeypatch, "2026-03-15")
    await agreement(owner_ui.api, {}, start="2026-03-01")
    html = (await owner_ui.get("/subscriptions?direction=sales")).text
    handler = next(s for s in BeautifulSoup(html, "html.parser").find_all("script") if "data-esc-clear" in s.text).text
    assert "e.isTrusted" in handler.split("data-esc-clear")[0]


async def test_the_summary_offers_its_send_beside_what_happened(owner_ui, session, monkeypatch):
    """Every summary row's action sits in the description cell: no column is left empty."""
    from agreement_kit import company_id, contact, schedule, tick

    freeze_day(monkeypatch, "2026-07-01")
    monkeypatch.setattr("celerp.config.settings.gateway_token", "")
    monkeypatch.setattr("celerp.config.settings.smtp_host", "")
    eid = await agreement(owner_ui.api, {}, start="2026-07-01",
                          contact_id=await contact(owner_ui.api, {}, "member@example.test"))
    assert (await schedule(owner_ui.api, {}, eid, generation_mode="auto_invoice", auto_send=True)).status_code == 200
    await tick(session, await company_id(owner_ui.api, {}), "2026-07-01")
    table = BeautifulSoup((await owner_ui.get("/subscriptions/summary")).text, "html.parser").select_one("table")
    headers = [th.get_text(strip=True) for th in table.select("thead th")]
    assert all(headers), headers
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        assert len(cells) == len(headers)
        buttons = [i for i, td in enumerate(cells) if td.select("button, [hx-post]")]
        assert buttons in ([], [headers.index(t("th.description"))]), (headers, buttons)
    assert any(tr.select("[hx-post]") for tr in table.select("tbody tr"))
