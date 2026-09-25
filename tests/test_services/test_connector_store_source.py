# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Which store a company's imported connector records came from.

WooCommerce and QuickBooks number orders and customers per store, so records
from a second store would overwrite the first store's. These tests pin that a
store is accepted only when nothing was imported yet or when it still holds
the imported records, and that company settings play no part in it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa

from celerp.connectors.base import ConnectorContext
from celerp.connectors.ownership import ConnectorStoreChangedError, bind_connector_store
from celerp.connectors.quickbooks import QuickBooksConnector
from celerp.connectors.woocommerce import WooCommerceConnector
from celerp.models.company import Company
from celerp.models.connector_source import ConnectorSource
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio

_ORDER = {
    "id": 7, "total": "25.00", "currency": "USD",
    "line_items": [{"name": "Blue mug"}],
}
_ORDER_DOC = {
    "woocommerce_order_id": "7", "total": 25.0, "currency": "USD",
    "line_items": [{"name": "Blue mug"}],
}


async def _company(session, *, settings=None) -> uuid.UUID:
    cid = uuid.uuid4()
    session.add(Company(
        id=cid, name="Store", slug=f"store-{cid.hex[:8]}", settings=settings or {},
    ))
    await session.flush()
    return cid


async def _imported_order(session, cid, state=_ORDER_DOC) -> None:
    session.add(Projection(
        company_id=cid, entity_id=f"doc:woocommerce:order:{state['woocommerce_order_id']}",
        entity_type="doc", state=state, version=1,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    ))
    await session.flush()


async def _source(session, cid) -> str | None:
    return await session.scalar(sa.select(ConnectorSource.store_handle).where(
        ConnectorSource.company_id == str(cid), ConnectorSource.connector == "woocommerce",
    ))


def _ctx(cid, url) -> ConnectorContext:
    return ConnectorContext(company_id=str(cid), access_token="k:s", store_handle=url)


def _store(orders):
    return patch.object(WooCommerceConnector, "_paginate", AsyncMock(return_value=orders))


async def test_first_store_is_recorded_as_the_source(session):
    cid = await _company(session)
    await bind_connector_store(session, cid, WooCommerceConnector(), _ctx(cid, "https://a.example"))
    assert await _source(session, cid) == "https://a.example"


async def test_company_settings_cannot_redirect_the_source(session):
    """A value saved through company settings does not make a second store
    acceptable for the first store's orders."""
    cid = await _company(session, settings={"connector_store:woocommerce": "https://b.example"})
    await _imported_order(session, cid)
    session.add(ConnectorSource(
        company_id=str(cid), connector="woocommerce",
        store_handle="https://a.example", bound_at=datetime.now(timezone.utc),
    ))
    await session.flush()
    with _store([{**_ORDER, "total": "90.00"}]), pytest.raises(ConnectorStoreChangedError):
        await bind_connector_store(
            session, cid, WooCommerceConnector(), _ctx(cid, "https://b.example")
        )
    assert await _source(session, cid) == "https://a.example"


async def test_existing_orders_with_no_recorded_store_are_not_assigned_to_any_store(session):
    cid = await _company(session)
    await _imported_order(session, cid)
    with _store([{**_ORDER, "line_items": [{"name": "Garden hose"}]}]), \
            pytest.raises(ConnectorStoreChangedError, match="does not have them"):
        await bind_connector_store(
            session, cid, WooCommerceConnector(), _ctx(cid, "https://other.example")
        )
    assert await _source(session, cid) is None


async def test_existing_orders_are_kept_with_the_store_that_still_has_them(session):
    cid = await _company(session)
    await _imported_order(session, cid)
    with _store([_ORDER]):
        await bind_connector_store(
            session, cid, WooCommerceConnector(), _ctx(cid, "https://shop.example")
        )
    assert await _source(session, cid) == "https://shop.example"


async def test_a_store_that_moved_domain_keeps_its_orders(session):
    cid = await _company(session)
    await _imported_order(session, cid)
    session.add(ConnectorSource(
        company_id=str(cid), connector="woocommerce",
        store_handle="https://old.example", bound_at=datetime.now(timezone.utc),
    ))
    await session.flush()
    with _store([_ORDER]):
        await bind_connector_store(
            session, cid, WooCommerceConnector(), _ctx(cid, "https://new.example")
        )
    assert await _source(session, cid) == "https://new.example"


async def test_an_unreachable_store_is_not_accepted(session):
    cid = await _company(session)
    await _imported_order(session, cid)
    failing = patch.object(
        WooCommerceConnector, "_paginate", AsyncMock(side_effect=RuntimeError("timeout"))
    )
    with failing, pytest.raises(ConnectorStoreChangedError, match="Could not read"):
        await bind_connector_store(
            session, cid, WooCommerceConnector(), _ctx(cid, "https://shop.example")
        )
    assert await _source(session, cid) is None


_CUSTOMER = {"id": 3, "email": "pat@example.com", "first_name": "Pat", "last_name": "Buyer"}
_CONTACT = {"name": "Pat Buyer", "email": "pat@example.com", "attributes": {"woocommerce_id": "3"}}


@pytest.mark.parametrize("fetched, accepted", [
    ([_CUSTOMER], True),
    ([{**_CUSTOMER, "email": "someone@example.com"}], False),
    ([], False),
])
async def test_customers_alone_confirm_the_store_they_came_from(session, fetched, accepted):
    """With no orders imported, customers show whether a store is the one
    they came from; a customer the store does not have counts against it."""
    cid = await _company(session)
    session.add(Projection(
        company_id=cid, entity_id="contact:woocommerce:customer:3", entity_type="contact",
        state=_CONTACT, version=1, updated_at=datetime.now(timezone.utc),
    ))
    await session.flush()
    bind = bind_connector_store(
        session, cid, WooCommerceConnector(), _ctx(cid, "https://shop.example")
    )
    with _store(fetched):
        if accepted:
            await bind
        else:
            with pytest.raises(ConnectorStoreChangedError, match="does not have them"):
                await bind
    assert await _source(session, cid) == ("https://shop.example" if accepted else None)


