# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Xero invoice creation through the outbound queue: each Celerp invoice is
created in Xero at most once, across crashes, retries, local edits and
disconnects, and is the same invoice when imported back."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
import sqlalchemy as sa

from celerp.connectors.base import ConnectorContext
from celerp.connectors.outbound_queue import process_outbound_queue_once
from celerp.connectors.xero import XeroConnector
from celerp.models.company import Company
from celerp.models.connector_config import ConnectorConfig, OutboundQueue
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

API = "https://relay.test/connectors/xero/api"
DOC_ID = "doc:inv-1"
CONTACT = "contact-1"


@pytest.fixture
def relay():
    with patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"), \
         patch("celerp.gateway.state.relay_session_headers", return_value={}):
        yield


@pytest.fixture
async def company(_db_engine):
    from celerp.db import get_session_ctx

    company_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with get_session_ctx() as seed:
        seed.add(Company(
            id=company_id, name="Xero Push", slug=f"xero-push-{company_id.hex[:8]}", settings={},
        ))
        seed.add(ConnectorConfig(company_id=str(company_id), connector="xero", direction="both"))
        seed.add(Projection(
            company_id=company_id,
            entity_id=DOC_ID,
            entity_type="doc",
            version=1,
            created_at=now,
            updated_at=now,
            state={
                "doc_type": "invoice",
                "ref_id": "INV-1",
                "customer_external_id": CONTACT,
                "line_items": [
                    {"description": "Service", "quantity": 1, "unit_price": 100, "total": 100},
                ],
            },
        ))
        await seed.commit()
    try:
        yield company_id
    finally:
        async with get_session_ctx() as cleanup:
            key = str(company_id)
            for model in (OutboundQueue, ConnectorConfig):
                await cleanup.execute(sa.delete(model).where(model.company_id == key))
            for model in (LedgerEntry, Projection):
                await cleanup.execute(sa.delete(model).where(model.company_id == company_id))
            await cleanup.execute(sa.delete(Company).where(Company.id == company_id))
            await cleanup.commit()


def _ctx(company_id) -> ConnectorContext:
    return ConnectorContext(company_id=str(company_id), access_token="", store_handle="t")


async def _rows(company_id) -> list[OutboundQueue]:
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        return list((await session.execute(
            sa.select(OutboundQueue).where(OutboundQueue.company_id == str(company_id))
        )).scalars().all())


async def _doc(company_id) -> dict:
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": DOC_ID})
        return dict(row.state)


async def _edit_doc(company_id, **changes) -> None:
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": DOC_ID})
        row.state = {**row.state, **changes}
        await session.commit()


async def _age_attempt(company_id, minutes: int = 7) -> dict:
    """Move the in-flight attempt into the past, as if the process had been
    down that long. Returns the operation's state."""
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        row = (await session.execute(
            sa.select(OutboundQueue).where(OutboundQueue.company_id == str(company_id))
        )).scalar_one()
        state = json.loads(row.payload_json)
        state["attempted_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=minutes)
        ).isoformat()
        row.payload_json = json.dumps(state)
        await session.commit()
    return state


def _remote(number: str = "INV-1", contact: str = CONTACT, amount: float = 100.0,
            invoice_id: str = "xero-1") -> dict:
    return {
        "InvoiceID": invoice_id,
        "Type": "ACCREC",
        "InvoiceNumber": number,
        "Contact": {"ContactID": contact},
        "LineItems": [
            {"Description": "Service", "Quantity": 1.0, "UnitAmount": amount, "LineAmount": amount},
        ],
    }


class _Xero:
    """A stand-in Xero behind the relay. `put` and `get` are lists of responses
    (or exceptions) consumed in order; every request is recorded."""

    def __init__(self, put=(), get=()):
        self.put_plan = list(put)
        self.get_plan = list(get)
        self.puts: list[httpx.Request] = []
        self.gets: list[httpx.Request] = []

    def _next(self, plan, request):
        outcome = plan.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def __enter__(self):
        self.mock = respx.mock(assert_all_called=False)
        self.mock.__enter__()
        self.mock.put(f"{API}/Invoices").mock(
            side_effect=lambda r: (self.puts.append(r), self._next(self.put_plan, r))[1]
        )
        self.mock.get(f"{API}/Invoices").mock(
            side_effect=lambda r: (self.gets.append(r), self._next(self.get_plan, r))[1]
        )
        return self

    def __exit__(self, *exc):
        return self.mock.__exit__(*exc)

    def put_keys(self) -> list[str]:
        return [r.headers["Idempotency-Key"] for r in self.puts]

    def put_bodies(self) -> list[dict]:
        return [json.loads(r.content)["Invoices"][0] for r in self.puts]


