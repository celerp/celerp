# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What a subscription's email send records, and what the user is told about it.

A send the mail server never took is a failed send with a retry; only a send cut off after
the message was handed over may have arrived, and that one is never sent again by Celerp.
Every reason and refusal reads in the reader's language, a missing email service links to
the page that sets it up, and an email still waiting to go out is shown until it goes."""
from __future__ import annotations

import ast
import uuid
from datetime import date
from pathlib import Path

import pytest

from agreement_kit import (
    agreement, billed_docs, company_id, contact, freeze_day, register, schedule, summary, template, tick,
)
from smtp_support import SmtpStub, smtp_settings
from ui.i18n import t


async def _sending(client, hd, *, start="2026-07-01", catchup=0) -> str:
    eid = await agreement(client, hd, start=start, contact_id=await contact(client, hd, "member@example.test"))
    r = await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=catchup, auto_send=True)
    assert r.status_code == 200, r.text
    return eid


def _closed_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _send_row(client, hd, eid: str, **params) -> dict:
    [row] = [r for r in (await summary(client, hd, **params))["rows"]
             if r["entity_id"] == eid and r["kind"].startswith("send")]
    return row


@pytest.mark.parametrize("server", ["closed port", "MAIL", "RCPT", "DATA", "END"])
async def test_a_send_the_mail_server_never_took_is_failed_and_can_be_retried(client, session, monkeypatch, server):
    """Refused before or at the hand-over (nothing listening, sender, recipient, DATA or the
    message itself refused): the send failed, the summary offers Retry, and Retry sends."""
    freeze_day(monkeypatch, "2026-07-01")
    async with SmtpStub(refuse=None if server == "closed port" else server) as stub:
        with smtp_settings(_closed_port() if server == "closed port" else stub.port):
            hd = await register(client)
            cid = await company_id(client, hd)
            eid = await _sending(client, hd)
            await tick(session, cid, "2026-07-01")
            assert (await template(client, hd, eid))["periods"]["0"]["send"]["state"] == "failed"
            row = await _send_row(client, hd, eid)
            assert row["kind"] == "send_failed" and row["retry_url"]
    async with SmtpStub() as working:
        with smtp_settings(working.port):
            retried = await client.post(row["retry_url"], headers=hd)
            assert retried.status_code == 200, retried.text
            assert retried.json()["state"] == "sent" and len(working.messages) == 1
    assert [d["status"] for d in await billed_docs(session, eid)] == ["sent"]


async def test_a_send_cut_off_after_the_hand_over_may_have_arrived_and_is_not_resent(client, session, monkeypatch):
    """The server took the message and the connection dropped before its answer: the email
    may have arrived, so it stays unconfirmed, with no Retry, and a retry is refused by key."""
    freeze_day(monkeypatch, "2026-07-01")
    async with SmtpStub(drop_after_message=True) as stub:
        with smtp_settings(stub.port):
            hd = await register(client)
            cid = await company_id(client, hd)
            eid = await _sending(client, hd)
            await tick(session, cid, "2026-07-01")
            assert (await template(client, hd, eid))["periods"]["0"]["send"]["state"] == "sending"
            row = await _send_row(client, hd, eid)
            assert row["kind"] == "send_unconfirmed" and row["retry_url"] is None
            refused = await client.post(f"/subscriptions/{eid}/periods/0/send", headers=hd)
    assert refused.status_code == 409
    assert refused.json()["detail"]["message_key"] == "subscriptions.err_send_unverified"


async def test_a_reused_request_key_is_refused_by_key(client, monkeypatch):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-07-01")
    key = str(uuid.uuid4())
    assert (await client.post(f"/subscriptions/{eid}/generate-due", headers=hd, json={"request_key": key})).status_code == 200
    other = await client.post(f"/subscriptions/{eid}/bill-early", headers=hd, json={"request_key": key})
    assert other.status_code == 409
    assert other.json()["detail"]["message_key"] == "subscriptions.err_request_key_reused"


def test_every_subscription_refusal_is_a_catalog_text():
    """Class guard: no HTTPException in the module carries a sentence written in place (a
    string literal or f-string), so every refusal reads in the reader's language."""
    root = Path(__file__).resolve().parents[1] / "celerp_subscriptions"
    written = []
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HTTPException":
                for kw in node.keywords:
                    if kw.arg == "detail" and isinstance(kw.value, (ast.Constant, ast.JoinedStr)):
                        written.append(f"{path.name}:{node.lineno}")
    assert written == []


