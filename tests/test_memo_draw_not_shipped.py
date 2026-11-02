# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Invoice selected on a memo line that has not been shipped runs and refuses, saying to set the
line as shipped first, instead of leaving the panel with nothing to confirm and no reason."""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from memo_draw_support import auth, draw, lot, ok, state
from test_helpers import make_test_token
from ui.i18n import t

MEMO = "doc:memo-1"
SHIPPED, WAITING = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def _memo() -> dict:
    return {"id": MEMO, "doc_type": "memo", "status": "final", "ref_id": "M-12", "currency": "USD",
            "line_items": [
                {"line_id": SHIPPED, "sku": "S-1", "item_id": "item:1", "quantity": 1, "out_quantity": 1,
                 "sell_by": "piece", "unit_price": 10.0, "shipped_label": "On Memo"},
                {"line_id": WAITING, "sku": "S-2", "item_id": "item:2", "quantity": 1, "out_quantity": 0,
                 "sell_by": "piece", "unit_price": 10.0, "shipped_label": "Not shipped"}]}


@pytest.mark.asyncio
async def test_a_line_not_shipped_is_refused_with_what_to_do(client, session):
    """The server's preview and the draw itself both name the line and say to set it as shipped."""
    h = await auth(session)
    ids = [await lot(client, h, sku, 1, price=10.0) for sku in ("NS-1", "NS-2")]
    body = {"doc_type": "memo", "line_items": [
        {"item_id": i, "sku": s, "description": s, "quantity": 1, "unit_price": 10.0, "sell_by": "piece"}
        for i, s in zip(ids, ("NS-1", "NS-2"))]}
    memo = (await ok(await client.post("/docs", headers=h, json=body)))["id"]
    await ok(await client.post(f"/docs/{memo}/finalize", headers=h))
    lids = [li["line_id"] for li in (await state(client, h, memo))["line_items"]]
    await ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": lids[:1]}))

    preview = await ok(await draw(client, h, memo, line_ids=[lids[1]], dry_run=True))
    assert [p["message_key"] for p in preview["problems"]] == ["docs.draw_line_not_shipped"]
    assert preview["binding"], "a refused preview still lets the user confirm and read the refusal"
    assert "NS-2" in preview["problems"][0]["message"]

    r = await draw(client, h, memo, line_ids=[lids[1]], binding=preview["binding"])
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "docs.draw_line_not_shipped"


@pytest.fixture
def ui_client():
    from ui.app import app as ui_app
    client = AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False)
    client.cookies.update({"celerp_token": make_test_token(role="owner")})
    return client


def test_the_panel_row_of_a_line_not_shipped_sends_the_line():
    """A selected line with nothing out has no quantity to choose, yet still names its line, so
    the preview can say why it cannot be invoiced; its label is the line's own shipped state."""
    from fasthtml.common import to_xml
    from ui.routes.documents import _draw_panel

    panel = to_xml(_draw_panel(MEMO, _memo(), [], {}))
    row = re.search(r'<fieldset[^>]*data-line-index="1".*?</fieldset>', panel, re.S).group(0)
    assert f'name="pick_1" value="{WAITING}"' in row and 'name="qty_1"' not in row
    assert t("documents.line_label_not_shipped") in row


@pytest.mark.asyncio
async def test_the_preview_asks_the_server_about_a_line_not_shipped(ui_client):
    refusal = {"message_key": "docs.draw_line_not_shipped", "message": "S-2 has not been shipped."}
    preview = AsyncMock(return_value={"problems": [refusal], "binding": "b-1"})
    with patch("ui.api_client.invoice_lines_preview", new=preview):
        r = await ui_client.post(f"/docs/{MEMO}/invoice-lines/preview", data={"pick_1": WAITING, "currency": "USD"})
    sent = preview.await_args.args[2]
    assert sent["line_ids"] == [WAITING] and WAITING not in sent["quantities"]
    assert 'name="binding" value="b-1"' in r.text and "cell-error" in r.text