def _created(invoice_id: str = "xero-1") -> httpx.Response:
    return httpx.Response(200, json={"Invoices": [{"InvoiceID": invoice_id}]})


LOST = httpx.Response(504, json={"detail": "Xero did not respond in time."})


async def _sync(company_id):
    return await XeroConnector().sync_invoices_out(_ctx(company_id))


@pytest.mark.asyncio
async def test_first_push_creates_and_links_the_invoice(relay, company):
    with _Xero(put=[_created()]) as xero:
        result = await _sync(company)
    assert result.created == 1 and result.errors is None
    assert len(xero.puts) == 1 and xero.put_keys()[0]
    assert (await _doc(company))["xero_invoice_id"] == "xero-1"
    assert await _rows(company) == []


@pytest.mark.asyncio
async def test_write_back_failure_links_on_retry_without_resending(relay, company):
    with _Xero(put=[_created()]) as xero:
        with patch("celerp.connectors.upsert.mark_doc_pushed",
                   new=AsyncMock(side_effect=RuntimeError("write-back failed"))):
            first = await _sync(company)
        assert first.errors and len(await _rows(company)) == 1
        assert json.loads((await _rows(company))[0].payload_json)["remote_id"] == "xero-1"
        second = await _sync(company)
    assert second.created == 1 and second.errors is None
    assert len(xero.puts) == 1 and xero.gets == []
    assert (await _doc(company))["xero_invoice_id"] == "xero-1"
    assert await _rows(company) == []


@pytest.mark.asyncio
async def test_lost_response_links_by_lookup_after_key_window(relay, company):
    with _Xero(put=[LOST], get=[httpx.Response(200, json={"Invoices": [_remote()]})]) as xero:
        await _sync(company)
        await _age_attempt(company)
        result = await _sync(company)
    assert result.created == 1 and result.errors is None
    assert len(xero.puts) == 1, "never created a second time"
    where = xero.gets[0].url.params["where"]
    assert 'InvoiceNumber=="INV-1"' in where and xero.gets[0].url.params["page"] == "1"
    assert (await _doc(company))["xero_invoice_id"] == "xero-1"
    assert await _rows(company) == []


@pytest.mark.asyncio
async def test_after_key_window_confirmed_absence_resends_with_new_key(relay, company):
    with _Xero(put=[LOST, _created()], get=[httpx.Response(200, json={"Invoices": []})]) as xero:
        first = await _sync(company)
        assert first.errors
        rows = await _rows(company)
        assert len(rows) == 1 and rows[0].status == "pending"
        await _age_attempt(company)
        second = await _sync(company)
    assert second.created == 1 and second.errors is None
    keys = xero.put_keys()
    assert len(keys) == 2 and keys[0] != keys[1]
    state_bodies = xero.put_bodies()
    assert state_bodies[0] == state_bodies[1]
    assert await _rows(company) == []


@pytest.mark.asyncio
async def test_lost_response_retains_row_and_queue_resends_same_key(relay, company):
    with _Xero(put=[LOST, _created()]) as xero:
        await _sync(company)
        # The background queue honours the backoff; move it to now.
        from celerp.db import get_session_ctx
        async with get_session_ctx() as session:
            await session.execute(
                sa.update(OutboundQueue)
                .where(OutboundQueue.company_id == str(company))
                .values(next_retry_at=None)
            )
            await session.commit()
        with patch("celerp.connectors.relay_token.fetch_context",
                   new=AsyncMock(return_value=_ctx(company))):
            assert await process_outbound_queue_once() >= 1
    keys = xero.put_keys()
    assert len(keys) == 2 and keys[0] == keys[1]
    assert (await _doc(company))["xero_invoice_id"] == "xero-1"
    assert await _rows(company) == []


