# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A line added from inventory starts with the company's default tax for its document's
side, the same way on every path: a new invoice or memo from items, items appended to an
existing document, and a purchase order from reorder. The document itself gets no stacked
document-level taxes, a list's lines stay untaxed, and when the tax list cannot be read the
error is shown and nothing is created or changed."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from celerp.tax_regimes import TAX_REGIMES
from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui.api_client import APIError

pytestmark = pytest.mark.asyncio

_FORM = {"content-type": "application/x-www-form-urlencoded"}
_ITEM = {"entity_id": "item:p1", "sku": "JWL-002", "name": "Gold Ring", "quantity": 2, "sell_by": "piece",
         "sale_price": 1200, "cost_price": 10, "reorder_qty": 24, "attributes": {}}


def _line_tax(name: str, rate: float) -> list[dict]:
    return [{"code": name, "rate": rate, "amount": 0, "order": 0, "is_compound": False, "label": ""}]


def _taxes(country: str = "TH") -> AsyncMock:
    return AsyncMock(return_value=TAX_REGIMES[country]["taxes"])


async def _post(ui_client, url: str, body: bytes, *, taxes: AsyncMock, create=None, doc=None, patch_doc=None):
    ui_client.cookies.update(_authed())
    with (
        patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
        patch("ui.api_client.get_document_taxes", new=taxes),
        patch("ui.api_client.create_doc", new=create or AsyncMock(return_value={"id": "doc:NEW-1"})),
        patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc or {})),
        patch("ui.api_client.patch_doc", new=patch_doc or AsyncMock(return_value={"event_id": "e1"})),
    ):
        return await ui_client.post(url, content=body, headers=_FORM)


@pytest.mark.parametrize("country, expected", [
    ("TH", _line_tax("VAT 7%", 7.0)),
    ("IN", _line_tax("IGST 18%", 18.0)),
    ("GB", _line_tax("VAT 20%", 20.0)),
    ("US", None),
])
async def test_a_new_invoice_from_items_starts_with_the_default_line_tax(ui_client, country, expected):
    create = AsyncMock(return_value={"id": "doc:INV-1"})
    r = await _post(ui_client, "/docs/from-items/new", b"selected=item%3Ap1", taxes=_taxes(country), create=create)
    assert r.status_code == 204, r.text
    payload = create.call_args[0][1]
    assert not payload.get("doc_taxes")
    line = payload["line_items"][0]
    assert line.get("taxes") == expected if expected else not line.get("taxes")

    # A list is not a taxed document: its lines stay untaxed and no tax list is read.
    taxes = _taxes(country)
    create = AsyncMock(return_value={"id": "list:L-1"})
    with (
        patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
        patch("ui.api_client.get_document_taxes", new=taxes),
        patch("ui.api_client.create_list", new=create),
    ):
        r = await ui_client.post("/lists/from-items/new", content=b"selected=item%3Ap1", headers=_FORM)
    assert r.status_code == 204, r.text
    assert not create.call_args[0][1]["line_items"][0].get("taxes")
    taxes.assert_not_awaited()


async def test_send_to_new_invoice_starts_with_the_default_line_tax(ui_client):
    create = AsyncMock(return_value={"id": "doc:INV-1"})
    r = await _post(ui_client, "/api/items/send-to",
                    b"selected=item%3Ap1&send_to_doc_type=invoice&send_to_target=__new__",
                    taxes=_taxes(), create=create)
    assert r.status_code == 204, r.text
    payload = create.call_args[0][1]
    assert not payload.get("doc_taxes")
    assert payload["line_items"][0]["taxes"] == _line_tax("VAT 7%", 7.0)


async def test_memo_from_items_starts_with_the_default_line_tax(ui_client):
    create = AsyncMock(return_value={"id": "doc:MEMO-1"})
    taxes = _taxes()
    r = await _post(ui_client, "/api/items/send-to",
                    b"selected=item%3Ap1&send_to_doc_type=memo&send_to_target=__new__",
                    taxes=taxes, create=create)
    assert r.status_code == 204, r.text
    payload = create.call_args[0][1]
    assert not payload.get("doc_taxes")
    assert payload["line_items"][0]["taxes"] == _line_tax("VAT 7%", 7.0)
    assert taxes.await_args.args[1] == "memo"

    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    memo = {"doc_type": "memo", "line_items": [{"description": "Old", "quantity": 1, "unit_price": 5}]}
    r = await _post(ui_client, "/api/items/send-to",
                    b"selected=item%3Ap1&send_to_doc_type=memo&send_to_target=doc%3AMEMO-1",
                    taxes=_taxes(), doc=memo, patch_doc=patch_doc)
    assert r.status_code == 204, r.text
    patched = patch_doc.call_args[0][2]
    assert not patched.get("doc_taxes")
    assert patched["line_items"][1]["taxes"] == _line_tax("VAT 7%", 7.0)