@pytest.mark.parametrize("returned, expected", [(1, False), (10, False), (11, True)])
async def test_store_must_hold_most_of_the_whole_sample(returned, expected):
    """Records the store does not return count against it, so a store holding
    a few of the same order numbers is not taken for the one they came from."""
    orders = [{**_ORDER_DOC, "woocommerce_order_id": str(i)} for i in range(1, 21)]
    with _store([{**_ORDER, "id": i} for i in range(1, returned + 1)]):
        assert await WooCommerceConnector().same_store(
            _ctx(uuid.uuid4(), "https://s.example"), orders
        ) is expected

    invoices = [
        {"quickbooks_invoice_id": str(i), "ref_id": f"10{i}", "total": 50.0} for i in range(1, 21)
    ]
    fetched = [{"Id": str(i), "DocNumber": f"10{i}", "TotalAmt": 50} for i in range(1, returned + 1)]
    with patch("celerp.connectors.quickbooks._query", AsyncMock(return_value=fetched)):
        ctx = ConnectorContext(company_id="c", access_token="t", store_handle="123")
        assert await QuickBooksConnector().same_store(ctx, invoices) is expected


@pytest.mark.parametrize("stored, fetched, expected", [
    ({"email": "Pat@Example.com", "name": "P"}, {"email": "pat@example.com"}, True),
    ({"email": "pat@example.com", "name": "Pat Buyer"}, {**_CUSTOMER, "email": "x@example.com"}, False),
    ({"phone": "555 0100", "name": "Pat"}, {"billing": {"phone": "555 0100"}}, True),
    ({"name": "Pat Buyer"}, {"first_name": "Pat", "last_name": "Buyer"}, True),
    ({"name": "woocommerce:3"}, {}, False),
])
async def test_woocommerce_customer_match(stored, fetched, expected):
    record = {**stored, "attributes": {"woocommerce_id": "3"}}
    with _store([{"id": 3, **fetched}]):
        assert await WooCommerceConnector().same_store(
            _ctx(uuid.uuid4(), "https://s.example"), [record]
        ) is expected


@pytest.mark.parametrize("fetched, expected", [
    ({"DisplayName": "Pat Buyer", "PrimaryEmailAddr": {"Address": "pat@example.com"}}, True),
    ({"DisplayName": "Pat Buyer", "PrimaryEmailAddr": {"Address": "x@example.com"}}, False),
])
async def test_quickbooks_customer_match(fetched, expected):
    records = [{"name": "Pat Buyer", "email": "pat@example.com", "attributes": {"quickbooks_id": "6"}},
               {"name": "No id", "attributes": {}}]
    query = AsyncMock(return_value=[{"Id": "6", **fetched}])
    with patch("celerp.connectors.quickbooks._query", query):
        ctx = ConnectorContext(company_id="c", access_token="t", store_handle="123")
        assert await QuickBooksConnector().same_store(ctx, records[:1]) is expected
        assert await QuickBooksConnector().same_store(ctx, records) is False
    assert query.await_args.args[1] == "SELECT * FROM Customer WHERE Id IN ('6')"


@pytest.mark.parametrize("fetched, expected", [
    ([], False),
    ([_ORDER], False),
    ([_ORDER, {**_ORDER, "id": 8}], True),
    ([{**_ORDER, "total": "26.00"}, {**_ORDER, "id": 8}], False),
    ([{**_ORDER, "currency": "EUR"}, {**_ORDER, "id": 8}], False),
    ([{**_ORDER, "line_items": [{"name": "Blue mug"}, {"name": "Lamp"}]}, {**_ORDER, "id": 8}], False),
    ([_ORDER, {**_ORDER, "id": 8}, {**_ORDER, "id": 9, "total": "1.00"}], True),
    ([_ORDER, {**_ORDER, "id": 8, "total": "1.00"}], False),
])
async def test_woocommerce_store_match(fetched, expected):
    records = [_ORDER_DOC, {**_ORDER_DOC, "woocommerce_order_id": "8"},
               {**_ORDER_DOC, "woocommerce_order_id": "9"}]
    with _store(fetched):
        assert await WooCommerceConnector().same_store(_ctx(uuid.uuid4(), "https://s.example"), records) is expected


@pytest.mark.parametrize("fetched, expected", [
    ([{"Id": "4", "DocNumber": "1004", "TotalAmt": 50}], True),
    ([{"Id": "4", "DocNumber": "2001", "TotalAmt": 50}], False),
    ([{"Id": "4", "DocNumber": "1004", "TotalAmt": 51}], False),
])
async def test_quickbooks_store_match(fetched, expected):
    records = [{"quickbooks_invoice_id": "4", "ref_id": "1004", "total": 50.0},
               {"quickbooks_invoice_id": "5", "ref_id": "1005", "total": 5.0},
               {"quickbooks_invoice_id": "x'; drop", "ref_id": "1", "total": 1.0}]
    query = AsyncMock(return_value=[*fetched, {"Id": "5", "DocNumber": "1005", "TotalAmt": 5}])
    with patch("celerp.connectors.quickbooks._query", query):
        ctx = ConnectorContext(company_id="c", access_token="t", store_handle="123")
        assert await QuickBooksConnector().same_store(ctx, records) is expected
    assert query.await_args.args[1] == "SELECT * FROM Invoice WHERE Id IN ('4','5')"