@pytest.mark.asyncio
async def test_local_edit_while_unresolved_links_the_original_and_holds(relay, company):
    with _Xero(put=[LOST, _created()]) as xero:
        await _sync(company)
        await _edit_doc(
            company,
            ref_id="INV-99",
            line_items=[{"description": "Service", "quantity": 2, "unit_price": 100, "total": 200}],
        )
        result = await _sync(company)
        assert result.created == 0
        assert result.errors and "changed in Celerp" in result.errors[0]
        bodies = xero.put_bodies()
        assert [b["InvoiceNumber"] for b in bodies] == ["INV-1", "INV-1"]
        assert [b["LineItems"][0]["LineAmount"] for b in bodies] == [100.0, 100.0]
        assert xero.put_keys()[0] == xero.put_keys()[1]
        assert (await _doc(company))["xero_invoice_id"] == "xero-1"
        rows = await _rows(company)
        assert len(rows) == 1 and rows[0].status == "blocked"
        assert json.loads(rows[0].payload_json)["remote_id"] == "xero-1"

        # A linked invoice is off the unsynced list; a manual sync still rechecks
        # the held row, and never sends again.
        again = await _sync(company)
        assert again.errors and "changed in Celerp" in again.errors[0]
        await _edit_doc(
            company,
            ref_id="INV-1",
            line_items=[{"description": "Service", "quantity": 1, "unit_price": 100, "total": 100}],
        )
        settled = await _sync(company)
    assert settled.created == 1 and settled.errors is None
    assert len(xero.puts) == 2 and xero.gets == []
    assert await _rows(company) == []


@pytest.mark.asyncio
async def test_after_key_window_confirmed_absence_sends_the_current_invoice(relay, company):
    with _Xero(put=[LOST, _created()], get=[httpx.Response(200, json={"Invoices": []})]) as xero:
        await _sync(company)
        await _edit_doc(
            company,
            ref_id="INV-99",
            line_items=[{"description": "Service", "quantity": 2, "unit_price": 100, "total": 200}],
        )
        frozen = await _age_attempt(company)
        result = await _sync(company)
    assert result.created == 1 and result.errors is None
    assert 'InvoiceNumber=="INV-1"' in xero.gets[0].url.params["where"]
    bodies = xero.put_bodies()
    assert [b["InvoiceNumber"] for b in bodies] == ["INV-1", "INV-99"]
    assert bodies[1]["LineItems"][0]["LineAmount"] == 200.0
    keys = xero.put_keys()
    assert keys[0] == frozen["idempotency_key"] and keys[1] != keys[0]
    assert (await _doc(company))["xero_invoice_id"] == "xero-1"
    assert await _rows(company) == []


@pytest.mark.asyncio
async def test_conflicting_remote_invoice_parks_the_push_and_never_creates(relay, company):
    other = _remote(contact="someone-else", invoice_id="xero-other")
    with _Xero(
        put=[LOST],
        get=[
            httpx.Response(200, json={"Invoices": [other]}),
            httpx.Response(200, json={"Invoices": []}),
            httpx.Response(200, json={"Invoices": [_remote(invoice_id="xero-fixed")]}),
        ],
    ) as xero:
        await _sync(company)
        await _age_attempt(company)
        parked = await _sync(company)
        assert parked.errors and "does not match" in parked.errors[0]
        rows = await _rows(company)
        assert len(rows) == 1 and rows[0].status == "blocked"

        # The background queue leaves a parked push alone.
        with patch("celerp.connectors.relay_token.fetch_context",
                   new=AsyncMock(return_value=_ctx(company))):
            await process_outbound_queue_once()
        assert len(xero.gets) == 1

        # A manual sync rechecks it. The conflicting invoice is gone, but
        # Celerp's own create may have landed, so it still does not create.
        still = await _sync(company)
        assert still.errors and "no invoice numbered INV-1" in still.errors[0]
        assert (await _rows(company))[0].status == "blocked"

        # Corrected in Xero: the next sync links it.
        linked = await _sync(company)
    assert linked.created == 1 and linked.errors is None
    assert len(xero.puts) == 1
    assert (await _doc(company))["xero_invoice_id"] == "xero-fixed"
    assert await _rows(company) == []