@pytest.mark.parametrize("cause", ["no email service", "no contact email", "preparation"])
async def test_a_failed_send_reads_in_the_readers_language(client, session, monkeypatch, cause):
    """Class guard for stored reasons: a failed send records the reason's catalog key, never
    its English, and the summary words it in the language of whoever reads it."""
    freeze_day(monkeypatch, "2026-07-01")
    monkeypatch.setattr("celerp.config.settings.gateway_token", "")
    monkeypatch.setattr("celerp.config.settings.smtp_host", "")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-07-01",
                          **({} if cause == "no contact email" else
                             {"contact_id": await contact(client, hd, "member@example.test")}))
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", auto_send=True)).status_code == 200
    if cause == "preparation":
        async def broken(*_, **__):
            raise RuntimeError("template engine down")
        monkeypatch.setattr("celerp_docs.routes.doc_email", broken)
    await tick(session, cid, "2026-07-01")
    send = (await template(client, hd, eid))["periods"]["0"]["send"]
    assert send["state"] == "failed" and "error" not in send
    key = {"no email service": "email.err_no_transport", "no contact email": "subscriptions.err_send_no_email",
           "preparation": "subscriptions.err_send_preparation"}[cause]
    assert send["error_key"] == key
    row = await _send_row(client, {**hd, "Accept-Language": "de"}, eid)
    ref = row["ref_id"]
    assert row["message"] == t("subscriptions.summary_send_failed", "de", ref=ref, error=t(key, "de"))


async def test_no_email_service_links_to_the_page_that_sets_it_up(owner_ui, session, monkeypatch):
    """A send that failed because no email service is set up names the page that sets one up."""
    freeze_day(monkeypatch, "2026-07-01")
    monkeypatch.setattr("celerp.config.settings.gateway_token", "")
    monkeypatch.setattr("celerp.config.settings.smtp_host", "")
    eid = await _sending(owner_ui.api, {})
    await tick(session, await company_id(owner_ui.api, {}), "2026-07-01")
    page = (await owner_ui.get("/subscriptions/summary")).text
    assert t("email.err_no_transport", "en") in page
    assert 'href="/settings/cloud"' in page and t("doc.send_relay_error_link", "en") in page
    assert f'href="/subscriptions/{eid}"' in page


async def test_an_email_waiting_after_the_app_stopped_is_shown_and_never_sent_late(client, session, monkeypatch):
    """The app stopped between billing and sending: the email waits. The summary and the
    period say so and offer Send. Months later, with Send turned back on, the old invoice is
    not emailed on its own (it is past the catch-up window, as an unbilled period would be)."""
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    async with SmtpStub() as stub:
        with smtp_settings(stub.port):
            hd = await register(client)
            cid = await company_id(client, hd)
            eid = await _sending(client, hd)
            await billing.run_due(session=session, company_id=uuid.UUID(cid), today=date(2026, 7, 1), budget=10)
            await session.commit()  # the after-commit send never ran: the app stopped
            row = await _send_row(client, hd, eid)
            assert row["kind"] == "send_pending" and row["retry_url"] == f"/subscriptions/{eid}/periods/0/send"
            periods = (await client.get(f"/subscriptions/{eid}/periods", headers=hd)).json()["rows"]
            assert next(p for p in periods if p["period_index"] == 0)["send"] == "pending"
            assert (await schedule(client, hd, eid, auto_send=False)).status_code == 200
            freeze_day(monkeypatch, "2026-12-01")
            assert (await schedule(client, hd, eid, auto_send=True)).status_code == 200
            await tick(session, cid, "2026-12-01")
            assert all("INV-2607" not in (m["Subject"] or "") for m in stub.messages)
            row = await _send_row(client, hd, eid, all=1)
            assert row["kind"] == "send_pending" and row["period_index"] == 0
            sent = await client.post(row["retry_url"], headers=hd)
            assert sent.status_code == 200 and sent.json()["state"] == "sent"


async def test_an_email_waiting_inside_the_window_goes_on_the_next_pass(client, session, monkeypatch):
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    async with SmtpStub() as stub:
        with smtp_settings(stub.port):
            hd = await register(client)
            cid = await company_id(client, hd)
            eid = await _sending(client, hd)
            await billing.run_due(session=session, company_id=uuid.UUID(cid), today=date(2026, 7, 1), budget=10)
            await session.commit()
            await tick(session, cid, "2026-07-02")
            assert len(stub.messages) == 1
            assert (await template(client, hd, eid))["periods"]["0"]["send"]["state"] == "sent"


def test_every_flash_style_the_ui_uses_is_defined():
    """Class guard: a banner class with no style renders as bare text under styled ones."""
    import re

    repo = Path(__file__).resolve().parents[3]
    css = (repo / "ui/static/app.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"\.flash--([\w-]+)", css))
    used = {m for p in (repo / "ui").rglob("*.py")
            for m in re.findall(r"flash--([\w-]+)", p.read_text(encoding="utf-8"))}
    assert used - defined == set()


async def test_a_send_to_check_is_a_warning_on_the_list(owner_ui, session, monkeypatch):
    """A failed customer email is as visible on the list as the catch-up warning."""
    freeze_day(monkeypatch, "2026-07-01")
    monkeypatch.setattr("celerp.config.settings.gateway_token", "")
    monkeypatch.setattr("celerp.config.settings.smtp_host", "")
    await _sending(owner_ui.api, {})
    await tick(session, await company_id(owner_ui.api, {}), "2026-07-01")
    listing = (await owner_ui.get("/subscriptions?direction=sales")).text
    banner = t("subscriptions.summary_banner", "en", generated=1, sent=0, failed=1)
    assert banner in listing
    start = listing.rindex("<div", 0, listing.index(banner))
    assert 'class="flash flash--warning"' in listing[start:listing.index(banner)]
