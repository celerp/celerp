# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A store order that ships goods another invoice had set aside takes their cost from
that invoice, as shipping them by hand does, and leaves a notice saying whose goods they
were and that the invoice is costed when it ships. Nobody is at the screen when a store
order completes, so the notice stays in the notification list until it is read."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from celerp.models.accounting import UserCompany
from celerp.models.notification import Notification
from stock_books import assert_settled
from test_connector_item_cost_rule import _woo_order
from test_cost_follows_goods import _doc_number, _invoice
from test_invoice_unshipped_books import _lot
from test_posting_roles_ingress import connector_session  # noqa: F401
from test_set_aside_goods_follow_the_lot import _per_doc

pytestmark = pytest.mark.asyncio

MOVED = "documents.cost_moved_with_goods"


async def _notices(session, auth) -> list[Notification]:
    session.expire_all()
    return list((await session.execute(select(Notification).where(
        Notification.company_id == auth["company_id"]))).scalars().all())


async def test_a_store_order_shipping_goods_set_aside_for_an_invoice_says_so(connector_session, client, auth):  # noqa: F811
    import celerp.connectors.upsert as connector

    session, cid = connector_session, str(auth["company_id"])
    await session.execute(update(UserCompany).where(UserCompany.company_id == auth["company_id"]).values(role="owner"))
    sku = f"WCM-{uuid.uuid4().hex[:6]}"
    lot = await _lot(client, auth, sku, 1, 40.0)
    held = await _invoice(client, auth, [(lot, sku, 1)])
    number = await _doc_number(session, auth, held)

    assert await connector.upsert_order_from_woocommerce(cid, _woo_order(951, sku, "processing")) == "created"
    assert await connector.upsert_order_from_woocommerce(cid, _woo_order(951, sku, "completed")) == "updated"
    shipped = await _per_doc(session, auth)
    assert held not in shipped and sum(shipped.values()) == 40.0

    [notice] = [n for n in await _notices(session, auth) if (n.i18n or {}).get("body") == MOVED]
    assert sku in notice.body and number in notice.body and "costed when it ships" in notice.body
    assert notice.i18n["params"] == {"sku": sku, "doc": number}
    assert notice.action_url == f"/docs/{held}"
    assert notice.priority == "high"

    assert await connector.upsert_order_from_woocommerce(cid, _woo_order(951, sku, "completed")) == "noop"
    assert len([n for n in await _notices(session, auth) if (n.i18n or {}).get("body") == MOVED]) == 1
    await assert_settled(client, session, auth)
