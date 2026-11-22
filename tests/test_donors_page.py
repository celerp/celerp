# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Donors page: each donor's gifts in a period, their statement and the send.

Initial conditions per test: a newly registered company signed in to the UI (owner_ui), its
API calls answered in process; the gift books, donors, invoices and payments of test_donors are
made through the API; email goes to an in-memory mailbox (test_donors._mailbox).
"""
from __future__ import annotations

import asyncio
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
    assert '"seen":' not in (await owner_ui.get(PAGE)).text  # no Send again: the statement reads not sent
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


async def test_the_toast_after_a_send_reads_as_success_only_when_a_statement_went_out():
    """No failed statement is not a delivered one: a send that reached nobody (every donor
    skipped, or the donor named for Send again no longer listed) is not shown as a success."""
    from ui.routes.donors import _sent_message
    skipped = {"name": "Di Donor", "state": "no_email"}
    assert _sent_message([skipped], {}) == (sentence("donors.done_sent", sent=0, skipped=1), "warning")
    assert _sent_message([], {}) == (sentence("donors.done_sent", sent=0, skipped=0), "warning")
    assert _sent_message([], {"gone": "1"}) == (sentence("donors.done_none"), "warning")
    assert _sent_message([skipped], {"di": "1"}) == (sentence("donors.done_none"), "warning")
    sent = {"name": "Cy Donor", "state": "sent"}
    assert _sent_message([sent, skipped], {}) == (sentence("donors.done_sent", sent=1, skipped=1), "success")
    assert _sent_message([{**sent, "repeat": True}], {})[1] == "success"
    assert _sent_message([sent], {"cy": "1"}) == (sentence("donors.done_resent", name="Cy Donor"), "success")


async def test_a_send_whose_answer_did_not_come_back_shows_each_statement_as_stored(owner_ui, monkeypatch):
    """A batch can take longer than the page waits for the API. When no answer comes back the
    send may still be running: the toast says so and names whose statement is still being sent,
    never a timeout or a failure, and the page reads each statement as stored, then as sent."""
    import ui.api_client as api

    await _statement_donors(owner_ui.api, AUTH)
    going, release, sent = asyncio.Event(), asyncio.Event(), []

    async def slow(to, subject, body_html, body_text="", **kw):
        going.set()
        await release.wait()
        sent.append(to)
        return EmailResult(ok=True, detail="")

    monkeypatch.setattr("celerp.services.email.send_email", slow)
    real, running = api.send_donor_statements, []

    async def unanswered(*args):
        running.append(asyncio.ensure_future(real(*args)))
        await going.wait()  # the attempt is recorded and its email is with the mail server
        raise api._no_response(504, api.timeout_message())

    monkeypatch.setattr(api, "send_donor_statements", unanswered)
    r = await _send(owner_ui, confirm="on")
    toast = _toast(r)
    assert r.headers.get("HX-Redirect") == PAGE and toast.get("type") == "info", (r.status_code, toast)
    assert sentence("donors.done_pending") in toast["message"], toast
    assert sentence("donors.done_sending", names="Cy Donor") in toast["message"], toast
    assert api.timeout_message() not in toast["message"]
    page = await owner_ui.get(PAGE)
    assert t("donors.statement_sending") in page.text and t("btn.check_again") in page.text
    assert t("donors.statement_sent") not in page.text and '"seen":' not in page.text  # not sent yet, no Send again
    release.set()
    await running[0]
    page = await owner_ui.get(PAGE)
    assert sent == ["cy@test.test"] and t("donors.statement_sent") in page.text
    assert t("donors.statement_sending") not in page.text


async def test_a_pending_sends_status_check_failure_shows_as_pending_with_its_own_error(owner_ui, monkeypatch):
    """When a send's answer does not come back and the follow-up read of who is still sending
    also fails, the toast still says the send may be pending, with that read's own error
    alongside it; it never reads as nobody still sending."""
    import ui.api_client as api
    from ui.api_client import APIError

    await _statement_donors(owner_ui.api, AUTH)

    async def unanswered(*args):
        raise api._no_response(504, api.timeout_message())

    async def broken_preview(*args):
        raise APIError(500, "boom")

    monkeypatch.setattr(api, "send_donor_statements", unanswered)
    monkeypatch.setattr(api, "donor_statements_preview", broken_preview)
    r = await _send(owner_ui, confirm="on")
    toast = _toast(r)
    assert r.headers.get("HX-Redirect") == PAGE and toast.get("type") == "info", (r.status_code, toast)
    assert sentence("donors.done_pending") in toast["message"], toast
    assert "boom" in toast["message"], toast


async def _activity(ui, contact: str) -> str:
    r = await ui.get(f"/contacts/{contact}/tab/activity")
    assert r.status_code == 200, r.text[:300]
    return r.text


@pytest.mark.parametrize("ok", [True, False])
async def test_a_donors_activity_reads_sent_only_when_the_statement_email_went_out(owner_ui, monkeypatch, ok):
    """The statement's event is the send's attempt record, kept whatever the email's outcome: the
    donor's activity says the statement was sent only when the email went out, and that it failed
    when the transport refused it."""
    async def answer(to, subject, body_html, body_text="", **kw):
        return EmailResult(ok=ok, detail="accepted" if ok else "refused")
    monkeypatch.setattr("celerp.services.email.send_email", answer)
    donors = await _statement_donors(owner_ui.api, AUTH)
    await _send(owner_ui, confirm="on")
    text = await _activity(owner_ui, donors["email"])
    labels = {key: t(key) in text for key in ("event.crm.contact.statement_sent", "event.crm.contact.statement_sent.failed",
                                              "event.crm.contact.statement_sent.unconfirmed")}
    assert labels == {"event.crm.contact.statement_sent": ok, "event.crm.contact.statement_sent.failed": not ok,
                      "event.crm.contact.statement_sent.unconfirmed": False}, labels


async def test_a_donors_activity_reads_not_confirmed_while_the_statement_email_is_with_the_mail_server(
        owner_ui, monkeypatch):
    """While the email is with the mail server unanswered the donor's activity says it is not
    confirmed, never sent; once the server accepts it, sent."""
    going, release = asyncio.Event(), asyncio.Event()

    async def slow(to, subject, body_html, body_text="", **kw):
        going.set()
        await release.wait()
        return EmailResult(ok=True, detail="")
    monkeypatch.setattr("celerp.services.email.send_email", slow)
    donors = await _statement_donors(owner_ui.api, AUTH)
    send = asyncio.ensure_future(_send(owner_ui, confirm="on"))
    await asyncio.wait_for(going.wait(), 10)
    text = await _activity(owner_ui, donors["email"])
    assert t("event.crm.contact.statement_sent.unconfirmed") in text
    assert t("event.crm.contact.statement_sent") not in text
    release.set()
    await asyncio.wait_for(send, 30)
    text = await _activity(owner_ui, donors["email"])
    assert t("event.crm.contact.statement_sent") in text
    assert t("event.crm.contact.statement_sent.unconfirmed") not in text


async def test_a_statement_the_earlier_build_recorded_reads_sent_in_activity():
    """The earlier build kept only the answer ({"result": {"state": "sent"}}), written once the email
    had gone out: the activity reader labels it sent. With no recorded outcome it is not confirmed."""
    from ui.components.activity import history_rows
    event = {"id": 1, "event_type": "crm.contact.statement_sent", "ts": "2026-03-31T00:00:00+00:00", "data": {}}
    legacy = history_rows([{**event, "metadata": {"result": {"state": "sent", "sent": 1}}}])
    bare = history_rows([{**event, "metadata": {}}])
    assert [r["event"] for r in legacy] == [t("event.crm.contact.statement_sent")]
    assert [r["event"] for r in bare] == [t("event.crm.contact.statement_sent.unconfirmed")]