@pytest.mark.asyncio
async def test_already_linked_invoice_clears_its_row_without_calling_xero(relay, company):
    with _Xero(put=[LOST]) as xero:
        await _sync(company)
        await _edit_doc(company, xero_invoice_id="xero-linked-elsewhere")
        from celerp.db import get_session_ctx
        async with get_session_ctx() as session:
            await session.execute(
                sa.update(OutboundQueue)
                .where(OutboundQueue.company_id == str(company))
                .values(next_retry_at=None)
            )
            await session.commit()
        with patch("celerp.connectors.relay_token.fetch_context",
                   new=AsyncMock(return_value=_ctx(company))):
            await process_outbound_queue_once()
    assert len(xero.puts) == 1 and xero.gets == []
    assert await _rows(company) == []


@pytest.mark.asyncio
async def test_validation_rejection_clears_the_row_and_reports_why(relay, company):
    rejected = httpx.Response(400, json={"Elements": [{"ValidationErrors": [
        {"Message": "Contact could not be found."}
    ]}]})
    with _Xero(put=[rejected]):
        result = await _sync(company)
    assert result.errors and "Contact could not be found." in result.errors[0]
    assert await _rows(company) == []
    assert "xero_invoice_id" not in await _doc(company)


LEGACY = "legacy-instance"


async def _disconnect(company_id) -> None:
    from celerp.connectors.ownership import release_connector_ownership
    from celerp.db import get_session_ctx
    with patch("celerp.connectors.ownership.ensure_instance_id", return_value=LEGACY):
        async with get_session_ctx() as session:
            await release_connector_ownership(session, str(company_id), "xero")
            await session.commit()


async def _reconnect(company_id) -> None:
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        session.add(ConnectorConfig(company_id=str(company_id), connector="xero", direction="both"))
        await session.commit()


@pytest.mark.asyncio
async def test_attempted_create_survives_disconnect_and_resumes_on_reconnect(relay, company):
    with _Xero(put=[LOST, _created()]) as xero:
        await _sync(company)
        frozen = json.loads((await _rows(company))[0].payload_json)
        await _disconnect(company)
        rows = await _rows(company)
        assert len(rows) == 1 and rows[0].status == "blocked"
        assert rows[0].next_retry_at is None
        assert rows[0].error_message.startswith("Xero was disconnected before Celerp knew whether this invoice reached Xero.")
        assert json.loads(rows[0].payload_json) == frozen

        # The background queue leaves it parked.
        await _reconnect(company)
        with patch("celerp.connectors.relay_token.fetch_context",
                   new=AsyncMock(return_value=_ctx(company))):
            await process_outbound_queue_once()
        assert len(xero.puts) == 1

        # A manual sync on the same organisation resumes the same create.
        result = await _sync(company)
    assert result.created == 1 and result.errors is None
    assert xero.put_keys() == [frozen["idempotency_key"]] * 2
    assert (await _doc(company))["xero_invoice_id"] == "xero-1"
    assert await _rows(company) == []


@pytest.mark.asyncio
async def test_unattempted_row_is_deleted_on_disconnect(relay, company):
    from celerp.connectors.outbound_queue import enqueue_outbound
    await enqueue_outbound(str(company), "xero", "invoice", DOC_ID)
    await _disconnect(company)
    assert await _rows(company) == []


@pytest.mark.asyncio
async def test_other_organisation_makes_no_call_and_stays_blocked(relay, company):
    with _Xero(put=[LOST]) as xero:
        await _sync(company)
        await _disconnect(company)
        await _reconnect(company)
        other = ConnectorContext(company_id=str(company), access_token="", store_handle="other")
        result = await XeroConnector().sync_invoices_out(other)
        assert result.errors and "different Xero organisation" in result.errors[0]
        await _age_attempt(company)
        await XeroConnector().sync_invoices_out(other)
    assert len(xero.puts) == 1 and xero.gets == []
    rows = await _rows(company)
    assert len(rows) == 1 and rows[0].status == "blocked"
    assert "xero_invoice_id" not in await _doc(company)


