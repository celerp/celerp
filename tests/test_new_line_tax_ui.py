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
    async def _patch(token, entity_id, data, expected_version=None):
        if "doc_taxes" in data:
            raise APIError(422, "This tax isn't valid on this document.")
        return {"version": 5}

    r = await _save_lines(ui_client, {"line_items": [], "doc_taxes": [{"code": "?", "rate": "x"}]},
                          patch_doc=AsyncMock(side_effect=_patch), get_doc=AsyncMock(return_value=_DRAFT))
    assert r.status_code == 422
    assert r.json()["error"] == "This tax isn't valid on this document."


async def test_save_doc_lines_reread_failure_reports_the_error(ui_client):
    r = await _save_lines(ui_client, {"line_items": _DRAFT["line_items"]},
                          patch_doc=AsyncMock(return_value={"version": 5}),
                          get_doc=AsyncMock(side_effect=APIError(503, "The document could not be read.")))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "version": 5, "error": "The document could not be read."}


_LEVY_WHOLE = [{"code": "Levy 5%", "rate": 5.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}]
_LINES_DIFFER = {"doc_type": "invoice", "doc_taxes": _LEVY_WHOLE,
                 "line_items": [{"description": "Old", "quantity": 1, "unit_price": 100,
                                 "taxes": _line_tax("VAT 7%", 7.0)},
                                {"description": "Older", "quantity": 1, "unit_price": 100, "taxes": []}]}


@pytest.mark.parametrize("url, body", [
    ("/docs/from-items/add", b"selected=item%3Ap1&target_id=doc%3AINV-1"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=invoice&send_to_target=doc%3AINV-1"),
])
async def test_appending_where_the_lines_differ_leaves_the_tax_to_the_user(ui_client, url, body):
    """A draft taxed as a whole whose lines carry different taxes: the appended line is stored with
    its tax still to be set (None), so the editor shows "--" for it after a reload."""
    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    r = await _post(ui_client, url, body, taxes=_taxes(), doc=_LINES_DIFFER, patch_doc=patch_doc)
    assert r.status_code == 204, r.text
    added = patch_doc.call_args[0][2]["line_items"][2]
    assert "taxes" in added and added["taxes"] is None


async def _import_csv(ui_client, csv: str, *, doc: dict, patch_doc: AsyncMock, taxes: AsyncMock | None = None):
    ui_client.cookies.update(_authed())
    with (
        patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM]})),
        patch("ui.api_client.get_document_taxes", new=taxes or _taxes()),
        patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
        patch("ui.api_client.patch_doc", new=patch_doc),
    ):
        return await ui_client.post("/docs/doc:INV-1/items/csv", files={"file": ("lines.csv", csv.encode())})


@pytest.mark.parametrize("doc, expected", [
    ({"doc_type": "invoice", "line_items": []}, _line_tax("VAT 7%", 7.0)),
    ({"doc_type": "invoice", "doc_taxes": _LEVY_WHOLE,
      "line_items": [{"description": "Old", "quantity": 1, "unit_price": 100, "taxes": _line_tax("VAT 7%", 7.0)}]},
     _line_tax("VAT 7%", 7.0)),
    (_LINES_DIFFER, None),
], ids=["new-line-default", "levy-keeps-vat", "lines-differ"])
async def test_a_line_imported_from_csv_takes_the_tax_a_new_line_takes(ui_client, doc, expected):
    """A CSV row with no tax of its own is a new line like any other: the company default on a plain
    draft ($107 for $100), the taxes the lines carry on a draft taxed as a whole ($224 with the levy),
    or left for the user to set where those lines differ. The client sends no money of its own."""
    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    r = await _import_csv(ui_client, "sku,quantity\nJWL-002,1\n", doc=doc, patch_doc=patch_doc)
    assert r.status_code == 200, r.text
    patched = patch_doc.call_args[0][2]
    added = patched["line_items"][-1]
    assert "taxes" in added and added["taxes"] == expected
    assert "tax_rate" not in added and "tax_code" not in added
    assert "subtotal" not in patched and "total" not in patched


async def test_a_csv_row_with_its_own_tax_keeps_it(ui_client):
    """A row that names its tax rate keeps it, a zero rate included: the file decided it."""
    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    r = await _import_csv(ui_client, "sku,quantity,tax_rate\nJWL-002,1,0\n",
                          doc={"doc_type": "invoice", "line_items": []}, patch_doc=patch_doc)
    assert r.status_code == 200, r.text
    added = patch_doc.call_args[0][2]["line_items"][-1]
    assert added["tax_rate"] == 0 and "taxes" not in added


async def test_a_failed_tax_read_imports_no_csv_line(ui_client):
    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    r = await _import_csv(ui_client, "sku,quantity\nJWL-002,1\n", doc={"doc_type": "invoice", "line_items": []},
                          patch_doc=patch_doc, taxes=AsyncMock(side_effect=APIError(503, "The tax list could not be read.")))
    assert r.status_code == 503 and r.json()["error"] == "The tax list could not be read."
    patch_doc.assert_not_awaited()


@pytest.mark.parametrize("url, body", [
    ("/docs/from-items/add", b"selected=item%3Ap1&target_id=doc%3AINV-1"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=invoice&send_to_target=doc%3AINV-1"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=memo&send_to_target=doc%3AMEMO-1"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=purchase_order&send_to_target=doc%3APO-1"),
])
async def test_appending_items_saves_against_the_version_it_read(ui_client, url, body):
    """The new line's tax is chosen from the document as read, so the save names that version: an
    edit made in between is refused (documents.err_doc_changed), never overwritten."""
    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    r = await _post(ui_client, url, body, taxes=_taxes(), doc={**_DRAFT, "version": 9}, patch_doc=patch_doc)
    assert r.status_code == 204, r.text
    assert patch_doc.call_args.kwargs.get("expected_version") == 9


async def test_a_csv_import_saves_against_the_version_it_read(ui_client):
    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    r = await _import_csv(ui_client, "sku,quantity\nJWL-002,1\n", doc={**_DRAFT, "version": 9}, patch_doc=patch_doc)
    assert r.status_code == 200, r.text
    assert patch_doc.call_args.kwargs.get("expected_version") == 9


async def test_a_line_field_edit_saves_against_the_version_it_read(ui_client):
    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value={**_DRAFT, "version": 9})), \
            patch("ui.api_client.patch_doc", new=patch_doc):
        await ui_client.patch("/docs/doc:INV-1/line/0/field/description", data={"value": "Ring, 18k"})
    assert patch_doc.call_args.kwargs.get("expected_version") == 9


async def test_every_rewrite_of_lines_read_from_the_server_names_the_version_it_read():
    """A UI route that reads a document and saves its line_items back from that read passes the
    version it read, as the list paths do. The editor's own save forwards the version the page read
    (tests/test_doc_line_save_version.py)."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "ui"
    calls = [m.group(0) for p in root.rglob("*.py")
             for m in re.finditer(r'api\.patch_doc\(token, \w+, \{"line_items":(?:[^\n]|\n\s+(?=expected_version=))*', p.read_text())]
    assert len(calls) == 6 and all("expected_version=" in call for call in calls), calls
