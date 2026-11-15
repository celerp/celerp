# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Donors page: each donor's gifts in a period, their statement and the send.

Initial conditions per test: a newly registered company signed in to the UI (owner_ui), its
API calls answered in process; the gift books, donors, invoices and payments of test_donors are
made through the API; email goes to an in-memory mailbox (test_donors._mailbox).
"""
from __future__ import annotations

import json

import pytest

from celerp.services.email import EmailResult
from test_donors import PERIOD, _mailbox, _statement_donors
from ui.i18n import sentence, t

pytestmark = pytest.mark.asyncio

AUTH = {"headers": {}}
PAGE = f"/donors?from={PERIOD['from']}&to={PERIOD['to']}"


def _toast(r) -> dict:
    return json.loads(r.headers.get("HX-Trigger") or "{}").get("celerpToast") or {}


async def _send(ui, **extra):
    return await ui.post("/donors/statements/send", data={**PERIOD, **extra})


async def test_donors_page_lists_each_donors_gifts_and_statement(owner_ui):
    """Each donor of the period with their total, each gift and where it came from, their email
    or No email, whether their statement went out, and a link to the statement PDF."""
    donors = await _statement_donors(owner_ui.api, AUTH)
    r = await owner_ui.get(PAGE)
    assert r.status_code == 200, r.text[:300]
    for text in ("Cy Donor", "Di Donor", "cy@test.test", t("donors.no_email"), t("donors.statement_not_sent")):
        assert text in r.text, text
    assert "75.00" in r.text and "INV-" in r.text
    assert f'href="/donors/{donors["email"]}/statement.pdf?from={PERIOD["from"]}&amp;to={PERIOD["to"]}"' in r.text
    pdf = await owner_ui.get(f"/donors/{donors['email']}/statement.pdf", params=PERIOD)
    assert pdf.status_code == 200 and pdf.headers["content-type"].startswith("application/pdf")


async def test_donor_statements_send_from_the_page_confirmed_once_and_again_explicitly(owner_ui, monkeypatch):
    """Send needs the confirmation ticked; it reports how many went out and who was skipped, the
    page then shows each donor's statement as sent, a second send emails nobody again, and Send
    again on one donor emails only that donor."""
    donors = await _statement_donors(owner_ui.api, AUTH)
    sent = _mailbox(monkeypatch)
    refused = await _send(owner_ui)
    assert _toast(refused)["type"] == "error" and t("donors.err_confirm_send") in _toast(refused)["message"]
    assert sent == []
    r = await _send(owner_ui, confirm="on")
    assert r.status_code < 400 and _toast(r)["type"] == "success", (r.status_code, _toast(r))
    assert r.headers["HX-Redirect"] == PAGE
    assert sentence("donors.done_sent", sent=1, skipped=1) in _toast(r)["message"]
    assert [m["to"] for m in sent] == ["cy@test.test"]
    page = await owner_ui.get(PAGE)
    assert t("donors.statement_sent") in page.text and t("btn.send_again") in page.text
    assert _toast(await _send(owner_ui, confirm="on"))["type"] == "success" and len(sent) == 1
    again = await _send(owner_ui, confirm="on", resend=donors["email"], seen="1")
    assert _toast(again)["type"] == "success" and len(sent) == 2
    assert sentence("donors.done_resent", name="Cy Donor") in _toast(again)["message"]
    # the same press arriving twice sends once and says so; the page's button names the two sends
    twice = await _send(owner_ui, confirm="on", resend=donors["email"], seen="1")
    assert len(sent) == 2 and sentence("donors.done_already", already=1) in _toast(twice)["message"]
    assert '"seen": 2}' in (await owner_ui.get(PAGE)).text


async def test_a_failed_statement_names_the_donor_and_sending_again_retries(owner_ui, monkeypatch):
    """A send the mail server refuses records nothing: the answer names the donor it could not
    reach, and the next Send emails that donor."""
    await _statement_donors(owner_ui.api, AUTH)
    attempts: list[str] = []

    async def refused(to, subject, body_html, body_text="", **kw):
        attempts.append(to)
        return EmailResult(ok=False, detail="mailbox unavailable")

    monkeypatch.setattr("celerp.services.email.send_email", refused)
    r = await _send(owner_ui, confirm="on")
    assert _toast(r)["type"] == "warning", _toast(r)
    assert sentence("donors.done_failed", names="Cy Donor") in _toast(r)["message"]
    assert t("donors.statement_not_sent") in (await owner_ui.get(PAGE)).text
    sent = _mailbox(monkeypatch)
    await _send(owner_ui, confirm="on")
    assert attempts == ["cy@test.test"] and [m["to"] for m in sent] == ["cy@test.test"]


async def test_statement_wording_saved_on_the_page(owner_ui):
    """The wording saves where it is shown and is read back from what is stored; an empty
    wording is refused and the stored one stays."""
    r = await owner_ui.put("/donors/statements/wording", data={"text": "Thank you, {name}, for every gift."})
    assert r.status_code == 200 and "Thank you, {name}, for every gift." in r.text, r.text[:300]
    stored = (await owner_ui.api.get("/donors/statements/wording")).json()
    assert stored == {"text": "Thank you, {name}, for every gift.", "version": 1}
    for blank in ("", "  ", "\u200b", "\x00\n"):
        refused = await owner_ui.put("/donors/statements/wording", data={"text": blank})
        said = _toast(refused)
        # The refusal says what to do, and the box shows the wording still stored.
        assert said["type"] == "error" and said["message"] == "Write the statement wording before saving it.", said
        assert "Thank you, {name}, for every gift." in refused.text, refused.text[:300]
        assert (await owner_ui.api.get("/donors/statements/wording")).json() == stored


@pytest.mark.parametrize(("vertical", "shown"), [("nonprofit", True), ("consulting", False), (None, False)])
async def test_donors_in_the_sidebar_of_a_nonprofit_only(vertical, shown):
    from ui.components.shell import module_nav

    keys = {item.get("key") for item in module_nav({"vertical": vertical} if vertical else {})}
    assert ("donors" in keys) is shown
