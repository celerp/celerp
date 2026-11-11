# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A WooCommerce order whose WOO- number a document already holds, stored under it for good
(renumbered or void) or showing it: the order waits on a person with two ways out, run here
through real syncs, the attention-list buttons and the API to their end state."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import text

from celerp.connectors.base import ConnectorContext
from company_backup_support import company, owner, token
from connector_test_support import connected
from migration_support import maker, real_client, real_engine  # noqa: F401 - fixtures
from test_helpers import ui_signed_in

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("connector_modules_running", "signed_in_to_cloud")]

_ORDER = {"id": 61, "number": "1001", "status": "pending", "total": "5.00",
          "line_items": [{"name": "Widget", "quantity": 1, "price": "5.00", "total": "5.00"}]}
_DOC = "doc:woocommerce:order:61"
_ATTENTION = "/settings/connectors/woocommerce/attention/61"

# A document stored under the order's number keeps it for good, renumbered or void.
_HOLDERS = {
    "draft": {"doc_type": "invoice", "ref_id": "WOO-1001", "status": "draft"},
    "void": {"doc_type": "invoice", "ref_id": "WOO-1001", "status": "void"},
    "renumbered": {"doc_type": "invoice", "ref_id": "MANUAL-1", "doc_number": "MANUAL-1", "status": "draft"},
}


@pytest.fixture
async def store(real_engine, real_client, monkeypatch):
    """A connected WooCommerce store returning order 1001, its sync counted by the order
    imports it tries. Yields an object with cid, tok, hold(entity_id, state), sync() (the
    attention list the sync left), imports and created() (the order's doc.created events)."""
    from types import SimpleNamespace

    import celerp.connectors.upsert as u
    from celerp import connectors
    from celerp.connectors.sync_runner import attention_entries, run_sync
    from celerp.models.projections import Projection

    user = await owner(real_engine)
    cid = str(await company(real_engine, user, "Woo Held", "woo-held"))
    connector = connectors.get("woocommerce")
    await connected(cid, connector, connector.default_directions)
    monkeypatch.setattr("celerp.connectors.ownership._lock_active_company", AsyncMock(return_value=None))
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(
        side_effect=lambda company_id, *_a, **_k: ConnectorContext(
            company_id=str(company_id), access_token="ck_k:cs_s", store_handle="https://store.example.com")))
    monkeypatch.setattr("celerp.connectors.woocommerce.validate_public_base_url", AsyncMock(return_value=None))
    imports: list[str] = []
    real_upsert = u.upsert_order_from_woocommerce

    async def counted(company_id, order, first_sync=None, **options):
        imports.append(str(order.get("id")))
        return await real_upsert(company_id, order, first_sync, **options)

    monkeypatch.setattr(u, "upsert_order_from_woocommerce", counted)

    async def hold(entity_id: str, state: dict) -> None:
        async with maker(real_engine)() as s:
            s.add(Projection(company_id=cid, entity_id=entity_id, entity_type="doc", version=1,
                             updated_at=datetime.now(timezone.utc), state=state))
            await s.commit()

    async def sync() -> list[dict]:
        with respx.mock:
            respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
                return_value=httpx.Response(200, json=[_ORDER]))
            await run_sync(connector, cid, "orders", purpose="manual_sync")
        return await attention_entries(cid, "woocommerce", "orders")

    async def created() -> tuple[int, str | None]:
        async with maker(real_engine)() as s:
            count = await s.scalar(text(
                "SELECT count(*) FROM ledger WHERE company_id = :c AND event_type = 'doc.created' "
                "AND idempotency_key LIKE 'woocommerce:order:61:%'"), {"c": cid})
            ref_id = await s.scalar(text(
                "SELECT state ->> 'ref_id' FROM projections WHERE company_id = :c AND entity_id = :e"),
                {"c": cid, "e": _DOC})
        return count, ref_id

    return SimpleNamespace(cid=cid, tok=await token(real_engine, user, cid), hold=hold, sync=sync,
                           imports=imports, created=created)


@pytest.mark.parametrize("holder", sorted(_HOLDERS))
async def test_marked_reconciled_the_order_is_never_retried(store, holder):
    """The holder is this order, imported by hand: Mark reconciled takes it off the open
    list, and later syncs leave it alone (no import tried, nothing booked)."""
    await store.hold("doc:WOO-1001", _HOLDERS[holder])
    [entry] = await store.sync()
    assert entry["signature"] and entry["number_held"]["number"] == "WOO-1001", entry
    async with ui_signed_in(store.tok) as ui:
        r = await ui.post(f"{_ATTENTION}/reconciled", data={"signature": entry["signature"]},
                          headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert 'class="flash' not in r.text, r.text
    assert f'hx-delete="{_ATTENTION}/reconciled"' in r.text, r.text
    store.imports.clear()
    [entry] = await store.sync()
    assert entry["reconciled"] is True, entry
    assert store.imports == []
    assert await store.created() == (0, None)


@pytest.mark.parametrize(("holder", "taken", "expected"), [
    *[(h, (), "WOO-1001-2") for h in sorted(_HOLDERS)],
    ("draft", ("WOO-1001-2",), "WOO-1001-3"),
])
async def test_import_as_books_the_order_once_under_the_next_free_suffix(store, holder, taken, expected):
    """The holder is another record: Import as WOO-<n>-<k> books the order once, under the
    first suffix no record holds, says which number it used, and later syncs keep it."""
    from ui.i18n import t

    await store.hold("doc:WOO-1001", _HOLDERS[holder])
    for number in taken:
        await store.hold(f"doc:{number}", {"doc_type": "invoice", "ref_id": number, "status": "draft"})
    [entry] = await store.sync()
    assert entry["number_held"]["next_number"] == expected, entry
    async with ui_signed_in(store.tok) as ui:
        page = await ui.get("/settings/connectors/woocommerce")
        assert t("connectors.attention_import_as", number=expected) in page.text, page.text
        r = await ui.post(f"{_ATTENTION}/import-next", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert t("connectors.attention_imported_as", number=expected) in r.text, r.text
    assert f'href="/docs/{_DOC}"' in r.text, r.text
    assert await store.created() == (1, expected)
    assert await store.sync() == []
    assert await store.created() == (1, expected)


async def test_import_as_refuses_an_order_not_waiting_on_a_held_number(store):
    """Import as is the way out of a held number only: an order waiting on anything else is
    refused with that, and nothing is imported."""
    from ui.i18n import t

    _ORDER_UNKNOWN = {**_ORDER, "line_items": [{"name": "Widget", "sku": "NOPE", "product_id": 5,
                                                 "quantity": 1, "price": "5.00", "total": "5.00"}]}
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(200, json=[_ORDER_UNKNOWN]))
        from celerp import connectors
        from celerp.connectors.sync_runner import run_sync
        await run_sync(connectors.get("woocommerce"), store.cid, "orders", purpose="manual_sync")
    store.imports.clear()
    async with ui_signed_in(store.tok) as ui:
        r = await ui.api.post("/connectors/woocommerce/orders/61/import-next")
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == t("error.order_number_not_held")
        line = await ui.post(f"{_ATTENTION}/import-next", headers={"HX-Request": "true"})
    assert t("error.order_number_not_held") in line.text, line.text
    assert store.imports == []
    assert await store.created() == (0, None)
