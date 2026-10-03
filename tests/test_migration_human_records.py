# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Moved and restored records read as people's records, not as storage.

After a move the books show the company's own account numbers, document numbers,
activity words that match the direction money moved, times in the company's own
timezone, and every stocked item on the dashboard. Stored identities are unchanged."""

from __future__ import annotations

import html
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from fixtures.manager_io.support import ref
from migration_support import maker, real_client, real_engine  # noqa: F401 - fixtures
from test_company_backup_ui import ui  # noqa: F401 - fixture
from test_migration_inventory_provenance import _doc, _migrated

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
COST = ref("@ProfitAndLossStatementAccountInventoryPurchases")
SALES = ref("@ProfitAndLossStatementAccountInventorySales")


def _visible(page: str) -> str:
    text = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", page, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text)))


async def test_codeless_accounts_get_chart_numbers_in_their_type_range(real_engine, monkeypatch, tmp_path):
    """RED before the change: an account the source gave no code got 'M' plus hex."""
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    codes = {m: t for (_, m), t in books.maps.items()}
    assert re.fullmatch(r"5\d{3}", codes[COST]), codes[COST]
    assert re.fullmatch(r"4\d{3}", codes[SALES]), codes[SALES]
    assert not any(re.fullmatch(r"M[0-9a-f]{8}", str(c)) for c in codes.values())


async def test_journal_names_documents_by_number(real_engine, monkeypatch, tmp_path, ui):  # noqa: F811
    """RED before the change: system descriptions showed 'doc:' plus the record's id."""
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    ui.cookies.set("celerp_token", books.token)
    paid = await _doc(books, "INVD")
    page = _visible((await ui.get("/accounting?tab=journal")).text)
    assert "doc:" not in page
    assert not UUID.search(page)
    assert f"Auto JE for {paid['doc_number']} payment" in page


async def test_dashboard_charts_uncategorized_stock(real_engine, monkeypatch, tmp_path, ui):  # noqa: F811
    """RED before the change: stock with no category read as 'No inventory yet'."""
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    ui.cookies.set("celerp_token", books.token)
    page = (await ui.get("/dashboard")).text
    assert "No inventory yet" not in page
    assert '"Uncategorized"' in page


async def test_supplier_payment_reads_as_payment_made(real_engine, real_client, monkeypatch, tmp_path, ui):  # noqa: F811
    """RED before the change: money paid on a bill was listed as 'Payment received'."""
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    bill = books.id("PurchaseInvoice", "BILLU")
    paid = await real_client.post(f"/docs/{bill}/payment", headers=books.headers, json={
        "amount": 12.0, "payment_date": "2026-01-20", "bank_account": "1111"})
    assert paid.status_code == 200, paid.text
    feed = (await real_client.get("/dashboard/activity", headers=books.headers)).json()["activities"]
    entry = next(a for a in feed if a["event_type"] == "doc.payment.received" and a["entity_id"] == bill)
    assert entry["entity_doc_type"] == "bill"
    ledger = (await real_client.get("/ledger", headers=books.headers,
                                    params={"entity_id": bill, "resolve": "true"})).json()["items"]
    assert {e["entity_doc_type"] for e in ledger} == {"bill"}

    ui.cookies.set("celerp_token", books.token)
    page = _visible((await ui.get("/dashboard")).text)
    assert "Payment made" in page
    assert 'Payment received: "BILLU"' not in page


def test_customer_payment_keeps_payment_received():
    from ui.components.activity import _event_display
    assert _event_display({"event_type": "doc.payment.received", "entity_doc_type": "invoice"})[0] == "Payment received"
    assert _event_display({"event_type": "doc.payment.received", "entity_doc_type": "bill"})[0] == "Payment made"


async def test_activity_times_are_in_company_timezone(real_engine, real_client, monkeypatch, tmp_path):
    """RED before the change: times were sent and shown in UTC whatever the company's timezone."""
    from celerp.models.ledger import LedgerEntry
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    changed = await real_client.patch("/companies/me", headers=books.headers,
                                      json={"settings": {"timezone": "Asia/Bangkok"}})
    assert changed.status_code == 200, changed.text
    async with maker(real_engine)() as s:
        stored = await s.scalar(select(LedgerEntry).where(LedgerEntry.company_id == books.run.company_id)
                                .order_by(LedgerEntry.id.desc()).limit(1))
    from zoneinfo import ZoneInfo
    expected = stored.ts.astimezone(ZoneInfo("Asia/Bangkok")).isoformat()
    feed = (await real_client.get("/dashboard/activity", headers=books.headers)).json()["activities"]
    assert feed[0]["ts"] == expected and feed[0]["ts"].endswith("+07:00")
    ledger = (await real_client.get("/ledger", headers=books.headers, params={"limit": 1})).json()["items"]
    assert ledger[0]["ts"] == expected


@pytest.mark.parametrize("feed", ["ledger", "dashboard"])
async def test_every_feed_shows_a_carried_backup_actor(real_engine, feed):  # noqa: F811
    """RED before the change: the dashboard feed dropped the name a company backup carried."""
    from datetime import datetime, timezone
    row = SimpleNamespace(entity_id="doc:none", actor_id=None, event_type="doc.created", data={},
                          entity_type="doc", ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
                          metadata_={"backup_actor": {"name": "Former Clerk", "user_ref": "x"}})
    async with maker(real_engine)() as s:
        if feed == "ledger":
            from celerp.services.ledger_display import display_fields
            shown = (await display_fields([row], "00000000-0000-0000-0000-000000000000", s))[0]
        else:
            from celerp_dashboard.routes import _hydrate_entries
            shown = (await _hydrate_entries([row], "00000000-0000-0000-0000-000000000000", s, {}, "owner"))[0]
    assert shown["actor_name"] == "Former Clerk"
    assert shown["actor_historical"] is True