async def _docs(company_id) -> list[Projection]:
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        return list((await session.execute(
            sa.select(Projection).where(
                Projection.company_id == company_id, Projection.entity_type == "doc"
            )
        )).scalars().all())


async def _event_count(company_id) -> int:
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        return await session.scalar(
            sa.select(sa.func.count()).select_from(LedgerEntry)
            .where(LedgerEntry.company_id == company_id)
        )


async def _finalize_doc(company_id) -> None:
    """Give the native invoice the accounting state an import must never replace."""
    await _edit_doc(
        company_id, status="final", total=100.0, amount_outstanding=100.0,
        currency="THB", conversion_rate=1.0,
        line_items=[{"description": "Service", "quantity": 1, "unit_price": 100,
                     "total": 100, "item_id": "item-1", "sku": "SKU-1"}],
    )


@pytest.mark.asyncio
async def test_push_then_import_leaves_the_native_invoice_unchanged(relay, company):
    from celerp.connectors.upsert import upsert_invoice_from_xero
    await _finalize_doc(company)
    with _Xero(put=[_created()]):
        await _sync(company)
    before, events = await _doc(company), await _event_count(company)
    remote = {**_remote(), "Status": "PAID", "Total": 90, "AmountDue": 0,
              "CurrencyCode": "USD", "CurrencyRate": 0.03}
    assert await upsert_invoice_from_xero(str(company), remote) == "noop"
    assert [d.entity_id for d in await _docs(company)] == [DOC_ID]
    assert await _doc(company) == before
    assert "idempotency_key" not in before
    assert await _event_count(company) == events


@pytest.mark.asyncio
async def test_quickbooks_pushed_invoice_import_leaves_it_unchanged(company):
    from celerp.connectors.upsert import mark_doc_pushed, upsert_invoice_from_quickbooks
    await _finalize_doc(company)
    await mark_doc_pushed(str(company), DOC_ID, "quickbooks", "qb-7")
    before, events = await _doc(company), await _event_count(company)
    assert await upsert_invoice_from_quickbooks(str(company), {
        "Id": "qb-7", "DocNumber": "INV-1", "TotalAmt": 90, "Balance": 0, "Line": [],
        "CurrencyRef": {"value": "USD"}, "ExchangeRate": 33.0,
    }) == "noop"
    assert [d.entity_id for d in await _docs(company)] == [DOC_ID]
    assert await _doc(company) == before
    assert await _event_count(company) == events


@pytest.mark.asyncio
async def test_unconfirmed_invoice_is_not_imported_before_outbound_links_it(relay, company):
    """The create reached Xero but its reply was lost; the next sync imports first."""
    with _Xero(put=[LOST], get=[
        httpx.Response(200, json={"Invoices": [_remote()]}),
        httpx.Response(200, json={"Invoices": [_remote()]}),
    ]) as xero:
        await _sync(company)
        pulled = await XeroConnector().sync_orders(_ctx(company))
        assert pulled.skipped == 1 and pulled.created == 0
        assert [d.entity_id for d in await _docs(company)] == [DOC_ID]
        await _age_attempt(company)
        assert (await _sync(company)).created == 1
    assert len(xero.puts) == 1
    assert (await _doc(company))["xero_invoice_id"] == "xero-1"
    assert await _rows(company) == []
    with _Xero(get=[httpx.Response(200, json={"Invoices": [_remote()]})]):
        pulled = await XeroConnector().sync_orders(_ctx(company))
    assert pulled.created == 0 and pulled.errors is None
    assert [d.entity_id for d in await _docs(company)] == [DOC_ID]


@pytest.mark.asyncio
async def test_unconfirmed_invoice_in_another_organisation_does_not_hold_imports(relay, company):
    with _Xero(put=[LOST]):
        await _sync(company)
    other = ConnectorContext(company_id=str(company), access_token="", store_handle="other")
    with _Xero(get=[httpx.Response(200, json={"Invoices": [_remote(invoice_id="xero-o")]})]):
        pulled = await XeroConnector().sync_orders(other)
    assert pulled.created == 1