@pytest.mark.parametrize("url, body", [
    ("/docs/from-items/add", b"selected=item%3Ap1&target_id=doc%3AINV-1"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=invoice&send_to_target=doc%3AINV-1"),
])
async def test_appending_items_uses_the_target_documents_default(ui_client, url, body):
    """The appended line takes the default of the document it lands on, read by that
    document's own type, and the client sends no money of its own: the draft is recomputed."""
    taxes = _taxes()
    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    target = {"doc_type": "invoice", "line_items": [{"description": "Old", "quantity": 1, "unit_price": 100}]}
    r = await _post(ui_client, url, body, taxes=taxes, doc=target, patch_doc=patch_doc)
    assert r.status_code == 204, r.text
    patched = patch_doc.call_args[0][2]
    assert patched["line_items"][1]["taxes"] == _line_tax("VAT 7%", 7.0)
    assert "subtotal" not in patched and "total" not in patched
    assert taxes.await_args.args[1] == "invoice"


@pytest.mark.parametrize("target", ["__new__", "doc%3APO-1"])
async def test_po_from_reorder_starts_with_the_purchase_default(ui_client, target):
    """A purchase order line starts with the purchase-side default; a withholding row is
    never a default, so it is never added on its own."""
    taxes = _taxes()
    create = AsyncMock(return_value={"id": "doc:PO-1"})
    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    po = {"doc_type": "purchase_order", "line_items": [{"description": "Old", "quantity": 1, "unit_price": 5}]}
    r = await _post(ui_client, "/api/items/send-to",
                    f"selected=item%3Ap1&send_to_doc_type=purchase_order&send_to_target={target}".encode(),
                    taxes=taxes, create=create, doc=po, patch_doc=patch_doc)
    assert r.status_code == 204, r.text
    lines = (create.call_args[0][1] if target == "__new__" else patch_doc.call_args[0][2])["line_items"]
    assert lines[-1]["taxes"] == _line_tax("VAT 7%", 7.0)
    assert taxes.await_args.args[1] == "purchase_order"


@pytest.mark.parametrize("url, body", [
    ("/docs/from-items/new", b"selected=item%3Ap1"),
    ("/docs/from-items/add", b"selected=item%3Ap1&target_id=doc%3AINV-1"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=invoice&send_to_target=__new__"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=memo&send_to_target=__new__"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=purchase_order&send_to_target=__new__"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=invoice&send_to_target=doc%3AINV-1"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=memo&send_to_target=doc%3AMEMO-1"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=purchase_order&send_to_target=doc%3APO-1"),
])
async def test_a_failed_tax_read_creates_no_document(ui_client, url, body):
    create = AsyncMock(return_value={"id": "doc:NEW-1"})
    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    failing = AsyncMock(side_effect=APIError(503, "The tax list could not be read."))
    r = await _post(ui_client, url, body, taxes=failing, create=create,
                    doc={"doc_type": "invoice", "line_items": []}, patch_doc=patch_doc)
    assert r.status_code == 200, r.text
    assert "The tax list could not be read." in r.text
    assert "flash--error" in r.text
    create.assert_not_awaited()
    patch_doc.assert_not_awaited()


_DRAFT = {"entity_id": "doc:INV-1", "doc_type": "invoice", "status": "draft", "currency": "THB", "version": 4,
          "line_items": [{"description": "Ring", "quantity": 1, "unit_price": 100}],
          "doc_taxes": [{"code": "VAT 7%", "rate": 7.0, "amount": 7.0, "order": 0, "is_compound": False}],
          "subtotal": 100.0, "tax": 7.0, "total": 107.0}


async def _save_lines(ui_client, body: dict, *, patch_doc: AsyncMock, get_doc: AsyncMock):
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.patch_doc", new=patch_doc), patch("ui.api_client.get_doc", new=get_doc):
        return await ui_client.post("/docs/doc:INV-1/lines", json=body)


async def test_save_doc_lines_forwards_doc_taxes(ui_client):
    """Removing a stored document tax on a draft is saved with the lines, and the reply carries
    the stored tax rows and total the API computed, so the screen shows the server's figures."""
    patch_doc = AsyncMock(return_value={"version": 5})
    r = await _save_lines(ui_client, {"line_items": _DRAFT["line_items"], "doc_taxes": _DRAFT["doc_taxes"]},
                          patch_doc=patch_doc, get_doc=AsyncMock(return_value=_DRAFT))
    assert r.status_code == 200, r.text
    assert patch_doc.call_args[0][2]["doc_taxes"] == _DRAFT["doc_taxes"]
    out = r.json()
    assert out["ok"] is True and out["version"] == 5
    assert "VAT 7%" in out["stored_tax_rows_html"] and 'data-doc-tax-index="0"' in out["stored_tax_rows_html"]
    assert "107" in out["total"]

    r = await _save_lines(ui_client, {"line_items": _DRAFT["line_items"], "doc_taxes": []},
                          patch_doc=patch_doc, get_doc=AsyncMock(return_value={**_DRAFT, "doc_taxes": []}))
    assert r.status_code == 200, r.text
    assert patch_doc.call_args[0][2]["doc_taxes"] == []

    await _save_lines(ui_client, {"line_items": _DRAFT["line_items"]},
                      patch_doc=patch_doc, get_doc=AsyncMock(return_value=_DRAFT))
    assert "doc_taxes" not in patch_doc.call_args[0][2]


async def test_save_doc_lines_refused_doc_taxes_returns_the_api_error(ui_client):
    async def _patch(token, entity_id, data):
        if "doc_taxes" in data:
            raise APIError(422, "This tax isn't valid on this document.")
        return {"version": 5}

    r = await _save_lines(ui_client, {"line_items": [], "doc_taxes": [{"code": "?", "rate": "x"}]},
                          patch_doc=AsyncMock(side_effect=_patch), get_doc=AsyncMock(return_value=_DRAFT))
    assert r.status_code == 400
    assert r.json()["error"] == "This tax isn't valid on this document."


async def test_save_doc_lines_reread_failure_reports_the_error(ui_client):
    r = await _save_lines(ui_client, {"line_items": _DRAFT["line_items"]},
                          patch_doc=AsyncMock(return_value={"version": 5}),
                          get_doc=AsyncMock(side_effect=APIError(503, "The document could not be read.")))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "version": 5, "error": "The document could not be read."}