def _sent() -> dict:
    from celerp.connectors.xero import _invoice_request
    return _invoice_request({
        "ref_id": "INV-1", "customer_external_id": CONTACT,
        "line_items": [
            {"description": "Service", "quantity": 1, "unit_price": 100, "total": 100},
            {"description": "Parts", "quantity": 4, "unit_price": 2.5, "total": 10},
        ],
    })["Invoices"][0]


def _echo(**changes) -> dict:
    """Xero's copy of the sent invoice: lines reordered, amounts in its own format."""
    lines = [
        {"Description": "Parts", "Quantity": 4.0, "UnitAmount": 2.5, "LineAmount": 10.0},
        {"Description": "Service", "Quantity": 1.0, "UnitAmount": 100.0, "LineAmount": 100.0},
    ]
    remote = {"InvoiceID": "xero-1", "Type": "ACCREC", "InvoiceNumber": "INV-1",
              "Status": "PAID", "Contact": {"ContactID": CONTACT}, "LineItems": lines}
    for key, value in changes.items():
        if key.startswith("line_"):
            lines[1][key[5:]] = value
        else:
            remote[key] = value
    return remote


def test_sent_invoice_matches_its_own_echo():
    from celerp.connectors.xero import _is_sent_invoice
    assert _is_sent_invoice(_echo(), _sent())


@pytest.mark.parametrize("changes", [
    {"InvoiceNumber": "INV-2"},
    {"Type": "ACCPAY"},
    {"Contact": {"ContactID": "someone-else"}},
    {"line_Description": "Consulting"},
    {"line_Quantity": 2.0, "line_UnitAmount": 50.0},
    {"line_UnitAmount": "not a number"},
    {"LineItems": [{"Description": "Service", "Quantity": 1.0, "UnitAmount": 100.0,
                    "LineAmount": 100.0}]},
])
def test_any_differing_field_is_not_the_sent_invoice(changes):
    from celerp.connectors.xero import _is_sent_invoice
    assert not _is_sent_invoice(_echo(**changes), _sent())


@pytest.mark.asyncio
async def test_same_totals_with_different_lines_blocks_instead_of_linking(relay, company):
    lookalike = _remote()
    lookalike["LineItems"] = [
        {"Description": "Other work", "Quantity": 2.0, "UnitAmount": 50.0, "LineAmount": 100.0},
    ]
    with _Xero(put=[LOST], get=[httpx.Response(200, json={"Invoices": [lookalike]})]) as xero:
        await _sync(company)
        await _age_attempt(company)
        result = await _sync(company)
    assert result.errors and len(xero.puts) == 1
    rows = await _rows(company)
    assert len(rows) == 1 and rows[0].status == "blocked"
    assert "xero_invoice_id" not in await _doc(company)


@pytest.mark.asyncio
async def test_import_matching_two_invoices_fails_closed(company):
    from celerp.connectors.upsert import mark_doc_pushed, upsert_invoice_from_xero
    from celerp.db import get_session_ctx
    from celerp.events.engine import ConnectorIdentityConflict

    await mark_doc_pushed(str(company), DOC_ID, "xero", "xero-9")
    now = datetime.now(timezone.utc)
    async with get_session_ctx() as session:
        session.add(Projection(
            company_id=company, entity_id="doc:imported", entity_type="doc", version=1,
            created_at=now, updated_at=now,
            state={"doc_type": "invoice", "idempotency_key": "xero:invoice:xero-9"},
        ))
        await session.commit()
        before = await session.scalar(
            sa.select(sa.func.count()).select_from(LedgerEntry)
            .where(LedgerEntry.company_id == company)
        )
    with pytest.raises(ConnectorIdentityConflict):
        await upsert_invoice_from_xero(str(company), {**_remote(invoice_id="xero-9"), "Total": 100})
    async with get_session_ctx() as session:
        after = await session.scalar(
            sa.select(sa.func.count()).select_from(LedgerEntry)
            .where(LedgerEntry.company_id == company)
        )
    assert after == before
    assert sorted(d.entity_id for d in await _docs(company)) == sorted([DOC_ID, "doc:imported"])
